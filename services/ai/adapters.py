"""Vendor wire formats live here, outside quiz orchestration."""
import asyncio
import base64
import json
import os

import httpx

from services.ai.contracts import AIResult, Attachment
from services.ai.provider_presets import chat_service


class ProviderError(Exception):
    def __init__(self, status=None):
        self.status = status
        super().__init__(f"Provider request failed (status={status})")


def parse_result(result, schema):
    if not result.text.strip():
        raise ValueError("Empty AI response")
    if schema:
        from services.ai.parsing import _repair_json_backslashes
        raw = result.text.strip()
        if raw.startswith("```"):
            raw = raw.split("\n", 1)[1].rsplit("```", 1)[0].strip()
        result.parsed = schema.model_validate(json.loads(_repair_json_backslashes(raw)))
        if hasattr(result.parsed, "questions") and not result.parsed.questions:
            raise ValueError("AI returned no questions")
    return result


class ChatAdapter:
    capabilities = {"text", "image"}

    def capabilities_for(self, provider):
        # Workers AI's documented compatible endpoint is for text generation.
        return {"text"} if chat_service(provider["base_url"]) == "cloudflare" else self.capabilities

    async def generate(self, provider, credential, model, request):
        service = chat_service(provider["base_url"])
        if not request.capabilities <= self.capabilities_for(provider):
            raise ValueError("Unsupported attachment for this provider endpoint")
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
        body = {"model": model, "messages": [{"role": "user", "content": content}]}
        if request.schema:
            content.append({"type": "text", "text": "Return ONLY valid JSON matching this schema:\n" +
                            json.dumps(request.schema.model_json_schema(), ensure_ascii=False)})
            body["response_format"] = {"type": "json_object"}
            if service == "openrouter":
                # Only route structured requests to endpoints honoring JSON mode.
                body["provider"] = {"require_parameters": True}
        if service == "cloudflare":
            body["messages"][0]["content"] = "\n\n".join(part["text"] for part in content)
        # One client per attempt, always closed including cancellation. No SDK retries:
        # the runtime owns the fallback policy. Never forward credentials on redirects.
        async with httpx.AsyncClient(follow_redirects=False, timeout=None) as client:
            response = await client.post(provider["base_url"] + "/chat/completions",
                                         headers={"Authorization": "Bearer " + credential}, json=body)
            if response.status_code >= 300:
                raise ProviderError(response.status_code)
            payload = response.json()
        if payload.get("error") or payload.get("success") is False:
            error = payload.get("error")
            code = error.get("code") if isinstance(error, dict) else None
            raise ProviderError(code if type(code) is int else None)
        choice = payload["choices"][0]
        usage = payload.get("usage") or {}
        # Completion tokens already include reasoning tokens on this protocol.
        # Keep thoughts_tokens=0 to avoid double charging them in existing reports.
        result = AIResult(text=choice["message"].get("content") or "",
                          input_tokens=int(usage.get("prompt_tokens") or 0),
                          output_tokens=int(usage.get("completion_tokens") or 0),
                          total_tokens=int(usage.get("total_tokens") or 0),
                          truncated=choice.get("finish_reason") == "length")
        return parse_result(result, request.schema)


class GoogleAdapter:
    capabilities = {"text", "image", "pdf", "audio"}

    async def generate(self, provider, credential, model, request):
        from google import genai
        from google.genai import types
        http_options = types.HttpOptions(retry_options=types.HttpRetryOptions(attempts=1))
        if provider["transport"] == "vertex":
            from google.oauth2 import service_account
            credentials = service_account.Credentials.from_service_account_info(
                json.loads(credential), scopes=["https://www.googleapis.com/auth/cloud-platform"])
            client = genai.Client(vertexai=True, project=os.environ["GOOGLE_VERTEX_PROJECT"],
                                  location=os.getenv("GOOGLE_VERTEX_LOCATION", "us-central1"),
                                  credentials=credentials, http_options=http_options)
        else:
            client = genai.Client(api_key=credential, http_options=http_options)
        uploaded = []
        try:
            contents = []
            total_size = sum(os.path.getsize(part.path) if part.path else len(part.data or b"")
                             for part in request.contents if isinstance(part, Attachment))
            for part in request.contents:
                if isinstance(part, str):
                    contents.append(part)
                elif (part.path and total_size > 15 * 1024 * 1024
                      and provider["transport"] != "vertex"):
                    # Upload is asynchronous so cancellation cannot orphan a background
                    # upload thread; files belong to this credential and this attempt.
                    uploaded_file = await client.aio.files.upload(file=part.path)
                    uploaded.append(uploaded_file.name)
                    contents.append(uploaded_file)
                else:
                    contents.append(types.Part.from_bytes(
                        data=await asyncio.to_thread(part.read), mime_type=part.mime_type))
            config = (types.GenerateContentConfig(response_mime_type="application/json",
                                                 response_schema=request.schema)
                      if request.schema else None)
            response = await client.aio.models.generate_content(model=model, contents=contents, config=config)
            usage = response.usage_metadata
            finish = response.candidates[0].finish_reason if response.candidates else None
            result = AIResult(text=response.text or "",
                              input_tokens=int(getattr(usage, "prompt_token_count", 0) or 0),
                              output_tokens=int(getattr(usage, "candidates_token_count", 0) or 0),
                              thoughts_tokens=int(getattr(usage, "thoughts_token_count", 0) or 0),
                              total_tokens=int(getattr(usage, "total_token_count", 0) or 0),
                              truncated="MAX_TOKENS" in str(finish))
            return parse_result(result, request.schema)
        finally:
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
