from copy import deepcopy
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock
import uuid
import html
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

try:
    from aiogram import types
except ModuleNotFoundError:
    class _InlineKeyboardButton:
        def __init__(self, text, callback_data=None, url=None, copy_text=None):
            self.text = text
            self.callback_data = callback_data
            self.url = url
            self.copy_text = copy_text

    class _InlineKeyboardMarkup:
        def __init__(self, inline_keyboard):
            self.inline_keyboard = inline_keyboard

    types = SimpleNamespace(
        InlineKeyboardButton=_InlineKeyboardButton,
        InlineKeyboardMarkup=_InlineKeyboardMarkup,
        Message=object,
        CallbackQuery=object,
    )

from reliability_support import load_functions
from services.gift_presentation import expiry_text, remaining_expiry_text

DIRECT_GIFT_PRESET_TITLES = {
    "admin": "هدية من الإدارة",
    "special": "هدية خاصة",
    "reward": "مكافأة تميز",
    "welcome": "هدية مستخدم جديد",
}

kb_env = load_functions(
    "keyboards.py",
    {
        "get_admin_direct_message_charge_keyboard",
        "get_admin_direct_gift_mode_keyboard",
        "get_admin_direct_gift_title_keyboard",
        "get_admin_direct_gift_amount_keyboard",
        "get_admin_direct_gift_days_keyboard",
        "get_admin_direct_message_mode_keyboard",
        "get_cancel_keyboard",
        "get_admin_dashboard_keyboard",
    },
    types=types,
    Optional=object,
)


class FakeState:
    def __init__(self):
        self.data = {}
        self.status = None

    async def get_data(self):
        return deepcopy(self.data)

    async def update_data(self, **kwargs):
        self.data.update(deepcopy(kwargs))

    async def get_state(self):
        return self.status

    async def set_state(self, value):
        self.status = getattr(value, "state", value)

    async def clear(self):
        self.data = {}
        self.status = None


class DirectGiftChargeTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.state = FakeState()
        state_names = [
            "waiting_for_direct_message_target",
            "waiting_for_direct_message_mode",
            "waiting_for_direct_message_text",
            "waiting_for_direct_message_charge_amount",
            "waiting_for_direct_message_charge_type",
            "waiting_for_direct_gift_mode",
            "waiting_for_direct_gift_title",
            "waiting_for_direct_gift_amount",
            "waiting_for_direct_gift_days",
            "waiting_for_direct_message_confirm",
        ]
        self.admin_state = SimpleNamespace(**{s: SimpleNamespace(state=s) for s in state_names})
        self.bot = SimpleNamespace(send_message=AsyncMock())
        self.safe_edit = AsyncMock()
        self.add_points = AsyncMock(return_value=175.0)
        self.grant_gift = AsyncMock(return_value={
            "status": "ok",
            "gift_id": "gift-1",
            "title": "هدية مستخدم جديد",
            "points": 150.0,
            "expires_at": "2099-12-11T18:00:00+00:00",
            "gift_points": 150.0,
            "total_points": 175.0,
        })
        self.env = load_functions(
            "handlers/admin/users.py",
            {
                "_format_gift_validity_days",
                "process_direct_message_text",
                "process_direct_message_charge_type",
                "process_direct_message_charge_amount",
                "process_direct_gift_preset",
                "process_direct_gift_title_button",
                "process_direct_gift_title_text",
                "process_direct_gift_amount_button",
                "process_direct_gift_amount_text",
                "process_direct_gift_days_button",
                "process_direct_gift_days_text",
                "show_direct_message_confirmation",
                "toggle_direct_message_balance",
                "process_confirm_direct_message",
            },
            AdminState=self.admin_state,
            DIRECT_GIFT_PRESET_TITLES=DIRECT_GIFT_PRESET_TITLES,
            get_admin_direct_message_charge_keyboard=kb_env["get_admin_direct_message_charge_keyboard"],
            get_admin_direct_gift_mode_keyboard=kb_env["get_admin_direct_gift_mode_keyboard"],
            get_admin_direct_gift_title_keyboard=kb_env["get_admin_direct_gift_title_keyboard"],
            get_admin_direct_gift_amount_keyboard=kb_env["get_admin_direct_gift_amount_keyboard"],
            get_admin_direct_gift_days_keyboard=kb_env["get_admin_direct_gift_days_keyboard"],
            get_admin_direct_message_mode_keyboard=kb_env["get_admin_direct_message_mode_keyboard"],
            get_cancel_keyboard=kb_env["get_cancel_keyboard"],
            get_admin_dashboard_keyboard=kb_env["get_admin_dashboard_keyboard"],
            safe_edit_text=self.safe_edit,
            admin_add_points=self.add_points,
            admin_grant_direct_gift=self.grant_gift,
            expiry_text=expiry_text,
            remaining_expiry_text=remaining_expiry_text,
            bot=self.bot,
            types=types,
            html=html,
            uuid=uuid,
            TelegramForbiddenError=Exception,
            logger=Mock(),
        )

    def _msg(self, text):
        return SimpleNamespace(text=text, answer=AsyncMock())

    def _call(self, data):
        return SimpleNamespace(
            data=data,
            answer=AsyncMock(),
            message=SimpleNamespace(answer=AsyncMock()),
        )

    def test_charge_keyboard_order_is_free_gift_paid_none(self):
        kb = kb_env["get_admin_direct_message_charge_keyboard"](42)
        callbacks = [btn.callback_data for row in kb.inline_keyboard for btn in row]
        self.assertEqual(
            callbacks[:4],
            [
                "admin_direct_charge_free_42",
                "admin_direct_charge_gift_42",
                "admin_direct_charge_paid_42",
                "admin_direct_charge_none_42",
            ],
        )
        for cb in callbacks:
            self.assertLessEqual(len(cb.encode("utf-8")), 64)

    async def test_welcome_gift_preset_flow_grants_150_points_for_30_days_and_sends_gift_details(self):
        await self.state.update_data(
            direct_message_target_id=777,
            direct_message_balance_before=25.0,
            direct_message_mode="charge",
        )
        msg = self._msg("أهلاً بك في البوت!")
        await self.env["process_direct_message_text"](msg, self.state)
        self.assertEqual(self.state.status, "waiting_for_direct_message_charge_type")

        call_gift = self._call("admin_direct_charge_gift_777")
        await self.env["process_direct_message_charge_type"](call_gift, self.state)
        self.assertEqual(self.state.status, "waiting_for_direct_gift_mode")

        call_welcome = self._call("admin_direct_gift_preset_welcome")
        await self.env["process_direct_gift_preset"](call_welcome, self.state)
        self.assertEqual(self.state.status, "waiting_for_direct_message_confirm")
        self.assertEqual(self.state.data["direct_message_charge_amount"], 150)
        self.assertEqual(self.state.data["direct_message_gift_days"], 30)
        self.assertEqual(self.state.data["direct_message_gift_title"], "هدية مستخدم جديد")
        self.assertTrue(self.state.data["direct_message_include_balance"])

        preview_text = call_welcome.message.answer.await_args.args[0]
        self.assertIn("شحن هدايا", preview_text)
        self.assertIn("هدية مستخدم جديد", preview_text)
        self.assertIn("شهر (30 يوماً)", preview_text)

        call_confirm = self._call("admin_confirm_direct_message")
        await self.env["process_confirm_direct_message"](call_confirm, self.state)
        self.grant_gift.assert_awaited_once()
        kwargs = self.grant_gift.await_args.kwargs
        self.assertEqual(kwargs["target_id"], 777)
        self.assertEqual(kwargs["amount"], 150)
        self.assertEqual(kwargs["valid_days"], 30)
        self.assertEqual(kwargs["title"], "هدية مستخدم جديد")

        sent_text = self.bot.send_message.await_args.kwargs["text"]
        self.assertIn("تفاصيل الهدية والرصيد", sent_text)
        self.assertIn("هدية مستخدم جديد", sent_text)
        self.assertIn("+150", sent_text)
        self.assertIn("25.00", sent_text)
        self.assertIn("175.00", sent_text)

    async def test_custom_gift_flow_with_buttons_and_custom_inputs(self):
        await self.state.update_data(
            direct_message_target_id=888,
            direct_message_balance_before=10.0,
            direct_message_mode="charge",
            direct_message_text="مبارك التفوق!",
        )
        call_gift = self._call("admin_direct_charge_gift_888")
        await self.env["process_direct_message_charge_type"](call_gift, self.state)

        call_custom = self._call("admin_direct_gift_preset_custom")
        await self.env["process_direct_gift_preset"](call_custom, self.state)
        self.assertEqual(self.state.status, "waiting_for_direct_gift_title")

        # Select quick title button
        call_title = self._call("admin_direct_gift_title_reward")
        await self.env["process_direct_gift_title_button"](call_title, self.state)
        self.assertEqual(self.state.data["direct_message_gift_title"], "مكافأة تميز")
        self.assertEqual(self.state.status, "waiting_for_direct_gift_amount")

        # Select preset amount 50
        call_amt = self._call("admin_direct_gift_amt_50")
        await self.env["process_direct_gift_amount_button"](call_amt, self.state)
        self.assertEqual(self.state.data["direct_message_charge_amount"], 50)
        self.assertEqual(self.state.status, "waiting_for_direct_gift_days")

        # Select custom days then send 10 days via text
        call_days_custom = self._call("admin_direct_gift_days_custom")
        await self.env["process_direct_gift_days_button"](call_days_custom, self.state)
        self.assertEqual(self.state.status, "waiting_for_direct_gift_days")

        msg_days = self._msg("10")
        await self.env["process_direct_gift_days_text"](msg_days, self.state)
        self.assertEqual(self.state.status, "waiting_for_direct_message_confirm")
        self.assertEqual(self.state.data["direct_message_gift_days"], 10)
        preview_text = msg_days.answer.await_args.args[0]
        self.assertIn("مكافأة تميز", preview_text)
        self.assertIn("10 يوم", preview_text)

        call_confirm = self._call("admin_confirm_direct_message")
        await self.env["process_confirm_direct_message"](call_confirm, self.state)
        self.grant_gift.assert_awaited_once()
        kwargs = self.grant_gift.await_args.kwargs
        self.assertEqual(kwargs["target_id"], 888)
        self.assertEqual(kwargs["amount"], 50)
        self.assertEqual(kwargs["valid_days"], 10)
        self.assertEqual(kwargs["title"], "مكافأة تميز")

    async def test_custom_gift_flow_with_zero_days_no_expiry(self):
        await self.state.update_data(
            direct_message_target_id=889,
            direct_message_balance_before=10.0,
            direct_message_mode="charge",
            direct_message_text="هدية دائمة",
        )
        call_gift = self._call("admin_direct_charge_gift_889")
        await self.env["process_direct_message_charge_type"](call_gift, self.state)

        call_custom = self._call("admin_direct_gift_preset_custom")
        await self.env["process_direct_gift_preset"](call_custom, self.state)

        call_title = self._call("admin_direct_gift_title_special")
        await self.env["process_direct_gift_title_button"](call_title, self.state)

        call_amt = self._call("admin_direct_gift_amt_100")
        await self.env["process_direct_gift_amount_button"](call_amt, self.state)

        call_days_0 = self._call("admin_direct_gift_days_0")
        await self.env["process_direct_gift_days_button"](call_days_0, self.state)
        self.assertEqual(self.state.status, "waiting_for_direct_message_confirm")
        self.assertEqual(self.state.data["direct_message_gift_days"], 0)

        call_confirm = self._call("admin_confirm_direct_message")
        await self.env["process_confirm_direct_message"](call_confirm, self.state)
        kwargs = self.grant_gift.await_args.kwargs
        self.assertEqual(kwargs["target_id"], 889)
        self.assertEqual(kwargs["amount"], 100)
        self.assertEqual(kwargs["valid_days"], 0)
        self.assertEqual(kwargs["title"], "هدية خاصة")

    async def test_free_and_paid_charge_types_prompt_for_amount_then_confirm(self):
        await self.state.update_data(
            direct_message_target_id=555,
            direct_message_balance_before=5.0,
            direct_message_mode="charge",
            direct_message_text="تم الشحن",
        )
        call_paid = self._call("admin_direct_charge_paid_555")
        await self.env["process_direct_message_charge_type"](call_paid, self.state)
        self.assertEqual(self.state.status, "waiting_for_direct_message_charge_amount")
        self.assertEqual(self.state.data["direct_message_charge_type"], "paid")

        msg_amt = self._msg("200")
        await self.env["process_direct_message_charge_amount"](msg_amt, self.state)
        self.assertEqual(self.state.status, "waiting_for_direct_message_confirm")
        self.assertEqual(self.state.data["direct_message_charge_amount"], 200)
        preview = msg_amt.answer.await_args.args[0]
        self.assertIn("200", preview)
        self.assertIn("(مدفوع)", preview)

    async def test_admin_grant_direct_gift_rpc_call(self):
        fake_supabase = Mock()
        fake_supabase.rpc.return_value.execute = AsyncMock(
            return_value=SimpleNamespace(
                data={
                    "status": "ok",
                    "gift_id": "g-123",
                    "title": "هدية مستخدم جديد",
                    "points": 150,
                    "expires_at": "2099-01-01T00:00:00Z",
                    "gift_points": 150,
                    "total_points": 200,
                }
            )
        )
        ops_env = load_functions(
            "helpers/supabase_helper/admin_ops.py",
            {"admin_grant_direct_gift"},
            supabase=fake_supabase,
            gift_balances=AsyncMock(return_value={99: 150.0}),
            logger=Mock(),
            datetime=datetime,
            timedelta=timedelta,
            timezone=timezone,
            uuid=uuid,
            Optional=object,
            Dict=dict,
        )
        res = await ops_env["admin_grant_direct_gift"](
            target_id=99,
            amount=150,
            valid_days=30,
            title="هدية مستخدم جديد",
            source_key="direct:test-1",
        )
        self.assertEqual(res["status"], "ok")
        self.assertEqual(res["total_points"], 200.0)
        fake_supabase.rpc.assert_called_once_with(
            "admin_grant_user_gift",
            {
                "p_user_id": 99,
                "p_source_key": "direct:test-1",
                "p_title": "هدية مستخدم جديد",
                "p_points": 150.0,
                "p_valid_days": 30,
            },
        )


if __name__ == "__main__":
    unittest.main()
