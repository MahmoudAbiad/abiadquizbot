"""
task_utils.py
==============================================================================
مساعد مركزي لتشغيل المهام الخلفية (fire-and-forget) بأمان.

المشكلة: asyncio.create_task() بتحتفظ بالمهمة بـ "weak reference" فقط داخل الـ event
loop. لو ما أحد احتفظ بمرجع للـ Task، ممكن الـ garbage collector يمسحها بنص
تنفيذها (وثّقتها بايثون رسمياً بتحذير بتوثيق asyncio.create_task) - يعني ممكن حلقة
مثل group_quiz_heartbeat_loop تتوقف بصمت بلا أي خطأ، أو حدث تحليلات يضيع.

spawn() بتحل هالشي بنقطتين:
  1) بتحتفظ بمرجع قوي لكل مهمة بـ set عام لحد ما تخلص.
  2) بتسجّل أي استثناء غير ملتقَط بدل ما يضيع كـ "Task exception was never retrieved".

⚠️ هالملف عمداً ما بيستورد إلا asyncio و logger.get_logger لتفادي أي Circular Import.
وبيستخدم logger.error العادي (مو log_error من logger.py) حتى ما ينتج عن فشل مهمة
تتبّع مهمة جديدة تتبّع الخطأ نفسه بحلقة لا نهائية.
==============================================================================
"""

import asyncio
from typing import Any, Coroutine, Optional, Set

from logger import get_logger

logger = get_logger(__name__)

_background_tasks: Set["asyncio.Task[Any]"] = set()


def _on_task_done(task: "asyncio.Task[Any]") -> None:
    _background_tasks.discard(task)
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        logger.error(
            f"Background task '{task.get_name()}' failed: {exc!r}",
            exc_info=(type(exc), exc, exc.__traceback__),
        )


def spawn(coro: Coroutine[Any, Any, Any], *, name: Optional[str] = None) -> "asyncio.Task[Any]":
    """بديل مباشر لـ asyncio.create_task(coro) للمهام اللي ما حدا بينتظر نتيجتها."""
    task = asyncio.create_task(coro, name=name)
    _background_tasks.add(task)
    task.add_done_callback(_on_task_done)
    return task


def background_task_count() -> int:
    """عدد المهام الخلفية الحيّة حالياً (مفيد للمراقبة/الاختبارات)."""
    return len(_background_tasks)
