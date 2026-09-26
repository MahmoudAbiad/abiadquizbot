"""Centralised, deterministic pricing for quiz generation and audio transcription."""

from constants import (
    DISCOUNT_RATE_FOR_CACHED,
    MAX_ALBUM_IMAGES,
    MAX_LIMIT_PAGES,
    MAX_STANDARD_PAGES,
    MAX_STANDARD_QUESTIONS,
)

# 🆕 Audio transcription pricing tier: first AUDIO_STANDARD_MINUTES minutes are billed at
# the standard 1.0 point/minute rate, any minute beyond that is billed at the overage rate.
AUDIO_STANDARD_MINUTES = 10
AUDIO_OVERAGE_RATE = 1.5


def _tier_cost(quantity: int, standard_limit: int) -> float:
    """Price the first tier at 1 point and the remainder at 1.5 points."""
    quantity = max(0, int(quantity))
    return float(min(quantity, standard_limit)) + max(0, quantity - standard_limit) * 1.5


def calculate_quiz_points_cost(
    pages_or_images: int, questions: int, is_album: bool = False
) -> float:
    """Calculate the full price for a requested quiz.

    Album images have a flat 1-point image charge; non-album documents use
    the page tier. Super processing uses its own flat rate for all items.

    🆕 عتبة "سوبر" (السعر المضاعف بالأسفل) صارت مطابقة تماماً لشرط التنفيذ الفعلي بـ
    gemini_helper.generate_quiz_smart (نفس منطق determine_execution_mode بـ
    quiz_service.py - راجع الملاحظة هناك للتفاصيل): ألبوم > MAX_ALBUM_IMAGES (10)، أو
    ملف > MAX_LIMIT_PAGES (35) صفحة. كانت سابقاً تُطبَّق نفس عتبة الصفحات (35) حتى على
    الصور - يعني ألبوم Super Images فعلي (>10 صور) بعدد أسئلة معتدل كان يُسعَّر بالسعر
    العادي (تحت-تسعير حقيقي، مو مجرد خطأ عرض). عدد الأسئلة وحده لم يعد يُفعّل السعر
    المضاعف لنفس السبب (لا معالجة متوازية فعلية تقابله).
    """
    items = max(0, int(pages_or_images))
    question_count = max(0, int(questions))

    is_super = (items > MAX_ALBUM_IMAGES) if is_album else (items > MAX_LIMIT_PAGES)
    if is_super:
        return round((items + question_count) * 1.5, 2)

    item_cost = float(items) if is_album else _tier_cost(items, MAX_STANDARD_PAGES)
    question_cost = _tier_cost(question_count, MAX_STANDARD_QUESTIONS)
    return round(item_cost + question_cost, 2)


def calculate_cached_points_cost(
    pages_or_images: int, questions: int, is_album: bool = False
) -> float:
    """Return the exact discounted cache price (10% of the full price)."""
    return round(
        calculate_quiz_points_cost(pages_or_images, questions, is_album)
        * DISCOUNT_RATE_FOR_CACHED,
        2,
    )


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
    return round(_tier_cost(minutes, AUDIO_STANDARD_MINUTES), 2)
