"""
أدوات مساعدة لإدارة الذاكرة على مستوى العملية (Heroku/Docker container).

المشكلة التقنية: عندما تُنشئ matplotlib/reportlab/Pillow/fitz آلاف الـ allocations
المؤقتة الكبيرة أثناء رسم صورة أو بناء PDF/Word ثم تُحرَّر، مخصص الذاكرة الافتراضي
في Linux (glibc malloc) لا يعيد هذه الصفحات لنظام التشغيل تلقائيًا في أغلب الحالات
(تجزّؤ الـ arenas / Memory Fragmentation). النتيجة: RSS الظاهر بمقاييس Heroku يبقى
مرتفعًا بعد أول ذروة استخدام حتى لو كانت كل الكائنات الحية فعليًا قليلة جدًا.

الحل: استدعاء malloc_trim(0) من glibc مباشرة بعد أي عملية ثقيلة الذاكرة، لإجبار
المخصص على فحص الصفحات الفارغة الكبيرة وإعادتها فعليًا لنظام التشغيل.
"""

import ctypes
import gc

from logger import get_logger, log_warning

logger = get_logger(__name__)

_libc = None
_libc_load_attempted = False


def _get_libc():
    """تحميل كسول لـ libc - مرة واحدة فقط طوال عمر العملية."""
    global _libc, _libc_load_attempted
    if not _libc_load_attempted:
        _libc_load_attempted = True
        try:
            _libc = ctypes.CDLL("libc.so.6")
        except Exception as exc:
            # بيئات غير Linux (تطوير محلي على Windows/Mac) - لا يوجد libc.so.6،
            # نتجاهل بصمت مرة واحدة فقط ونتجنب إعادة المحاولة كل نداء.
            log_warning(logger, f"mem_utils: libc.so.6 not available, malloc_trim disabled: {exc}")
            _libc = None
    return _libc


def release_memory_to_os() -> None:
    """
    يُستدعى بعد أي عملية ثقيلة الذاكرة (رسم صورة LaTeX، بناء PDF/Word، تقسيم/معالجة
    PDF عبر fitz، طلب Gemini بملفات كبيرة) لإعادة الصفحات الفارغة فعليًا لنظام
    التشغيل بدل تركها محجوزة لدى glibc "احتياطًا".

    آمن تمامًا: gc.collect() يُحرّر أي كائنات بايثون لم تعد مُشار إليها، ثم
    malloc_trim(0) يفحص كومة glibc نفسها ويعيد الصفحات الكبيرة الفارغة فعليًا -
    لا يؤثر إطلاقًا على أي كائن حي أو بيانات قيد الاستخدام.
    """
    gc.collect()
    libc = _get_libc()
    if libc is not None:
        try:
            libc.malloc_trim(0)
        except Exception as exc:
            log_warning(logger, f"mem_utils: malloc_trim call failed: {exc}")
