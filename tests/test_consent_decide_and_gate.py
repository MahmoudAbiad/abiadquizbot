"""Phase 2 (second half): handlers/consent.py::_decide + ConsentMiddleware gate + log privacy.

aiogram is not required: production code is executed through reliability_support.load_functions
with injected doubles (same technique as the other consent tests).
"""
import asyncio
import types
import unittest
from unittest.mock import AsyncMock, Mock, patch

from reliability_support import load_functions

SECRET = "SECRET-STUDENT-TEXT-123"


# ---------------------------------------------------------------- _decide
class LockRedis:
    def __init__(self):
        self.keys = {}
        self.fail_set = False

    async def set(self, key, value, nx=False, ex=None):
        if self.fail_set:
            raise ConnectionError("down")
        if nx and key in self.keys:
            return None
        self.keys[key] = value
        return True

    async def delete(self, key):
        self.keys.pop(key, None)


class DecideHarness:
    def __init__(self, changed=True, rpc_error=None, pending=("m1",), pending_cb=None, payload=None):
        self.redis = LockRedis()
        self.order = []
        self.spawned = []
        self.changed = changed
        self.rpc_error = rpc_error
        pending_state = {"msgs": list(pending), "cb": pending_cb}

        async def pop_messages(uid, bot=None):
            self.order.append("pop_messages")
            msgs, pending_state["msgs"] = pending_state["msgs"], []
            return msgs

        async def pop_cb(uid):
            self.order.append("pop_cb")
            cb, pending_state["cb"] = pending_state["cb"], None
            return cb

        self.svc = types.SimpleNamespace(
            _redis=lambda: self.redis,
            CONSENT_ACCEPT="consent:accept",
            current_versions=AsyncMock(return_value=(1, 1)),
            remember_accepted=AsyncMock(),
            forget_denied=AsyncMock(),
            pop_pending_messages=pop_messages,
            pop_pending_callback=pop_cb,
            clear_pending_messages=AsyncMock(),
            clear_pending_callback=AsyncMock(),
            pop_start_payload=AsyncMock(return_value=payload),
            replay_pending_messages=AsyncMock(),
            build_accepted_text=Mock(return_value="accepted"),
            build_declined_text=Mock(return_value="declined"),
            build_consent_keyboard=Mock(return_value="kb"),
        )

        async def execute():
            self.order.append("rpc")
            await asyncio.sleep(0)
            if self.rpc_error:
                raise self.rpc_error
            return types.SimpleNamespace(data=[{"changed": self.changed}])

        rpc = Mock()
        rpc.return_value.execute = execute
        self.supabase = types.SimpleNamespace(rpc=rpc)
        self.log_usage_event = AsyncMock()
        self.edit = AsyncMock()
        self.env = load_functions(
            "handlers/consent.py", {"_decide"},
            svc=self.svc, logger=Mock(), spawn=self.spawned.append, _edit_or_send=self.edit,
            types=types.SimpleNamespace(
                CallbackQuery=object,
                InlineKeyboardMarkup=lambda inline_keyboard: ("markup", inline_keyboard),
                InlineKeyboardButton=lambda text, url: (text, url),
            ),
        )

    def call(self, uid=1):
        bot = types.SimpleNamespace(get_me=AsyncMock(return_value=types.SimpleNamespace(username="bot")))
        return types.SimpleNamespace(from_user=types.SimpleNamespace(id=uid), bot=bot, answer=AsyncMock())

    async def decide(self, call, decision):
        mods = {
            "helpers.supabase_helper": types.SimpleNamespace(supabase=self.supabase),
            "helpers.supabase_helper.analytics": types.SimpleNamespace(log_usage_event=self.log_usage_event),
        }
        with patch.dict("sys.modules", mods):
            await self.env["_decide"](call, decision)
        await self.drain()

    async def drain(self):
        pending, self.spawned[:] = list(self.spawned), []
        if pending:
            await asyncio.gather(*pending)


