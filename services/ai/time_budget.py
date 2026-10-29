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


def quiz_timeout_limits(items: int, questions: int) -> tuple[float, float]:
    # Pages/images are counted upstream; text uses its word-based page count.
    # A dense one-page exam also needs more time to produce a long response.
    attempt = min(300.0, 50.0 + 10 * max(0, items - 1)
                  + 2 * max(0, questions - 10))
    return attempt, min(600.0, attempt * 3)


def current_quiz_budget():
    return _current_budget.get()


@asynccontextmanager
async def quiz_ai_budget(items: int, questions: int):
    # Never give nested work a fresh allowance. Child tasks inherit this same
    # immutable absolute deadline through ContextVar propagation.
    existing = current_quiz_budget()
    if existing is not None:
        yield existing
        return
    attempt, total = quiz_timeout_limits(items, questions)
    budget = QuizTimeBudget(attempt, total, time.monotonic() + total)
    logging.getLogger(__name__).info(
        "QUIZ_AI_BUDGET items=%s questions=%s attempt_s=%s total_s=%s",
        items, questions, attempt, total,
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
