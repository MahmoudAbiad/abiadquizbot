# services/detection_common.py
"""
==============================================================================
MODULE: Shared Content-Detection Infrastructure
==============================================================================
الوصف:
البنية التحتية المشتركة بين كل فحوصات "عيّنة سريعة + سؤال yes/no" التي تُنفَّذ
قبل التوليد الفعلي (فحص الرياضيات في math_detector.py، وفحص مادة اللغة
الإنجليزية في english_detector.py). لا يحتوي هذا الملف أي منطق خاص بغرض
تصنيف معيّن - فقط الأدوات المشتركة: عملاء Gemini الثابتين، دالة الاستدعاء
الفعلي (_classify)، وأخذ العيّنات من النص أو الصفحة الأولى من ملف.

IMPORTANT: أي تعديل هون بينعكس تلقائياً على كل الفحوصات (رياضيات + إنجليزي +
أي فحص مستقبلي بنفس النمط) - وهاد بالضبط سبب وجود هالملف: تفادي تكرار نفس
الباغ (أو نفس الإصلاح) بمكانين منفصلين.
==============================================================================
"""

import asyncio
import gc
import os
from typing import List, Optional

import fitz
from google import genai
from google.genai import types

from constants import MATH_DETECTION_MODEL, MATH_DETECTION_TEXT_SAMPLE_CHARS, MATH_DETECTION_TIMEOUT
from logger import get_logger, log_warning

logger = get_logger(__name__)

API_KEYS = [key.strip() for key in os.getenv("GEMINI_API_KEYS", "").split(",") if key.strip()]

# AI-NOTE (memory-leak fix): هاد الفحص بيتنفّذ قبل كل عملية توليد كويز (على كل الملفات
# والنصوص)، فكان إنشاء genai.Client() جديد بكل key بكل استدعاء (بدون إغلاقه) من أكبر
# مصادر تسريب الذاكرة الفعلية لأنه على المسار الساخن. نفس الحل: عملاء ثابتين لمرة وحدة.
_GEMINI_CLIENTS: List[genai.Client] = [genai.Client(api_key=key) for key in API_KEYS]

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
    """
    يرسل المحتوى (نص أو صورة) لنموذج الفحص السريع ويعيد True فقط لو أجاب بوضوح yes/نعم.

    🩹 BUGFIX (محاولة أولى - ثبت خطؤها بالإنتاج): كان `max_output_tokens=5` مضبوطاً مع
    `thinking_config` مفعّلاً بنفس الوقت. بموديلات Gemini الحديثة، توكنات "التفكير" الداخلي
    (thinking tokens) تُحسب من نفس ميزانية max_output_tokens - فحتى مع thinking_level="low"،
    الموديل كان يصرف الـ 5 توكنات كاملة على التفكير الداخلي ولا يتبقى له مجال ليكتب "yes"/"no"
    الفعلية. النتيجة: `response.text` يرجع فارغاً بصمت (بدون Exception) وبيُفسَّر خطأً كـ "no"
    دائماً تقريباً - أي الفحص كان يفشل بهدوء في أغلب الحالات دون أي أثر بالـ logs.

    ⚠️ محاولة الإصلاح الأولى استخدمت `thinking_config=ThinkingConfig(thinking_budget=0)` بدل
    `thinking_level`، لكن هذا الموديل تحديداً (MATH_DETECTION_MODEL) لا يدعم حقل thinking_budget
    إطلاقاً - أي استخدام له يُرجع `400 INVALID_ARGUMENT` فوراً من الـ API (كما ظهر بلوجات
    الإنتاج)، أي كان بيفشل تماماً بدل ما يرجع فاضي بصمت مثل قبل. الموديل يدعم فقط
    `thinking_level` (enum منفصل عن thinking_budget وليس بديلاً متوافقاً معه بكل الموديلات).

    الحل الفعلي: الإبقاء على `thinking_level="low"` (الحقل المُثبَت أنه مدعوم فعلياً لهذا
    الموديل) مع رفع `max_output_tokens` بشكل كافٍ (لا 5 ولا حتى 20) بحيث يبقى مجال كتابة
    للجواب الفعلي بعد ما يُستهلك جزء من الميزانية على التفكير الداخلي، مهما كان صغيراً.
    """
    if not API_KEYS:
        return False
    for client in _GEMINI_CLIENTS:
        try:
            response = await asyncio.wait_for(
                client.aio.models.generate_content(
                    model=MATH_DETECTION_MODEL,
                    contents=contents,
                    config=types.GenerateContentConfig(
                        max_output_tokens=200,
                        thinking_config=types.ThinkingConfig(thinking_level="low"),
                    ),
                ),
                timeout=MATH_DETECTION_TIMEOUT,
            )
            answer = (getattr(response, "text", None) or "").strip().lower()
            if not answer:
                # 🩹 تسجيل واضح لأي حالة رد فارغ بدل ابتلاعها بصمت كـ "no" - لالتقاط أي
                # مشكلة مشابهة مستقبلاً (تغيير موديل، تغيير سلوك thinking، إلخ) فوراً بالـ logs.
                finish_reason = None
                try:
                    finish_reason = response.candidates[0].finish_reason if response.candidates else None
                except Exception:
                    pass
                log_warning(logger, f"[{caller}] Detection call returned empty text (finish_reason={finish_reason})")
                continue
            return answer.startswith("yes") or answer.startswith("نعم")
        except Exception as exc:
            log_warning(logger, f"[{caller}] Detection call failed with one key, trying next key if available: {exc}")
            continue
    return False