class DecideAcceptTests(unittest.IsolatedAsyncioTestCase):
    async def test_first_accept_with_pending_message_replays_once(self):
        h = DecideHarness(changed=True, pending=["m1", "m2"])
        await h.decide(h.call(), "accepted")
        h.svc.build_accepted_text.assert_called_once_with(resuming=True, callback_pending=False)
        h.svc.replay_pending_messages.assert_awaited_once()
        self.assertEqual(h.svc.replay_pending_messages.await_args.args[1], ["m1", "m2"])
        h.log_usage_event.assert_awaited_once()

    async def test_first_accept_with_pending_callback_asks_to_press_again_without_replay(self):
        h = DecideHarness(changed=True, pending=[], pending_cb={"data": "x", "message_id": 1, "chat_id": 1})
        await h.decide(h.call(), "accepted")
        h.svc.build_accepted_text.assert_called_once_with(resuming=False, callback_pending=True)
        h.svc.replay_pending_messages.assert_not_awaited()

    async def test_first_accept_with_nothing_pending(self):
        h = DecideHarness(changed=True, pending=[])
        call = h.call()
        await h.decide(call, "accepted")
        h.svc.build_accepted_text.assert_called_once_with(resuming=False, callback_pending=False)
        h.svc.replay_pending_messages.assert_not_awaited()
        call.answer.assert_awaited_once()

    async def test_start_payload_adds_continue_button(self):
        h = DecideHarness(changed=True, pending=[], payload="ref_1")
        await h.decide(h.call(), "accepted")
        markup = h.edit.await_args.args[2]
        self.assertEqual(markup[0], "markup")
        self.assertIn("start=ref_1", markup[1][0][0][1])

    async def test_second_accept_still_replays_and_never_double_replays(self):
        h = DecideHarness(changed=False, pending=["m1"])
        await h.decide(h.call(), "accepted")
        h.svc.replay_pending_messages.assert_awaited_once()
        h.log_usage_event.assert_not_awaited()
        h.edit.assert_not_awaited()
        # pop is destructive: a later press finds nothing and replays nothing more.
        await h.decide(h.call(), "accepted")
        h.svc.replay_pending_messages.assert_awaited_once()

    async def test_record_consent_failure_pulls_nothing_pending(self):
        h = DecideHarness(rpc_error=RuntimeError("db"))
        call = h.call()
        await h.decide(call, "accepted")
        self.assertNotIn("pop_messages", h.order)
        self.assertNotIn("pop_cb", h.order)
        h.svc.remember_accepted.assert_not_awaited()
        h.svc.replay_pending_messages.assert_not_awaited()
        self.assertTrue(call.answer.await_args.kwargs.get("show_alert"))

    async def test_pop_happens_after_record_consent(self):
        h = DecideHarness(changed=True)
        await h.decide(h.call(), "accepted")
        self.assertLess(h.order.index("rpc"), h.order.index("pop_messages"))

    async def test_concurrent_presses_replay_once_and_lock_is_released(self):
        h = DecideHarness(changed=True, pending=["m1"])
        c1, c2 = h.call(), h.call()
        mods = {
            "helpers.supabase_helper": types.SimpleNamespace(supabase=h.supabase),
            "helpers.supabase_helper.analytics": types.SimpleNamespace(log_usage_event=h.log_usage_event),
        }
        with patch.dict("sys.modules", mods):
            await asyncio.gather(h.env["_decide"](c1, "accepted"), h.env["_decide"](c2, "accepted"))
        await h.drain()
        h.svc.replay_pending_messages.assert_awaited_once()
        self.assertEqual(h.redis.keys, {})  # قفل المستخدم تحرّر

    async def test_redis_lock_failure_does_not_block_accept(self):
        h = DecideHarness(changed=True, pending=["m1"])
        h.redis.fail_set = True
        await h.decide(h.call(), "accepted")
        h.svc.replay_pending_messages.assert_awaited_once()


