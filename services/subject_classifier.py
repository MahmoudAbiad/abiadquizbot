# services/subject_classifier.py
"""
==============================================================================
MODULE: Unified Subject Classifier (الفحص الموحّد لتصنيف المادة)
==============================================================================
الوصف:
يحل هذا الموديول محل الاستدعاءين المنفصلين اللذين كانا موجودين سابقاً
(services/math_detector.py و services/english_detector.py) باستدعاء AI واحد
فقط لكل عملية توليد، يُنفَّذ مبكراً (قبل شاشة اختيار نوع/عدد الأسئلة) ويُعاد
استخدام نتيجته لاحقاً وقت التوليد الفعلي بدل إعادة الفحص من جديد.

قبل هذا التوحيد: حتى استدعاءان منفصلان لكل عملية (فحص إنجليزي مبكراً + فحص
رياضيات متأخراً وقت التوليد)، كل وحدة برد نصي حر (yes/no) يُفسَّر يدوياً.

بعد التوحيد: استدعاء واحد فقط، مبكراً، يرجع تصنيفاً منظّماً (Structured Output
عبر response_schema بنفس نمط QuizResponse بـ helpers/gemini_helper.py):
- subject: "math" | "english" | "french" | "other"
- suggested_types: قائمة 2-4 اقتراحات AI لنوع الأسئلة (تُملأ فقط لو
  subject == "other"؛ لمواد الرياضيات والإنجليزي والفرنسي الأنواع ثابتة مسبقاً
  بـ constants.QUESTION_TYPE_OPTIONS ولا حاجة لاقتراح AI لها).

النتيجة تُخزَّن بحالة الـ FSM (state.update_data) من قبل الجهة المستدعية
(handlers/files.py) وتُعاد قراءتها لاحقاً بـ services/quiz_service.py بدل
إعادة تنفيذ أي فحص إضافي على نفس المحتوى.

القرارات الهندسية:
1. 🆕 فحص الملف/الوسائط كاملة، لا عيّنة صفحة واحدة: كان الفحص سابقاً يكتفي بأول
   صفحة (PDF) أو أول صورة من الألبوم فقط، ما قد يُخطئ التصنيف لو كانت خصائص
   المادة (رياضيات/إنجليزي) غير واضحة إلا بصفحات لاحقة (مثلاً: صفحة غلاف عامة
   ثم محتوى رياضي بباقي الصفحات). الآن يُرسل الملف بالكامل (كل صفحات الـ PDF -
   Gemini يقرأها أصلاً بشكل أصلي دون تحويلها لصور - أو كل صور الألبوم دفعة
   واحدة) لنفس الموديل الخفيف. نفس أسلوب Inline-vs-Files API المستخدم بمسار
   التوليد الفعلي (helpers/gemini_helper.py) يُطبَّق هنا أيضاً لضمان صلاحية
   الطلب حتى على الملفات الأكبر حجماً.
2. نموذج سريع وخفيف حصراً (MATH_DETECTION_MODEL بـ thinking_level="low") -
   بالتحديد لتعويض تكلفة إرسال محتوى أكبر (ملف كامل بدل صفحة واحدة) والحفاظ
   على سرعة استجابة لا يشعر بها الطالب، بدل موديل تفكير أبطأ وأدق.
3. فشل آمن (Fail-Safe): أي خطأ يُعتبر تلقائياً subject="other" بلا اقتراحات،
   ويكمل التدفق بمسار عام بدل تعطيل الاستقبال بأكمله.
==============================================================================
"""

import asyncio
import os
from typing import Any, List, Literal, Optional

from google import genai
from google.genai import types
from pydantic import BaseModel, Field

from constants import (
    MATH_DETECTION_TIMEOUT,
    MAX_QUESTIONS_TO_GENERATE,
    MIN_QUESTIONS_TO_GENERATE,
    SUBJECT_ENGLISH,
    SUBJECT_FRENCH,
    SUBJECT_MATH,
    SUBJECT_OTHER,
    SUBJECT_QUIZ_SOLVED,
    SUBJECT_QUIZ_UNSOLVED,
    SYSTEM_PROMPT_CLASSIFY_SUBJECT,
)
from logger import get_logger, log_warning
from services.detection_common import IMAGE_EXTENSIONS, build_text_sample
from ai_models_helper import get_detection_model

logger = get_logger(__name__)

API_KEYS = [key.strip() for key in os.getenv("GEMINI_API_KEYS", "").split(",") if key.strip()]
# AI-NOTE: عملاء ثابتون لمرة واحدة (نفس منطق detection_common.py) لتفادي تسريب
# الذاكرة الناتج عن إنشاء genai.Client() جديد بكل استدعاء على المسار الساخن.
_GEMINI_CLIENTS: List[genai.Client] = [genai.Client(api_key=key) for key in API_KEYS]

