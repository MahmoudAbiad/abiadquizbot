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
import math
from typing import Dict, Optional

from logger import get_logger, log_error
from constants import (
    WELCOME_POINTS, DAILY_RENEWAL_POINTS, REFERRAL_BONUS_POINTS,
    CLASSIFICATION_VOTE_THRESHOLD, AI_REQUEST_TIMEOUT,
)

logger = get_logger(__name__)

SETTINGS_CACHE_TTL_SECONDS = 30

# القيم الافتراضية (خط أمان أخير) لو تعذّر الوصول لجدول app_settings لأي سبب.
_DEFAULTS: Dict[str, float] = {
    "welcome_points": float(WELCOME_POINTS),
    "welcome_gift_days": 30,
    "referral_gift_days": 0,
    "daily_renewal_points": float(DAILY_RENEWAL_POINTS),
    "referral_bonus_points": float(REFERRAL_BONUS_POINTS),
    # 🩹 كانت ثابتاً وحيداً بـ constants.py بلا أي ربط فعلي بعتبة vote_on_classification
    # في SQL (كل منهما كان يحمل نفس الرقم 3 بشكل مستقل ومكرر بلا مصدر حقيقة واحد -
    # خطر انحراف صامت لو عُدِّل أحدهما بدون الآخر). الآن أصبح app_settings هو المصدر
    # الوحيد الفعلي، ويُمرَّر صراحة لـ RPC عبر submit_classification_vote أدناه.
    "classification_vote_threshold": float(CLASSIFICATION_VOTE_THRESHOLD),
    # 🆕 تسعير الكويزات (كان رقمين ثابتين مباشرة بالكود بـ helpers/points_calculator.py:
    # 1.0 للتير الأساسي و1.5 للتجاوز/سوبر) - صارا معدّلين قابلين للتعديل من لوحة الأدمن
    # دون لمس عتبات التفعيل نفسها (MAX_STANDARD_PAGES/MAX_STANDARD_QUESTIONS/
    # MAX_LIMIT_PAGES/MAX_ALBUM_IMAGES تبقى ثابتة بـ constants.py كما هي تماماً، لأنها
    # مرتبطة أيضاً بقرار التنفيذ الفعلي is_super_pdf/is_super_images بـ gemini_helper.py).
    "quiz_standard_rate": 1.0,
    "quiz_overage_rate": 1.5,
    # 🆕 مهلة كل محاولة توليد (مفتاح × موديل) بالثواني - كانت AI_REQUEST_TIMEOUT ثابتاً بالكود.
    "ai_request_timeout": float(AI_REQUEST_TIMEOUT),
    # Enum codes keep compatibility with app_settings' numeric value column.
    "ai_detection_thinking": 2.0,
    "ai_generation_thinking": 0.0,
}

AI_THINKING_LEVELS = {"auto": 0, "low": 1, "medium": 2, "high": 3}
AI_THINKING_LABELS = {"auto": "تلقائي", "low": "منخفض", "medium": "متوسط", "high": "مرتفع"}
AI_THINKING_SETTINGS = {
    "detection": "ai_detection_thinking",
    "generation": "ai_generation_thinking",
}


def thinking_level_from_settings(settings, task):
    key = AI_THINKING_SETTINGS[task]
    value = settings.get(key, _DEFAULTS[key])
    return next((level for level, code in AI_THINKING_LEVELS.items() if value == code),
                "medium" if task == "detection" else "auto")


async def get_ai_thinking_level(task):
    settings = await get_app_settings()
    level = thinking_level_from_settings(settings, task)
    return None if level == "auto" else level


async def update_ai_thinking_level(task, level):
    if task not in AI_THINKING_SETTINGS or level not in AI_THINKING_LEVELS:
        return False
    result = await update_app_setting(AI_THINKING_SETTINGS[task], AI_THINKING_LEVELS[level])
    return result is not None

# تسميات عرض للوحة الإدارة (تُستخدم في الرسائل والأزرار).
SETTING_LABELS: Dict[str, str] = {
    "welcome_gift_days": "⏳ صلاحية هدية الترحيب بالأيام (0 = بلا انتهاء)",
    "referral_gift_days": "⏳ صلاحية هدية الإحالة بالأيام (0 = بلا انتهاء)",
    "welcome_points": "🎁 نقاط الترحيب (عند أول تسجيل)",
    "daily_renewal_points": "🔄 نقاط التجديد اليومي المجاني",
    "referral_bonus_points": "🤝 مكافأة الإحالة (لكل صديق)",
    "classification_vote_threshold": "🗳 عتبة تثبيت تصنيف المادة (عدد الأصوات)",
    "quiz_standard_rate": "💎 سعر الكويز العادي (نقطة/وحدة)",
    "quiz_overage_rate": "🚀 سعر التجاوز/السوبر (نقطة/وحدة)",
    "ai_request_timeout": "⏱ مهلة طلب الذكاء الاصطناعي (ثانية)",
}

# 🩹 حد أدنى مخصص لبعض الإعدادات (0 معقول لنقاط/مكافآت، لكن غير منطقي هنا: عتبة=0
# تعني تثبيت أي تصنيف فور أول صوت "نعم" واحد بدل انتظار إجماع طلاب فعلي). الإعدادات
# غير المذكورة هنا تبقى بحدها الأدنى الافتراضي (0) كما كانت دائماً.
SETTING_MIN_VALUES: Dict[str, float] = {
    "classification_vote_threshold": 1,
    # 🆕 تفادياً لتصفير سعر الكويز بالغلط (0 حد أدنى افتراضي كان سيسمح بذلك تماماً).
    "quiz_standard_rate": 0.1,
    "quiz_overage_rate": 0.1,
    # 🆕 أقل من 30 ثانية يقتل أي طلب ملف حقيقي (الملفات الممسوحة تحتاج 40-90 ثانية عادة)
    # ويحظر الزوج (مفتاح، موديل) دقيقة كاملة لكل مهلة منتهية.
    "ai_request_timeout": 30,
}

