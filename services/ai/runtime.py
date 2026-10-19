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
from services.ai.provider_presets import chat_service
from services.ai.rate_limits import RateLimitInfo, decide_cooldown

logger = get_logger(__name__)
_blocked = {}
_pointers = {}
_shared_blocked = {}  # (provider id, hash of configured key-set) -> (deadline, reason)


def credentials_for(provider):
    if provider["transport"] == "vertex":
        secret = os.getenv("GOOGLE_VERTEX_SA_KEY_JSON", "").strip()
        return [secret] if secret and os.getenv("GOOGLE_VERTEX_PROJECT") else []
    return [key.strip() for key in os.getenv(provider["api_keys_env"], "").split(",") if key.strip()]


def _attempt_identity(provider, credential, model):
    # Credential/endpoint changes must not inherit old credentials' cooldown.
    fingerprint = hashlib.sha256((provider["transport"] + str(provider["base_url"]) + credential).encode()).digest()
    return provider["id"], fingerprint, model


def _free_pool_identity(provider, credentials):
    # Changing keys or endpoint invalidates previous shared cooldown.
    fingerprint = hashlib.sha256(
        (provider["transport"] + str(provider["base_url"]) +
         "\\0".join(sorted(credentials))).encode()
    ).digest()
    return provider["id"], fingerprint


async def _effective_vertex_exclusion(exclude_vertex: bool) -> bool:
    """Allow Vertex in previously excluded multi-part flows only by explicit admin opt-in.

    Missing settings or storage failures preserve the historical exclusion.
    The feature-flags helper caches reads for 30 seconds and invalidates them
    after an admin write, so ordinary AI requests incur no extra lookup.
    """
    if not exclude_vertex:
        return False
    try:
        from helpers.supabase_helper.feature_flags import is_feature_enabled
        enabled = await is_feature_enabled("vertex_parallel_enabled", default=False)
        return not enabled
    except Exception:
        logger.warning("VERTEX_PARALLEL_FLAG_READ_FAILED preserving_exclusion=true")
        return True


