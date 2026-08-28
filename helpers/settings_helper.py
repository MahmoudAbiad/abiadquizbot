"""
إدارة الإعدادات القابلة للتعديل (نقاط الترحيب، التجديد اليومي، مكافأة الإحالة) من جدول
app_settings في سوبا بيس، بدلاً من قيم ثابتة في constants.py. الهدف: تمكين تعديلها من
لوحة الإدارة مباشرة دون الحاجة لتعديل الكود أو إعادة نشر البوت.

الكاش المحلي (TTL قصير) يقلل عدد الاستعلامات على كل رسالة/تفاعل، ويُصفَّر فوراً عند أي
تحديث يقوم به الأدمن حتى تنعكس القيمة الجديدة في نفس اللحظة على نفس العملية (worker).
لو كان هناك أكثر من عملية (worker) واحدة تعمل بالتوازي، فستلتقط بقية العمليات القيمة
الجديدة خلال مدة الكاش القصوى (SETTINGS_CACHE_TTL_SECONDS) على الأكثر.
"""

import time
from typing import Dict, Optional

from logger import get_logger, log_error
from constants import WELCOME_POINTS, DAILY_RENEWAL_POINTS, REFERRAL_BONUS_POINTS

logger = get_logger(__name__)

SETTINGS_CACHE_TTL_SECONDS = 30

# القيم الافتراضية (خط أمان أخير) لو تعذّر الوصول لجدول app_settings لأي سبب.
_DEFAULTS: Dict[str, float] = {
    "welcome_points": float(WELCOME_POINTS),
    "daily_renewal_points": float(DAILY_RENEWAL_POINTS),
    "referral_bonus_points": float(REFERRAL_BONUS_POINTS),
}

# تسميات عرض للوحة الإدارة (تُستخدم في الرسائل والأزرار).
SETTING_LABELS: Dict[str, str] = {
    "welcome_points": "🎁 نقاط الترحيب (عند أول تسجيل)",
    "daily_renewal_points": "🔄 نقاط التجديد اليومي المجاني",
    "referral_bonus_points": "🤝 مكافأة الإحالة (لكل صديق)",
}

_cache: Dict[str, float] = {}
_cache_timestamp: float = 0.0


async def get_app_settings(force_refresh: bool = False) -> Dict[str, float]:
    """يرجع كل الإعدادات الحالية كقاموس {key: value}، مع كاش قصير لتقليل الاستعلامات."""
    global _cache, _cache_timestamp
    now = time.monotonic()

    if not force_refresh and _cache and (now - _cache_timestamp) < SETTINGS_CACHE_TTL_SECONDS:
        return _cache

    from supabase_helper import supabase  # استيراد متأخر لتفادي أي دورة استيراد

    try:
        response = await supabase.table("app_settings").select("key, value").execute()
        if response.data:
            fresh = {row["key"]: float(row["value"]) for row in response.data}
            _cache = {**_DEFAULTS, **fresh}
            _cache_timestamp = now
            return _cache
    except Exception as e:
        log_error(logger, f"Error fetching app_settings, falling back to cached/default values: {e}", exception=e)

    # لو فشل الجلب: أرجع آخر كاش معروف إن وجد، وإلا القيم الافتراضية من constants.py
    return _cache or dict(_DEFAULTS)


async def get_setting(key: str) -> float:
    """يرجع قيمة إعداد واحد بالاسم (مثلاً 'daily_renewal_points')."""
    settings = await get_app_settings()
    return settings.get(key, _DEFAULTS.get(key, 0.0))


async def update_app_setting(key: str, value: float) -> Optional[float]:
    """يحدّث إعداداً واحداً بشكل ذري عبر update_app_setting_atomic، ويصفّر الكاش المحلي
    فوراً حتى تنعكس القيمة الجديدة في نفس اللحظة. يرجع القيمة الجديدة عند النجاح، أو None
    عند الفشل (مفتاح غير معروف، قيمة سالبة، أو خطأ اتصال)."""
    if key not in _DEFAULTS:
        return None
    if value is None or value < 0:
        return None

    from supabase_helper import supabase

    try:
        rpc_response = await supabase.rpc("update_app_setting_atomic", {
            "setting_key": key,
            "setting_value": float(value),
        }).execute()

        if rpc_response.data:
            row = rpc_response.data[0] if isinstance(rpc_response.data, list) else rpc_response.data
            new_value = float(row["value"])

            global _cache, _cache_timestamp
            if not _cache:
                _cache = dict(_DEFAULTS)
            _cache[key] = new_value
            _cache_timestamp = time.monotonic()

            return new_value
        return None
    except Exception as e:
        log_error(logger, f"Error updating app_setting '{key}' to {value}: {e}", exception=e)
        return None
