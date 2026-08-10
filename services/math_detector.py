# services/math_detector.py
"""
==============================================================================
MODULE: Math Content Detector (نمط الكويز المصوّر - Image Quiz Mode Trigger)
==============================================================================
الوصف:
موديول خفيف مهمته الوحيدة الإجابة على سؤال واحد بسرعة: "هل يحتوي هذا المحتوى
على معادلات/قوانين رياضية؟" - قبل أي توليد فعلي للأسئلة - لتقرير هل نُفعّل نمط
"الكويز المصوّر LaTeX" (صورة لكل سؤال + Poll بحروف الإجابة فقط) أم النمط العادي.

القرارات الهندسية:
1. عيّنة فقط، لا فحص كامل: أول صفحة/صورة للملفات، وعيّنة نصية محدودة الطول
   للنصوص - لتفادي أي تأخير أو كلفة محسوسة على تجربة الطالب قبل بدء التوليد.
2. نموذج سريع وخفيف حصراً (Gemini Flash-Lite) بحد أقصى قليل من التوكنات
   للاستجابة (yes/no فقط) - هذا الاستدعاء منفصل تماماً عن استدعاء التوليد
   الفعلي في gemini_helper.py ولا يستهلك من نفس ميزانية إعادة المحاولة.
3. فشل آمن (Fail-Safe): أي خطأ أو انقطاع في الفحص السريع يُعتبر تلقائياً "لا
   يوجد محتوى رياضي" ويكمل التوليد بالنمط العادي المعتاد، بدل تعطيل الطلب.
==============================================================================
"""

import asyncio
import gc
import os
from typing import List, Optional

import fitz
from google import genai
from google.genai import types

from constants import (
    MATH_DETECTION_MODEL,
    MATH_DETECTION_TEXT_SAMPLE_CHARS,
    MATH_DETECTION_TIMEOUT,
    SYSTEM_PROMPT_DETECT_MATH_TEXT,
    SYSTEM_PROMPT_DETECT_MATH_VISUAL,
    SYSTEM_PROMPT_DETECT_ENGLISH_TEXT,
    SYSTEM_PROMPT_DETECT_ENGLISH_VISUAL,
)
from logger import get_logger, log_warning

logger = get_logger(__name__)

API_KEYS = [key.strip() for key in os.getenv("GEMINI_API_KEYS", "").split(",") if key.strip()]

# AI-NOTE (memory-leak fix): هاد الفحص بيتنفّذ قبل كل عملية توليد كويز (على كل الملفات
# والنصوص)، فكان إنشاء genai.Client() جديد بكل key بكل استدعاء (بدون إغلاقه) من أكبر
# مصادر تسريب الذاكرة الفعلية لأنه على المسار الساخن. نفس الحل: عملاء ثابتين لمرة وحدة.
_GEMINI_CLIENTS: List[genai.Client] = [genai.Client(api_key=key) for key in API_KEYS]

# ملفات الصور المدعومة للفحص المباشر (بدون تحويل)
_IMAGE_EXTENSIONS = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".webp": "image/webp"}


