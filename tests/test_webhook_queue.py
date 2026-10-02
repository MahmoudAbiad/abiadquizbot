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