# 🆕 حدود قصوى مخصصة (الحد العام لكل الإعدادات 100000 بـ handlers/admin/settings.py - غير
# منطقي لمهلة شبكة: 600 ثانية = 10 دقائق سقف معقول لطلب واحد).
SETTING_MAX_VALUES: Dict[str, float] = {
    "welcome_gift_days": 3650,
    "referral_gift_days": 3650,
    "ai_request_timeout": 600,
}

# إعدادات الذكاء الاصطناعي: تُعرض بلوحة "التحكم بالذكاء الاصطناعي" لا بلوحة النقاط،
# ويرجع الأدمن لتلك اللوحة بعد التعديل.
AI_SETTING_KEYS = frozenset({"ai_request_timeout", *AI_THINKING_SETTINGS.values()})

# 🩹 عتبة تصنيف المادة إعداد خاص بميزة "رسالة التحقق المجتمعي من التصنيف"
# (classification_vote_prompt بـ constants.FEATURE_FLAGS_REGISTRY) وليست إعداد نقاط -
# لذلك مكانها الطبيعي هو لوحة تفاصيل تلك الميزة ضمن "🧩 مفاتيح التحكم"، وليس ضمن
# "🎯 إعدادات النقاط" كما كانت سابقاً. تبقى مسجّلة بـ SETTING_LABELS/SETTING_MIN_VALUES
# فوق (تدفّق التعديل العام بـ handlers/admin/settings.py يعتمد عليها بغض النظر عن مكان
# عرضها)، وهذا القاموس فقط يوجّه أي إعداد من هذا النوع لواجهة الميزة الصحيحة بدل إعدادات
# النقاط. راجع handlers/admin/feature_flags.py (FEATURE_EXTRA_SETTINGS + render_feature_detail).
SETTING_TO_FEATURE_FLAG: Dict[str, str] = {
    "classification_vote_threshold": "classification_vote_prompt",
}

# نسخة من SETTING_LABELS تستثني أي إعداد صار خاصاً بميزة معيّنة (راجع
# SETTING_TO_FEATURE_FLAG فوق) - هي التي تُستخدم فعلياً بعرض لوحة "إعدادات النقاط"
# (keyboards.get_admin_settings_keyboard عبر handlers/admin/settings.py) حتى لا يظهر
# فيها إعداد لا علاقة له بالنقاط.
POINTS_SETTING_LABELS: Dict[str, str] = {
    key: label for key, label in SETTING_LABELS.items()
    if key not in SETTING_TO_FEATURE_FLAG and key not in AI_SETTING_KEYS
}


def format_setting_value(value: Optional[float]) -> str:
    """يعرض قيمة الإعداد كعدد صحيح لو كانت صحيحة فعلاً (مثلاً 50.0 -> "50")، وإلا
    بمنزلتين عشريتين كحد أقصى مع حذف الأصفار الزائدة (مثلاً 1.5 -> "1.5"،
    0.1 -> "0.1"). 🩹 قبل هذه الدالة كانت كل قيم إعدادات النقاط تُعرض بصيغة `:.0f`
    (تقريب لعدد صحيح دائماً)، فكانت قيمة عشرية فعلية مثل سعر السوبر 1.5 تظهر بلوحة
    الأدمن "2" رغم إن المخزَّن فعلياً 1.5 - يسبب لبساً حقيقياً للأدمن عند التعديل."""
    if value is None:
        return "؟"
    value = float(value)
    if value == int(value):
        return str(int(value))
    return f"{value:.2f}".rstrip("0").rstrip(".")

_cache: Dict[str, float] = {}
_cache_timestamp: float = 0.0


async def get_app_settings(force_refresh: bool = False) -> Dict[str, float]:
    """يرجع كل الإعدادات الحالية كقاموس {key: value}، مع كاش قصير لتقليل الاستعلامات."""
    global _cache, _cache_timestamp
    now = time.monotonic()

    if not force_refresh and _cache and (now - _cache_timestamp) < SETTINGS_CACHE_TTL_SECONDS:
        return _cache

    try:
        from helpers.supabase_helper import supabase  # استيراد متأخر لتفادي أي دورة استيراد
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
    if value is None or not math.isfinite(value) or value < 0:
        return None
    if key in AI_THINKING_SETTINGS.values() and value not in AI_THINKING_LEVELS.values():
        return None

    from helpers.supabase_helper import supabase

    try:
        if key in AI_THINKING_SETTINGS.values():
            # New settings create their own rows on first save; no migration or
            # wider write permissions are needed. Only the server uses this client.
            from datetime import datetime, timezone
            rpc_response = await supabase.table("app_settings").upsert({
                "key": key, "value": float(value),
                "updated_at": datetime.now(timezone.utc).isoformat(),
            }, on_conflict="key").execute()
        else:
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


async def get_ai_request_timeout() -> float:
    """مهلة المحاولة الواحدة بالثواني، تُقرأ عند كل محاولة (كاش 30 ثانية) فتسري فوراً على
    الطلبات الجديدة بلا إعادة نشر. محصورة بحدود آمنة حتى لو خُزِّنت قيمة شاذة بالجدول."""
    value = await get_setting("ai_request_timeout")
    low = SETTING_MIN_VALUES["ai_request_timeout"]
    high = SETTING_MAX_VALUES["ai_request_timeout"]
    return max(low, min(high, float(value)))

