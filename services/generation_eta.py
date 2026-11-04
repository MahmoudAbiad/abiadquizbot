"""Waiting-time estimate and wording for the "generating your quiz" message.

Pure functions only (no I/O, no Telegram), so they are cheap to unit-test and can
never block or break a generation. Callers must treat a failure here as cosmetic.

The numbers come from production telemetry (``usage_events`` rows of type
``quiz_generated``, 2 Sep - 31 Oct 2026, 184 successful generations); see
docs/generation-eta-policy.md. They describe *successful* runs only, so they are
a guide, never a promise: when a run overshoots, the wording changes instead of
the countdown going negative.
"""
from dataclasses import dataclass
from math import ceil
from typing import Any, Mapping, Optional, Sequence, Tuple

from constants import (
    QUESTION_TYPE_SIMILAR_MATH,
    SUBJECT_MATH,
    SUBJECT_QUIZ_SOLVED,
    SUBJECT_QUIZ_UNSOLVED,
)

Point = Tuple[float, float]

# (questions, typical seconds): medians of successful runs, lightly smoothed.
_IMAGES_POINTS: Sequence[Point] = ((5, 20), (10, 37), (15, 58), (20, 85), (30, 120))
_PDF_POINTS: Sequence[Point] = ((5, 40), (10, 55), (20, 75), (30, 110))
_TEXT_CASCADE_POINTS: Sequence[Point] = ((5, 30), (10, 45), (20, 70), (30, 100))

# Plain text with few questions normally goes through the fast text slot (~5 s);
# anything else (more questions, math, ready-made exams, batched) uses the cascade.
_FAST_TEXT_MAX_QUESTIONS = 10
_EXTRA_SECONDS_PER_IMAGE = 10
_EXTRA_SECONDS_PER_PDF_PAGE = 1.5
_MAX_TYPICAL_SECONDS = 420
_FALLBACK_CEILING_SECONDS = 600


@dataclass(frozen=True)
class GenerationEta:
    typical: int   # seconds: roughly the median of normal runs
    slow: int      # seconds: past this point we say "longer than usual"
    ceiling: int   # seconds: hard AI deadline; the charge is refunded after it


def _interp(points: Sequence[Point], x: float) -> float:
    """Piecewise-linear lookup; flat below the first point, extrapolated above the last."""
    if x <= points[0][0]:
        return points[0][1]
    for (x0, y0), (x1, y1) in zip(points, points[1:]):
        if x <= x1:
            return y0 + (y1 - y0) * (x - x0) / (x1 - x0)
    (x0, y0), (x1, y1) = points[-2], points[-1]
    return y1 + (y1 - y0) / (x1 - x0) * (x - x1)


def _round5(seconds: float) -> int:
    return max(5, int(5 * round(seconds / 5)))


def _ceiling_seconds(items: int, questions: int, is_math: bool) -> int:
    """Total AI deadline for this request (same source of truth that enforces it)."""
    try:
        from services.ai.time_budget import quiz_timeout_limits
        return int(quiz_timeout_limits(items, questions, is_math=is_math)[1])
    except Exception:
        return _FALLBACK_CEILING_SECONDS


def _typical_seconds(data: Mapping[str, Any], items: int, questions: int, mode: str,
                     is_math: bool, ready_exam: bool) -> float:
    text_source = data.get("input_type") != "media" or bool(data.get("cached_office_text"))
    if ready_exam:
        # Output size dominates (one question extracted per answer written).
        typical = 40 + 2.5 * questions if mode == "Standard" else 60 + 2.2 * questions
        return typical * 1.5 if is_math else typical
    if text_source:
        if mode == "Standard" and not is_math and questions <= _FAST_TEXT_MAX_QUESTIONS:
            return 8 + 0.3 * questions
        typical = _interp(_TEXT_CASCADE_POINTS, questions)
        return typical * 1.3 if mode != "Standard" else typical
    if data.get("content_source") == "images" or data.get("is_album"):
        if mode == "Super-Images":
            return 120 + 1.5 * questions
        return _interp(_IMAGES_POINTS, questions) + _EXTRA_SECONDS_PER_IMAGE * (items - 1)
    # PDF
    if mode in ("Super-PDF", "Super-Text"):
        return 120 + 1.5 * questions
    if mode == "Over-Limit":
        return 90 + max(0, questions - 30)
    return _interp(_PDF_POINTS, questions) + _EXTRA_SECONDS_PER_PDF_PAGE * max(0, items - 5)


