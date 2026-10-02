import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, Mock

from reliability_support import load_functions

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

SID = "11111111-1111-1111-1111-111111111111"


def make_call(data):
    call = Mock()
    call.data = data
    call.answer = AsyncMock()
    call.message.edit_text = AsyncMock()
    call.message.edit_reply_markup = AsyncMock()
    return call


class TopNHandlerTests(unittest.IsolatedAsyncioTestCase):
    def env(self, *, channel=False, session=None):
        session = session or {"id": SID, "chat_id": -100123, "status": "waiting"}
        self.update_session = AsyncMock(return_value=session)
        self.guard = AsyncMock(return_value=session)
        return load_functions(
            "handlers/group_quiz.py", {"choose_group_top_n", "choose_group_anonymity", "_is_channel_chat"},
            _guard_config_callback=self.guard, update_session=self.update_session,
            get_chat_membership=AsyncMock(return_value={"chat_type": "channel" if channel else "supergroup"}),
            get_group_top_n_keyboard=Mock(return_value="KB_TOPN"), get_group_timer_keyboard=Mock(return_value="KB_TIMER"),
            get_group_names_privacy_keyboard=Mock(return_value="KB_NAMES"),
            GROUP_TOP_N_MIN=1, GROUP_TOP_N_MAX=20, GROUP_TOP_N_DEFAULT=10,
            _TOP_N_PROMPT="prompt", log_error=Mock(), logger=Mock(), Optional=object, Dict=dict, Any=object,
        )

    async def test_confirm_saves_clamped_value_and_moves_to_timer(self):
        for raw, expected in (("0", 1), ("7", 7), ("20", 20), ("99", 20)):
            env = self.env()
            call = make_call(f"gqk:{SID}:ok:{raw}")
            await env["choose_group_top_n"](call)
            self.update_session.assert_awaited_once_with(SID, {"top_n": expected})
            self.assertEqual(call.message.edit_text.await_args.kwargs["reply_markup"], "KB_TIMER")

    async def test_navigation_does_not_write_to_db(self):
        env = self.env()
        call = make_call(f"gqk:{SID}:n:4")
        await env["choose_group_top_n"](call)
        self.update_session.assert_not_awaited()
        env["get_group_top_n_keyboard"].assert_called_once_with(SID, 4)

    async def test_malformed_callbacks_are_rejected(self):
        for data in (f"gqk:{SID}", f"gqk:{SID}:ok:abc", f"gqk:{SID}:zzz:5"):
            env = self.env()
            call = make_call(data)
            await env["choose_group_top_n"](call)
            self.update_session.assert_not_awaited()
            self.assertTrue(call.answer.await_args.kwargs.get("show_alert"))

    async def test_competitive_mode_blocked_for_channels(self):
        env = self.env(channel=True)
        call = make_call(f"gqa:{SID}:c")
        await env["choose_group_anonymity"](call)
        self.update_session.assert_not_awaited()
        # المعالج بيستدعي call.answer() مرة تانية بـ finally، فنفحص أي نداء كان تنبيه.
        self.assertTrue(any(c.kwargs.get("show_alert") for c in call.answer.await_args_list))

    async def test_anonymous_mode_allowed_for_channels(self):
        env = self.env(channel=True)
        call = make_call(f"gqa:{SID}:a")
        await env["choose_group_anonymity"](call)
        self.update_session.assert_awaited_once_with(SID, {"poll_is_anonymous": True})

    async def test_competitive_mode_still_allowed_for_groups(self):
        env = self.env(channel=False)
        call = make_call(f"gqa:{SID}:c")
        await env["choose_group_anonymity"](call)
        self.update_session.assert_awaited_once_with(SID, {"poll_is_anonymous": False})


if __name__ == "__main__":
    unittest.main()
