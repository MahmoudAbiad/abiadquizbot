"""Periodic edits of the "generating your quiz" status message.

Kept out of handlers/files.py so the timing rules can be unit-tested with a fake
clock. The message object is injected (anything with an async ``edit_text``), so
this module never imports Telegram. Every failure here is cosmetic: it must never
affect the generation it decorates.
"""
import asyncio
import time
from typing import Any, Awaitable, Callable, Optional

from services.generation_eta import GenerationEta, build_processing_text

FAST_INTERVAL_SECONDS = 5       # while a countdown is shown
SLOW_INTERVAL_SECONDS = 15      # once the usual time has passed (text changes slowly)
GENERIC_INTERVAL_SECONDS = 20   # no estimate available: the previous behaviour
MAX_RETRY_AFTER_SECONDS = 30    # cap when Telegram asks us to slow down


def next_interval(elapsed: float, eta: Optional[GenerationEta]) -> float:
    if eta is None:
        return GENERIC_INTERVAL_SECONDS
    return FAST_INTERVAL_SECONDS if elapsed < eta.typical else SLOW_INTERVAL_SECONDS


async def run_processing_heartbeat(
    status_msg: Any,
    eta: Optional[GenerationEta] = None,
    *,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> None:
    """Refresh ``status_msg`` until cancelled (the caller cancels it when generation ends).

    - The text is skipped when it has not changed (Telegram rejects identical edits).
    - A failed edit (deleted message, flood limit...) is ignored and retried on the
      next tick; if Telegram sends ``retry_after`` we wait that long first.
    """
    started = clock()
    last_text: Optional[str] = None
    try:
        while True:
            await sleep(next_interval(clock() - started, eta))
            elapsed = clock() - started
            try:
                text = build_processing_text(elapsed, eta)
            except Exception:
                continue
            if text == last_text:
                continue
            try:
                await status_msg.edit_text(text)
                last_text = text
            except Exception as exc:
                retry_after = getattr(exc, "retry_after", None)
                if isinstance(retry_after, (int, float)) and retry_after > 0:
                    await sleep(min(float(retry_after), MAX_RETRY_AFTER_SECONDS))
    except asyncio.CancelledError:
        pass
