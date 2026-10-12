"""Content-free monotonic timing for quiz delivery diagnostics."""
import asyncio
import logging
import time

logger = logging.getLogger(__name__)


async def measured(stage, awaitable, detail=""):
    started = time.perf_counter()
    outcome = "ok"
    try:
        return await awaitable
    except BaseException as exc:
        outcome = type(exc).__name__
        raise
    finally:
        task = asyncio.current_task()
        logger.info("quiz_timing task=%s stage=%s ms=%.1f outcome=%s detail=%s",
                    task.get_name() if task else "none", stage,
                    (time.perf_counter() - started) * 1000, outcome, detail)
