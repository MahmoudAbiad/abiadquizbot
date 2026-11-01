import asyncio
import unittest
from unittest.mock import AsyncMock, patch

from services import generation_heartbeat as hb
from services.generation_eta import GenerationEta
from services.generation_heartbeat import next_interval, run_processing_heartbeat

ETA = GenerationEta(typical=20, slow=60, ceiling=240)


class FakeTime:
    """Fake clock whose sleep advances time and stops the loop at ``limit``."""

    def __init__(self, limit):
        self.now = 0.0
        self.limit = limit
        self.sleeps = []

    def clock(self):
        return self.now

    async def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds
        if self.now >= self.limit:
            raise asyncio.CancelledError()
        await asyncio.sleep(0)


class Message:
    def __init__(self, fake, side_effect=None):
        self.fake = fake
        self.calls = []  # (time, text)
        self.side_effect = list(side_effect or [])

    async def edit_text(self, text):
        if self.side_effect:
            effect = self.side_effect.pop(0)
            if effect is not None:
                raise effect
        self.calls.append((self.fake.now, text))


class IntervalTests(unittest.TestCase):
    def test_fast_before_typical_then_slow(self):
        self.assertEqual(next_interval(0, ETA), 5)
        self.assertEqual(next_interval(19.9, ETA), 5)
        self.assertEqual(next_interval(20, ETA), 15)
        self.assertEqual(next_interval(500, ETA), 15)

    def test_without_estimate_keeps_the_old_twenty_seconds(self):
        self.assertEqual(next_interval(0, None), 20)
        self.assertEqual(next_interval(300, None), 20)


class HeartbeatTests(unittest.IsolatedAsyncioTestCase):
    async def run_hb(self, eta, limit, side_effect=None):
        fake = FakeTime(limit)
        msg = Message(fake, side_effect)
        await run_processing_heartbeat(msg, eta, clock=fake.clock, sleep=fake.sleep)
        return fake, msg

    async def test_updates_every_five_seconds_then_every_fifteen(self):
        fake, msg = await self.run_hb(ETA, limit=70)
        self.assertEqual([t for t, _ in msg.calls], [5, 10, 15, 20, 35, 50, 65])

    async def test_without_estimate_updates_every_twenty_seconds_with_old_wording(self):
        fake, msg = await self.run_hb(None, limit=65)
        self.assertEqual([t for t, _ in msg.calls], [20, 40, 60])
        self.assertIn("لسا عم نعالج المستند", msg.calls[0][1])

    async def test_text_follows_the_phases(self):
        fake, msg = await self.run_hb(ETA, limit=70)
        texts = dict(msg.calls)
        self.assertIn("المتبقي", texts[5])
        self.assertIn("أكتر من المعتاد", texts[20])
        self.assertIn("لسا عم نولّد", texts[65])

    async def test_identical_text_is_not_sent_twice(self):
        with patch.object(hb, "build_processing_text", return_value="same"):
            fake, msg = await self.run_hb(ETA, limit=40)
        self.assertEqual(len(msg.calls), 1)

    async def test_failed_edit_is_retried_on_the_next_tick(self):
        fake, msg = await self.run_hb(ETA, limit=20, side_effect=[ValueError("deleted"), None])
        self.assertEqual([t for t, _ in msg.calls], [10, 15])

    async def test_flood_limit_waits_the_requested_time(self):
        class Flood(Exception):
            retry_after = 7

        fake, msg = await self.run_hb(ETA, limit=20, side_effect=[Flood()])
        self.assertEqual(fake.sleeps[:3], [5, 7, 5])

    async def test_flood_wait_is_capped(self):
        class Flood(Exception):
            retry_after = 9999

        fake, msg = await self.run_hb(ETA, limit=50, side_effect=[Flood()])
        self.assertEqual(fake.sleeps[1], hb.MAX_RETRY_AFTER_SECONDS)

    async def test_broken_wording_never_raises(self):
        with patch.object(hb, "build_processing_text", side_effect=RuntimeError("boom")):
            fake, msg = await self.run_hb(ETA, limit=30)
        self.assertEqual(msg.calls, [])

    async def test_cancel_stops_it_quietly(self):
        message = type("M", (), {"edit_text": AsyncMock()})()
        task = asyncio.create_task(run_processing_heartbeat(message, ETA))
        await asyncio.sleep(0)
        task.cancel()
        await task  # must not raise CancelledError
        self.assertTrue(task.done())


if __name__ == "__main__":
    unittest.main()
