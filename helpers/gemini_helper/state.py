"""
helpers/gemini_helper/state.py
حالة آخر توليد (ContextVars) + تسجيل التوكنز: تُقرأ من services/quiz_service.py ولوحة الأدمن.
"""

from typing import Any, Dict, Optional
import contextvars


# ==============================================================================
# 🆕 تتبّع بيانات آخر توليد ناجح (للوحة الأدمن: الوقت المستغرق + الموديل المستخدم)
# ==============================================================================
# AI-NOTE: ContextVar بدل تمرير القيمة عبر توقيع كل دالة بالسلسلة الطويلة (generate_quiz_smart
# → _generate_regular/_generate_super_pdf/... → _execute_cascade → _attempt) - كل طلب توليد
# يُعالَج ضمن asyncio Task مستقل (معالج تيليجرام لكل تحديث)، وContextVar مرتبط بالـ Task
# الحالي حصراً (نسخة معزولة تلقائياً)، فلا يوجد أي خطر تداخل بيانات بين طلبات متزامنة
# لطلاب مختلفين. تُقرأ فقط من generate_quiz_smart نفسها بعد انتهاء التوليد (نفس الـ Task).
_last_model_used_var: contextvars.ContextVar[Optional[Dict[str, Any]]] = contextvars.ContextVar(
    "_last_model_used_var", default=None
)
# 🆕 توكنز آخر محاولة ناجحة (input/output/total) - يُملأ من نفس الـ _attempt الذي نجح،
# فور قراءة usage_metadata من استجابة Gemini/Vertex الفعلية. منفصل عن _last_model_used_var
# لأن بعض المسارات (super_pdf/super_images) تحسب المجموع بعد asyncio.gather لعدة مهام
# فرعية، وتحتاج تُسجّله صراحة بنفسها (راجع الملاحظة أسفل _generate_super_pdf).
_last_token_usage_var: contextvars.ContextVar[Optional[Dict[str, int]]] = contextvars.ContextVar(
    "_last_token_usage_var", default=None
)
# البيانات النهائية (تشمل مدة التوليد الكاملة) - تُقرأ من خارج هذا الموديول عبر
# get_last_generation_metadata() بعد عودة generate_quiz_smart مباشرة وبنفس الـ Task.
_last_generation_metadata_var: contextvars.ContextVar[Optional[Dict[str, Any]]] = contextvars.ContextVar(
    "_last_generation_metadata_var", default=None
)
# 🆕 FIX: سبب تخطّي Groq الاستباقي (راجع الحاجز التقديري للتوكنز داخل _generate_text_quiz) -
# منفصل عن _last_model_used_var لأنه بيُضبط *قبل* ما نعرف مين رح يفوز أصلاً (Gemini/Vertex)،
# وازم يبقى مرتبط بنفس عملية التوليد النهائية حتى لو الفائز موديل تاني كلياً. None يعني Groq
# تُجرَّب فعلياً (نجح أو فشل بنداء حقيقي) أو المسار أصلاً ما بيمر على Groq إطلاقاً (is_math_mode).
_groq_skip_reason_var: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar(
    "_groq_skip_reason_var", default=None
)


def get_last_generation_metadata() -> Optional[Dict[str, Any]]:
    """🆕 يُستدعى من services/quiz_service.py فوراً بعد await generate_quiz_smart(...) بنفس
    الـ Task - يُرجع {"provider", "model", "duration_seconds", "input_tokens", "output_tokens",
    "total_tokens"} لآخر توليد ناجح ضمن هذا الـ Task، أو None إذا فشل التوليد بالكامل (لا
    موديل نجح) أو لم يُستدعَ أي توليد بعد.
    input_tokens/output_tokens قد تكون 0 لو تعذّر استخراجها من usage_metadata (نادر) - total_tokens
    يبقى الأدق دائماً لأنه مسحوب مباشرة من total_token_count بدل جمع input+output يدوياً.
    thoughts_tokens (🆕): توكنز "تفكير" موديلات reasoning (مثل gemini-3.6-flash) - مُحاسَبة
    ضمن total_tokens من Google لكن غير ظاهرة بـ output_tokens (راجع _record_token_usage
    لتفصيل السبب) - قد تكون 0 لموديلات لا تدعم التفكير أو لو لم تُفعَّل هذه الميزة.
    cascade_rank/cascade_total (🆕): ترتيب الموديل الفائز ضمن سلسلة الأولوية (ai_model_slots)
    *لحظة* نجاح هذا التوليد بالذات (مثلاً cascade_rank=2, cascade_total=6 يعني كان ثاني
    موديل بالسلسلة وقتها) - يبقى ثابتاً بالسجل حتى لو تغيّر ترتيب الكاسكيد لاحقاً من لوحة
    الأدمن. None لكلا الحقلين لو المسار الفائز لا ينتمي لسلسلة مرقّمة (Super PDF/Images
    يستخدم دائماً أقوى موديل بدون رقم ترتيب دقيق، أو Groq المستقل تماماً عن الكاسكيد)."""
    return _last_generation_metadata_var.get()


def _record_token_usage(response: Any) -> int:
    """🆕 يُستدعى مرة واحدة من كل _attempt فور نجاح الاستجابة - يقرأ usage_metadata
    (المتاحة بنفس الشكل على Gemini AI Studio وVertex AI كلاهما) ويسجّل input/output/thoughts/total
    بـ _last_token_usage_var (تُقرأ لاحقاً من _finalize بـ generate_quiz_smart)، ويُرجع
    total_token_count كما كان سابقاً (توافقاً مع كل استدعاءات هذه الدالة القديمة التي
    تتوقع رقماً واحداً فقط - لا تغيير على أي توقيع دالة موجود).

    input_tokens = prompt_token_count (النص/الملف/الصورة المُرسلة).
    output_tokens = candidates_token_count (الرد الفعلي فقط - يستثني thoughts_token_count).
    thoughts_tokens (🆕) = thoughts_token_count إن وُجد (موديلات التفكير مثل gemini-3.6-flash
    تحاسَب عليه كإخراج من Google لكنه منفصل عن الرد الظاهر للمستخدم) - 0 لو الموديل لا يدعم
    التفكير أو الحقل غير موجود بهذه النسخة من الـ SDK (getattr بقيمة افتراضية 0، لا ينهار).
    total_token_count يبقى المصدر الأدق للتكلفة الكلية دائماً - input/output/thoughts تفصيلية
    لتحليل نسبة التكلفة (الإخراج والتفكير أغلى تقريباً 5 أضعاف الإدخال)."""
    usage = getattr(response, "usage_metadata", None)
    input_tokens = int(getattr(usage, "prompt_token_count", 0) or 0)
    output_tokens = int(getattr(usage, "candidates_token_count", 0) or 0)
    thoughts_tokens = int(getattr(usage, "thoughts_token_count", 0) or 0)
    total_tokens = int(getattr(usage, "total_token_count", 0) or 0)
    _last_token_usage_var.set({
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "thoughts_tokens": thoughts_tokens,
        "total_tokens": total_tokens,
    })
    return total_tokens
