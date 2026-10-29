"""Vendor wire formats live here, outside quiz orchestration."""
import asyncio
import base64
import json
import os

import httpx
from pydantic import ValidationError

from services.ai.contracts import AIResult, Attachment
from services.ai.provider_presets import chat_service
from services.ai.rate_limits import classify


class ProviderError(Exception):
    """Upstream failure with only safe, structured diagnostic fields."""

    def __init__(self, status=None, *, reason="upstream_error", retry_after_s=None,
                 reset_after_s=None):
        self.status = status if type(status) is int else None
        self.reason = reason
        self.retry_after_s = retry_after_s
        self.reset_after_s = reset_after_s
        super().__init__(f"Provider request failed (status={self.status}, reason={reason})")


def _http_provider_error(response):
    # Never log or store raw error messages: they may contain student data or tokens.
    try:
        payload = response.json() if len(response.content) <= 65536 else None
    except ValueError:
        payload = None
    info = classify(response.status_code, payload, response.headers)
    return ProviderError(response.status_code, reason=info.reason,
                         retry_after_s=info.retry_after_s,
                         reset_after_s=info.reset_after_s)


_SAFE_FINISH_REASONS = frozenset({
    "STOP", "MAX_TOKENS", "SAFETY", "RECITATION", "LANGUAGE",
    "BLOCKLIST", "PROHIBITED_CONTENT", "SPII", "MALFORMED_FUNCTION_CALL",
    "OTHER", "UNEXPECTED_TOOL_CALL", "NO_IMAGE", "IMAGE_SAFETY",
})


def safe_finish_reason(value):
    """Allowlist finish codes; never log arbitrary provider-controlled text."""
    if value is None:
        return None
    name = str(getattr(value, "name", value)).rsplit(".", 1)[-1].upper()
    name = "MAX_TOKENS" if name == "LENGTH" else name
    return name if name in _SAFE_FINISH_REASONS else "UNKNOWN"


class AIResponseDiagnosticError(ValueError):
    """Non-sensitive machine-readable failure stage for model responses."""

    def __init__(self, detail, result=None):
        self.detail = detail
        self.finish_reason = safe_finish_reason(getattr(result, "finish_reason", None))
        raw = getattr(result, "text", None)
        self.response_chars = len(raw) if isinstance(raw, str) else None
        self.output_tokens = getattr(result, "output_tokens", None)
        self.thoughts_tokens = getattr(result, "thoughts_tokens", None)
        super().__init__(f"AI response validation failed: {detail}")


def parse_result(result, schema, *, allow_empty_questions=False):
    raw = getattr(result, "text", None)
    if not isinstance(raw, str):
        raise AIResponseDiagnosticError("non_text_response", result)
    raw = raw.strip()
    if not raw:
        detail = ("empty_after_thinking" if getattr(result, "thoughts_tokens", 0)
                  and not getattr(result, "output_tokens", 0)
                  else "empty_response")
        raise AIResponseDiagnosticError(detail, result)
    if schema:
        from services.ai.parsing import _repair_json_backslashes
        if raw.startswith("```"):
            raw = raw.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
        try:
            obj = json.loads(_repair_json_backslashes(raw))
        except (ValueError, TypeError):
            detail = "truncated_json" if result.truncated else "malformed_json"
            raise AIResponseDiagnosticError(detail, result) from None
        try:
            result.parsed = schema.model_validate(obj)
        except (ValidationError, ValueError, TypeError):
            raise AIResponseDiagnosticError("schema_validation", result) from None
        if (hasattr(result.parsed, "questions") and not result.parsed.questions
                and (not allow_empty_questions or result.truncated)):
            raise AIResponseDiagnosticError("empty_questions", result)
    return result


