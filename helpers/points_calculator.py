"""Centralised, deterministic pricing for quiz generation."""

from constants import (
    DISCOUNT_RATE_FOR_CACHED,
    MAX_LIMIT_PAGES,
    MAX_LIMIT_QUESTIONS,
    MAX_STANDARD_PAGES,
    MAX_STANDARD_QUESTIONS,
)


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
    """
    items = max(0, int(pages_or_images))
    question_count = max(0, int(questions))

    if items > MAX_LIMIT_PAGES or question_count > MAX_LIMIT_QUESTIONS:
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
