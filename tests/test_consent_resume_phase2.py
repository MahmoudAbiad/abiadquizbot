"""Phase 2 (first half): pending callbacks, replay flag, throttle exemption, failure notice."""
import asyncio
import json
import types
import unittest
from unittest.mock import AsyncMock, Mock, patch

from reliability_support import load_functions
from test_consent_resume import FakeMessage, FakeRedis as _BaseRedis

CB_NAMES = {
    "_pending_key", "_pending_cb_key", "_is_start_command", "remember_pending_message",
    "remember_pending_callback", "pop_pending_callback", "clear_pending_callback",
    "pop_pending_messages", "replay_pending_messages", "build_resume_failed_text",
    "build_accepted_text",
}


class FakeRedis(_BaseRedis):
    """Adds string values (set/get) and get-in-pipeline on top of the phase 1 fake."""

    def __init__(self):
        super().__init__()
        self.strings = {}

    async def set(self, k, v, ex=None, nx=False):
        self.strings[k] = v
        self.ttls[k] = ex
        return True

    def pipeline(self, transaction=True):
        redis = self

        class Pipe:
            def __init__(self):
                self.ops = []

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

            def get(self, k):
                self.ops.append(("get", k))

            def lrange(self, k, a, b):
                self.ops.append(("lrange", k))

            def delete(self, k):
                self.ops.append(("delete", k))

            async def execute(self):
                out = []
                for op, k in self.ops:
                    if op == "get":
                        out.append(redis.strings.get(k))
                    elif op == "lrange":
                        out.append(list(redis.lists.get(k, [])))
                    else:
                        redis.lists.pop(k, None)
                        redis.strings.pop(k, None)
                        out.append(1)
                return out

        return Pipe()

    async def delete(self, k):
        self.lists.pop(k, None)
        self.strings.pop(k, None)


def build(redis):
    return load_functions(
        "services/consent.py", CB_NAMES,
        Message=FakeMessage, CallbackQuery=object, Any=object, List=list, json=json,
        logger=Mock(), _redis=lambda: redis, _PENDING_TTL=3600, _PENDING_MAX_ALBUM=3,
        RESUME_FLAG="consent_resumed", asyncio=asyncio, time=__import__("time"),
    )


def fake_call(uid=1, data="menu:x", mid=7, chat=1):
    return types.SimpleNamespace(
        from_user=types.SimpleNamespace(id=uid), data=data,
        message=types.SimpleNamespace(message_id=mid, chat=types.SimpleNamespace(id=chat)),
    )


class PendingCallbackTests(unittest.IsolatedAsyncioTestCase):
    async def test_callback_is_stored_minimally_and_popped_once(self):
        r = FakeRedis(); env = build(r)
        await env["remember_pending_callback"](fake_call())
        self.assertEqual(r.ttls["consent:pending_cb:1"], 3600)
        self.assertEqual(await env["pop_pending_callback"](1),
                         {"data": "menu:x", "message_id": 7, "chat_id": 1})
        self.assertIsNone(await env["pop_pending_callback"](1))

    async def test_concurrent_pops_only_one_wins(self):
        r = FakeRedis(); env = build(r)
        await env["remember_pending_callback"](fake_call())
        results = await asyncio.gather(*(env["pop_pending_callback"](1) for _ in range(5)))
        self.assertEqual(len([x for x in results if x]), 1)

    async def test_latest_intent_wins_message_then_callback(self):
        r = FakeRedis(); env = build(r)
        await env["remember_pending_message"](FakeMessage(1, "file"))
        await env["remember_pending_callback"](fake_call())
        self.assertEqual(await env["pop_pending_messages"](1), [])
        self.assertIsNotNone(await env["pop_pending_callback"](1))

    async def test_latest_intent_wins_callback_then_message(self):
        r = FakeRedis(); env = build(r)
        await env["remember_pending_callback"](fake_call())
        await env["remember_pending_message"](FakeMessage(1, "file"))
        self.assertIsNone(await env["pop_pending_callback"](1))
        self.assertEqual(len(await env["pop_pending_messages"](1)), 1)

    async def test_clear_and_redis_failures_do_not_raise(self):
        r = FakeRedis(); env = build(r)
        await env["remember_pending_callback"](fake_call())
        await env["clear_pending_callback"](1)
        self.assertIsNone(await env["pop_pending_callback"](1))
        bad = Mock(); bad.set = AsyncMock(side_effect=ConnectionError()); bad.pipeline = Mock(side_effect=ConnectionError())
        bad.delete = AsyncMock(side_effect=ConnectionError())
        env = build(bad)
        await env["remember_pending_callback"](fake_call())
        self.assertIsNone(await env["pop_pending_callback"](1))
        await env["clear_pending_callback"](1)

    async def test_accepted_text_variants(self):
        env = build(FakeRedis())
        self.assertIn("متابعة طلبك السابق", env["build_accepted_text"](resuming=True))
        self.assertIn("اضغط الزر", env["build_accepted_text"](callback_pending=True))
        self.assertIn("/start", env["build_accepted_text"]())


