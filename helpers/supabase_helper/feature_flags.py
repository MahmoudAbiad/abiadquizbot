"""
supabase_helper.feature_flags
================================
🆕 مفاتيح التحكم العامة (Feature Flags): قراءة/كتابة حالة المفاتيح مع كاش
Redis قصير (راجع migration_feature_flags.sql + constants.FEATURE_FLAGS_REGISTRY
+ handlers/admin/feature_flags.py للتفاصيل الكاملة).
"""

import datetime
from typing import Dict, Any

from config import redis_client
from ._client import supabase, logger, log_error

# ==================== 🆕 مفاتيح التحكم العامة (Feature Flags) ====================
# راجع migration_feature_flags.sql + constants.FEATURE_FLAGS_REGISTRY +
# handlers/admin/feature_flags.py للتفاصيل الكاملة. كاش بـ Redis (TTL قصير) بدل
# قاموس بايثون محلي، لأن is_feature_enabled قد يُستدعى مع كل رفع محتوى من أي طالب -
# نتجنّب استعلام Supabase منفصل بكل مرة لمفتاح نادراً ما يتغيّر. استخدام Redis (بدل
# كاش داخل عملية Python وحدها) يضمن أنّ تبديل مفتاح من لوحة الأدمن ينعكس فوراً على
# كل نسخ/عمليات (processes/instances) البوت الشغّالة مع بعض، لا نسخة واحدة فقط.
# نفس redis_client المستخدم أصلاً بـ config.py لطابور التحليلات وتخزين حالة FSM.

_FEATURE_FLAG_REDIS_PREFIX = "feature_flag:"
FEATURE_FLAG_CACHE_TTL_SECONDS = 30


def _flag_redis_key(key: str) -> str:
    return f"{_FEATURE_FLAG_REDIS_PREFIX}{key}"


def _decode_flag_value(raw: Any) -> bool:
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", errors="ignore")
    return str(raw) == "1"


async def is_feature_enabled(key: str, default: bool = True) -> bool:
    """يرجع حالة تفعيل مفتاح معيّن. أي مفتاح غير موجود بالجدول أصلاً (لم يلمسه الأدمن
    بعد) أو فشل بالاتصال (بـ Supabase أو Redis) يُعامل كـ"مفعّل" افتراضياً (fail-safe -
    لا تعطيل غير مقصود لميزة قائمة بمجرد مشكلة اتصال عابرة)."""
    try:
        cached = await redis_client.get(_flag_redis_key(key))
        if cached is not None:
            return _decode_flag_value(cached)
    except Exception as e:
        log_error(logger, f"Redis error reading feature flag '{key}' (falling back to Supabase): {e}")
    try:
        res = await supabase.table("feature_flags").select("enabled").eq("key", key).limit(1).execute()
        enabled = bool(res.data[0]["enabled"]) if res.data else default
        try:
            await redis_client.set(_flag_redis_key(key), "1" if enabled else "0", ex=FEATURE_FLAG_CACHE_TTL_SECONDS)
        except Exception as re:
            log_error(logger, f"Redis error caching feature flag '{key}': {re}")
        return enabled
    except Exception as e:
        log_error(logger, f"Error fetching feature flag '{key}': {e}")
        return default


async def set_feature_flag(key: str, enabled: bool) -> bool:
    """يحفظ حالة مفتاح (تشغيل/إيقاف) من لوحة الأدمن بـ Supabase (المصدر الدائم للحقيقة)،
    ويحدّث كاش Redis المشترك فوراً لنفس اللحظة (بدل انتظار انتهاء TTL) حتى ينعكس القرار
    على أول طلب طالب تالٍ مباشرة، وعلى كل نسخ البوت الأخرى بنفس اللحظة أيضاً."""
    try:
        await supabase.table("feature_flags").upsert({
            "key": key, "enabled": enabled, "updated_at": datetime.datetime.utcnow().isoformat()
        }).execute()
        try:
            await redis_client.set(_flag_redis_key(key), "1" if enabled else "0", ex=FEATURE_FLAG_CACHE_TTL_SECONDS)
        except Exception as re:
            log_error(logger, f"Redis error updating feature flag cache '{key}': {re}")
        return True
    except Exception as e:
        log_error(logger, f"Error setting feature flag '{key}': {e}")
        return False


async def get_all_feature_flags(registry: Dict[str, str], default: bool = True) -> Dict[str, bool]:
    """يرجع حالة كل مفاتيح registry دفعة واحدة لعرضها بلوحة الأدمن - يقرأ أولاً من Redis
    (استعلام mget واحد)، وأي مفتاح غير موجود بالكاش يُجلب من Supabase دفعة واحدة أيضاً
    ثم يُخزَّن بـ Redis لتوفير الاستعلامات القادمة. أي مفتاح غير موجود بالجدول بعد يُعتبر
    مفعّلاً افتراضياً (نفس منطق is_feature_enabled)."""
    keys = list(registry.keys())
    result = {k: default for k in keys}
    if not keys:
        return result

    missing_keys = keys
    try:
        cached_vals = await redis_client.mget([_flag_redis_key(k) for k in keys])
        missing_keys = []
        for k, v in zip(keys, cached_vals):
            if v is None:
                missing_keys.append(k)
            else:
                result[k] = _decode_flag_value(v)
        if not missing_keys:
            return result
    except Exception as e:
        log_error(logger, f"Redis error reading feature flags batch (falling back to Supabase): {e}")
        missing_keys = keys

    try:
        res = await supabase.table("feature_flags").select("key, enabled").in_("key", missing_keys).execute()
        fetched = {row["key"]: bool(row["enabled"]) for row in (res.data or [])}
        result.update(fetched)
        try:
            async with redis_client.pipeline() as pipe:
                for k in missing_keys:
                    pipe.set(_flag_redis_key(k), "1" if result[k] else "0", ex=FEATURE_FLAG_CACHE_TTL_SECONDS)
                await pipe.execute()
        except Exception as re:
            log_error(logger, f"Redis error caching feature flags batch: {re}")
        return result
    except Exception as e:
        log_error(logger, f"Error fetching all feature flags: {e}")
        return result


