"""Temporary, test-branch-only live Cloudflare API diagnostic.

Runs synthetic prompts only; no Telegram updates, student data, secrets or response
text are ever logged. Remove the startup hook after collecting one run.
"""
import asyncio
import io
import json
import os
import time

import httpx

from logger import get_logger

logger = get_logger(__name__)
MODEL = "@cf/meta/llama-3.2-11b-vision-instruct"
MAX_SECONDS_PER_CASE = 14


def _error_codes(payload):
    if not isinstance(payload, dict):
        return []
    errors = payload.get("errors", [])
    if not isinstance(errors, list):
        return []
    return [e.get("code") for e in errors[:6]
            if isinstance(e, dict) and type(e.get("code")) is int]


def _result_details(payload):
    if not isinstance(payload, dict):
        return "non_json", 0, []
    result = payload.get("result")
    response = result.get("response") if isinstance(result, dict) else None
    # Never log generated text, even when inputs are synthetic.
    result_type = "dict" if isinstance(result, dict) else type(result).__name__
    return result_type, len(response) if isinstance(response, str) else 0, _error_codes(payload)


async def _run_case(base, token, name, body):
    # Configured provider base ends in /ai/v1; native run endpoint is /ai/run/.
    url = base.removesuffix("/v1") + "/run/" + MODEL
    t0 = time.monotonic()
    headers_elapsed_ms = None
    body_elapsed_ms = None
    bytes_sent = len(json.dumps(body, ensure_ascii=False).encode("utf-8"))
    logger.info("CF_PROBE_START case=%s request_bytes=%d payload_style=%s max_tokens=%s",
                name, bytes_sent, "messages" if "messages" in body else "prompt", body.get("max_tokens"))
    try:
        timeout = httpx.Timeout(connect=5.0, read=MAX_SECONDS_PER_CASE,
                                write=5.0, pool=5.0)
        async with httpx.AsyncClient(follow_redirects=False, timeout=timeout) as client:
            async with asyncio.timeout(MAX_SECONDS_PER_CASE):
                async with client.stream(
                        "POST", url, headers={"Authorization": "Bearer " + token},
                        json=body) as response:
                    headers_elapsed_ms = round((time.monotonic() - t0) * 1000)
                    raw = await response.aread()
                    body_elapsed_ms = round((time.monotonic() - t0) * 1000) - headers_elapsed_ms
                    payload = None
                    if len(raw) <= 65536:
                        try:
                            payload = json.loads(raw)
                        except (ValueError, UnicodeDecodeError):
                            pass
                    result_type, text_chars, codes = _result_details(payload)
                    schema_valid = None
                    parse_reason = "no_text"
                    output_tokens = None
                    if isinstance(payload, dict):
                        result = payload.get("result")
                        if isinstance(result, dict):
                            usage = result.get("usage") or {}
                            if isinstance(usage, dict):
                                output_tokens = usage.get("completion_tokens")
                            message = result.get("response")
                            if isinstance(message, str):
                                from services.ai.adapters import parse_result
                                from services.ai.contracts import AIResult
                                from services.subject_classifier import SubjectClassification
                                try:
                                    parsed = parse_result(AIResult(message), SubjectClassification)
                                    schema_valid = True
                                    parse_reason = "valid"
                                except (ValueError, TypeError, KeyError, IndexError) as exc:
                                    schema_valid = False
                                    parse_reason = type(exc).__name__
                    logger.info(
                        "CF_PROBE_RESULT case=%s status=%d headers_ms=%d body_ms=%d total_ms=%d "
                        "response_bytes=%d json=%s success_flag=%s result_type=%s text_chars=%d error_codes=%s schema_valid=%s parse_reason=%s output_tokens=%s",
                        name, response.status_code, headers_elapsed_ms, body_elapsed_ms,
                        round((time.monotonic() - t0) * 1000), len(raw),
                        payload is not None, payload.get("success") if isinstance(payload, dict) else None,
                        result_type, text_chars, codes, schema_valid, parse_reason, output_tokens,
                    )
                    return response.status_code == 200 and text_chars > 0
    except (TimeoutError, asyncio.TimeoutError, httpx.TimeoutException) as exc:
        logger.warning(
            "CF_PROBE_TIMEOUT case=%s phase=%s elapsed_ms=%d error_type=%s",
            name, "await_response_headers" if headers_elapsed_ms is None else "await_response_body",
            round((time.monotonic() - t0) * 1000), type(exc).__name__,
        )
    except httpx.HTTPError as exc:
        logger.warning("CF_PROBE_NETWORK_ERROR case=%s elapsed_ms=%d error_type=%s",
                       name, round((time.monotonic() - t0) * 1000), type(exc).__name__)
    except Exception as exc:
        logger.warning("CF_PROBE_UNEXPECTED case=%s elapsed_ms=%d error_type=%s",
                       name, round((time.monotonic() - t0) * 1000), type(exc).__name__)
    return False


