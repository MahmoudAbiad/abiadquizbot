"""Shared seven-day Runner image cache and bounded, low-priority prewarming."""
import asyncio
import logging
import os
from datetime import datetime, timedelta, timezone

from services import offload
from services.quiz_timing import question_key
from services.redis_reliability import UserLock

logger = logging.getLogger(__name__)
_inflight = {}
_prewarming = set()
_foreground = 0
_local = asyncio.Semaphore(2)
_remote = asyncio.Semaphore(offload.REMOTE_CONCURRENCY)
_background_local = asyncio.Semaphore(1)


def db():
    from helpers.supabase_helper import supabase
    return supabase


def integer(name, default, maximum):
    try:
        return max(1, min(maximum, int(os.getenv(name, str(default)))))
    except ValueError:
        return default


def image_question(q):
    return bool(q.get('is_math') or q.get('table') or q.get('matrices')
                or '$' in q.get('question', '') or any('$' in o for o in q.get('options', [])))


def image_path(qid, key, part):
    return f'runner/{qid}/{key}/{part}.png'


def image_lock(qid, key, part):
    from config import redis_client
    lock = UserLock(redis_client, 'runner-image', ttl=120)
    lock.key = f'runner:img:{qid}:{key}:{part}'
    return lock


async def cache_row(qid, key, part):
    result = await db().table('runner_images').select('*').eq('quiz_id', qid).eq('question_key', key).eq('part', part).limit(1).execute()
    return result.data[0] if result.data else None


def fresh(row):
    if not row or not row.get('expires_at'):
        return False
    return datetime.fromisoformat(row['expires_at'].replace('Z', '+00:00')) > datetime.now(timezone.utc)


async def cached_image(qid, key, part):
    row = await cache_row(qid, key, part)
    return row['game_image_url'] if fresh(row) else None


async def _produce(qid, question, part, background):
    key = question_key(question)
    lock = image_lock(qid, key, part)
    acquired = False
    try:
        until = asyncio.get_running_loop().time() + 15
        while not (acquired := await lock.acquire()):
            url = await cached_image(qid, key, part)
            if url:
                return url
            if asyncio.get_running_loop().time() >= until:
                raise TimeoutError('runner image lock busy')
            await asyncio.sleep(.3)
        url = await cached_image(qid, key, part)
        if url:
            return url
        # A configured but unhealthy worker must NEVER spill prewarm onto 512MB.
        remote_only = background and offload.ENABLED
        if remote_only and not offload.is_active():
            raise offload.OffloadUnavailable('runner prewarm circuit open')
        gate = _remote if offload.is_active() else _local
        from services.image_quiz_renderer import render_game_image_async
        from helpers.supabase_helper.quiz_images import upload_quiz_question_image
        # Local fallback also shares the two-slot Runner ceiling. Avoid nested
        # acquisition when the entire request already uses the local gate.
        async with gate:
            content = await render_game_image_async(question, part=part, allow_local_fallback=not remote_only,
                                                   local_gate=_local if gate is _remote else None)
        # Do not leave objects behind if the quiz was deleted during rendering.
        exists = await db().table('quizzes').select('id').eq('id', qid).limit(1).execute()
        if not exists.data:
            raise ValueError('runner quiz deleted')
        path = image_path(qid, key, part)
        url = await upload_quiz_question_image(content, path)
        if not url:
            raise RuntimeError('runner image upload failed')
        now = datetime.now(timezone.utc)
        try:
            await db().table('runner_images').upsert({
                'quiz_id': qid, 'question_key': key, 'part': part, 'game_image_url': url,
                'created_at': now.isoformat(),
                'expires_at': (now + timedelta(days=integer('RUNNER_IMAGE_CACHE_DAYS', 7, 365))).isoformat(),
            }).execute()
        except Exception:
            # Re-enqueue before removal: the deletion loop may already have
            # acknowledged the tombstone while this upload was in flight.
            exists = await db().table('quizzes').select('id').eq('id', qid).limit(1).execute()
            if not exists.data:
                await db().table('runner_image_gc').upsert({'quiz_id': qid}).execute()
            from services.runner_cleanup import remove_files
            await remove_files([path])
            raise
        return url
    finally:
        if acquired:
            await lock.release()


async def get_or_render_image(qid, question, part, *, background=False):
    global _foreground
    key = (str(qid), question_key(question), str(part))
    if not background:
        _foreground += 1
    try:
        url = await cached_image(*key)
        if url:
            return url
        task = _inflight.get(key)
        if task is None:
            task = asyncio.create_task(_produce(str(qid), question, str(part), background), name='runner-image')
            task.runner_background = background
            _inflight[key] = task
            def finished(done):
                if _inflight.get(key) is done:
                    _inflight.pop(key, None)
                if not done.cancelled():
                    done.exception()  # Consume failures even if every HTTP waiter disconnected.
            task.add_done_callback(finished)
        # A disconnected/timed-out HTTP request must not cancel another player's render.
        try:
            return await asyncio.shield(task)
        except offload.OffloadUnavailable:
            if background or not getattr(task, 'runner_background', False):
                raise
            # A player's wait may join remote-only prewarm; if that worker
            # fails, retry on demand with the existing local fallback policy.
            if _inflight.get(key) is task:
                _inflight.pop(key, None)
            return await get_or_render_image(qid, question, part)
    finally:
        if not background:
            _foreground -= 1


async def retry_pause():
    await asyncio.sleep(30)


async def prewarm_quiz_images(qid, questions):
    qid = str(qid)
    if os.getenv('RUNNER_PREWARM_ENABLED', '1') != '1' or qid in _prewarming:
        return
    _prewarming.add(qid)
    lock = None
    acquired = False
    counts = {'cached': 0, 'rendered': 0, 'failed': 0}
    try:
        from config import redis_client
        lock = UserLock(redis_client, 'runner-prewarm', ttl=900)
        lock.key = f'runner:prewarm:{qid}'
        acquired = await lock.acquire()
        if not acquired:
            return
        remote = offload.ENABLED
        limit = integer('RUNNER_PREWARM_CONCURRENCY_REMOTE', 4, offload.REMOTE_CONCURRENCY) if remote else integer('RUNNER_PREWARM_CONCURRENCY_LOCAL', 1, 1)
        # Only `limit` tasks, not 1500 futures holding source/render buffers.
        jobs = iter((q, part) for q in questions if image_question(q)
                    for part in ['question', *map(str, range(len(q['options'])))])

        async def worker():
            for q, part in jobs:
                for attempt in range(4):
                    try:
                        if await cached_image(qid, question_key(q), part):
                            counts['cached'] += 1
                            break
                        while _foreground:
                            await asyncio.sleep(.05)
                        if remote:
                            await get_or_render_image(qid, q, part, background=True)
                        else:
                            async with _background_local:
                                await get_or_render_image(qid, q, part, background=True)
                        counts['rendered'] += 1
                        break
                    except Exception:
                        if remote and attempt < 3:
                            await retry_pause()
                        else:
                            counts['failed'] += 1
                            break
        await asyncio.gather(*(worker() for _ in range(limit)))
        logger.info('runner prewarm quiz=%s mode=%s %s', qid, 'remote' if remote else 'local', counts)
    finally:
        _prewarming.discard(qid)
        if acquired:
            await lock.release()
