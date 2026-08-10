# services/math_detector.py
"""
==============================================================================
MODULE: Math Content Detector (نمط الكويز المصوّر - Image Quiz Mode Trigger)
==============================================================================
الوصف:
موديول خفيف مهمته الوحيدة الإجابة على سؤال واحد بسرعة: "هل يحتوي هذا المحتوى
على معادلات/قوانين رياضية؟" - قبل أي توليد فعلي للأسئلة - لتقرير هل نُفعّل نمط
"الكويز المصوّر LaTeX" (صورة لكل سؤال + Poll بحروف الإجابة فقط) أم النمط العادي.

هذا الملف مختص حصراً بمنطق فحص الرياضيات. البنية التحتية المشتركة (عملاء
Gemini، دالة الاستدعاء، أخذ العيّنات) موجودة بـ services/detection_common.py
ومشتركة مع services/english_detector.py - راجعه لفهم آلية الفحص نفسها.

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
import os
from typing import List, Optional

from google.genai import types

from constants import SYSTEM_PROMPT_DETECT_MATH_TEXT, SYSTEM_PROMPT_DETECT_MATH_VISUAL
from logger import get_logger, log_warning
from services.detection_common import (
    IMAGE_EXTENSIONS,
    build_text_sample,
    classify_yes_no,
    first_page_png_bytes_sync,
)

logger = get_logger(__name__)


async def detect_math_in_text(pure_text: str) -> bool:
    """فحص عينة من نص مباشر (أو نص مُستخرج من مستند أوفيس) بحثاً عن محتوى رياضي."""
    if not pure_text or not pure_text.strip():
        return False
    sample = build_text_sample(pure_text)
    prompt = f"{SYSTEM_PROMPT_DETECT_MATH_TEXT}{sample}"
    return await classify_yes_no([prompt], caller="math_detector")


async def detect_math_in_file(file_path: str) -> bool:
    """فحص أول صفحة (PDF) أو الصورة نفسها (صورة مباشرة) بحثاً عن محتوى رياضي."""
    ext = os.path.splitext(file_path)[1].lower()

    if ext == ".pdf":
        image_bytes = await asyncio.to_thread(first_page_png_bytes_sync, file_path)
        if not image_bytes:
            return False
        mime_type = "image/png"
    elif ext in IMAGE_EXTENSIONS:
        try:
            with open(file_path, "rb") as f:
                image_bytes = f.read()
        except Exception as exc:
            log_warning(logger, f"Could not read image for math detection: {exc}")
            return False
        mime_type = IMAGE_EXTENSIONS[ext]
    else:
        # مستندات أوفيس (.docx/.pptx/.txt) تُفحص كنص بعد الاستخراج، وليس هنا مباشرة
        return False

    part = types.Part.from_bytes(data=image_bytes, mime_type=mime_type)
    return await classify_yes_no([SYSTEM_PROMPT_DETECT_MATH_VISUAL, part], caller="math_detector")


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