async def run_cloudflare_probe():
    """Execute once in a background task on test branch; no external trigger route."""
    token = os.environ.get("CLOUDFLARE_API_TOKEN", "").strip()
    from helpers.ai_providers_helper import get_providers
    from services.ai.provider_presets import chat_service

    # Resolve endpoint from the bot's actual provider configuration; do not
    # assume a different account or expose endpoint/account details in logs.
    try:
        providers = await get_providers()
        cf = providers.get("cloudflare")
        base = cf["base_url"].rstrip("/") if cf else ""
    except Exception:
        logger.warning("CF_PROBE_ABORT reason=provider_configuration_unavailable")
        return
    if not token or not base or chat_service(base) != "cloudflare":
        logger.warning("CF_PROBE_ABORT reason=missing_token_or_invalid_cloudflare_endpoint")
        return
    from constants import SYSTEM_PROMPT_CLASSIFY_SUBJECT
    from services.subject_classifier import SubjectClassification
    schema = SubjectClassification.model_json_schema()
    sample = "احسب مشتقة الدالة س تربيع ثم حدّد نوع المحتوى."
    full = (
        SYSTEM_PROMPT_CLASSIFY_SUBJECT + "\n\n[عينة النص]:\n" + sample +
        "\n\nReturn ONLY valid JSON matching this schema:\n" +
        json.dumps(schema, ensure_ascii=False)
    )
    compact = (
        SYSTEM_PROMPT_CLASSIFY_SUBJECT + "\n\n[عينة النص]:\n" + sample +
        '\n\nأجب بكائن JSON فقط، دون Markdown أو شرح، بالصيغة '
        '{"subject":"math","suggested_types":[],"question_count":null,"content_subject":null}'
    )
    short = (
        'المحتوى: س تربيع زائد ٣س. '
        'أعد حرفياً JSON فقط: {"subject":"math"}'
    )
    cases = [
        ("messages_mini_json_ar", {"messages": [{"role": "user", "content": short}],
                                   "max_tokens": 96, "stream": False}),
        ("prompt_mini_json_ar", {"prompt": short, "max_tokens": 96, "stream": False}),
        ("messages_full_schema_512", {"messages": [{"role": "user", "content": full}],
                                      "max_tokens": 512, "stream": False}),
        ("messages_full_compact_256", {"messages": [{"role": "user", "content": compact}],
                                       "max_tokens": 256, "stream": False}),
        ("prompt_full_compact_256", {"prompt": compact, "max_tokens": 256, "stream": False}),
    ]
    logger.info("CF_PROBE_BEGIN cases=%d timeout_per_case_s=%d", len(cases), MAX_SECONDS_PER_CASE)
    started = time.monotonic()
    successes = 0
    for name, body in cases:
        if await _run_case(base, token, name, body):
            successes += 1
        await asyncio.sleep(1)
    logger.info("CF_PROBE_DONE cases=%d successes=%d total_ms=%d",
                len(cases), successes, round((time.monotonic() - started) * 1000))
