"""Model ordering, credential rotation and cooldown, independent of vendor SDKs."""
import asyncio
import hashlib
import os
import time

from helpers.ai_models_helper import get_enabled_models
from helpers.ai_providers_helper import get_providers
from logger import get_logger
from services.ai.adapters import ADAPTERS

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
    providers = await get_providers()
    models = await get_enabled_models(slot)
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
        credentials = credentials_for(provider)
        if not credentials:
            continue
        start = _pointers.get(provider["id"], 0) % len(credentials)
        _pointers[provider["id"]] = (start + 1) % len(credentials)
        for offset in range(len(credentials)):
            index = (start + offset) % len(credentials)
            identity = _attempt_identity(provider, credentials[index], entry["model_name"])
            if _blocked.get(identity, 0) > time.monotonic():
                continue
            try:
                result = await asyncio.wait_for(adapter.generate(provider, credentials[index], entry["model_name"], request),
                                                timeout=timeout)
                _last_model_used_var.set({"provider": provider["id"], "model": entry["model_name"],
                                          "key_index": index, "cascade_rank": rank,
                                          "cascade_total": len(models), "mode": mode})
                _last_token_usage_var.set(result.usage)
                return result
            except FileNotFoundError:
                # A missing local upload is independent of every provider's health.
                raise
            except Exception as exc:
                status = getattr(exc, "status", None) or getattr(exc, "code", None)
                _blocked[identity] = time.monotonic() + (86400 if status == 429 else 60)
                # Upstream exception strings can contain API keys, URLs or user data.
                logger.warning("AI attempt failed provider=%s model=%s credential=%s error=%s status=%s",
                               provider["id"], entry["model_name"], index, type(exc).__name__, status)
    logger.warning("No eligible AI provider succeeded for slot=%s capabilities=%s", slot, sorted(request.capabilities))
    return None