class DecideDeclineTests(unittest.IsolatedAsyncioTestCase):
    async def test_decline_clears_both_pending_kinds(self):
        h = DecideHarness(changed=True)
        await h.decide(h.call(), "declined")
        h.svc.clear_pending_messages.assert_awaited_once_with(1)
        h.svc.clear_pending_callback.assert_awaited_once_with(1)
        h.svc.forget_denied.assert_awaited_once()
        h.svc.replay_pending_messages.assert_not_awaited()
        self.assertNotIn("pop_messages", h.order)

    async def test_repeated_decline_clears_without_edit(self):
        h = DecideHarness(changed=False)
        call = h.call()
        await h.decide(call, "declined")
        h.svc.clear_pending_messages.assert_awaited_once()
        h.edit.assert_not_awaited()
        call.answer.assert_awaited_once()


# ---------------------------------------------------------------- gate
class _Msg:
    def __init__(self, uid=5, chat_type="private", is_bot=False):
        self.from_user = types.SimpleNamespace(id=uid, is_bot=is_bot)
        self.chat = types.SimpleNamespace(type=chat_type)
        self.text = SECRET
        self.answer = AsyncMock()


class _CB:
    def __init__(self, uid=5, data="menu:x", chat_type="private"):
        self.from_user = types.SimpleNamespace(id=uid, is_bot=False)
        self.data = data
        self.message = types.SimpleNamespace(chat=types.SimpleNamespace(type=chat_type), answer=AsyncMock())
        self.answer = AsyncMock()


class _TTL(dict):
    def __init__(self, **kw):
        super().__init__()


class GateHarness:
    def __init__(self, state="new"):
        self.remember_msg = AsyncMock()
        self.remember_cb = AsyncMock()
        self.log = Mock()
        self.state = state
        self.env = load_functions(
            "services/consent.py", {"ConsentMiddleware"},
            BaseMiddleware=object, TTLCache=_TTL, Message=_Msg, CallbackQuery=_CB,
            TelegramBadRequest=type("TBR", (Exception,), {}),
            TelegramForbiddenError=type("TFE", (Exception,), {}),
            Any=object, Dict=dict, Callable=object, Awaitable=object,
            STATE_OK="ok", RESUME_FLAG="consent_resumed", _NOTICE_TTL=10, _PROMPTED_TTL=1,
            logger=self.log,
            remember_start_payload=AsyncMock(),
            get_consent_state=AsyncMock(side_effect=lambda uid: self.state),
            remember_pending_message=self.remember_msg,
            remember_pending_callback=self.remember_cb,
            build_consent_text=AsyncMock(return_value="prompt"),
            build_consent_keyboard=Mock(return_value="kb"),
            _mark_prompted=AsyncMock(),
            _redis=lambda: types.SimpleNamespace(set=AsyncMock(return_value=True)),
        )
        self.mw = self.env["ConsentMiddleware"]()

    async def run(self, event, data=None):
        handler = AsyncMock(return_value="handled")
        mods = {
            "config": types.SimpleNamespace(ADMIN_ID=999),
            "task_utils": types.SimpleNamespace(spawn=lambda c: c.close()),
            "helpers.supabase_helper": types.SimpleNamespace(),
            "helpers.supabase_helper.analytics": types.SimpleNamespace(log_usage_event=AsyncMock()),
        }
        with patch.dict("sys.modules", mods):
            result = await self.mw(handler, event, data if data is not None else {})
        return handler, result