class ChatAdapter:
    capabilities = {"text", "image"}

    def capabilities_for(self, provider):
        return self.capabilities

    def supports_request(self, provider, model, request):
        if chat_service(provider["base_url"]) == "cloudflare" and "image" in request.capabilities:
            # This native model takes one image; classification samples the first.
            image_count = sum(isinstance(part, Attachment) and part.capability == "image"
                              for part in request.contents)
            return (model == "@cf/meta/llama-3.2-11b-vision-instruct" and
                    (image_count == 1 or request.allow_image_sampling))
        return True

    async def _generate_cloudflare_vision(self, provider, credential, model, request):
        texts, image = [], None
        for part in request.contents:
            if isinstance(part, str):
                texts.append(part)
            elif part.capability == "text":
                texts.append((await asyncio.to_thread(part.read)).decode("utf-8"))
            elif part.capability == "image" and image is None:
                encoded = base64.b64encode(await asyncio.to_thread(part.read)).decode("ascii")
                image = f"data:{part.mime_type};base64,{encoded}"
        if request.schema:
            # Llama 3.2 Vision has no response_format input. Validate its JSON locally.
            texts.append("Return ONLY valid JSON matching this schema:\n" +
                         json.dumps(request.schema.model_json_schema(), ensure_ascii=False))
        # Workers AI vision accepts a single prompt, not distinct system/user roles.
        body = {"prompt": "\n\n".join(([request.system_instruction] if request.system_instruction else []) + texts),
                "stream": False, "max_tokens": 1024}
        if image is not None:
            body["image"] = image
        url = provider["base_url"].rstrip("/").removesuffix("/v1") + "/run/" + model
        async with httpx.AsyncClient(follow_redirects=False, timeout=None) as client:
            response = await client.post(url, headers={"Authorization": "Bearer " + credential}, json=body)
            if response.status_code >= 300:
                raise _http_provider_error(response)
            payload = response.json()
        if payload.get("success") is False or payload.get("errors"):
            raise ProviderError()
        output = payload.get("result")
        if not isinstance(output, dict):
            raise ValueError("Invalid Workers AI response")
        usage = output.get("usage") or {}
        result = AIResult(text=output.get("response") or "",
                          input_tokens=int(usage.get("prompt_tokens") or 0),
                          output_tokens=int(usage.get("completion_tokens") or 0),
                          total_tokens=int(usage.get("total_tokens") or 0),
                          truncated=output.get("finish_reason") == "length",
                          finish_reason=safe_finish_reason(output.get("finish_reason")),
                          sampled=sum(isinstance(part, Attachment) and part.capability == "image"
                                      for part in request.contents) > 1)
        return parse_result(result, request.schema, allow_empty_questions=request.allow_empty_questions)

    async def generate(self, provider, credential, model, request):
        service = chat_service(provider["base_url"])
        if (not request.capabilities <= self.capabilities_for(provider) or
                not self.supports_request(provider, model, request)):
            raise ValueError("Unsupported attachment for this provider endpoint")
        if service == "cloudflare" and model == "@cf/meta/llama-3.2-11b-vision-instruct":
            from services.ai.thinking import chat_thinking
            chat_thinking(service, model, request.reasoning_effort)
            return await self._generate_cloudflare_vision(provider, credential, model, request)
        content = []
        for part in request.contents:
            if isinstance(part, str):
                content.append({"type": "text", "text": part})
            elif part.capability == "text":
                content.append({"type": "text", "text": (await asyncio.to_thread(part.read)).decode("utf-8")})
            elif part.capability == "image":
                encoded = base64.b64encode(await asyncio.to_thread(part.read)).decode("ascii")
                content.append({"type": "image_url", "image_url": {
                    "url": f"data:{part.mime_type};base64,{encoded}"}})
            else:
                raise ValueError("Unsupported attachment for chat transport")
        messages = ([{"role": "system", "content": request.system_instruction}]
                    if request.system_instruction else [])
        messages.append({"role": "user", "content": content})
        body = {"model": model, "messages": messages}
        from services.ai.thinking import chat_thinking
        body.update(chat_thinking(service, model, request.reasoning_effort))
        if request.schema:
            content.append({"type": "text", "text": "Return ONLY valid JSON matching this schema:\n" +
                            json.dumps(request.schema.model_json_schema(), ensure_ascii=False)})
            body["response_format"] = {"type": "json_object"}
            if service == "openrouter":
                # Only route structured requests to endpoints honoring JSON mode.
                body["provider"] = {"require_parameters": True}
        if service == "cloudflare":
            body["messages"][-1]["content"] = "\n\n".join(part["text"] for part in content)
        # One client per attempt, always closed including cancellation. No SDK retries:
        # the runtime owns the fallback policy. Never forward credentials on redirects.
        async with httpx.AsyncClient(follow_redirects=False, timeout=None) as client:
            response = await client.post(provider["base_url"] + "/chat/completions",
                                         headers={"Authorization": "Bearer " + credential}, json=body)
            if response.status_code >= 300:
                raise _http_provider_error(response)
            try:
                payload = response.json()
            except ValueError:
                raise AIResponseDiagnosticError("upstream_json_decode") from None
        if payload.get("error") or payload.get("success") is False:
            error = payload.get("error")
            code = error.get("code") if isinstance(error, dict) else None
            status = code if type(code) is int else None
            info = classify(status, payload)
            raise ProviderError(status, reason=info.reason,
                                retry_after_s=info.retry_after_s,
                                reset_after_s=info.reset_after_s)
        try:
            choice = payload["choices"][0]
            content_text = choice["message"].get("content") or ""
        except (KeyError, IndexError, TypeError):
            raise AIResponseDiagnosticError("upstream_payload_shape") from None
        usage = payload.get("usage") or {}
        # Completion tokens already include reasoning tokens on this protocol.
        # Keep thoughts_tokens=0 to avoid double charging them in existing reports.
        result = AIResult(text=content_text,
                          input_tokens=int(usage.get("prompt_tokens") or 0),
                          output_tokens=int(usage.get("completion_tokens") or 0),
                          total_tokens=int(usage.get("total_tokens") or 0),
                          truncated=choice.get("finish_reason") == "length",
                          finish_reason=safe_finish_reason(choice.get("finish_reason")))
        return parse_result(result, request.schema, allow_empty_questions=request.allow_empty_questions)


