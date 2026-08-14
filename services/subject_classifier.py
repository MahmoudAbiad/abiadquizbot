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
- subject: "math" | "english" | "other"
- suggested_types: قائمة 2-4 اقتراحات AI لنوع الأسئلة (تُملأ فقط لو
  subject == "other"؛ لمادتي الرياضيات والإنجليزي الأنواع ثابتة مسبقاً
  بـ constants.QUESTION_TYPE_OPTIONS ولا حاجة لاقتراح AI لهما).

النتيجة تُخزَّن بحالة الـ FSM (state.update_data) من قبل الجهة المستدعية
(handlers/files.py) وتُعاد قراءتها لاحقاً بـ services/quiz_service.py بدل
إعادة تنفيذ أي فحص إضافي على نفس المحتوى.

القرارات الهندسية (نفس فلسفة math_detector.py/english_detector.py السابقين):
1. عيّنة فقط، لا فحص كامل: أول صفحة/صورة للملفات، أو عيّنة نصية محدودة الطول.
2. نموذج سريع وخفيف حصراً (نفس MATH_DETECTION_MODEL) لتفادي أي تأخير محسوس.
3. فشل آمن (Fail-Safe): أي خطأ يُعتبر تلقائياً subject="other" بلا اقتراحات،
   ويكمل التدفق بمسار عام بدل تعطيل الاستقبال بأكمله.
==============================================================================
"""

import asyncio
import os
from typing import List, Optional

from google import genai
from google.genai import types
from pydantic import BaseModel, Field

from constants import (
    MATH_DETECTION_MODEL,
    MATH_DETECTION_TIMEOUT,
    SUBJECT_ENGLISH,
    SUBJECT_MATH,
    SUBJECT_OTHER,
    SYSTEM_PROMPT_CLASSIFY_SUBJECT,
)
from logger import get_logger, log_warning
from services.detection_common import (
    IMAGE_EXTENSIONS,
    build_text_sample,
    first_page_png_bytes_sync,
)

logger = get_logger(__name__)

API_KEYS = [key.strip() for key in os.getenv("GEMINI_API_KEYS", "").split(",") if key.strip()]
# AI-NOTE: عملاء ثابتون لمرة واحدة (نفس منطق detection_common.py) لتفادي تسريب
# الذاكرة الناتج عن إنشاء genai.Client() جديد بكل استدعاء على المسار الساخن.
_GEMINI_CLIENTS: List[genai.Client] = [genai.Client(api_key=key) for key in API_KEYS]

_VALID_SUBJECTS = {SUBJECT_MATH, SUBJECT_ENGLISH, SUBJECT_OTHER}


class SubjectClassification(BaseModel):
    subject: str = Field(description="one of: math, english, other")
    suggested_types: List[str] = Field(default_factory=list, max_length=4)


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
    return SubjectClassification(subject=subject, suggested_types=suggested)


async def _classify(contents: list) -> SubjectClassification:
    """الاستدعاء الفعلي الموحّد - نفس بنية _attempt بـ gemini_helper.py لكن بموديل خفيف ومخرجات منظّمة."""
    if not API_KEYS:
        return _fallback()
    for client in _GEMINI_CLIENTS:
        try:
            response = await asyncio.wait_for(
                client.aio.models.generate_content(
                    model=MATH_DETECTION_MODEL,
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


async def _classify_file(file_path: str) -> SubjectClassification:
    ext = os.path.splitext(file_path)[1].lower()

    if ext == ".pdf":
        image_bytes = await asyncio.to_thread(first_page_png_bytes_sync, file_path)
        if not image_bytes:
            return _fallback()
        mime_type = "image/png"
    elif ext in IMAGE_EXTENSIONS:
        try:
            with open(file_path, "rb") as f:
                image_bytes = f.read()
        except Exception as exc:
            log_warning(logger, f"[subject_classifier] Could not read image for classification: {exc}")
            return _fallback()
        mime_type = IMAGE_EXTENSIONS[ext]
    else:
        # مستندات أوفيس (.docx/.pptx/.txt) تُفحص كنص بعد الاستخراج، وليس هنا مباشرة
        return _fallback()

    part = types.Part.from_bytes(data=image_bytes, mime_type=mime_type)
    return await _classify([SYSTEM_PROMPT_CLASSIFY_SUBJECT, part])


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
            return await _classify_file(file_paths[0])
    except Exception as exc:
        log_warning(logger, f"[subject_classifier] Unexpected error during classification, defaulting to 'other': {exc}")
    return _fallback()
