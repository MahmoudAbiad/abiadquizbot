import asyncio
import os
import types
import unittest
from unittest.mock import AsyncMock, Mock

from reliability_support import HTTPError, load_functions
from services.webhook_queue import WebhookQueue, KEYS, MAX_ATTEMPTS


class QueueTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.now=1000
        if os.getenv('TEST_REDIS_URL'):
            from redis.asyncio import Redis
            self.redis=Redis.from_url(os.environ['TEST_REDIS_URL'])
            await self.redis.flushdb()  # Dedicated local CI service only.
        else:
            from lua_redis_double import LuaRedis
            self.redis=LuaRedis();self.redis.clock=lambda:self.now
        self.queue=WebhookQueue(self.redis,clock=lambda:self.now,lease_seconds=10)
    async def asyncTearDown(self):
        if os.getenv('TEST_REDIS_URL'):await self.redis.aclose()
        else:self.redis.close()

    async def test_enqueue_ack_worker_and_duplicate_update_id(self):
        self.assertTrue(await self.queue.enqueue({'update_id':1}))
        self.assertFalse(await self.queue.enqueue({'update_id':1}))
        process=AsyncMock()
        self.assertTrue(await self.queue.process_one(process))
        process.assert_awaited_once_with({'update_id':1})
        self.assertFalse(await self.queue.enqueue({'update_id':1}))
        self.assertFalse(await self.queue.process_one(process))

    async def test_claimed_job_recovers_after_worker_interruption(self):
        await self.queue.enqueue({'update_id':2})
        abandoned=await self.queue.claim()
        self.assertIsNone(await self.queue.claim())
        self.now+=11
        recovered=await self.queue.claim()
        self.assertEqual(recovered[0],abandoned[0])
        self.assertEqual(recovered[2],2)
        self.assertFalse(await self.queue.finish(abandoned,True))
        self.assertTrue(await self.queue.finish(recovered,True))

    async def test_exception_retries_then_poison_stops(self):
        await self.queue.enqueue({'update_id':3})
        process=AsyncMock(side_effect=ValueError('bad update'))
        with self.assertLogs('services.webhook_queue',level='ERROR'):
            for _ in range(MAX_ATTEMPTS):
                self.assertTrue(await self.queue.process_one(process));self.now+=61
        self.assertFalse(await self.queue.process_one(process))
        self.assertFalse(await self.queue.enqueue({'update_id':3}))
        self.assertFalse(await self.queue.process_one(process))
        self.assertEqual(process.await_count,MAX_ATTEMPTS)

    async def test_worker_cancellation_keeps_lease_recoverable(self):
        await self.queue.enqueue({'update_id':4})
        started=asyncio.Event()
        async def process(payload):started.set();await asyncio.Event().wait()
        task=asyncio.create_task(self.queue.process_one(process));await started.wait();task.cancel()
        with self.assertRaises(asyncio.CancelledError):await task
        self.now+=11
        recovered=AsyncMock();await self.queue.process_one(recovered);recovered.assert_awaited_once()

    async def test_long_job_survives_lease_renewals(self):
        # Regression: ZADD XX replies 0 when it only updates a score, so RENEW used to be read
        # as "lease lost" and any job outliving lease_seconds/3 was cancelled mid-flight.
        queue = WebhookQueue(self.redis, clock=lambda: self.now, lease_seconds=0.3)
        await queue.enqueue({'update_id': 20})
        finished = asyncio.Event()
        async def process(payload):
            await asyncio.sleep(0.5)  # several heartbeat ticks (every 0.1s)
            finished.set()
        self.assertTrue(await queue.process_one(process))
        self.assertTrue(finished.is_set())
        self.assertFalse(await queue.enqueue({'update_id': 20}))  # acked as done, not retried

    async def test_redis_failed_enqueue_never_acknowledged(self):
        queue=types.SimpleNamespace(enqueue=AsyncMock(side_effect=ConnectionError('unavailable')))
        update=types.SimpleNamespace(model_dump=lambda **kw:{'update_id':5})
        env=load_functions('webhook_server.py',{'webhook'}, TELEGRAM_WEBHOOK_SECRET='secret',
            hmac=__import__('hmac'), HTTPException=HTTPError,logger=Mock(),webhook_queue=queue,
            Update=types.SimpleNamespace(model_validate=lambda payload:update))
        request=types.SimpleNamespace(headers={'x-telegram-bot-api-secret-token':'secret'},json=AsyncMock(return_value={'update_id':5}))
        with self.assertRaises(HTTPError) as exc:await env['webhook'](request)
        self.assertEqual(exc.exception.status_code,503)
        queue.enqueue.side_effect=None
        self.assertEqual(await env['webhook'](request),{'ok':True})

    async def test_duplicate_concurrent_claims(self):
        await self.queue.enqueue({'update_id':6})
        records=await asyncio.gather(*(self.queue.claim() for _ in range(10)))
        self.assertEqual(sum(r is not None for r in records),1)

    async def test_idle_workers_do_not_poll_redis(self):
        calls = 0
        real_eval = self.redis.eval
        async def counting_eval(*a, **kw):
            nonlocal calls
            calls += 1
            return await real_eval(*a, **kw)
        self.redis.eval = counting_eval
        self.queue.poll_interval = 3600
        process = AsyncMock()
        workers = [asyncio.create_task(self.queue.worker(process, sweeper=(i == 0))) for i in range(15)]
        await asyncio.sleep(0.3)
        # One initial claim per worker at startup, then total silence while idle.
        self.assertEqual(calls, 15)
        await asyncio.sleep(1.2)
        self.assertEqual(calls, 15)
        # An enqueue wakes a worker without any polling interval elapsing.
        await self.queue.enqueue({'update_id': 7})
        await asyncio.sleep(0.3)
        process.assert_awaited_once_with({'update_id': 7})
        for w in workers:
            w.cancel()
        await asyncio.gather(*workers, return_exceptions=True)

    async def test_worker_backs_off_when_redis_fails(self):
        self.redis.eval = AsyncMock(side_effect=RuntimeError('max requests limit exceeded'))
        with self.assertLogs('services.webhook_queue', level='ERROR'):
            task = asyncio.create_task(self.queue.worker(AsyncMock()))
            await asyncio.sleep(0.5)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        # Exponential backoff (2s, 4s, ...), not a tight retry loop.
        self.assertEqual(self.redis.eval.await_count, 1)

