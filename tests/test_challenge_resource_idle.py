"""Resource-saving challenge-worker regressions (no real Supabase/Upstash calls)."""
import asyncio
import datetime
import unittest
from collections import OrderedDict
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from cachetools import TTLCache
from reliability_support import load_functions


class FakeRedis:
    def __init__(self):
        self.values = {}
        self.get_calls = 0
        self.set_calls = 0

    async def get(self, key):
        self.get_calls += 1
        return self.values.get(key)

    async def set(self, key, value, ex):
        self.set_calls += 1
        self.values[key] = value


class FlagTable:
    def __init__(self, db):
        self.db, self.op, self.key, self.values = db, None, None, None

    def select(self, *args):
        self.op = "select"
        return self

    def eq(self, name, key):
        self.key = key
        return self

    def limit(self, n):
        return self

    def upsert(self, values):
        self.op, self.values = "upsert", values
        return self

    async def execute(self):
        if self.op == "upsert":
            if self.db.fail_writes:
                raise ConnectionError("database unavailable")
            self.db.data[self.values["key"]] = self.values["enabled"]
            self.db.writes += 1
            return SimpleNamespace(data=[self.values])
        self.db.reads += 1
        return SimpleNamespace(
            data=[{"enabled": self.db.data[self.key]}]
            if self.key in self.db.data else []
        )


class FakeFlagsDB:
    def __init__(self):
        self.data, self.reads, self.writes, self.fail_writes = {}, 0, 0, False

    def table(self, name):
        assert name == "feature_flags"
        return FlagTable(self)


class FeatureFlagCacheTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.redis, self.db = FakeRedis(), FakeFlagsDB()
        self.cache = TTLCache(maxsize=128, ttl=30)
        self.f = load_functions(
            "helpers/supabase_helper/feature_flags.py",
            {"is_feature_enabled", "set_feature_flag", "_flag_redis_key", "_decode_flag_value"},
            redis_client=self.redis, supabase=self.db,
            _FEATURE_FLAG_LOCAL_CACHE=self.cache, _FEATURE_FLAG_CACHE_EPOCH=0,
            _GAME_FLAGS={"challenge_system_enabled", "live_matches_enabled", "spectator_mode_enabled"},
            FEATURE_FLAG_CACHE_TTL_SECONDS=30,
            datetime=datetime, logger=Mock(), log_error=Mock(),
        )
        self.flag = self.f["is_feature_enabled"]
        self.set_flag = self.f["set_feature_flag"]

    async def test_repeated_reads_do_not_touch_redis_or_supabase(self):
        self.db.data["challenge_system_enabled"] = True
        self.assertTrue(await self.flag("challenge_system_enabled", default=False))
        self.assertTrue(await self.flag("challenge_system_enabled", default=False))
        self.assertEqual(self.db.reads, 1)
        self.assertEqual(self.redis.get_calls, 1)
        self.assertEqual(self.redis.set_calls, 1)
        self.assertEqual(self.cache.ttl, 30)

    async def test_admin_toggle_invalidates_immediately(self):
        self.db.data["challenge_system_enabled"] = True
        self.assertTrue(await self.flag("challenge_system_enabled"))
        self.assertIn("challenge_system_enabled", self.cache)
        self.assertTrue(await self.set_flag("challenge_system_enabled", False))
        self.assertNotIn("challenge_system_enabled", self.cache)
        self.assertFalse(await self.flag("challenge_system_enabled"))
        self.assertEqual(self.db.writes, 1)
        self.assertEqual(self.redis.get_calls, 2)
        self.assertEqual(self.db.reads, 1)

    async def test_failed_admin_write_keeps_existing_cache(self):
        self.db.data["spectator_mode_enabled"] = True
        self.assertTrue(await self.flag("spectator_mode_enabled"))
        self.db.fail_writes = True
        self.assertFalse(await self.set_flag("spectator_mode_enabled", False))
        self.assertTrue(await self.flag("spectator_mode_enabled"))
        self.assertEqual(self.redis.get_calls, 1)

    async def test_disabled_game_flag_missing_row_defaults_off(self):
        self.assertFalse(await self.flag("challenge_system_enabled", default=True))
        self.assertEqual(self.db.reads, 1)


