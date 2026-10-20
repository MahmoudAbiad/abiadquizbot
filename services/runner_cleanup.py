"""TEST-gated retention. Storage deletion always precedes metadata deletion."""
import asyncio
import logging
from datetime import datetime, timedelta, timezone
from uuid import UUID

from services.runner_core import enabled
from services.runner_images import db, cache_row, fresh, image_lock, image_path, integer
from services.redis_reliability import UserLock

logger = logging.getLogger(__name__)


def bucket():
    return db().storage.from_('quiz-images')


async def remove_files(paths):
    if paths:
        # Storage raises on failure; do not acknowledge a failed removal in SQL.
        await bucket().remove(paths)


async def delete_runner_prefix(quiz_id):
    if not enabled():
        return False
    qid = str(UUID(str(quiz_id)))
    # Called after successful deletion; never remove a live quiz's images.
    if (await db().table('quizzes').select('id').eq('id', qid).limit(1).execute()).data:
        return False
    root = f'runner/{qid}'
    # Fixed depth: quiz/question-hash/part.png. Restart at offset zero after
    # removals to avoid pagination skips. Each invocation does bounded work.
    folders = await bucket().list(root, {'limit': 100, 'sortBy': {'column': 'name', 'order': 'asc'}})
    budget = 200
    for folder in folders:
        name = folder['name']
        if '/' in name or name in ('.', '..'):
            raise ValueError('invalid runner storage prefix')
        prefix = f'{root}/{name}'
        files = await bucket().list(prefix, {'limit': budget})
        await remove_files([f"{prefix}/{f['name']}" for f in files])
        budget -= len(files)
        if budget <= 0:
            return False
    # Only acknowledge the durable quiz-deletion tombstone once storage is empty.
    if await bucket().list(root, {'limit': 1}):
        return False
    await db().table('runner_image_gc').delete().eq('quiz_id', qid).execute()
    return True


async def cleanup_once(now=None):
    if not enabled():
        return
    from config import redis_client
    lock = UserLock(redis_client, 'runner-cleanup', ttl=300)
    lock.key = 'runner:cleanup'
    if not await lock.acquire():
        return
    try:
        now = now or datetime.now(timezone.utc)
        expired = await db().table('runner_images').select('*').lt('expires_at', now.isoformat()).order('expires_at').limit(200).execute()
        for row in expired.data:
            image = image_lock(row['quiz_id'], row['question_key'], row['part'])
            if not await image.acquire():
                continue
            try:
                current = await cache_row(row['quiz_id'], row['question_key'], row['part'])
                if not current or fresh(current) or current['expires_at'] != row['expires_at']:
                    continue
                await remove_files([image_path(row['quiz_id'], row['question_key'], row['part'])])
                await db().table('runner_images').delete().eq('quiz_id', row['quiz_id']).eq('question_key', row['question_key']).eq('part', row['part']).eq('expires_at', row['expires_at']).execute()
            except Exception:
                logger.warning('runner expired image cleanup failed; will retry', exc_info=True)
            finally:
                await image.release()
        cutoff = (now - timedelta(days=integer('RUNNER_ROUND_RETENTION_DAYS', 7, 365))).isoformat()
        rounds = await db().table('runner_rounds').select('id').or_(f'finished_at.lt.{cutoff},and(finished_at.is.null,expires_at.lt.{cutoff})').order('expires_at').limit(500).execute()
        if rounds.data:
            await db().table('runner_rounds').delete().in_('id', [r['id'] for r in rounds.data]).or_(f'finished_at.lt.{cutoff},and(finished_at.is.null,expires_at.lt.{cutoff})').execute()
        deleted = await db().table('runner_image_gc').select('quiz_id').order('created_at').limit(10).execute()
        for row in deleted.data:
            try:
                await delete_runner_prefix(row['quiz_id'])
            except Exception:
                logger.warning('runner orphan cleanup failed; tombstone retained', exc_info=True)
    finally:
        await lock.release()


async def runner_cleanup_loop():
    while True:
        try:
            await cleanup_once()
        except Exception:
            logger.warning('runner retention failed; retry next cycle', exc_info=True)
        await asyncio.sleep(integer('RUNNER_CLEANUP_INTERVAL_SECONDS', 3600, 86400))