# 🆕 SUBJECT_FRENCH أُضيف بنفس معاملة SUBJECT_ENGLISH بالضبط (راجع constants.py).
_VALID_SUBJECTS = {
    SUBJECT_MATH, SUBJECT_ENGLISH, SUBJECT_FRENCH, SUBJECT_QUIZ_SOLVED, SUBJECT_QUIZ_UNSOLVED, SUBJECT_OTHER,
}

# 🆕 نفس حد helpers.gemini_helper.INLINE_DATA_SIZE_THRESHOLD (15MB): إرسال الملف كاملاً
# Inline ضمن الطلب لو كان بهذا الحجم أو أقل (أسرع، بدون Round-trip رفع منفصل)، وإلا
# نلجأ لـ Files API (نفس المسار المستخدم بالتوليد الفعلي للملفات الكبيرة/الألبومات).
DETECTION_INLINE_SIZE_THRESHOLD = 15 * 1024 * 1024


class SubjectClassification(BaseModel):
    # 🩹 BUGFIX: كانت description تقول "one of: math, english, other" فقط - نسيت
    # تحديثها لما أُضيف SUBJECT_FRENCH، رغم إن SYSTEM_PROMPT_CLASSIFY_SUBJECT وكل
    # الطبقات الأخرى كانت محدّثة بالفعل. بما إن هالـ description بتتحول تلقائياً
    # لجزء من الـ JSON Schema المُرسل فعلياً لـ Gemini (response_schema=...)، كانت
    # بتناقض الـ system prompt وتمنع النموذج عملياً من إرجاع "french" أبداً.
    # استخدام Literal بدل str+description بيضمن القيمة من مستوى الـ schema نفسه
    # (تعليمة صريحة وملزمة للنموذج) بدل الاعتماد فقط على نص وصفي حر قد يُنسى تحديثه
    # بالمستقبل لو أُضيفت مادة جديدة.
    # 🆕 quiz_solved/quiz_unsolved أُضيفا بنفس منطق SUBJECT_FRENCH أعلاه (راجع البُقشة
    # التوضيحية أعلى BUGFIX 🩹 بهذا الملف): Literal صريح يضمن القيمة من مستوى الـ
    # schema نفسه المُرسل فعلياً لـ Gemini، لا يكفي تحديث SYSTEM_PROMPT_CLASSIFY_SUBJECT وحده.
    subject: Literal["math", "english", "french", "quiz_solved", "quiz_unsolved", "other"] = Field(
        description="one of: math, english, french, quiz_solved, quiz_unsolved, other"
    )
    suggested_types: List[str] = Field(default_factory=list, max_length=4)
    # 🆕 تقدير آلي لعدد الأسئلة الفعلي الموجود بالمستند - يُملأ فقط عندما subject يساوي
    # quiz_solved أو quiz_unsolved (اختبار جاهز الصياغة أصلاً)، ويُستخدم لاحقاً لعرض شاشة
    # تأكيد عدد/تكلفة جاهزة بدل تخيير الطالب بأزرار عدد عشوائية (5/10/15/20) لا علاقة لها
    # بالعدد الحقيقي بمستند اختبار جاهز أصلاً. راجع handlers/files.py._show_question_count_screen.
    question_count: Optional[int] = Field(
        default=None,
        description=(
            "approximate count of actual questions found in the document; only set when "
            "subject is quiz_solved or quiz_unsolved, otherwise null"
        ),
    )
    # 🆕 المادة العلمية الفعلية للمحتوى، بمعزل عن كونه اختباراً جاهزاً أم لا - تُملأ فقط
    # عندما subject يساوي quiz_solved أو quiz_unsolved (عندها subject نفسه يصف "شكل"
    # المحتوى فقط لا مادته العلمية - راجع SYSTEM_PROMPT_CLASSIFY_SUBJECT). تُستخدم لاحقاً
    # (services/quiz_service.py) لتفعيل نمط الكويز المصوّر LaTeX أو موجّه اللغة الصحيح حتى
    # للاختبارات الجاهزة، بدل معاملتها جميعاً كمادة "عامة" دائماً بغض النظر عن مضمونها
    # الفعلي. null لأي subject آخر (math/english/french/other تحمل مادتها أصلاً بقيمة subject نفسها).
    content_subject: Optional[Literal["math", "english", "french", "other"]] = Field(
        default=None,
        description=(
            "the actual academic subject of the content (math/english/french/other), filled "
            "only when subject is quiz_solved or quiz_unsolved, otherwise null"
        ),
    )


def _fallback() -> SubjectClassification:
    """التصنيف الافتراضي الآمن عند أي خطأ أو انقطاع - يعامل المحتوى كمادة عامة بلا تخصيص."""
    return SubjectClassification(subject=SUBJECT_OTHER, suggested_types=[])


