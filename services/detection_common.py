# services/detection_common.py
"""Shared content sampling and provider-independent yes/no detection."""

import asyncio
import gc
from typing import List, Optional

import fitz

from constants import MATH_DETECTION_TEXT_SAMPLE_CHARS, MATH_DETECTION_TIMEOUT
from logger import get_logger, log_warning

logger = get_logger(__name__)

# ملفات الصور المدعومة للفحص المباشر (بدون تحويل)
IMAGE_EXTENSIONS = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".webp": "image/webp"}


def build_text_sample(text: str) -> str:
    """
    يأخذ عينة موزّعة (بداية + وسط + نهاية) بدل الاكتفاء ببداية النص فقط، لأن
    المحتوى محل الفحص (معادلة أو فقرة إنجليزية) قد يتركز في أي جزء من المستند
    وليس بالضرورة في أول سطر.
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


def first_page_png_bytes_sync(file_path: str) -> Optional[bytes]:
    """يحوّل أول صفحة فقط من ملف PDF إلى صورة PNG لفحصها - بدقة كافية للفحص السريع فقط."""
    try:
        doc = fitz.open(file_path)
        try:
            if doc.page_count < 1:
                return None
            page = doc.load_page(0)
            from services.upload_validation import MAX_IMAGE_PIXELS, MAX_IMAGE_DIMENSION
            width, height = page.rect.width * 120 / 72, page.rect.height * 120 / 72
            if max(width, height) > MAX_IMAGE_DIMENSION or width * height > MAX_IMAGE_PIXELS:
                raise ValueError("PDF page raster dimensions exceed the memory budget")
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
        log_warning(logger, f"Could not rasterize first PDF page for content detection: {exc}")
        return None


async def classify_yes_no(contents: list, *, caller: str = "detection") -> bool:
    from helpers.gemini_helper.generic import _normalize_contents
    from services.ai.contracts import AIRequest
    from services.ai.runtime import execute
    result = await execute(AIRequest(_normalize_contents(contents)), slot="detection",
                           mode="detection", timeout=MATH_DETECTION_TIMEOUT)
    return bool(result and result.text.strip().lower().startswith(("yes", "نعم")))
