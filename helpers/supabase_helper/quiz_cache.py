"""
supabase_helper.quiz_cache
===========================
Central Quiz & Cache Operations: كاش الكويزات المركزي، تسجيل استدعاءات AI،
وأسعار الموديلات.
"""

import asyncio
import os
import time
from typing import Optional, Dict, List, Any

from ._client import supabase, logger, log_error, log_warning, log_info

# ==================== Central Quiz & Cache Operations ====================

def _is_transient_jwt_clock_skew_error(error: Exception) -> bool:
    """🩹 خطأ PGRST303 ('JWT issued at future') متقطع وغير متعلق بالكود - سببه انزياح
    بسيط بساعة نظام الـ dyno (clock skew) وقت restart أحياناً، وليس مشكلة بالتوكن نفسه
    (SUPABASE_KEY ثابت من env، لا يُبنى بالكود). عادة يزول خلال ثوانٍ لما الساعة تتزامن
    من جديد عبر NTP، فمحاولة واحدة بعد تأخير بسيط كفيلة بحله دون التأثير على أي مسار آخر."""
    message = str(error).lower()
    return "pgrst303" in message or "jwt issued at future" in message


async def get_file_quizzes(file_hash: str) -> list:
    """جلب كل الكويزات التابعة للملف مرتبة تلقائياً حسب التقييم الأعلى لزملائك الطلاب.
    🆕 يشمل الآن subject_type/question_type/question_type_label/difficulty لعرض
    تفاصيل كل كويز مخزّن (نوع + صعوبة) وللسماح بالفلترة والتحقق من سقف كل تركيبة
    على حدة بدل سقف مشترك واحد للملف بأكمله.
    🩹 يعيد المحاولة مرة واحدة عند PGRST303 (انزياح ساعة مؤقت) بدل الاستسلام فوراً
    وإرجاع قائمة فارغة (كانت تُفسَّر خطأً كـ"لا يوجد كويزات محفوظة لهذا الملف")."""
    for attempt in range(2):
        try:
            res = await supabase.table("quizzes").select(
                "id, creator_id, likes, dislikes, score, quiz_data, is_math_quiz, "
                "subject_type, question_type, question_type_label, difficulty"
            ).eq("file_hash", file_hash).order("score", desc=True).execute()
            return res.data or []
        except Exception as e:
            if attempt == 0 and _is_transient_jwt_clock_skew_error(e):
                log_warning(logger, f"Transient JWT clock-skew error getting file quizzes, retrying once: {e}")
                await asyncio.sleep(2)
                continue
            log_error(logger, f"Error getting file quizzes from central table: {e}")
            return []
    return []

async def save_file_quiz_multiple(
    file_hash: str, creator_id: int, source_title: str, quiz_data: list, total_tokens: int,
    is_math_quiz: bool = False, subject_type: str = "other", question_type: str = "general",
    question_type_label: Optional[str] = None, difficulty: str = "medium",
) -> Optional[str]:
    """حفظ كويز جديد مولد كلياً بالجدول المركزي وعزل التكرار لخدمة الدفعة الدراسية.
    🆕 يخزّن الآن تركيبة (subject_type, question_type, difficulty) مع كل كويز -
    راجع migration_quiz_options.sql - لدعم عرض التفاصيل والفلترة وسقف مستقل لكل تركيبة."""
    try:
        res = await supabase.table("quizzes").insert({
            "creator_id": creator_id,
            "file_hash": file_hash,
            "source_title": source_title,
            "quiz_data": quiz_data,
            "total_tokens": total_tokens,
            "is_math_quiz": is_math_quiz,
            "subject_type": subject_type,
            "question_type": question_type,
            "question_type_label": question_type_label,
            "difficulty": difficulty,
        }).execute()
        if res.data:
            return res.data[0]['id']
        return None
    except Exception as e:
        log_error(logger, f"Error saving central quiz data: {e}")
        return None