def _build_text_sample(text: str) -> str:
    """
    يأخذ عينة موزّعة (بداية + وسط + نهاية) بدل الاكتفاء ببداية النص فقط، لأن
    المعادلات قد تتركز في أي جزء من المستند وليس بالضرورة في أول سطر.
    """
    text = text or ""
    if len(text) <= MATH_DETECTION_TEXT_SAMPLE_CHARS:
        return text
    third = MATH_DETECTION_TEXT_SAMPLE_CHARS // 3
    start = text[:third]
    mid_point = len(text) // 2
    middle = text[max(0, mid_point - third // 2): mid_point + third // 2]
    end = text[-third:]
    return f"{start}\n...\n{middle}\n...\n{end}"


async def _classify(contents: list) -> bool:
    """يرسل المحتوى (نص أو صورة) لنموذج الفحص السريع ويعيد True فقط لو أجاب بوضوح yes/نعم."""
    if not API_KEYS:
        return False
    for client in _GEMINI_CLIENTS:
        try:
            response = await asyncio.wait_for(
                client.aio.models.generate_content(
                    model=MATH_DETECTION_MODEL,
                    contents=contents,
                    config=types.GenerateContentConfig(
                        max_output_tokens=5,
                        thinking_config=types.ThinkingConfig(thinking_level="low"),
                    ),
                ),
                timeout=MATH_DETECTION_TIMEOUT,
            )
            answer = (getattr(response, "text", None) or "").strip().lower()
            return answer.startswith("yes") or answer.startswith("نعم")
        except Exception as exc:
            log_warning(logger, f"Math detection call failed with one key, trying next key if available: {exc}")
            continue
    return False


def _first_page_png_bytes_sync(file_path: str) -> Optional[bytes]:
    """يحوّل أول صفحة فقط من ملف PDF إلى صورة PNG لفحصها - بدقة كافية للفحص السريع فقط."""
    try:
        doc = fitz.open(file_path)
        try:
            if doc.page_count < 1:
                return None
            page = doc.load_page(0)
            pixmap = page.get_pixmap(dpi=120)
            return pixmap.tobytes("png")
        finally:
            doc.close()
            # AI-NOTE (memory-leak fix): PyMuPDF بيحتفظ بمراجع دورية (cyclic refs) داخلية
            # لبعض الكائنات (Document/Page/Pixmap) ما بينضفّها الـ refcounting العادي فوراً
            # حتى بعد doc.close(). بما إن هاد المسار بينفّذ على كل ملف PDF يوصل للبوت،
            # gc.collect() فوري بعد الإغلاق بيمنع تراكم هاد الكائنات بالذاكرة مع الوقت.
            gc.collect()
    except Exception as exc:
        log_warning(logger, f"Could not rasterize first PDF page for math detection: {exc}")
        return None


async def detect_math_in_text(pure_text: str) -> bool:
    """فحص عينة من نص مباشر (أو نص مُستخرج من مستند أوفيس) بحثاً عن محتوى رياضي."""
    if not pure_text or not pure_text.strip():
        return False
    sample = _build_text_sample(pure_text)
    prompt = f"{SYSTEM_PROMPT_DETECT_MATH_TEXT}{sample}"
    return await _classify([prompt])


async def detect_math_in_file(file_path: str) -> bool:
    """فحص أول صفحة (PDF) أو الصورة نفسها (صورة مباشرة) بحثاً عن محتوى رياضي."""
    ext = os.path.splitext(file_path)[1].lower()

    if ext == ".pdf":
        image_bytes = await asyncio.to_thread(_first_page_png_bytes_sync, file_path)
        if not image_bytes:
            return False
        mime_type = "image/png"
    elif ext in _IMAGE_EXTENSIONS:
        try:
            with open(file_path, "rb") as f:
                image_bytes = f.read()
        except Exception as exc:
            log_warning(logger, f"Could not read image for math detection: {exc}")
            return False
        mime_type = _IMAGE_EXTENSIONS[ext]
    else:
        # مستندات أوفيس (.docx/.pptx/.txt) تُفحص كنص بعد الاستخراج، وليس هنا مباشرة
        return False

    part = types.Part.from_bytes(data=image_bytes, mime_type=mime_type)
    return await _classify([SYSTEM_PROMPT_DETECT_MATH_VISUAL, part])


async def detect_math_content(file_paths: Optional[List[str]], pure_text: Optional[str]) -> bool:
    """
    نقطة الدخول الموحّدة التي تستدعيها services/quiz_service.py قبل التوليد:
    تفحص فقط أول عنصر (أول صفحة/صورة، أو عينة نصية) لتقرير تفعيل نمط الكويز
    المصوّر LaTeX. أي خطأ غير متوقع يُعتبر تلقائياً "لا يوجد محتوى رياضي"
    (فشل آمن) بدل تعطيل تدفق توليد الكويز بأكمله.
    """
    try:
        if pure_text:
            return await detect_math_in_text(pure_text)
        if file_paths:
            return await detect_math_in_file(file_paths[0])
    except Exception as exc:
        log_warning(logger, f"Unexpected error during math content detection, defaulting to standard mode: {exc}")
    return False


# 🆕 ==================== English Subject Detection (اقتراح ترجمة الأسئلة) ====================
# نفس آلية/عيّنة فحص الرياضيات أعلاه بالضبط (نموذج خفيف، عينة واحدة فقط)، لكن بغرض مختلف:
# هذا الفحص يُستدعى من handlers/files.py مباشرة بعد استقبال المحتوى (قبل شاشة عدد الأسئلة)
# لأن قراره - عرض خيار "مترجمة/بدون ترجمة" - يتطلب تفاعل الطالب عبر كيبورد، على عكس فحص
# الرياضيات الذي يبقى صامتاً ويُنفّذ لاحقاً أثناء التوليد الفعلي.
# IMPORTANT: هذا فحص أضيق من "هل النص مكتوب بالإنجليزي؟" - يتحقق تحديداً هل المادة نفسها
# هي مادة تعليم اللغة الإنجليزية (قواعد/مفردات/Reading...)، وليس أي مادة أخرى (فيزياء، تاريخ،
# إلخ) تصادف أنها مكتوبة بالإنجليزي كلغة تدريس فقط - راجع الموجّهات بـ constants.py للتفاصيل.

async def detect_english_in_text(pure_text: str) -> bool:
    """فحص عينة من نص مباشر (أو نص مُستخرج من مستند أوفيس) بحثاً عن مادة تعليم لغة إنجليزية."""
    if not pure_text or not pure_text.strip():
        return False
    sample = _build_text_sample(pure_text)
    prompt = f"{SYSTEM_PROMPT_DETECT_ENGLISH_TEXT}{sample}"
    return await _classify([prompt])


async def detect_english_in_file(file_path: str) -> bool:
    """فحص أول صفحة (PDF) أو الصورة نفسها (صورة مباشرة) بحثاً عن مادة تعليم لغة إنجليزية."""
    ext = os.path.splitext(file_path)[1].lower()

    if ext == ".pdf":
        image_bytes = await asyncio.to_thread(_first_page_png_bytes_sync, file_path)
        if not image_bytes:
            return False
        mime_type = "image/png"
    elif ext in _IMAGE_EXTENSIONS:
        try:
            with open(file_path, "rb") as f:
                image_bytes = f.read()
        except Exception as exc:
            log_warning(logger, f"Could not read image for English detection: {exc}")
            return False
        mime_type = _IMAGE_EXTENSIONS[ext]
    else:
        # مستندات أوفيس (.docx/.pptx/.txt) تُفحص كنص بعد الاستخراج، وليس هنا مباشرة
        return False

    part = types.Part.from_bytes(data=image_bytes, mime_type=mime_type)
    return await _classify([SYSTEM_PROMPT_DETECT_ENGLISH_VISUAL, part])


async def detect_english_content(file_paths: Optional[List[str]], pure_text: Optional[str]) -> bool:
    """
    نقطة الدخول الموحّدة التي تستدعيها handlers/files.py فور استقبال المحتوى (ملف/صورة/نص)
    وقبل عرض شاشة عدد الأسئلة: تفحص فقط أول عنصر (أول صفحة/صورة، أو عينة نصية) لتقرير هل
    المادة نفسها هي مادة تعليم لغة إنجليزية (وليس أي مادة أخرى مكتوبة بالإنجليزي فقط)، لعرض
    خيار "أسئلة مترجمة أم إنجليزية فقط" على الطالب. أي خطأ غير متوقع يُعتبر تلقائياً "ليست
    مادة لغة إنجليزية" (فشل آمن) بدل تعطيل تدفق الاستقبال بأكمله.
    """
    try:
        if pure_text:
            return await detect_english_in_text(pure_text)
        if file_paths:
            return await detect_english_in_file(file_paths[0])
    except Exception as exc:
        log_warning(logger, f"Unexpected error during English content detection, defaulting to standard mode: {exc}")
    return False