class GateTests(unittest.IsolatedAsyncioTestCase):
    async def test_blocked_message_is_remembered_and_prompted(self):
        g = GateHarness("new"); msg = _Msg()
        handler, result = await g.run(msg)
        handler.assert_not_awaited(); self.assertIsNone(result)
        g.remember_msg.assert_awaited_once_with(msg)
        msg.answer.assert_awaited_once()

    async def test_message_remembered_even_when_notice_is_throttled(self):
        g = GateHarness("new")
        first, second = _Msg(), _Msg()
        await g.run(first)
        await g.run(second)
        self.assertEqual(g.remember_msg.await_count, 2)   # الثانية حُفظت
        first.answer.assert_awaited_once()
        second.answer.assert_not_awaited()                # الإشعار كُتم (_NOTICE_TTL)

    async def test_pending_saved_before_notify(self):
        g = GateHarness("update"); order = []
        g.remember_msg.side_effect = lambda m: order.append("remember")
        msg = _Msg(); msg.answer = AsyncMock(side_effect=lambda *a, **k: order.append("notify"))
        await g.run(msg)
        self.assertEqual(order, ["remember", "notify"])

    async def test_blocked_callback_is_remembered(self):
        g = GateHarness("returning"); cb = _CB()
        handler, _ = await g.run(cb)
        handler.assert_not_awaited()
        g.remember_cb.assert_awaited_once_with(cb)
        g.remember_msg.assert_not_awaited()

    async def test_resumed_event_is_not_stored_again(self):
        for event in (_Msg(), _CB()):
            g = GateHarness("new")
            await g.run(event, {"consent_resumed": True})
            g.remember_msg.assert_not_awaited()
            g.remember_cb.assert_not_awaited()

    async def test_accepted_user_passes_without_storing(self):
        g = GateHarness("ok"); msg = _Msg()
        handler, result = await g.run(msg)
        handler.assert_awaited_once(); self.assertEqual(result, "handled")
        g.remember_msg.assert_not_awaited()

    async def test_consent_buttons_group_chats_and_admin_are_not_stored(self):
        g = GateHarness("new")
        for event in (_CB(data="consent:accept"), _Msg(chat_type="supergroup"), _Msg(uid=999)):
            handler, _ = await g.run(event)
            handler.assert_awaited_once()
        g.remember_msg.assert_not_awaited(); g.remember_cb.assert_not_awaited()

    async def test_consent_check_failure_fails_closed_and_stores_nothing(self):
        g = GateHarness("new")
        g.env["get_consent_state"].side_effect = RuntimeError("db")
        handler, result = await g.run(_Msg())
        handler.assert_not_awaited(); self.assertIsNone(result)
        g.remember_msg.assert_not_awaited()


# ---------------------------------------------------------------- privacy
class PendingLogPrivacyTests(unittest.IsolatedAsyncioTestCase):
    """رسائل السجل في دوال الاستئناف لا تطبع نص الطالب ولا file_id حتى عند الأخطاء."""

    async def test_failures_never_log_message_content(self):
        import json
        from test_consent_resume import FakeMessage
        from test_consent_resume_phase2 import CB_NAMES, build

        class BrokenRedis:
            def __getattr__(self, name):
                raise ConnectionError("redis down")  # لا يذكر المحتوى

        log = Mock()
        env = build(BrokenRedis())
        env["logger"] = log
        msg = FakeMessage(1, SECRET)
        await env["remember_pending_message"](msg)
        await env["pop_pending_messages"](1)
        await env["pop_pending_callback"](1)

        feed = AsyncMock(side_effect=RuntimeError("handler failed"))
        bot = types.SimpleNamespace(send_message=AsyncMock(side_effect=RuntimeError("blocked")))
        Update = lambda update_id, message: types.SimpleNamespace(update_id=update_id, message=message)
        mods = {"aiogram.types": types.SimpleNamespace(Update=Update),
                "config": types.SimpleNamespace(dp=types.SimpleNamespace(feed_update=feed))}
        with patch.dict("sys.modules", mods):
            await env["replay_pending_messages"](bot, [msg])

        logged = json.dumps([[str(a) for a in c.args] for c in log.method_calls], ensure_ascii=False)
        self.assertGreater(len(log.method_calls), 0)
        self.assertNotIn(SECRET, logged)


if __name__ == "__main__":
    unittest.main()