async def execute(request, *, slot="cascade", exclude_vertex=False, mode="regular", timeout=None):
    from helpers.settings_helper import get_ai_request_timeout
    from services.ai.state import _last_model_used_var, _last_token_usage_var
    # Batch/super callers that historically excluded Vertex may opt in from
    # the admin AI menu. Requests without exclusion retain their old behavior.
    exclude_vertex = await _effective_vertex_exclusion(exclude_vertex)
    timeout = timeout if timeout is not None else await get_ai_request_timeout()
    request_id = uuid.uuid4().hex[:12]
    request_start = time.monotonic()
    providers = await get_providers()
    models = await get_enabled_models(slot)
    # Sticky model/key only within a multi-batch PDF session. Regular callers
    # retain the configured order and round-robin rotation unchanged.
    session = getattr(request, "file_session", None)
    preferred = getattr(session, "preferred", None)
    indexed_models = list(enumerate(models, start=1))
    if preferred is not None:
        indexed_models.sort(key=lambda item: 0 if (
            item[1]["provider"], item[1]["model_name"]
        ) == preferred[:2] else 1)
    failed_attempts = 0
    logger.info(
        "AI_REQUEST_START request_id=%s slot=%s mode=%s capabilities=%s models=%d timeout_s=%s",
        request_id, slot, mode, sorted(request.capabilities), len(models), timeout,
    )
    now = time.monotonic()
    for key, until in list(_blocked.items()):
        if now >= until or key[0] not in providers:
            _blocked.pop(key, None)
    for key, (until, _) in list(_shared_blocked.items()):
        if now >= until or key[0] not in providers:
            _shared_blocked.pop(key, None)
    for key in list(_pointers):
        if key not in providers:
            _pointers.pop(key, None)
    for rank, entry in indexed_models:
        provider = providers.get(entry["provider"])
        if not provider or not provider["is_enabled"]:
            logger.info("AI_MODEL_SKIPPED request_id=%s slot=%s provider=%s model=%s reason=provider_disabled_or_missing",
                        request_id, slot, entry["provider"], entry["model_name"])
            continue
        if exclude_vertex and provider["transport"] == "vertex":
            logger.info("AI_MODEL_SKIPPED request_id=%s slot=%s provider=%s model=%s reason=vertex_excluded",
                        request_id, slot, provider["id"], entry["model_name"])
            continue
        adapter = ADAPTERS.get(provider["transport"])
        if adapter is None:
            logger.warning("AI_MODEL_SKIPPED request_id=%s slot=%s provider=%s model=%s reason=unsupported_transport",
                           request_id, slot, provider["id"], entry["model_name"])
            continue
        capabilities = (adapter.capabilities_for(provider) if hasattr(adapter, "capabilities_for")
                        else adapter.capabilities)
        if not request.capabilities <= (set(entry["capabilities"]) & capabilities):
            logger.info("AI_MODEL_SKIPPED request_id=%s slot=%s provider=%s model=%s reason=incompatible_capabilities required=%s",
                        request_id, slot, provider["id"], entry["model_name"], sorted(request.capabilities))
            continue
        if (hasattr(adapter, "supports_request") and
                not adapter.supports_request(provider, entry["model_name"], request)):
            logger.info("AI_MODEL_SKIPPED request_id=%s slot=%s provider=%s model=%s reason=unsupported_request",
                        request_id, slot, provider["id"], entry["model_name"])
            continue
        credentials = credentials_for(provider)
        if not credentials:
            logger.warning(
                "AI_MODEL_SKIPPED request_id=%s slot=%s provider=%s model=%s reason=no_api_credentials",
                request_id, slot, provider["id"], entry["model_name"],
            )
            continue
        is_openrouter_free = (
            chat_service(provider.get("base_url") or "") == "openrouter"
            and entry["model_name"].endswith(":free")
        )
        pool_identity = _free_pool_identity(provider, credentials) if is_openrouter_free else None
        if pool_identity is not None:
            shared = _shared_blocked.get(pool_identity)
            if shared and shared[0] > time.monotonic():
                logger.info(
                    "AI_MODEL_SKIPPED request_id=%s slot=%s provider=%s model=%s "
                    "reason=shared_rate_limit cooldown_scope=openrouter_free_models "
                    "limit_reason=%s cooldown_s=%d",
                    request_id, slot, provider["id"], entry["model_name"],
                    shared[1], max(1, round(shared[0] - time.monotonic())),
                )
                continue
        start = _pointers.get(provider["id"], 0) % len(credentials)
        if (preferred is not None and (provider["id"], entry["model_name"]) == preferred[:2]
                and isinstance(preferred[2], int) and 0 <= preferred[2] < len(credentials)):
            start = preferred[2]
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
                if session is not None:
                    session.preferred = (provider["id"], entry["model_name"], index)
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
                reset_after_s = exc.reset_after_s if isinstance(exc, ProviderError) else None
                # Batch-only Gemini SDK quota details can distinguish RPM from
                # daily quota. Keep ordinary generation fallback behavior intact.
                if (status == 429 and reason == "request_error"
                        and getattr(request, "file_session", None) is not None
                        and provider["transport"] == "gemini"):
                    hint = str(exc).casefold()[:4096]  # inspect only; never log raw text
                    if any(tag in hint for tag in (
                            "perday", "per_day", "requests/day", "daily quota", "daily limit")):
                        reason = "daily_quota"
                    elif any(tag in hint for tag in (
                            "perminute", "per_minute", "requests/min", "minute limit")):
                        reason = "minute_rate_limit"
                cooldown = decide_cooldown(
                    status, RateLimitInfo(reason, retry_after_s, reset_after_s),
                    is_openrouter_free=is_openrouter_free,
                )
                if (provider["transport"] == "gemini" and reason == "daily_quota"
                        and getattr(request, "file_session", None) is not None
                        and reset_after_s is None and retry_after_s is None):
                    # Do not assume another vendor's UTC quota reset for Gemini.
                    from services.ai.rate_limits import Cooldown
                    cooldown = Cooldown(86400, "model_credential", "conservative_daily")
                until = time.monotonic() + cooldown.seconds
                if cooldown.scope == "openrouter_free_models" and pool_identity is not None:
                    _shared_blocked[pool_identity] = (until, reason)
                else:
                    _blocked[identity] = until
                # Never log raw upstream responses, user content or credentials.
                logger.warning(
                    "AI_ATTEMPT_FAILED request_id=%s slot=%s provider=%s model=%s credential_index=%d "
                    "rank=%d/%d elapsed_ms=%d error_type=%s status=%s reason=%s "
                    "retry_after_s=%s reset_after_s=%s cooldown_s=%d "
                    "cooldown_scope=%s cooldown_basis=%s",
                    request_id, slot, provider["id"], entry["model_name"], index, rank, len(models),
                    round((time.monotonic() - started) * 1000),
                    type(exc).__name__, status, reason, retry_after_s, reset_after_s,
                    cooldown.seconds, cooldown.scope, cooldown.basis,
                )
                if cooldown.scope == "openrouter_free_models":
                    # Other OpenRouter keys under this provider share the same free quota.
                    # Do not burn another request against a known account-wide limit.
                    break
    logger.warning(
        "AI_REQUEST_FAILED request_id=%s slot=%s capabilities=%s elapsed_ms=%d failed_attempts=%d reason=no_eligible_success",
        request_id, slot, sorted(request.capabilities),
        round((time.monotonic() - request_start) * 1000), failed_attempts,
    )
    return None