def _normalize(result: Optional[SubjectClassification]) -> SubjectClassification:
    """يتحقق من صحة قيمة subject الراجعة من النموذج ويعيد للقيمة الآمنة لو كانت غير متوقعة."""
    if result is None:
        return _fallback()
    subject = (result.subject or "").strip().lower()
    if subject not in _VALID_SUBJECTS:
        log_warning(logger, f"[subject_classifier] Unexpected subject value '{subject}', defaulting to 'other'")
        subject = SUBJECT_OTHER
    suggested = [s.strip() for s in (result.suggested_types or []) if s and s.strip()][:4]
    if subject != SUBJECT_OTHER:
        suggested = []

    # 🆕 question_count منطقي فقط لاختبار جاهز (محلول/غير محلول) - لأي مادة أخرى يبقى null
    # حتى لو أرجع النموذج قيمة عن طريق الخطأ. القيمة المُرجَعة تُقيَّد ضمن نفس مجال العدد
    # المسموح للتوليد (VALID_QUESTIONS_RANGE) لتفادي رقم شاذ (صفر/سالب/ضخم جداً) يصل لاحقاً
    # لحساب التكلفة أو موجّه التوليد بدون أي تحقق آخر بمنتصف الطريق.
    question_count: Optional[int] = None
    if subject in (SUBJECT_QUIZ_SOLVED, SUBJECT_QUIZ_UNSOLVED):
        try:
            raw_count = int(result.question_count) if result.question_count else None
        except (TypeError, ValueError):
            raw_count = None
        if raw_count is not None:
            question_count = max(MIN_QUESTIONS_TO_GENERATE, min(raw_count, MAX_QUESTIONS_TO_GENERATE))

    # 🆕 content_subject منطقي فقط لاختبار جاهز (محلول/غير محلول) - لأي subject آخر يبقى
    # null دائماً (مادته العلمية محمولة أصلاً بقيمة subject نفسها). قيمة غير متوقعة راجعة
    # من النموذج تُعامَل كـ "other" أماناً (نفس منطق subject أعلاه) بدل تعطيل الفحص بالكامل.
    content_subject: Optional[str] = None
    if subject in (SUBJECT_QUIZ_SOLVED, SUBJECT_QUIZ_UNSOLVED):
        raw_content_subject = (result.content_subject or "").strip().lower() if result.content_subject else ""
        if raw_content_subject in (SUBJECT_MATH, SUBJECT_ENGLISH, SUBJECT_FRENCH, SUBJECT_OTHER):
            content_subject = raw_content_subject
        else:
            content_subject = SUBJECT_OTHER

    return SubjectClassification(
        subject=subject, suggested_types=suggested, question_count=question_count, content_subject=content_subject,
    )


def _read_bytes_sync(path: str) -> bytes:
    with open(path, "rb") as f:
        return f.read()


async def _safe_delete_gemini_file(client: genai.Client, file_name: str) -> None:
    """حذف أي ملف رُفع مؤقتاً عبر Files API بعد انتهاء الفحص (نفس منطق gemini_helper.py)."""
    try:
        await asyncio.to_thread(client.files.delete, name=file_name)
    except Exception as exc:
        log_warning(logger, f"[subject_classifier] Could not delete temporary upload {file_name}: {exc}")


async def _classify(contents: list) -> SubjectClassification:
    """الاستدعاء الفعلي الموحّد - نفس بنية _attempt بـ gemini_helper.py لكن بموديل خفيف ومخرجات منظّمة."""
    if not API_KEYS:
        return _fallback()
    # 🆕 اسم الموديل صار ديناميكياً من لوحة تحكم الأدمن (slot="detection") - نفس الموديل
    # المستخدم بفحص الرياضيات السريع بـ services/detection_common.py (سلسلة موحّدة واحدة).
    detection_model = (await get_detection_model())["model_name"]
    for client in _GEMINI_CLIENTS:
        try:
            response = await asyncio.wait_for(
                client.aio.models.generate_content(
                    model=detection_model,
                    contents=contents,
                    config=types.GenerateContentConfig(
                        thinking_config=types.ThinkingConfig(thinking_level="low"),
                        response_mime_type="application/json",
                        response_schema=SubjectClassification,
                    ),
                ),
                timeout=MATH_DETECTION_TIMEOUT,
            )
            if response.parsed:
                return _normalize(response.parsed)
            log_warning(logger, "[subject_classifier] Classification call returned no parsed result")
        except Exception as exc:
            log_warning(logger, f"[subject_classifier] Classification call failed with one key, trying next key if available: {exc}")
            continue
    return _fallback()