class GameHeartbeatTests(unittest.IsolatedAsyncioTestCase):
    def setup_loop(self, flags, has_active):
        self.calls = []
        async def sleep(seconds):
            self.calls.append(seconds)
            if seconds == 60:
                raise asyncio.CancelledError
            if len(self.calls) > 2:
                raise asyncio.CancelledError

        self.repo = SimpleNamespace(
            has_active_attacks=AsyncMock(return_value=has_active),
            due_attacks=AsyncMock(return_value=[]),
            claim_notifications=AsyncMock(return_value=[]),
        )
        self.flag = AsyncMock(side_effect=flags)
        fake_asyncio = SimpleNamespace(sleep=sleep, CancelledError=asyncio.CancelledError)
        return load_functions(
            "handlers/challenge_game.py", {"challenge_heartbeat_loop"},
            repo=self.repo, is_feature_enabled=self.flag,
            asyncio=fake_asyncio, dt=datetime, logger=Mock(),
        )["challenge_heartbeat_loop"]

    async def test_disabling_an_already_running_worker_rechecks_flag(self):
        loop = self.setup_loop([True, False], False)
        with self.assertRaises(asyncio.CancelledError):
            await loop(None)
        self.assertEqual(self.calls, [3, 60])
        self.assertEqual(self.flag.await_count, 2)
        self.assertEqual(self.repo.due_attacks.await_count, 1)
        self.assertEqual(self.repo.claim_notifications.await_count, 1)

    async def test_disabled_but_in_flight_attack_continues(self):
        loop = self.setup_loop([False, False], True)
        with self.assertRaises(asyncio.CancelledError):
            await loop(None)
        self.assertEqual(self.repo.due_attacks.await_count, 3)
        self.assertEqual(self.repo.claim_notifications.await_count, 3)
        self.assertEqual(self.calls, [3, 3, 3])

    async def test_disabled_and_drained_skips_expensive_queries(self):
        loop = self.setup_loop([False], False)
        with self.assertRaises(asyncio.CancelledError):
            await loop(None)
        self.repo.has_active_attacks.assert_awaited_once()
        self.repo.due_attacks.assert_not_awaited()
        self.repo.claim_notifications.assert_not_awaited()
        self.assertEqual(self.calls, [60])


class LiveHeartbeatTests(unittest.IsolatedAsyncioTestCase):
    def setup_loop(self, *, enabled, has_work, seen=None):
        self.delays = []

        async def wait_for_work(delay):
            self.delays.append(delay)
            raise asyncio.CancelledError

        self.live = SimpleNamespace(has_live_work=AsyncMock(return_value=has_work))
        self.live_open = AsyncMock(return_value=enabled)
        self.beat = AsyncMock(return_value=2)
        return load_functions(
            "handlers/challenge_live.py", {"challenge_live_loop"},
            live=self.live, live_open=self.live_open, live_beat=self.beat,
            _seen=set(seen or []), wait_for_work=wait_for_work,
            asyncio=asyncio, log_failure=Mock(), logger=Mock(),
        )["challenge_live_loop"]

    async def test_disabled_and_empty_checks_database_once_then_sleeps_60s(self):
        loop = self.setup_loop(enabled=False, has_work=False)
        with self.assertRaises(asyncio.CancelledError):
            await loop(None)
        self.assertEqual(self.delays, [60])
        self.live.has_live_work.assert_awaited_once()
        self.beat.assert_not_awaited()

    async def test_disabled_with_durable_match_still_beats(self):
        loop = self.setup_loop(enabled=False, has_work=True)
        with self.assertRaises(asyncio.CancelledError):
            await loop(None)
        self.beat.assert_awaited_once()
        self.assertEqual(self.delays, [2])

    async def test_seen_match_renders_final_frame_without_flag_or_queue_query(self):
        loop = self.setup_loop(enabled=False, has_work=False, seen={"match-id"})
        with self.assertRaises(asyncio.CancelledError):
            await loop(None)
        self.beat.assert_awaited_once()
        self.live_open.assert_not_awaited()
        self.live.has_live_work.assert_not_awaited()

    async def test_enabled_idle_keeps_normal_polling_interval(self):
        loop = self.setup_loop(enabled=True, has_work=False)
        with self.assertRaises(asyncio.CancelledError):
            await loop(None)
        self.assertEqual(self.delays, [2])


class SpectatorHeartbeatTests(unittest.IsolatedAsyncioTestCase):
    def setup_loop(self, *, enabled, pending=None):
        self.pending = OrderedDict((mid, None) for mid in (pending or []))
        self.flag = AsyncMock(return_value=enabled)
        self.beat = AsyncMock()
        self.delays = []

        async def wait_for(awaitable, timeout):
            awaitable.close()  # avoid leaving the Event.wait coroutine unawaited
            self.delays.append(timeout)
            raise asyncio.CancelledError

        fake_asyncio = SimpleNamespace(
            wait_for=wait_for, CancelledError=asyncio.CancelledError,
            TimeoutError=asyncio.TimeoutError,
        )
        return load_functions(
            "handlers/challenge_spectator.py", {"challenge_spectator_loop"},
            beat=self.beat, _pending=self.pending, _wake=asyncio.Event(),
            is_feature_enabled=self.flag, asyncio=fake_asyncio, logger=Mock(),
        )["challenge_spectator_loop"]

    async def test_disabled_and_no_events_skips_redis_lua_batch(self):
        loop = self.setup_loop(enabled=False)
        with self.assertRaises(asyncio.CancelledError):
            await loop(None)
        self.beat.assert_not_awaited()
        self.assertEqual(self.delays, [60])

    async def test_events_are_processed_even_if_spectator_flag_is_off(self):
        loop = self.setup_loop(enabled=False, pending=["match-id"])
        with self.assertRaises(asyncio.CancelledError):
            await loop(None)
        self.beat.assert_awaited_once()
        self.flag.assert_not_awaited()
        self.assertEqual(self.delays, [3])

    async def test_enabled_mode_keeps_three_second_beat(self):
        loop = self.setup_loop(enabled=True)
        with self.assertRaises(asyncio.CancelledError):
            await loop(None)
        self.beat.assert_awaited_once()
        self.assertEqual(self.delays, [3])


if __name__ == "__main__":
    unittest.main()
