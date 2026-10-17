"""Model ordering, credential rotation and cooldown, independent of vendor SDKs."""
import asyncio
import hashlib
import os
import time
import uuid

from helpers.ai_models_helper import get_enabled_models
from helpers.ai_providers_helper import get_providers
from logger import get_logger
from services.ai.adapters import ADAPTERS, ProviderError

logger = get_logger(__name__)
_blocked = {}
_pointers = {}


def credentials_for(provider):
    if provider["transport"] == "vertex":
        secret = os.getenv("GOOGLE_VERTEX_SA_KEY_JSON", "").strip()
        return [secret] if secret and os.getenv("GOOGLE_VERTEX_PROJECT") else []
    return [key.strip() for key in os.getenv(provider["api_keys_env"], "").split(",") if key.strip()]


def _attempt_identity(provider, credential, model):
    # Credential/endpoint changes must not inherit old credentials' cooldown.
    fingerprint = hashlib.sha256((provider["transport"] + str(provider["base_url"]) + credential).encode()).digest()
    return provider["id"], fingerprint, model


async def execute(request, *, slot="cascade", exclude_vertex=False, mode="regular", timeout=None):
    from helpers.settings_helper import get_ai_request_timeout
    from services.ai.state import _last_model_used_var, _last_token_usage_var
    timeout = timeout if timeout is not None else await get_ai_request_timeout()
    request_id = uuid.uuid4().hex[:12]
    request_start = time.monotonic()
    providers = await get_providers()
    models = await get_enabled_models(slot)
    failed_attempts = 0
    logger.info(
        "AI_REQUEST_START request_id=%s slot=%s mode=%s capabilities=%s models=%d timeout_s=%s",
        request_id, slot, mode, sorted(request.capabilities), len(models), timeout,
    )
    now = time.monotonic()
    for key, until in list(_blocked.items()):
        if now >= until or key[0] not in providers:
            _blocked.pop(key, None)
    for key in list(_pointers):
        if key not in providers:
            _pointers.pop(key, None)
    for rank, entry in enumerate(models, start=1):
        provider = providers.get(entry["provider"])
        if not provider or not provider["is_enabled"]:
            continue
        if exclude_vertex and provider["transport"] == "vertex":
            continue
        adapter = ADAPTERS.get(provider["transport"])
        if adapter is None:
            continue
        capabilities = (adapter.capabilities_for(provider) if hasattr(adapter, "capabilities_for")
                        else adapter.capabilities)
        if not request.capabilities <= (set(entry["capabilities"]) & capabilities):
            continue
        if (hasattr(adapter, "supports_request") and
                not adapter.supports_request(provider, entry["model_name"], request)):
            continue
        credentials = credentials_for(provider)
        if not credentials:
            logger.warning(
                "AI_MODEL_SKIPPED request_id=%s slot=%s provider=%s model=%s reason=no_api_credentials",
                request_id, slot, provider["id"], entry["model_name"],
            )
            continue
        start = _pointers.get(provider["id"], 0) % len(credentials)
        _pointers[provider["id"]] = (start + 1) % len(credentials)
        for offset in range(len(credentials)):
            index = (start + offset) % len(credentials)
            identity = _attempt_identity(provider, credentials[index], entry["model_name"])
            remaining = _blocked.get(identity, 0) - time.monotonic()
            if remaining > 0:
                logger.info(
                    "AI_ATTEMPT_SKIPPED request_id=%s slot=%s provider=%s model=%s credential_index=%d reason=cooldown cooldown_s=%d",
                    request_id, slot, provider["id"], entry["model_name"], index, round(remaining),
                )
                continue
            started = time.monotonic()
            logger.info(
                "AI_ATTEMPT_START request_id=%s slot=%s provider=%s model=%s credential_index=%d rank=%d/%d",
                request_id, slot, provider["id"], entry["model_name"], index, rank, len(models),
            )
            try:
                result = await asyncio.wait_for(
                    adapter.generate(provider, credentials[index], entry["model_name"], request),
                    timeout=timeout,
                )
                _last_model_used_var.set({"provider": provider["id"], "model": entry["model_name"],
                                          "key_index": index, "cascade_rank": rank,
                                          "cascade_total": len(models), "mode": mode})
                _last_token_usage_var.set(result.usage)
                logger.info(
                    "AI_ATTEMPT_SUCCESS request_id=%s slot=%s provider=%s model=%s credential_index=%d "
                    "rank=%d/%d elapsed_ms=%d input_tokens=%d output_tokens=%d total_tokens=%d "
                    "truncated=%s fallback=%s",
                    request_id, slot, provider["id"], entry["model_name"], index, rank, len(models),
                    round((time.monotonic() - started) * 1000),
                    result.input_tokens, result.output_tokens, result.total_tokens,
                    result.truncated, bool(failed_attempts),
                )
                logger.info(
                    "AI_REQUEST_SUCCESS request_id=%s slot=%s provider=%s model=%s elapsed_ms=%d failed_attempts=%d",
                    request_id, slot, provider["id"], entry["model_name"],
                    round((time.monotonic() - request_start) * 1000), failed_attempts,
                )
                return result
            except FileNotFoundError:
                logger.warning(
                    "AI_REQUEST_ABORTED request_id=%s slot=%s reason=missing_local_attachment",
                    request_id, slot,
                )
                raise
            except Exception as exc:
                failed_attempts += 1
                status = getattr(exc, "status", None)
                if status is None:
                    status = getattr(exc, "code", None)
                status = status if type(status) is int else None
                reason = (exc.reason if isinstance(exc, ProviderError) else
                          "timeout" if isinstance(exc, (asyncio.TimeoutError, TimeoutError)) else
                          "invalid_response" if isinstance(exc, (ValueError, KeyError, IndexError, TypeError)) else
                          "request_error")
                retry_after_s = exc.retry_after_s if isinstance(exc, ProviderError) else None
                cooldown_s = 86400 if status == 429 else 60
                _blocked[identity] = time.monotonic() + cooldown_s
                # Never log exception text, image/text contents, credentials or HTTP response bodies.
                logger.warning(
                    "AI_ATTEMPT_FAILED request_id=%s slot=%s provider=%s model=%s credential_index=%d "
                    "rank=%d/%d elapsed_ms=%d error_type=%s status=%s reason=%s "
                    "retry_after_s=%s cooldown_s=%d",
                    request_id, slot, provider["id"], entry["model_name"], index, rank, len(models),
                    round((time.monotonic() - started) * 1000),
                    type(exc).__name__, status, reason, retry_after_s, cooldown_s,
                )
    logger.warning(
        "AI_REQUEST_FAILED request_id=%s slot=%s capabilities=%s elapsed_ms=%d failed_attempts=%d reason=no_eligible_success",
        request_id, slot, sorted(request.capabilities),
        round((time.monotonic() - request_start) * 1000), failed_attempts,
    )
    return None
