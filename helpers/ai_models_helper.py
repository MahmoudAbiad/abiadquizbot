# helpers/ai_models_helper.py
"""
==============================================================================
MODULE: Dynamic AI Model Configuration (لوحة التحكم بموديلات الذكاء الاصطناعي)
==============================================================================
الوصف:
يستبدل هذا الموديول القوائم الثابتة اللي كانت مكتوبة مباشرة بالكود:
- MODELS_CASCADE (كانت بـ helpers/gemini_helper.py): سلسلة موديلات Gemini المستخدَمة
  لتوليد الأسئلة بالترتيب (Waterfall) - slot="cascade".
- MATH_DETECTION_MODEL (كانت بـ constants.py): الموديل المستخدَم لفحص الرياضيات/تصنيف
  المادة السريع - slot="detection".
- الموديل الثابت "openai/gpt-oss-120b" (كان بـ _generate_text_quiz): المسار السريع
  عبر Groq لتوليد الكويز من نص صريح - slot="groq_fast".

كل هذه القيم صارت مخزَّنة بجدول ai_model_slots بسوبا بيس، وقابلة للتعديل مباشرة من
لوحة تحكم الأدمن (handlers/admin/ai_control.py) دون أي حاجة لتعديل الكود أو إعادة
نشر البوت - بنفس فلسفة settings_helper.py (نقاط الترحيب/التجديد/الإحالة) بالضبط.

كاش محلي قصير (TTL) لتقليل عدد الاستعلامات على المسار الساخن (كل عملية توليد كويز
تقرأ سلسلة الـ cascade)، يُصفَّر فوراً عند أي تعديل يقوم به الأدمن.

⚠️ ملاحظة معمارية مهمة: التنفيذ الفعلي لسلسلة "cascade" و"detection" حالياً مربوط حصراً
بمزوّد Gemini (helpers/gemini_helper.py و services/detection_common.py يستخدمان
google-genai SDK مباشرة لقراءة الملفات/الصور بشكل أصلي). حقل "provider" بالجدول عام
ويقبل "gemini"/"groq"/"openai" لإتاحة إضافة مزوّدين جدد مستقبلاً من نفس اللوحة، لكن أي
صف بمزوّد غير "gemini" ضمن slot="cascade" أو slot="detection" سيُتخطى تلقائياً وقت
التنفيذ الفعلي (مع تسجيل تحذير بالـ logs) لحين إضافة تكامل SDK مخصص لذلك المزوّد -
راجع التعليق أعلى _attempt بـ helpers/gemini_helper.py. أما slot="groq_fast" فمربوط
فعلياً بعميل Groq الموجود أصلاً (AsyncGroq) ويمكن تغيير اسم الموديل منه بأمان تام.
==============================================================================
"""

import time
from typing import Dict, List, Optional

from logger import get_logger, log_error

logger = get_logger(__name__)

CACHE_TTL_SECONDS = 30

SLOT_CASCADE = "cascade"
SLOT_DETECTION = "detection"
SLOT_GROQ_FAST = "groq_fast"

VALID_SLOTS = {SLOT_CASCADE, SLOT_DETECTION, SLOT_GROQ_FAST}
VALID_PROVIDERS = {"gemini", "groq", "openai"}

PROVIDER_LABELS: Dict[str, str] = {
    "gemini": "🟦 Gemini (Google)",
    "groq": "🟩 Groq",
    "openai": "⚪ OpenAI",
}

SLOT_LABELS: Dict[str, str] = {
    SLOT_CASCADE: "🧠 سلسلة توليد الأسئلة (Cascade)",
    SLOT_DETECTION: "🔍 فحص المحتوى السريع (رياضيات/تصنيف المادة)",
    SLOT_GROQ_FAST: "⚡ المسار السريع (Groq - نص صريح)",
}

# خط أمان أخير لو تعذّر الوصول لقاعدة البيانات بالكامل (نفس القيم المزروعة بالـ migration).
_FALLBACK_DEFAULTS: Dict[str, List[Dict]] = {
    SLOT_CASCADE: [
        {"id": None, "provider": "gemini", "model_name": "gemini-3.6-flash", "display_order": 1, "is_enabled": True},
        {"id": None, "provider": "gemini", "model_name": "gemini-3.7-flash", "display_order": 2, "is_enabled": True},
        {"id": None, "provider": "gemini", "model_name": "gemini-3.5-flash", "display_order": 3, "is_enabled": True},
        {"id": None, "provider": "gemini", "model_name": "gemini-3.5-flash-lite", "display_order": 4, "is_enabled": True},
    ],
    SLOT_DETECTION: [
        {"id": None, "provider": "gemini", "model_name": "gemini-3.5-flash-lite", "display_order": 1, "is_enabled": True},
    ],
    SLOT_GROQ_FAST: [
        {"id": None, "provider": "groq", "model_name": "openai/gpt-oss-120b", "display_order": 1, "is_enabled": True},
    ],
}

# كاش لكل slot على حدة: {slot: (timestamp, [rows...])}
_cache: Dict[str, tuple] = {}


def invalidate_cache(slot: Optional[str] = None) -> None:
    """تصفير الكاش فوراً بعد أي تعديل (إضافة/حذف/تفعيل/ترتيب) حتى تنعكس القيمة الجديدة
    في نفس اللحظة. لو لم يُحدَّد slot، يُصفَّر الكاش بالكامل."""
    if slot is None:
        _cache.clear()
    else:
        _cache.pop(slot, None)


