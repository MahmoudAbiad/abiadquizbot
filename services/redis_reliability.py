"""Atomic upload claims and renewable, owner-checked correctness locks."""
import asyncio
import contextlib
import hashlib
import uuid
import functools
import inspect
import logging

RELEASE = """-- release_owned
if redis.call('GET', KEYS[1]) == ARGV[1] then
 return redis.call('DEL', KEYS[1])
end
return 0
"""
RENEW = """-- renew_owned
if redis.call('GET', KEYS[1]) == ARGV[1] then
 return redis.call('EXPIRE', KEYS[1], ARGV[2])
end
return 0
"""
UPLOAD_CLAIM_TTL = 86400
_WEB_PREPARATION_SEMAPHORE = asyncio.Semaphore(2)


async def complete_once(redis, kind, identity, start):
    key = 'upload:complete:' + kind + ':' + hashlib.sha256(identity.encode()).hexdigest()
    token = uuid.uuid4().hex
    if not await redis.set(key, token, nx=True, ex=UPLOAD_CLAIM_TTL):
        return {'ok': True, 'duplicate': True}
    started = False
    try:
        await start()  # Checks + task creation; caller returns only after spawn succeeds.
        started = True
        return {'ok': True}
    finally:
        if not started:
            await redis.eval(RELEASE, 1, key, token)


class UserLock:
    def __init__(self, redis, user_id, ttl=600):
        self.redis = redis
        self.key = f'processing:user:{user_id}'
        self.token = uuid.uuid4().hex
        self.ttl = ttl
        self.heartbeat = None
        self.owner = None
        self.lost = False

    async def acquire(self):
        if not await self.redis.set(self.key, self.token, nx=True, ex=self.ttl):
            return False
        self.owner = asyncio.current_task()
        self.heartbeat = asyncio.create_task(self._renew())
        return True

    async def _renew(self):
        try:
            while True:
                await asyncio.sleep(self.ttl / 3)
                if not await self.redis.eval(RENEW, 1, self.key, self.token, self.ttl):
                    raise RuntimeError('User processing lock lost')
        except asyncio.CancelledError:
            raise
        except Exception:
            self.lost = True
            self.owner.cancel()  # Stop protected work if ownership cannot be maintained.

    async def release(self):
        if self.heartbeat:
            self.heartbeat.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self.heartbeat
        await self.redis.eval(RELEASE, 1, self.key, self.token)


def user_locked(*, web_kind=None, delay=0, wait_seconds=0):
    """Serialize intake/cancellation/transcription against per-user generation."""
    def decorate(fn):
        signature = inspect.signature(fn)
        @functools.wraps(fn)
        async def wrapped(*args, **kwargs):
            if delay:
                await asyncio.sleep(delay)
            from config import redis_client, bot
            bound = signature.bind(*args, **kwargs).arguments
            event = bound.get('call') or bound.get('message') or bound.get('msg_or_call')
            user_id = bound.get('user_id') or event.from_user.id
            lock = UserLock(redis_client, user_id)
            try:
                acquired = await lock.acquire()
                deadline = asyncio.get_running_loop().time() + wait_seconds
                while not acquired and asyncio.get_running_loop().time() < deadline:
                    await asyncio.sleep(0.05)
                    acquired = await lock.acquire()
            except Exception:
                acquired = False
            if not acquired:
                text = '⏳ طلبك قيد المعالجة بالفعل، الرجاء الانتظار حتى ينتهي قبل الضغط مجدداً.'
                if event:
                    if bound.get('call') or getattr(event, 'data', None):
                        await event.answer(text, show_alert=True)
                    else:
                        await event.answer(text)
                else:
                    try:
                        await bot.send_message(bound['chat_id'], text)
                    finally:
                        from helpers.r2_helper import delete_audio_temp, delete_file_temp, delete_file_temp_batch
                        if web_kind == 'audio':
                            await delete_audio_temp(bound['object_path'])
                        elif web_kind == 'file':
                            await delete_file_temp(bound['object_path'])
                        else:
                            await delete_file_temp_batch(bound['object_paths'])
                return
            try:
                if web_kind:
                    async with _WEB_PREPARATION_SEMAPHORE:
                        return await fn(*args, **kwargs)
                return await fn(*args, **kwargs)
            except BaseException:
                if web_kind:
                    from helpers.r2_helper import delete_audio_temp, delete_file_temp, delete_file_temp_batch
                    if web_kind == 'audio':
                        await delete_audio_temp(bound['object_path'])
                    elif web_kind == 'file':
                        await delete_file_temp(bound['object_path'])
                    else:
                        await delete_file_temp_batch(bound['object_paths'])
                raise
            finally:
                try:
                    await lock.release()
                except Exception:
                    logging.getLogger(__name__).exception('User lock release failed; relying on TTL')
        return wrapped
    return decorate
