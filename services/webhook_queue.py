"""Redis-backed pending/leased queue. ACK only after atomic enqueue.

Leases make interrupted work reclaimable; completion is ownership-checked.
Delivery is at least once: arbitrary downstream side effects cannot be exactly once.
"""
import asyncio
import contextlib
import json
import logging
import random
import time
import uuid

logger = logging.getLogger(__name__)
PREFIX = 'telegram:updates:'
KEYS = [PREFIX + suffix for suffix in ('payload', 'pending', 'leased', 'owners', 'attempts', 'dead')]
LEASE_SECONDS = 120
MAX_ATTEMPTS = 5
DONE_TTL = 7 * 86400
# Safety-net poll (lease recovery only), every 5 min. Upstash bills each redis.call inside a Lua script,
# so one empty CLAIM costs ~3 commands. Normal work is delivered by in-process wake-ups.
POLL_INTERVAL = 300

ENQUEUE = """-- webhook_enqueue
if redis.call('EXISTS', KEYS[3]) == 1 or redis.call('HEXISTS', KEYS[1], ARGV[1]) == 1 then return 0 end
redis.call('HSET', KEYS[1], ARGV[1], ARGV[2])
redis.call('ZADD', KEYS[2], ARGV[3], ARGV[1])
return 1
"""
CLAIM = """-- webhook_claim
local expired = redis.call('ZRANGEBYSCORE', KEYS[3], '-inf', ARGV[1], 'LIMIT', 0, 20)
for _, id in ipairs(expired) do
 redis.call('ZREM', KEYS[3], id)
 redis.call('HDEL', KEYS[4], id)
 redis.call('ZADD', KEYS[2], ARGV[1], id)
end
local ready = redis.call('ZRANGEBYSCORE', KEYS[2], '-inf', ARGV[1], 'LIMIT', 0, 1)
if #ready == 0 then return nil end
local id = ready[1]
redis.call('ZREM', KEYS[2], id)
local attempt = redis.call('HINCRBY', KEYS[5], id, 1)
local payload = redis.call('HGET', KEYS[1], id)
if attempt > tonumber(ARGV[4]) then
 redis.call('LPUSH', KEYS[6], payload)
 redis.call('LTRIM', KEYS[6], 0, 999)
 redis.call('EXPIRE', KEYS[6], ARGV[5])
 redis.call('SET', KEYS[7] .. id, 'dead', 'EX', ARGV[5])
 redis.call('HDEL', KEYS[1], id)
 redis.call('HDEL', KEYS[5], id)
 return nil
end
redis.call('ZADD', KEYS[3], ARGV[2], id)
redis.call('HSET', KEYS[4], id, ARGV[3])
return {id, payload, tostring(attempt)}
"""
FINISH = """-- webhook_finish
if redis.call('HGET', KEYS[4], ARGV[1]) ~= ARGV[2] then return 0 end
redis.call('ZREM', KEYS[3], ARGV[1])
redis.call('HDEL', KEYS[4], ARGV[1])
if ARGV[3] == 'ok' then
 redis.call('SET', KEYS[7], 'done', 'EX', ARGV[5])
 redis.call('HDEL', KEYS[1], ARGV[1])
 redis.call('HDEL', KEYS[5], ARGV[1])
else
 redis.call('ZADD', KEYS[2], ARGV[4], ARGV[1])
end
return 1
"""
RENEW = """-- webhook_renew
if redis.call('HGET', KEYS[1], ARGV[1]) ~= ARGV[2] then return 0 end
return redis.call('ZADD', KEYS[2], 'XX', ARGV[3], ARGV[1])
"""


class WebhookQueue:
    def __init__(self, redis, clock=time.time, lease_seconds=LEASE_SECONDS, poll_interval=POLL_INTERVAL):
        self.redis = redis
        self.clock = clock
        self.lease_seconds = lease_seconds
        self.poll_interval = poll_interval
        # Wake-up tokens: one per enqueued/retry-scheduled update. Idle workers sleep on
        # this in-process queue instead of polling Redis (each poll is a billed command).
        self._tokens = asyncio.Queue(maxsize=1000)

    def _wake(self):
        with contextlib.suppress(asyncio.QueueFull):
            self._tokens.put_nowait(1)

    async def enqueue(self, update):
        identity = str(update['update_id'])
        added = bool(await self.redis.eval(ENQUEUE, 3, KEYS[0], KEYS[1], PREFIX+'done:'+identity,
                                           identity, json.dumps(update, ensure_ascii=False), self.clock()))
        if added:
            self._wake()
        return added

    async def claim(self):
        token = uuid.uuid4().hex
        now = self.clock()
        record = await self.redis.eval(CLAIM, 7, *KEYS, PREFIX+'done:', now,
                                       now+self.lease_seconds, token, MAX_ATTEMPTS, DONE_TTL)
        if not record:
            return None
        identity, payload, attempt = [v.decode() if isinstance(v, bytes) else v for v in record]
        return identity, json.loads(payload), int(attempt), token

    async def finish(self, record, success):
        identity, _, attempt, token = record
        delay = min(2**attempt, 60)
        result = await self.redis.eval(FINISH, 7, *KEYS, PREFIX+'done:'+identity, identity, token,
                                       'ok' if success else 'retry', self.clock()+delay, DONE_TTL)
        if result and not success:
            # Wake a worker exactly when the retry becomes due (no idle polling needed).
            asyncio.get_running_loop().call_later(delay + 0.1, self._wake)
        return result

    async def _heartbeat(self, record, owner):
        try:
            while True:
                await asyncio.sleep(self.lease_seconds/3)
                identity, _, _, token = record
                if not await self.redis.eval(RENEW, 2, KEYS[3], KEYS[2], identity, token,
                                             self.clock()+self.lease_seconds):
                    raise RuntimeError('Webhook queue lease lost')
        except asyncio.CancelledError:
            raise
        except Exception:
            owner.cancel()

    async def process_one(self, process):
        record = await self.claim()
        if record is None:
            return False
        processing = asyncio.create_task(process(record[1]))
        renewal = asyncio.create_task(self._heartbeat(record, processing))
        try:
            try:
                await processing
            except asyncio.CancelledError:
                if asyncio.current_task().cancelling():
                    raise
                # Lease/lock loss cancels only this job; keep the consumer alive.
                await self.finish(record, False)
            except Exception:
                logger.exception('Telegram update %s failed (attempt %s)', record[0], record[2])
                await self.finish(record, False)
            else:
                await self.finish(record, True)
        finally:
            # Cancellation leaves the lease queued for recovery, not acknowledged.
            renewal.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await renewal
        return True

    async def _idle(self, sweeper):
        """Sleep without touching Redis until work is signalled.

        Only the sweeper also wakes every poll_interval to reclaim expired leases
        (e.g. another process died) - one cheap poll instead of one per worker.
        """
        if sweeper:
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(self._tokens.get(), self.poll_interval)
        else:
            await self._tokens.get()

    async def worker(self, process, sweeper=True):
        failures = 0
        while True:
            try:
                if await self.process_one(process):
                    failures = 0
                    continue  # more work may be waiting; claim again immediately
                failures = 0
                await self._idle(sweeper)
            except asyncio.CancelledError:
                raise
            except Exception:
                failures += 1
                delay = min(2 ** failures, 60)
                if failures == 1:
                    logger.exception('Webhook queue worker failed; retrying')
                else:
                    logger.error('Webhook queue worker still failing (%s in a row); retry in %ss', failures, delay)
                await asyncio.sleep(delay + random.random())