async def log_ai_generation(
    user_id: int, source_title: str, provider: Optional[str], model_name: Optional[str],
    duration_seconds: Optional[float], questions_count: int,
    input_tokens: int = 0, output_tokens: int = 0, total_tokens: int = 0,
    thoughts_tokens: int = 0,
) -> None:
    """🆕 يسجّل توليد ذكاء اصطناعي واحد بجدول ai_generation_log (كان فارغاً تماماً - غير
    موصول من أي مكان بالكود سابقاً). يُستدعى عبر asyncio.create_task من services/quiz_service.py
    مباشرة بعد generate_quiz_smart - لا يُوقف التوليد أبداً لو فشل هذا التسجيل (تحليلات
    ثانوية فقط، ليست بالمسار الحرج لتسليم الكويز للطالب).

    thoughts_tokens (🆕): توكنز "تفكير" موديلات reasoning (مثل gemini-3.6-flash) - مفصولة
    عن output_tokens لأنها غير ظاهرة بالرد النهائي، لكنها محاسَبة ضمن total_tokens من Google
    (راجع helpers/gemini_helper.py::_record_token_usage للتفصيل). 0 دائماً للموديلات التي
    لا تدعم التفكير.

    يتطلب migration_ai_generation_log_tokens.sql (يضيف input_tokens/output_tokens/total_tokens)
    ومigration_ai_generation_log_thoughts_tokens.sql (يضيف thoughts_tokens) - راجع الملفين
    المرفقين قبل أول استخدام."""
    if not provider or not model_name:
        # 🆕 لا موديل فائز فعلياً (فشل التوليد بالكامل، أو مسار لا يمرّ بعد بآلية تتبّع
        # الموديل - مثل Groq النصي السريع) - تسجيل صف بلا provider/model عديم الفائدة تحليلياً.
        return
    try:
        await supabase.table("ai_generation_log").insert({
            "user_id": user_id,
            "source_title": source_title,
            "provider": provider,
            "model_name": model_name,
            "duration_seconds": duration_seconds,
            "questions_count": questions_count,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "thoughts_tokens": thoughts_tokens,
            "total_tokens": total_tokens,
        }).execute()
    except Exception as e:
        log_error(logger, f"Error logging AI generation to ai_generation_log: {e}")


# 🆕 كاش محلي قصير (نفس فكرة settings_helper) - شاشة الأدمن فقط تستدعيها، بس بلا كاش
# رح نضرب الجدول بكل صفحة/تنقل صفحات بلوحة الأدمن بلا داعي.
_pricing_cache: Dict[tuple, Dict[str, Any]] = {}
_pricing_cache_timestamp: float = 0.0
_PRICING_CACHE_TTL_SECONDS = 60


async def get_ai_model_pricing(force_refresh: bool = False) -> Dict[tuple, Dict[str, Any]]:
    """🆕 يرجع أسعار كل الموديلات من جدول ai_model_pricing (migration_ai_model_pricing.sql)
    كقاموس {(provider, model_name): {"input": .., "output": .., "verified": bool}} - يُستخدم
    لحساب التكلفة التقديرية بالدولار لكل كويز بشاشة "📊 سجل توليد الكويزات" (handlers/admin/ai_control.py).
    موديل غير موجود بالجدول = سعره غير معروف (تُعرض "—" بدل رقم تكلفة، لا صفر مضلّل).
    كاش قصير (60 ثانية) لتخفيف الضغط على شاشة الأدمن فقط - ليست بمسار حرج."""
    global _pricing_cache, _pricing_cache_timestamp
    now = time.monotonic()
    if not force_refresh and _pricing_cache and (now - _pricing_cache_timestamp) < _PRICING_CACHE_TTL_SECONDS:
        return _pricing_cache
    try:
        res = await supabase.table("ai_model_pricing") \
            .select("provider, model_name, input_price_per_million, output_price_per_million, verified") \
            .execute()
        fresh = {
            (row["provider"], row["model_name"]): {
                "input": float(row["input_price_per_million"] or 0),
                "output": float(row["output_price_per_million"] or 0),
                "verified": bool(row.get("verified", False)),
            }
            for row in (res.data or [])
        }
        _pricing_cache = fresh
        _pricing_cache_timestamp = now
        return _pricing_cache
    except Exception as e:
        log_error(logger, f"Error fetching ai_model_pricing: {e}")
        return _pricing_cache or {}

async def get_cached_quiz(file_hash: str) -> Optional[Dict[str, Any]]:
    """توجيه ذكي وفولباك (Backward Compatibility) لمحاذاة كود ملف البوت القديم مع الجدول المركزي الجديد"""
    try:
        res = await supabase.table("quizzes").select("quiz_data, total_tokens").eq("file_hash", file_hash).order("score", desc=True).limit(1).execute()
        if res.data:
            log_info(logger, f"Cache HIT (Central Table redirection) for hash: {file_hash}")
            row = res.data[0]
            return {
                "questions_data": row["quiz_data"],
                "total_tokens": row["total_tokens"]
            }
        return None
    except Exception as e:
        log_error(logger, f"Error reading fallback cache content: {e}")
        return None

async def save_quiz_to_cache(file_hash: str, quiz_data: List[Dict[str, Any]], total_tokens: int) -> bool:
    """دالة فولباك للتخزين السريع في المسار المركزي الافتراضي"""
    try:
        # استخدام معرف الإدارة كمنشئ افتراضي في حال عدم تمريره من السيرفر القديم
        admin_id = int(os.getenv("ADMIN_ID", "0"))
        res = await save_file_quiz_multiple(file_hash, admin_id, "كويز مخزن تلقائياً", quiz_data, total_tokens)
        return res is not None
    except Exception as e:
        log_error(logger, f"Error routing fallback cache saving: {e}")
        return False

