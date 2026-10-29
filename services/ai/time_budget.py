"""Task-local quiz deadlines shared by model fallbacks and parallel parts.

Only quiz generation opts in. Audio transcription and other AI tasks retain
their configured timeouts. Cancellation remains cooperative during cleanup.
"""
import asyncio
from contextlib import asynccontextmanager
from contextvars import ContextVar
from dataclasses import dataclass
import logging
import time


class QuizAITimeout(TimeoutError):
    """The entire quiz's AI time allowance has been consumed."""


@dataclass(frozen=True)
class QuizTimeBudget:
    attempt_seconds: float
    total_seconds: float
    deadline: float

    def remaining(self):
        return max(0.0, self.deadline - time.monotonic())


_current_budget = ContextVar("quiz_ai_time_budget", default=None)


def quiz_timeout_limits(items: int, questions: int, *, is_math: bool = False) -> tuple[float, float]:
    # Calibrated against 30 days of production successes; see
    # docs/quiz-timeout-policy.md for cohort limitations and sparse large inputs.
    # Pages/images are counted upstream; text uses its word-based page count.
    if items <= 1:
        source_limit = 120.0
    elif items <= 5:
        source_limit = 150.0
    elif items <= 15:
        source_limit = 180.0
    elif items <= 35:
        source_limit = 240.0
    else:
        source_limit = 300.0
    question_limit = (120.0 if questions <= 10 else 180.0 if questions <= 30
                      else 240.0 if questions <= 60 else 300.0)
    math_limit = (180.0 if items <= 5 else 240.0) if is_math else 0.0
    # Take the largest allowance; don't add independent safety margins together.
    attempt = max(source_limit, question_limit, math_limit)
    return attempt, min(600.0, attempt * 2)


def current_quiz_budget():
    return _current_budget.get()


@asynccontextmanager
async def quiz_ai_budget(items: int, questions: int, *, is_math: bool = False):
    # Never give nested work a fresh allowance. Child tasks inherit this same
    # immutable absolute deadline through ContextVar propagation.
    existing = current_quiz_budget()
    if existing is not None:
        yield existing
        return
    attempt, total = quiz_timeout_limits(items, questions, is_math=is_math)
    budget = QuizTimeBudget(attempt, total, time.monotonic() + total)
    logging.getLogger(__name__).info(
        "QUIZ_AI_BUDGET items=%s questions=%s is_math=%s attempt_s=%s total_s=%s",
        items, questions, is_math, attempt, total,
    )
    token = _current_budget.set(budget)
    timer = asyncio.timeout(total)
    try:
        async with timer:
            yield budget
    except TimeoutError as exc:
        if timer.expired():
            raise QuizAITimeout("Quiz AI deadline exceeded") from exc
        raise
    finally:
        _current_budget.reset(token)


@asynccontextmanager
async def quiz_cleanup_deadline(seconds=5):
    """Bound aggregate remote cleanup so cancellation cannot wait per file."""
    timer = asyncio.timeout(seconds if current_quiz_budget() is not None else None)
    try:
        async with timer:
            yield
    except TimeoutError:
        if not timer.expired():
            raise
        # Remote uploads also expire server-side. Cleanup must not discard a
        # successful quiz or extend a cancelled quiz by N files * timeout.
        logging.getLogger(__name__).warning("QUIZ_AI_CLEANUP_DEADLINE_EXCEEDED")
