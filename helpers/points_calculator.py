"""Centralised, deterministic pricing for quiz generation and audio transcription."""

from constants import (
    DISCOUNT_RATE_FOR_CACHED,
    MAX_ALBUM_IMAGES,
    MAX_LIMIT_PAGES,
    MAX_STANDARD_PAGES,
    MAX_STANDARD_QUESTIONS,
)
from helpers.settings_helper import get_setting

# 🆕 Audio transcription pricing tier: first AUDIO_STANDARD_MINUTES minutes are billed at
# the standard 1.0 point/minute rate, any minute beyond that is billed at the overage rate.
AUDIO_STANDARD_MINUTES = 10
AUDIO_OVERAGE_RATE = 1.5


def _tier_cost(quantity: int, standard_limit: int, standard_rate: float, overage_rate: float) -> float:
    """Price the first tier at `standard_rate` and the remainder at `overage_rate`.

    🆕 صارت تاخد المعدّلين كباراميترين صراحة (بدل 1 و1.5 ثابتين بالكود) - القيم الفعلية
    تُجلَب من إعدادات الأدمن (quiz_standard_rate/quiz_overage_rate) من قِبل المستدعي
    (calculate_quiz_points_cost) وتُمرَّر هنا جاهزة. الدالة نفسها بقيت sync عمداً (حساب
    محلي بحت بلا أي I/O) حتى لا يضطر calculate_audio_transcription_cost - المستدعي
    الآخر لنفس الدالة، بتسعير محاضرات صوتية منفصل تماماً وغير مطلوب تعديله هون - أن
    يتحوّل لدالة async هو الآخر ويُعاد تحديث كل استدعاءاته بـ handlers/audio.py.
    """
    quantity = max(0, int(quantity))
    return float(min(quantity, standard_limit)) * standard_rate + max(0, quantity - standard_limit) * overage_rate


async def calculate_quiz_points_cost(
    pages_or_images: int, questions: int, is_album: bool = False
) -> float:
    """Calculate the full price for a requested quiz.

    Album images have a flat image charge (at the standard rate); non-album
    documents use the page tier. Super processing uses its own flat overage
    rate for all items.

    🆕 عتبة "سوبر" (السعر المضاعف بالأسفل) صارت مطابقة تماماً لشرط التنفيذ الفعلي بـ
    gemini_helper.generate_quiz_smart (نفس منطق determine_execution_mode بـ
    quiz_service.py - راجع الملاحظة هناك للتفاصيل): ألبوم > MAX_ALBUM_IMAGES (10)، أو
    ملف > MAX_LIMIT_PAGES (35) صفحة. كانت سابقاً تُطبَّق نفس عتبة الصفحات (35) حتى على
    الصور - يعني ألبوم Super Images فعلي (>10 صور) بعدد أسئلة معتدل كان يُسعَّر بالسعر
    العادي (تحت-تسعير حقيقي، مو مجرد خطأ عرض). عدد الأسئلة وحده لم يعد يُفعّل السعر
    المضاعف لنفس السبب (لا معالجة متوازية فعلية تقابله).

    🆕 المعدّلان (1.0 العادي و1.5 التجاوز/سوبر) صارا قابلين للتعديل من لوحة الأدمن
    (quiz_standard_rate/quiz_overage_rate بـ settings_helper.py) بدل رقمين ثابتين
    بالكود - العتبات نفسها (MAX_STANDARD_PAGES/MAX_STANDARD_QUESTIONS/MAX_LIMIT_PAGES/
    MAX_ALBUM_IMAGES) بقيت ثابتة كما هي تماماً بلا أي تغيير، لأنها مرتبطة أيضاً بقرار
    التنفيذ الفعلي (is_super_pdf/is_super_images) لا بالتسعير فقط.
    """
    items = max(0, int(pages_or_images))
    question_count = max(0, int(questions))

    standard_rate = float(await get_setting("quiz_standard_rate"))
    overage_rate = float(await get_setting("quiz_overage_rate"))

    is_super = (items > MAX_ALBUM_IMAGES) if is_album else (items > MAX_LIMIT_PAGES)
    if is_super:
        return round((items + question_count) * overage_rate, 2)

    item_cost = float(items) * standard_rate if is_album else _tier_cost(items, MAX_STANDARD_PAGES, standard_rate, overage_rate)
    question_cost = _tier_cost(question_count, MAX_STANDARD_QUESTIONS, standard_rate, overage_rate)
    return round(item_cost + question_cost, 2)


async def calculate_cached_points_cost(
    pages_or_images: int, questions: int, is_album: bool = False
) -> float:
    """Return the exact discounted cache price (10% of the full price)."""
    full_price = await calculate_quiz_points_cost(pages_or_images, questions, is_album)
    return round(full_price * DISCOUNT_RATE_FOR_CACHED, 2)


def calculate_audio_transcription_cost(duration_minutes: int) -> float:
    """Calculate the price for audio transcription based on rounded-up minutes.

    Pricing tier:
    - 1.0 point / minute for the first AUDIO_STANDARD_MINUTES (10) minutes.
    - 1.5 points / minute for every minute beyond that.

    IMPORTANT: `duration_minutes` must already be pre-rounded UPWARDS by the caller from
    the raw audio duration in seconds, e.g.:
        minutes = max(1, (duration_seconds + 59) // 60)
    This function does not perform that rounding itself - it only prices whole minutes.
    """
    minutes = max(0, int(duration_minutes))
    # 🩹 غير مرتبط بإعدادات تسعير الكويزات الجديدة (quiz_standard_rate/quiz_overage_rate)
    # عمداً - تسعير المحاضرات الصوتية خارج نطاق هذا التعديل تماماً، فبقي 1.0/AUDIO_OVERAGE_RATE
    # ثابتين بالكود بلا أي تغيير في السلوك.
    return round(_tier_cost(minutes, AUDIO_STANDARD_MINUTES, 1.0, AUDIO_OVERAGE_RATE), 2)
