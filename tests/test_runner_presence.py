"""Execute the real presence Lua, including multi-window and stale-request cases."""
import asyncio
import os
import sys
import types
import unittest
from unittest.mock import AsyncMock, patch
from uuid import uuid4
from fastapi import HTTPException
from services import runner_api as api
from services.runner_presence import presence
import test_runner_api as fixtures


class PresenceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.env = patch.dict(os.environ, {'SUPABASE_URL': 'presence-test-' + uuid4().hex})
        self.env.start()
        if os.getenv('TEST_REDIS_URL'):
            from redis.asyncio import Redis
            self.redis = Redis.from_url(os.environ['TEST_REDIS_URL'])
        else:
            import fakeredis.aioredis
            self.redis = fakeredis.aioredis.FakeRedis()

    async def asyncTearDown(self):
        from hashlib import sha256
        namespace = sha256(os.environ['SUPABASE_URL'].encode()).hexdigest()[:16]
        keys = await self.redis.keys(f'runner:presence:{{{namespace}}}*')
        if keys:
            await self.redis.delete(*keys)
        await self.redis.aclose()
        self.env.stop()

    async def test_dedup_across_windows_and_quizzes_and_independent_departures(self):
        self.assertEqual(await presence(self.redis, 1), 0)
        self.assertEqual(await presence(self.redis, 1, 'a', 1, True), 1)
        self.assertEqual(await presence(self.redis, 1, 'b', 1, True), 1)
        self.assertEqual(await presence(self.redis, 2, 'a', 1, True), 2)
        self.assertEqual(await presence(self.redis, 1, 'a', 2, False), 2)
        self.assertEqual(await presence(self.redis, 1, 'b', 2, False), 1)
        self.assertEqual(await presence(self.redis, 2, 'a', 2, False), 0)

    async def test_delayed_heartbeat_cannot_revive_departed_window(self):
        await presence(self.redis, 1, 'a', 1, True)
        await presence(self.redis, 1, 'a', 3, False)
        self.assertEqual(await presence(self.redis, 1, 'a', 2, True), 0)
        self.assertEqual(await presence(self.redis, 1, 'a', 4, True), 1)

    async def test_expiry_and_environment_isolation(self):
        await presence(self.redis, 1, 'a', 1, True)
        with patch.dict('os.environ', {'SUPABASE_URL': 'https://another-environment.invalid'}):
            self.assertEqual(await presence(self.redis, 2), 0)
        # Simulate Redis advancing beyond the liveness deadline without a 45s sleep.
        from hashlib import sha256
        prefix = 'runner:presence:{' + sha256(os.environ['SUPABASE_URL'].encode()).hexdigest()[:16] + '}'
        for key in await self.redis.keys(prefix + '*'):
            if await self.redis.type(key) == b'zset':
                for member in await self.redis.zrange(key, 0, -1):
                    await self.redis.zadd(key, {member: 0})
        self.assertEqual(await presence(self.redis, 2), 0)
        self.assertEqual(await presence(self.redis, 1, 'b', 1, True), 1)
        for key in await self.redis.keys(prefix + '*'):
            self.assertGreater(await self.redis.ttl(key), 0)

    async def test_concurrent_accounts_and_sessions(self):
        await asyncio.gather(*(presence(self.redis, i % 10, str(i), 1, True) for i in range(50)))
        self.assertEqual(await presence(self.redis, 99), 10)


class PresenceEndpointTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.fixture = fixtures.APITests()
        self.fixture.setUp()
        self.redis = types.SimpleNamespace(eval=AsyncMock(return_value=3))
        self.patch = patch.dict(sys.modules, {'config': types.SimpleNamespace(redis_client=self.redis)})
        self.patch.start()

    def tearDown(self):
        self.patch.stop()
        self.fixture.tearDown()

    def request(self, **extra):
        return api.PresenceRequest(init_data='valid', quiz_id=self.fixture.qid,
                                   session_id=uuid4(), **extra)

    async def test_active_requires_owned_running_unexpired_round(self):
        req = self.request(active=True, round_id=self.fixture.rid)
        self.assertEqual(await api.online_presence(req), {'online': 3})
        for field, value in [('user_id', 8), ('quiz_id', str(uuid4())),
                             ('status', 'completed'), ('expires_at', '2000-01-01T00:00:00Z')]:
            old = self.fixture.row[field]
            self.fixture.row[field] = value
            self.redis.eval.reset_mock()
            with self.assertRaises(HTTPException):
                await api.online_presence(req)
            self.redis.eval.assert_not_awaited()
            self.fixture.row[field] = old

    async def test_home_and_departure_need_no_round_or_database(self):
        with patch.object(api, 'db', side_effect=AssertionError('unexpected DB query')):
            self.assertEqual(await api.online_presence(self.request()), {'online': 3})
            self.assertEqual(await api.online_presence(api.PresenceRequest(
                init_data='valid', quiz_id=self.fixture.qid)), {'online': 3})

    async def test_failure_does_not_return_a_fictitious_zero(self):
        self.redis.eval.side_effect = ConnectionError('offline')
        with self.assertRaises(HTTPException) as error:
            await api.online_presence(self.request())
        self.assertEqual(error.exception.status_code, 503)