def estimate_generation_eta(data: Mapping[str, Any], items: int, questions: int,
                            mode: str, is_album: bool = False) -> GenerationEta:
    """Estimate how long the AI stage of this request will take.

    ``data`` is the student's FSM state; ``mode`` is determine_execution_mode()'s
    result (Standard / Over-Limit / Super-*), which already reflects the real path.
    """
    items = max(1, int(items or 1))
    questions = max(1, int(questions or 1))
    subject = data.get("subject_type")
    ready_subject = subject in (SUBJECT_QUIZ_SOLVED, SUBJECT_QUIZ_UNSOLVED)
    effective_subject = (data.get("content_subject_type") if ready_subject else None) or subject
    is_math = effective_subject == SUBJECT_MATH
    # Mirrors services/quiz_service.py: similar-math generation is not an extraction.
    ready_exam = ready_subject and not (is_math and data.get("question_type") == QUESTION_TYPE_SIMILAR_MATH)
    data = {**data, "is_album": bool(is_album) or bool(data.get("is_album"))}

    ceiling = _ceiling_seconds(items, questions, is_math)
    typical = min(_typical_seconds(data, items, questions, mode, is_math, ready_exam),
                  _MAX_TYPICAL_SECONDS, 0.6 * ceiling)
    slow = min(max(2.5 * typical, typical + 45), 0.85 * ceiling)
    return GenerationEta(typical=_round5(typical), slow=_round5(slow), ceiling=ceiling)


# ----------------------------- wording -----------------------------

def _seconds_label(n: int) -> str:
    if n == 1:
        return "ثانية"
    if n == 2:
        return "ثانيتين"
    return f"{n} ثواني" if 3 <= n <= 10 else f"{n} ثانية"


def _minutes_label(n: int) -> str:
    if n == 1:
        return "دقيقة"
    if n == 2:
        return "دقيقتين"
    return f"{n} دقائق" if 3 <= n <= 10 else f"{n} دقيقة"


def format_elapsed(seconds: float) -> str:
    total = int(seconds)
    minutes, secs = divmod(total, 60)
    if not minutes:
        return _seconds_label(secs)
    return _minutes_label(minutes) if not secs else f"{_minutes_label(minutes)} و{_seconds_label(secs)}"


def format_remaining(seconds: float) -> str:
    if seconds <= 10:
        return "أقل من 10 ثواني"
    if seconds < 60:
        return f"حوالي {_seconds_label(int(5 * ceil(seconds / 5)))}"
    return f"حوالي {_minutes_label(int(ceil(seconds / 60)))}"


def _format_upper_bound(seconds: float) -> str:
    if seconds < 60:
        return _seconds_label(int(5 * ceil(seconds / 5)))
    return _minutes_label(int(ceil(seconds / 60)))


def _bar(fraction: float, width: int = 10) -> str:
    filled = max(0, min(width, int(round(fraction * width))))
    return "▰" * filled + "▱" * (width - filled)


def _generic_text(elapsed: float) -> str:
    """Used only when no estimate could be computed (same wording as before ETAs)."""
    return (
        f"🤖 لسا عم نعالج المستند ونولّد الأسئلة... ({format_elapsed(elapsed)})\n"
        "الذكاء الاصطناعي مزدحم شوي حالياً، بس طلبك قيد التنفيذ فعلياً - ما في داعي "
        "لإعادة إرسال نفس الملف."
    )


def build_processing_text(elapsed: float, eta: Optional[GenerationEta]) -> str:
    """The full waiting message for a request that has been running ``elapsed`` seconds."""
    elapsed = max(0.0, float(elapsed))
    if eta is None:
        return _generic_text(elapsed)
    if elapsed < eta.typical:
        return (
            "🤖 عم نولّد أسئلتك بالذكاء الاصطناعي...\n\n"
            f"⏱️ المتبقي تقريباً: {format_remaining(eta.typical - elapsed)}\n"
            f"{_bar(min(elapsed / eta.typical, 0.9))}\n\n"
            f"ℹ️ وقت الازدحام ممكن يطول لحد {_format_upper_bound(eta.slow)}."
        )
    if elapsed < eta.slow:
        return (
            "🤖 قربنا نخلّص... الطلب أخد أكتر من المعتاد شوي.\n\n"
            f"⏱️ مضى: {format_elapsed(elapsed)}\n"
            "ما في داعي لإعادة إرسال الملف، طلبك شغّال."
        )
    return (
        f"🤖 لسا عم نولّد الأسئلة... ({format_elapsed(elapsed)})\n\n"
        "الذكاء الاصطناعي مزدحم شوي حالياً، بس طلبك قيد التنفيذ فعلياً - ما في داعي "
        "لإعادة إرسال نفس الملف.\n"
        f"لو ما خلص خلال حوالي {_minutes_label(max(1, round(eta.ceiling / 60)))} "
        "منوقّف المحاولة وبنرجّع نقاطك تلقائياً."
    )