class GoogleAdapter:
    capabilities = {"text", "image", "pdf", "audio"}

    async def generate(self, provider, credential, model, request):
        from services.ai.diagnostics import google_http_options, mark
        from google import genai
        from google.genai import types
        mark("client_setup")
        diagnostics_options, diagnostic_session = google_http_options()
        uploaded = []
        try:
            http_options = types.HttpOptions(retry_options=types.HttpRetryOptions(attempts=1), **diagnostics_options)
            return await self._generate(provider, credential, model, request, genai, types, http_options, uploaded)
        finally:
            # SDK deliberately leaves an injected session open: we own it.
            if diagnostic_session is not None:
                await diagnostic_session.close()

    async def _generate(self, provider, credential, model, request, genai, types, http_options, uploaded):
        from services.ai.diagnostics import begin_cleanup, mark
        if provider["transport"] == "vertex":
            from google.oauth2 import service_account
            credentials = service_account.Credentials.from_service_account_info(
                json.loads(credential), scopes=["https://www.googleapis.com/auth/cloud-platform"])
            client = genai.Client(vertexai=True, project=os.environ["GOOGLE_VERTEX_PROJECT"],
                                  location=os.getenv("GOOGLE_VERTEX_LOCATION", "us-central1"),
                                  credentials=credentials, http_options=http_options)
        else:
            client = genai.Client(api_key=credential, http_options=http_options)
        try:
            mark("attachment_prepare")
            contents = []
            total_size = sum(os.path.getsize(part.path) if part.path else len(part.data or b"")
                             for part in request.contents if isinstance(part, Attachment))
            for part in request.contents:
                if isinstance(part, str):
                    contents.append(part)
                elif (part.path and getattr(request, "file_session", None) is not None
                      and provider["transport"] == "gemini"):
                    # Reuse one Files API upload per credential for all batches.
                    # A different key automatically gets its own private upload.
                    mark("file_session_upload")
                    remote = await request.file_session.get(
                        client, provider["id"], credential, part.path,
                    )
                    contents.append(types.Part.from_uri(
                        file_uri=remote.uri, mime_type=remote.mime_type or part.mime_type,
                    ))
                elif (part.path and total_size > 15 * 1024 * 1024
                      and provider["transport"] != "vertex"
                      and not getattr(request, "inline_only", False)):
                    # Upload is asynchronous so cancellation cannot orphan a background
                    # upload thread; files belong to this credential and this attempt.
                    mark("file_upload")
                    uploaded_file = await client.aio.files.upload(file=part.path)
                    uploaded.append(uploaded_file.name)
                    contents.append(uploaded_file)
                else:
                    mark("attachment_read")
                    contents.append(types.Part.from_bytes(
                        data=await asyncio.to_thread(part.read), mime_type=part.mime_type))
            config = (types.GenerateContentConfig(response_mime_type="application/json",
                                                 response_schema=request.schema,
                                                 system_instruction=request.system_instruction)
                      if request.schema else
                      types.GenerateContentConfig(system_instruction=request.system_instruction)
                      if request.system_instruction else None)
            if request.reasoning_effort:
                from services.ai.thinking import google_thinking
                thinking = google_thinking(model, request.reasoning_effort, types)
                if thinking is not None:
                    if config is None:
                        config = types.GenerateContentConfig()
                    config.thinking_config = thinking
            try:
                mark("generate_wait")
                response = await client.aio.models.generate_content(
                    model=model, contents=contents, config=config,
                )
            except (ValueError, TypeError, KeyError, IndexError):
                # SDK-side decode errors are distinct from invalid model JSON.
                # Never record exception messages or upstream payloads.
                raise AIResponseDiagnosticError("sdk_generation_error") from None
            try:
                mark("response_parse")
                usage = response.usage_metadata
                finish = response.candidates[0].finish_reason if response.candidates else None
                text = response.text or ""
            except (ValueError, TypeError, KeyError, IndexError):
                raise AIResponseDiagnosticError("sdk_response_access_error") from None
            result = AIResult(text=text,
                              input_tokens=int(getattr(usage, "prompt_token_count", 0) or 0),
                              output_tokens=int(getattr(usage, "candidates_token_count", 0) or 0),
                              thoughts_tokens=int(getattr(usage, "thoughts_token_count", 0) or 0),
                              total_tokens=int(getattr(usage, "total_token_count", 0) or 0),
                              truncated="MAX_TOKENS" in str(finish),
                              finish_reason=safe_finish_reason(finish))
            return parse_result(result, request.schema, allow_empty_questions=request.allow_empty_questions)
        finally:
            # Preserve the last processing milestone on failure/timeout. Cleanup
            # emits its own events rather than hiding where the request stalled.
            begin_cleanup()
            from services.ai.time_budget import quiz_cleanup_deadline
            async with quiz_cleanup_deadline():
                for name in uploaded:
                    try:
                        await asyncio.wait_for(client.aio.files.delete(name=name), timeout=5)
                    except Exception:
                        pass
                await client.aio.aclose()
                client.close()


# To implement a different protocol, add one adapter and register it here.
ADAPTERS = {"gemini": GoogleAdapter(), "vertex": GoogleAdapter(),
            "openai_compatible": ChatAdapter()}