async def get_all_models(slot: str, force_refresh: bool = False) -> List[Dict]:
    """يرجع كل صفوف slot معيّن (مفعّلة وغير مفعّلة) مرتّبة حسب display_order - تُستخدم
    من لوحة الأدمن لعرض القائمة الكاملة. عند فشل الاتصال يرجع آخر كاش معروف أو fallback."""
    now = time.monotonic()
    cached = _cache.get(slot)
    if not force_refresh and cached and (now - cached[0]) < CACHE_TTL_SECONDS:
        return cached[1]

    from supabase_helper import supabase  # استيراد متأخر لتفادي أي دورة استيراد

    try:
        response = await (
            supabase.table("ai_model_slots")
            .select("id, provider, model_name, display_order, is_enabled")
            .eq("slot", slot)
            .order("display_order")
            .execute()
        )
        rows = response.data or []
        _cache[slot] = (now, rows)
        return rows
    except Exception as e:
        log_error(logger, f"Error fetching ai_model_slots for slot='{slot}': {e}", exception=e)
        if cached:
            return cached[1]
        return _FALLBACK_DEFAULTS.get(slot, [])


async def get_enabled_models(slot: str, force_refresh: bool = False) -> List[Dict]:
    """نفس get_all_models لكن مفلترة على is_enabled=True فقط - هذا ما يُستخدم فعلياً
    وقت التنفيذ (توليد الكويز/فحص المحتوى)."""
    rows = await get_all_models(slot, force_refresh=force_refresh)
    enabled = [row for row in rows if row.get("is_enabled")]
    return enabled or _FALLBACK_DEFAULTS.get(slot, [])


async def get_cascade_models(force_refresh: bool = False) -> List[Dict]:
    """سلسلة موديلات توليد الأسئلة المفعّلة بالترتيب - بديل MODELS_CASCADE السابقة."""
    return await get_enabled_models(SLOT_CASCADE, force_refresh=force_refresh)


async def get_detection_model(force_refresh: bool = False) -> Dict:
    """أول موديل مفعّل بسلسلة الفحص السريع - بديل MATH_DETECTION_MODEL السابقة.
    يرجع دائماً صفاً واحداً (Fallback الافتراضي لو الجدول فاضي لأي سبب)."""
    rows = await get_enabled_models(SLOT_DETECTION, force_refresh=force_refresh)
    if rows:
        return rows[0]
    return _FALLBACK_DEFAULTS[SLOT_DETECTION][0]


async def get_groq_fast_model(force_refresh: bool = False) -> Dict:
    """الموديل المفعّل الحالي للمسار السريع عبر Groq - بديل الاسم الثابت "openai/gpt-oss-120b"."""
    rows = await get_enabled_models(SLOT_GROQ_FAST, force_refresh=force_refresh)
    if rows:
        return rows[0]
    return _FALLBACK_DEFAULTS[SLOT_GROQ_FAST][0]


async def add_model(slot: str, provider: str, model_name: str) -> Optional[Dict]:
    """إضافة موديل جديد لسلسلة (slot) - عبر RPC ai_model_add (ترتيب تلقائي بآخر القائمة)."""
    if slot not in VALID_SLOTS or provider not in VALID_PROVIDERS or not (model_name or "").strip():
        return None

    from supabase_helper import supabase

    try:
        rpc_response = await supabase.rpc(
            "ai_model_add",
            {"p_slot": slot, "p_provider": provider, "p_model_name": model_name.strip()},
        ).execute()
        invalidate_cache(slot)
        rows = rpc_response.data or []
        return rows[0] if rows else None
    except Exception as e:
        log_error(logger, f"Error adding ai_model_slots row (slot={slot}, provider={provider}, model={model_name}): {e}", exception=e)
        return None


async def remove_model(model_id: int, slot: str) -> bool:
    """حذف موديل من السلسلة عبر RPC ai_model_remove."""
    from supabase_helper import supabase

    try:
        await supabase.rpc("ai_model_remove", {"p_id": model_id}).execute()
        invalidate_cache(slot)
        return True
    except Exception as e:
        log_error(logger, f"Error removing ai_model_slots id={model_id}: {e}", exception=e)
        return False


async def toggle_model(model_id: int, slot: str) -> Optional[Dict]:
    """تبديل حالة التفعيل عبر RPC ai_model_toggle."""
    from supabase_helper import supabase

    try:
        rpc_response = await supabase.rpc("ai_model_toggle", {"p_id": model_id}).execute()
        invalidate_cache(slot)
        rows = rpc_response.data or []
        return rows[0] if rows else None
    except Exception as e:
        log_error(logger, f"Error toggling ai_model_slots id={model_id}: {e}", exception=e)
        return None


async def move_model(model_id: int, slot: str, direction: str) -> bool:
    """تحريك موديل خطوة واحدة لأعلى/أسفل بالسلسلة عبر RPC ai_model_move (direction: 'up'|'down')."""
    if direction not in ("up", "down"):
        return False

    from supabase_helper import supabase

    try:
        await supabase.rpc("ai_model_move", {"p_id": model_id, "p_direction": direction}).execute()
        invalidate_cache(slot)
        return True
    except Exception as e:
        log_error(logger, f"Error moving ai_model_slots id={model_id} direction={direction}: {e}", exception=e)
        return False
