"""Phase 1: the message blocked by the consent gate is replayed after «أوافق»."""
import json
import types
import unittest
from unittest.mock import AsyncMock, Mock, patch

from reliability_support import load_functions

NAMES = {
    "_pending_key", "_is_start_command", "remember_pending_message",
    "clear_pending_messages", "pop_pending_messages", "replay_pending_messages",
}


class FakeMessage:
    def __init__(self, uid=1, text=None, group=None, mid=1, **extra):
        self.from_user = types.SimpleNamespace(id=uid)
        self.text = text
        self.media_group_id = group
        self.mid = mid
        self.extra = extra

    def model_dump_json(self, exclude_none=True):
        return json.dumps({"uid": self.from_user.id, "text": self.text,
                           "media_group_id": self.media_group_id, "mid": self.mid})

    @classmethod
    def model_validate_json(cls, raw):
        d = json.loads(raw)
        return cls(d["uid"], d["text"], d["media_group_id"], d["mid"])

    def as_(self, bot):
        self.bot = bot
        return self


class FakePipe:
    def __init__(self, redis):
        self.redis, self.ops = redis, []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    def lrange(self, k, a, b):
        self.ops.append(("lrange", k))

    def delete(self, k):
        self.ops.append(("delete", k))

    async def execute(self):
        out = []
        for op, k in self.ops:
            out.append(list(self.redis.lists.get(k, [])) if op == "lrange" else self.redis.lists.pop(k, None))
        return out


class FakeRedis:
    def __init__(self):
        self.lists, self.ttls = {}, {}

    async def lindex(self, k, i):
        l = self.lists.get(k)
        return l[i] if l else None

    async def llen(self, k):
        return len(self.lists.get(k, []))

    async def delete(self, k):
        self.lists.pop(k, None)

    async def rpush(self, k, v):
        self.lists.setdefault(k, []).append(v)

    async def expire(self, k, ttl):
        self.ttls[k] = ttl

    def pipeline(self, transaction=True):
        return FakePipe(self)


def build(redis, dp=None):
    env = load_functions(
        "services/consent.py", NAMES,
        Message=FakeMessage, Any=object, List=list, json=json, logger=Mock(),
        _redis=lambda: redis, _PENDING_TTL=3600, _PENDING_MAX_ALBUM=3,
        asyncio=__import__("asyncio"), time=__import__("time"),
        RESUME_FLAG="consent_resumed", build_resume_failed_text=lambda: "failed",
    )
    return env


class ConsentResumeTests(unittest.IsolatedAsyncioTestCase):
    async def test_blocked_message_is_stored_and_popped_in_order(self):
        r = FakeRedis(); env = build(r)
        await env["remember_pending_message"](FakeMessage(1, "hello", mid=5))
        self.assertEqual(r.ttls["consent:pending:1"], 3600)
        msgs = await env["pop_pending_messages"](1, bot="BOT")
        self.assertEqual([m.text for m in msgs], ["hello"])
        self.assertEqual(msgs[0].bot, "BOT")
        self.assertEqual(await env["pop_pending_messages"](1), [])  # popped once only

    async def test_latest_single_message_replaces_previous(self):
        r = FakeRedis(); env = build(r)
        await env["remember_pending_message"](FakeMessage(1, "a"))
        await env["remember_pending_message"](FakeMessage(1, "b"))
        self.assertEqual([m.text for m in await env["pop_pending_messages"](1)], ["b"])

    async def test_album_parts_are_kept_together_and_capped(self):
        r = FakeRedis(); env = build(r)
        for i in range(5):
            await env["remember_pending_message"](FakeMessage(1, None, group="g1", mid=i))
        msgs = await env["pop_pending_messages"](1)
        self.assertEqual([m.mid for m in msgs], [0, 1, 2])  # cap = 3 in this test

    async def test_new_album_replaces_old_album(self):
        r = FakeRedis(); env = build(r)
        await env["remember_pending_message"](FakeMessage(1, None, group="g1", mid=1))
        await env["remember_pending_message"](FakeMessage(1, None, group="g2", mid=2))
        self.assertEqual([m.mid for m in await env["pop_pending_messages"](1)], [2])

    async def test_start_command_is_not_stored(self):
        r = FakeRedis(); env = build(r)
        await env["remember_pending_message"](FakeMessage(1, "/start"))
        await env["remember_pending_message"](FakeMessage(1, "/start@abiad_bot gq_x"))
        self.assertEqual(r.lists, {})

    async def test_other_commands_are_stored(self):
        r = FakeRedis(); env = build(r)
        await env["remember_pending_message"](FakeMessage(1, "/help"))
        self.assertEqual(len(r.lists["consent:pending:1"]), 1)

    async def test_messages_of_other_user_are_dropped_on_pop(self):
        r = FakeRedis(); env = build(r)
        r.lists["consent:pending:1"] = [FakeMessage(2, "x").model_dump_json()]
        self.assertEqual(await env["pop_pending_messages"](1), [])

    async def test_clear_removes_pending(self):
        r = FakeRedis(); env = build(r)
        await env["remember_pending_message"](FakeMessage(1, "a"))
        await env["clear_pending_messages"](1)
        self.assertEqual(r.lists, {})

    async def test_redis_failure_does_not_break_gate(self):
        redis = Mock(); redis.lindex = AsyncMock(side_effect=ConnectionError())
        redis.delete = AsyncMock(side_effect=ConnectionError())
        env = build(redis)
        await env["remember_pending_message"](FakeMessage(1, "a"))  # must not raise

    async def test_replay_feeds_each_message_to_dispatcher(self):
        r = FakeRedis(); env = build(r)
        dp = types.SimpleNamespace(feed_update=AsyncMock())

        class Update:
            def __init__(self, update_id, message):
                self.update_id, self.message = update_id, message

        aiogram_types = types.SimpleNamespace(Update=Update)
        with patch.dict("sys.modules", {"aiogram.types": aiogram_types, "config": types.SimpleNamespace(dp=dp)}):
            n = await env["replay_pending_messages"]("BOT", [FakeMessage(1, "a"), FakeMessage(1, "b")])
        self.assertEqual(n, 2)
        self.assertEqual([c.args[1].message.text for c in dp.feed_update.await_args_list], ["a", "b"])

    async def test_replay_continues_after_one_failure(self):
        r = FakeRedis(); env = build(r)
        dp = types.SimpleNamespace(feed_update=AsyncMock(side_effect=[RuntimeError("x"), None]))
        Update = lambda update_id, message: types.SimpleNamespace(update_id=update_id, message=message)
        with patch.dict("sys.modules", {"aiogram.types": types.SimpleNamespace(Update=Update),
                                        "config": types.SimpleNamespace(dp=dp)}):
            n = await env["replay_pending_messages"]("BOT", [FakeMessage(1, "a"), FakeMessage(1, "b")])
        self.assertEqual(n, 1)


if __name__ == "__main__":
    unittest.main()



