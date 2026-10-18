"""Provider configuration CRUD. Secrets are environment variable references only."""
import copy
import re
import time
from urllib.parse import urlsplit

from logger import get_logger

logger = get_logger(__name__)
TRANSPORTS = {"gemini", "vertex", "openai_compatible"}
CAPABILITIES = {"text", "image", "pdf", "audio"}
_cache = None
_cached_at = 0.0


def validate_provider(data):
    allowed = {"id", "label", "transport", "base_url", "api_keys_env", "is_enabled"}
    if not isinstance(data, dict) or set(data) - allowed:
        raise ValueError("حقول المزوّد المسموحة: " + ", ".join(sorted(allowed)))
    row = {"base_url": None, "api_keys_env": None, "is_enabled": True, **data}
    if not re.fullmatch(r"[a-z][a-z0-9-]{0,23}", row.get("id", "")):
        raise ValueError("id: حروف إنجليزية صغيرة وأرقام وشرطة، حتى 24 حرفًا")
    if not isinstance(row.get("label"), str) or not 1 <= len(row["label"].strip()) <= 80:
        raise ValueError("label: اسم العرض مطلوب، حتى 80 حرفًا")
    row["label"] = row["label"].strip()
    if row.get("transport") not in TRANSPORTS or type(row["is_enabled"]) is not bool:
        raise ValueError("transport غير مدعوم أو is_enabled ليست قيمة منطقية")
    env = row["api_keys_env"]
    if env is not None and (not isinstance(env, str) or not re.fullmatch(r"[A-Z][A-Z0-9_]{0,99}", env)):
        raise ValueError("api_keys_env هو اسم متغير البيئة، وليس المفتاح نفسه")
    if row["transport"] == "openai_compatible":
        url = row["base_url"]
        if not isinstance(url, str):
            raise ValueError("base_url مطلوب")
        parsed = urlsplit(url)
        if (parsed.scheme != "https" or not parsed.hostname or parsed.username or
                parsed.password or parsed.query or parsed.fragment):
            raise ValueError("base_url يجب أن يكون HTTPS بلا مفاتيح أو اسم مستخدم أو query")
        row["base_url"] = url.rstrip("/")
        if not env:
            raise ValueError("api_keys_env مطلوب")
    else:
        if row["base_url"] is not None:
            raise ValueError("base_url مخصص للواجهة المتوافقة مع OpenAI")
        if row["transport"] == "gemini" and not env:
            raise ValueError("api_keys_env مطلوب لـ Gemini")
        if row["transport"] == "vertex" and env is not None:
            raise ValueError("Vertex يستخدم متغيرات GOOGLE_VERTEX_* الحالية")
    return row


def invalidate_provider_cache():
    global _cache, _cached_at
    _cache, _cached_at = None, 0.0


async def get_providers(force_refresh=False):
    global _cache, _cached_at
    if not force_refresh and _cache is not None and time.monotonic() - _cached_at < 30:
        return copy.deepcopy(_cache)
    from helpers.supabase_helper import supabase
    try:
        response = await supabase.table("ai_providers").select("*").order("id").execute()
        rows = [validate_provider(row) for row in response.data or []]
        _cache, _cached_at = {row["id"]: row for row in rows}, time.monotonic()
    except Exception as exc:
        # No built-in resurrection after an intentional deletion/disable.
        logger.warning("Provider configuration unavailable (%s)", type(exc).__name__)
        if force_refresh:
            raise RuntimeError("تعذّر قراءة المزوّدين؛ تحقق من تطبيق migration والاتصال") from None
    return copy.deepcopy(_cache or {})


async def save_provider(data, *, create=False):
    row = validate_provider(data)
    from helpers.supabase_helper import supabase
    if create:
        response = await supabase.table("ai_providers").insert(row).execute()
    else:
        response = await supabase.table("ai_providers").update(row).eq("id", row["id"]).execute()
    invalidate_provider_cache()
    if not response.data:
        raise ValueError("المزوّد غير موجود أو لم تُحفظ التغييرات")
    return response.data[0]


async def delete_provider(provider_id):
    from helpers.supabase_helper import supabase
    response = await supabase.table("ai_providers").delete().eq("id", provider_id).execute()
    invalidate_provider_cache()
    from helpers.ai_models_helper import invalidate_cache
    invalidate_cache()
    return bool(response.data)