class ReplayTests(unittest.IsolatedAsyncioTestCase):
    def _run(self, env, feed, bot, msgs):
        Update = lambda update_id, message: types.SimpleNamespace(update_id=update_id, message=message)
        dp = types.SimpleNamespace(feed_update=feed)
        return patch.dict("sys.modules", {"aiogram.types": types.SimpleNamespace(Update=Update),
                                          "config": types.SimpleNamespace(dp=dp)})

    async def test_replay_passes_resume_flag(self):
        env = build(FakeRedis()); feed = AsyncMock(); bot = types.SimpleNamespace(send_message=AsyncMock())
        with self._run(env, feed, bot, None):
            await env["replay_pending_messages"](bot, [FakeMessage(1, "a")])
        self.assertEqual(feed.await_args.kwargs, {"consent_resumed": True})
        bot.send_message.assert_not_awaited()

    async def test_failure_notifies_user_once(self):
        env = build(FakeRedis())
        feed = AsyncMock(side_effect=[RuntimeError("x"), RuntimeError("y"), None])
        bot = types.SimpleNamespace(send_message=AsyncMock())
        with self._run(env, feed, bot, None):
            n = await env["replay_pending_messages"](bot, [FakeMessage(1, "a"), FakeMessage(1, "b"), FakeMessage(1, "c")])
        self.assertEqual(n, 1)
        bot.send_message.assert_awaited_once()
        self.assertEqual(bot.send_message.await_args.args[0], 1)

    async def test_notice_failure_is_swallowed(self):
        env = build(FakeRedis()); feed = AsyncMock(side_effect=RuntimeError("x"))
        bot = types.SimpleNamespace(send_message=AsyncMock(side_effect=RuntimeError("blocked")))
        with self._run(env, feed, bot, None):
            self.assertEqual(await env["replay_pending_messages"](bot, [FakeMessage(1, "a")]), 0)


class MessageStub:
    def __init__(self, uid=1):
        self.from_user = types.SimpleNamespace(id=uid)
        self.media_group_id = None
        self.text = "t"; self.caption = None; self.content_type = "text"
        self.answer = AsyncMock()


class MiddlewareFlagTests(unittest.IsolatedAsyncioTestCase):
    def _throttle(self):
        env = load_functions("middlewares.py", {"ThrottlingMiddleware"}, BaseMiddleware=object,
                             TTLCache=lambda **kw: {}, Message=MessageStub,
                             CallbackQuery=type("CallbackQuery", (), {}), ADMIN_ID=99, logger=Mock())
        return env["ThrottlingMiddleware"]()

    async def test_resumed_message_bypasses_throttle_even_right_after_original(self):
        mw = self._throttle(); event = MessageStub()
        first = AsyncMock(); await mw(first, event, {})            # الرسالة الأصلية سجّلت الكاش
        blocked = AsyncMock(); await mw(blocked, event, {})        # نسخة عادية تُحجب
        blocked.assert_not_awaited()
        resumed = AsyncMock(); await mw(resumed, event, {"consent_resumed": True})
        resumed.assert_awaited_once()

    async def test_resumed_error_skips_generic_notice_but_still_raises_and_logs(self):
        scheduled = []; log_error_event = AsyncMock()
        env = load_functions("middlewares.py", {"ErrorTrackingMiddleware"}, BaseMiddleware=object,
                             Message=MessageStub, CallbackQuery=type("CallbackQuery", (), {}),
                             ADMIN_ID=99, logger=Mock(), spawn=scheduled.append,
                             _UNHANDLED_CALLBACK_NOTICE="c", _UNHANDLED_MESSAGE_NOTICE="m",
                             set_error_context=Mock(return_value="t"), reset_error_context=Mock())
        msg = MessageStub()
        update = types.SimpleNamespace(message=msg, callback_query=None, poll_answer=None,
                                       edited_message=None, my_chat_member=None, event_type="message", update_id=3)
        with patch.dict("sys.modules", {"helpers.supabase_helper": types.SimpleNamespace(log_error_event=log_error_event)}):
            with self.assertRaises(ConnectionError):
                await env["ErrorTrackingMiddleware"]()(AsyncMock(side_effect=ConnectionError("boom")), update,
                                                       {"consent_resumed": True})
            self.assertEqual(len(scheduled), 1)
            await scheduled[0]
        msg.answer.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()



