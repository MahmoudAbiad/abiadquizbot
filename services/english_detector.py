# services/english_detector.py
"""
==============================================================================
MODULE: English-Subject Content Detector (اقتراح ترجمة الأسئلة)
==============================================================================
الوصف:
موديول خفيف مهمته الوحيدة الإجابة على سؤال واحد بسرعة: "هل هذا المحتوى تحديداً
مادة تعليم اللغة الإنجليزية نفسها (قواعد/مفردات/Reading/Writing)؟" - قبل عرض
شاشة عدد الأسئلة - لتقرير هل نعرض على الطالب خيار "أسئلة مترجمة أم إنجليزية
فقط بدون ترجمة" أولاً.

هذا الملف مختص حصراً بمنطق فحص مادة اللغة الإنجليزية. البنية التحتية المشتركة
(عملاء Gemini، دالة الاستدعاء، أخذ العيّنات) موجودة بـ services/detection_common.py
ومشتركة مع services/math_detector.py - راجعه لفهم آلية الفحص نفسها.

IMPORTANT: هذا فحص أضيق من "هل النص مكتوب بالإنجليزي؟" - يتحقق تحديداً هل المادة
نفسها هي مادة تعليم اللغة الإنجليزية، وليس أي مادة أخرى (فيزياء، تاريخ، إلخ)
تصادف أنها مكتوبة بالإنجليزي كلغة تدريس فقط - راجع الموجّهات بـ constants.py
(SYSTEM_PROMPT_DETECT_ENGLISH_VISUAL / SYSTEM_PROMPT_DETECT_ENGLISH_TEXT) للتفاصيل.

القرارات الهندسية:
1. عيّنة فقط، لا فحص كامل: أول صفحة/صورة للملفات، وعيّنة نصية محدودة الطول
   للنصوص - لتفادي أي تأخير أو كلفة محسوسة على تجربة الطالب قبل بدء التوليد.
2. نموذج سريع وخفيف حصراً (Gemini Flash-Lite) بحد أقصى قليل من التوكنات
   للاستجابة (yes/no فقط) - يُستدعى من handlers/files.py مباشرة بعد استقبال
   المحتوى (قبل شاشة عدد الأسئلة) لأن قراره يتطلب تفاعل الطالب عبر كيبورد،
   على عكس فحص الرياضيات الذي يبقى صامتاً ويُنفّذ لاحقاً أثناء التوليد الفعلي.
3. فشل آمن (Fail-Safe): أي خطأ أو انقطاع في الفحص السريع يُعتبر تلقائياً
   "ليست مادة لغة إنجليزية" ويكمل الاستقبال بالتدفق العادي المعتاد، بدل
   تعطيل استقبال الملف/الصورة بأكمله.
==============================================================================
"""

import asyncio
import os
from typing import List, Optional

from google.genai import types

from constants import SYSTEM_PROMPT_DETECT_ENGLISH_TEXT, SYSTEM_PROMPT_DETECT_ENGLISH_VISUAL
from logger import get_logger, log_warning
from services.detection_common import (
    IMAGE_EXTENSIONS,
    build_text_sample,
    classify_yes_no,
    first_page_png_bytes_sync,
)

logger = get_logger(__name__)


async def detect_english_in_text(pure_text: str) -> bool:
    """فحص عينة من نص مباشر (أو نص مُستخرج من مستند أوفيس) بحثاً عن مادة تعليم لغة إنجليزية."""
    if not pure_text or not pure_text.strip():
        return False
    sample = build_text_sample(pure_text)
    prompt = f"{SYSTEM_PROMPT_DETECT_ENGLISH_TEXT}{sample}"
    return await classify_yes_no([prompt], caller="english_detector")


async def detect_english_in_file(file_path: str) -> bool:
    """فحص أول صفحة (PDF) أو الصورة نفسها (صورة مباشرة) بحثاً عن مادة تعليم لغة إنجليزية."""
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
            log_warning(logger, f"Could not read image for English detection: {exc}")
            return False
        mime_type = IMAGE_EXTENSIONS[ext]
    else:
        # مستندات أوفيس (.docx/.pptx/.txt) تُفحص كنص بعد الاستخراج، وليس هنا مباشرة
        return False

    part = types.Part.from_bytes(data=image_bytes, mime_type=mime_type)
    return await classify_yes_no([SYSTEM_PROMPT_DETECT_ENGLISH_VISUAL, part], caller="english_detector")


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
