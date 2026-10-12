"""Share image preparation between streaming lookahead and foreground playback."""
import asyncio
import copy
import hashlib
import json
import time
from collections import OrderedDict


# Keep only URLs in the cache. PNG bytes live only in the two active jobs.
_PREPARATION_SLOTS = asyncio.Semaphore(2)
_inflight = {}
_urls = OrderedDict()
_CACHE_LIMIT = 256
_CACHE_TTL = 7200


def question_image_key(question, idx, total):
    payload = {key: value for key, value in question.items()
               if key not in ("image_url", "rendered_image_url", "cached_image_url")}
    raw = json.dumps([idx, total, payload], sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


async def _prepare(question, idx, total, quiz_id):
    from services.image_quiz_renderer import render_question_image_async, looks_arabic
    from services.quiz_engine import _question_image_object_path
    from helpers.supabase_helper import upload_quiz_question_image
    async with _PREPARATION_SLOTS:
        image_bytes = await render_question_image_async(
            question, idx, total, looks_arabic(str(question.get("question", ""))))
        image_url = await upload_quiz_question_image(
            image_bytes, _question_image_object_path(quiz_id, idx, question))
        return image_url, None if image_url else image_bytes


async def prepare_math_image(question, idx, total, quiz_id=None):
    """Join an existing render/upload; a cancelled waiter cannot cancel its peers."""
    if question.get("image_url"):
        return question["image_url"], None
    key = question_image_key(question, idx, total)
    cached = _urls.get(key)
    if cached and cached[1] > time.monotonic():
        _urls.move_to_end(key)
        question["image_url"] = cached[0]
        return cached[0], None
    _urls.pop(key, None)
    task = _inflight.get(key)
    if task is None:
        task = asyncio.create_task(_prepare(copy.deepcopy(question), idx, total, quiz_id))
        _inflight[key] = task

        def finished(done):
            if _inflight.get(key) is done:
                _inflight.pop(key, None)
            if not done.cancelled() and done.exception() is None:
                url, _ = done.result()
                if url:
                    _urls[key] = (url, time.monotonic() + _CACHE_TTL)
                    _urls.move_to_end(key)
                    while len(_urls) > _CACHE_LIMIT:
                        _urls.popitem(last=False)

        task.add_done_callback(finished)
    image_url, image_bytes = await asyncio.shield(task)
    if image_url:
        question["image_url"] = image_url
    return image_url, image_bytes