async def _classify_text(pure_text: str) -> SubjectClassification:
    if not pure_text or not pure_text.strip():
        return _fallback()
    sample = build_text_sample(pure_text)
    prompt = f"{SYSTEM_PROMPT_CLASSIFY_SUBJECT}\n\n[عينة النص]:\n{sample}"
    return await _classify([prompt])


async def _classify_media(file_paths: List[str]) -> SubjectClassification:
    """
    🆕 يفحص الملف/الوسائط بالكامل (بدل صفحة أولى فقط): كل صفحات ملف PDF واحد، أو كل
    صور الألبوم دفعة واحدة. Gemini يقرأ ملفات PDF أصلياً بكل صفحاتها دون أي تحويل
    مسبق لصور (بعكس أسلوب "أول صفحة → صورة PNG" المستخدم سابقاً).
    """
    if not file_paths or not API_KEYS:
        return _fallback()

    # 🆕 اسم الموديل صار ديناميكياً من لوحة تحكم الأدمن (slot="detection").
    detection_model = (await get_detection_model())["model_name"]

    mime_types: List[str] = []
    total_size = 0
    for path in file_paths:
        ext = os.path.splitext(path)[1].lower()
        if ext == ".pdf":
            mime_types.append("application/pdf")
        elif ext in IMAGE_EXTENSIONS:
            mime_types.append(IMAGE_EXTENSIONS[ext])
        else:
            # مستندات أوفيس (.docx/.pptx/.txt) تُفحص كنص بعد الاستخراج، وليس هنا مباشرة
            return _fallback()
        try:
            total_size += os.path.getsize(path)
        except OSError:
            total_size = DETECTION_INLINE_SIZE_THRESHOLD + 1

    use_inline = total_size <= DETECTION_INLINE_SIZE_THRESHOLD
    inline_parts: Optional[List[types.Part]] = None
    if use_inline:
        try:
            inline_parts = []
            for path, mime_type in zip(file_paths, mime_types):
                file_bytes = await asyncio.to_thread(_read_bytes_sync, path)
                inline_parts.append(types.Part.from_bytes(data=file_bytes, mime_type=mime_type))
        except Exception as exc:
            log_warning(logger, f"[subject_classifier] Could not read file(s) for classification: {exc}")
            return _fallback()

    for client in _GEMINI_CLIENTS:
        uploaded: List[Any] = []
        try:
            if use_inline:
                parts = inline_parts
            else:
                # ملف/ألبوم أكبر من الحد الآمن للإرسال المباشر - يُرفع مؤقتاً عبر Files API
                # (نفس مسار helpers.gemini_helper._generate_with_key للملفات الكبيرة).
                parts = []
                for path in file_paths:
                    uploaded_file = await asyncio.to_thread(client.files.upload, file=path)
                    uploaded.append(uploaded_file)
                    parts.append(uploaded_file)

            response = await asyncio.wait_for(
                client.aio.models.generate_content(
                    model=detection_model,
                    contents=[SYSTEM_PROMPT_CLASSIFY_SUBJECT, *parts],
                    config=types.GenerateContentConfig(
                        thinking_config=types.ThinkingConfig(thinking_level="low"),
                        response_mime_type="application/json",
                        response_schema=SubjectClassification,
                    ),
                ),
                timeout=MATH_DETECTION_TIMEOUT,
            )
            if response.parsed:
                return _normalize(response.parsed)
            log_warning(logger, "[subject_classifier] Classification call returned no parsed result")
        except Exception as exc:
            log_warning(logger, f"[subject_classifier] Classification call failed with one key, trying next key if available: {exc}")
        finally:
            for uploaded_file in uploaded:
                asyncio.create_task(_safe_delete_gemini_file(client, uploaded_file.name))
    return _fallback()


async def classify_subject(
    file_paths: Optional[List[str]], pure_text: Optional[str]
) -> SubjectClassification:
    """
    نقطة الدخول الموحّدة: تستدعى مرة واحدة فقط مبكراً من handlers/files.py (بنفس توقيت
    فحص الإنجليزي القديم، قبل عرض شاشة اختيار نوع/عدد الأسئلة). النتيجة تُخزَّن بحالة
    الـ FSM وتُعاد قراءتها لاحقاً بـ services/quiz_service.py وقت التوليد الفعلي بدل
    إعادة تنفيذ أي فحص إضافي (math_detector/english_detector القديمين لم يعودا يُستدعيان
    على المسار الساخن). أي خطأ غير متوقع يُعتبر تلقائياً subject="other" (فشل آمن).
    """
    try:
        if pure_text:
            return await _classify_text(pure_text)
        if file_paths:
            return await _classify_media(file_paths)
    except Exception as exc:
        log_warning(logger, f"[subject_classifier] Unexpected error during classification, defaulting to 'other': {exc}")
    return _fallback()
