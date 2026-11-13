"""اختبارات ميزة حاسبة النقاط والباقات المخصصة ومكافأة الاشتراك في قناة إعلامات البوت."""
import datetime
import html
import sys
import types
import unittest
from pathlib import Path
from urllib.parse import urlencode
from unittest.mock import AsyncMock, MagicMock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from reliability_support import load_functions  # noqa: E402


class FakeInlineKeyboardButton:
    def __init__(self, text, callback_data=None, url=None, switch_inline_query=None, **kwargs):
        self.text = text
        self.callback_data = callback_data
        self.url = url
        self.switch_inline_query = switch_inline_query


class FakeInlineKeyboardMarkup:
    def __init__(self, inline_keyboard):
        self.inline_keyboard = inline_keyboard


fake_types = types.SimpleNamespace(
    InlineKeyboardButton=FakeInlineKeyboardButton,
    InlineKeyboardMarkup=FakeInlineKeyboardMarkup,
    CallbackQuery=object,
    Message=object,
    FSInputFile=lambda p: p,
)


class PointsCalculatorServiceTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.rows = []
        query = MagicMock()
        query.select.return_value = query
        query.eq.return_value = query
        query.gte.return_value = query
        query.order.return_value = query
        query.limit.return_value = query
        query.execute = AsyncMock(side_effect=lambda: types.SimpleNamespace(data=list(self.rows)))
        self.supabase = types.SimpleNamespace(table=MagicMock(return_value=query))

        import constants as C
        from decimal import Decimal, ROUND_HALF_UP
        import math
        import re
        from zoneinfo import ZoneInfo

        self.pcs = load_functions(
            "services/points_calculator_service.py",
            {
                "normalize_custom_points",
                "get_volume_discount_percent",
                "build_custom_package_quote",
                "build_pricing_explanation_text",
                "_extract_debit_net_points",
                "get_user_activity_summary",
                "build_auto_recommendation",
                "build_manual_recommendation",
                "parse_custom_manual_input",
                "format_quote_block",
            },
            Decimal=Decimal,
            ROUND_HALF_UP=ROUND_HALF_UP,
            datetime=datetime,
            math=math,
            re=re,
            ZoneInfo=ZoneInfo,
            Any=object,
            Dict=dict,
            List=list,
            Optional=object,
            Tuple=tuple,
            DISCOUNT_RATE_FOR_CACHED=C.DISCOUNT_RATE_FOR_CACHED,
            MAX_ALBUM_IMAGES=C.MAX_ALBUM_IMAGES,
            MAX_LIMIT_PAGES=C.MAX_LIMIT_PAGES,
            MAX_STANDARD_PAGES=C.MAX_STANDARD_PAGES,
            MAX_STANDARD_QUESTIONS=C.MAX_STANDARD_QUESTIONS,
            AUDIO_OVERAGE_RATE=1.5,
            AUDIO_STANDARD_MINUTES=10,
            calculate_quiz_points_cost=AsyncMock(side_effect=lambda p, q, is_album=False: float(p + q)),
            format_setting_value=lambda v: str(int(v)) if float(v).is_integer() else str(v),
            get_setting=AsyncMock(side_effect=lambda k: {"quiz_standard_rate": 1.0, "quiz_overage_rate": 1.5, "daily_renewal_points": 50.0}[k]),
            supabase=self.supabase,
            logger=MagicMock(),
            BASE_POINT_PRICE_SYP=Decimal("0.325"),
            DEFAULT_CURRENCY="SYP",
            MIN_CUSTOM_PACKAGE_POINTS=100,
            MAX_CUSTOM_PACKAGE_POINTS=10000,
            CUSTOM_PACKAGE_STEP=50,
            ACTIVITY_WINDOW_DAYS=14,
            MIN_ACTIVE_OPERATIONS=2,
            MIN_ACTIVE_POINTS_SPENT=30.0,
            VOLUME_DISCOUNT_TIERS=[
                (2500, 30),
                (1500, 25),
                (1001, 22),
                (500, 20),
                (350, 15),
                (250, 10),
                (0, 0),
            ],
        )

    def test_volume_discount_quotes_and_savings(self):
        build_quote = self.pcs["build_custom_package_quote"]

        q200 = build_quote(200)
        self.assertEqual(q200["points"], 200)
        self.assertEqual(q200["price"], 65)
        self.assertEqual(q200["discount_pct"], 0)
        self.assertEqual(q200["saved"], 0)
        self.assertIsNotNone(q200["next_tier_hint"])
        self.assertEqual(q200["next_tier_hint"]["points"], 250)

        q300 = build_quote(300)
        self.assertEqual(q300["discount_pct"], 10)
        self.assertEqual(q300["base_price"], 98)
        self.assertEqual(q300["price"], 88)
        self.assertEqual(q300["saved"], 10)

        q400 = build_quote(400)
        self.assertEqual(q400["discount_pct"], 15)
        self.assertEqual(q400["base_price"], 130)
        self.assertEqual(q400["price"], 111)
        self.assertEqual(q400["saved"], 19)

        q500 = build_quote(500)
        self.assertEqual(q500["discount_pct"], 20)
        self.assertEqual(q500["base_price"], 163)
        self.assertEqual(q500["price"], 130)
        self.assertEqual(q500["saved"], 33)

        q750 = build_quote(750)
        self.assertEqual(q750["discount_pct"], 20)
        self.assertEqual(q750["base_price"], 244)
        self.assertEqual(q750["price"], 195)
        self.assertEqual(q750["saved"], 49)

        q1500 = build_quote(1500)
        self.assertEqual(q1500["discount_pct"], 25)
        self.assertEqual(q1500["base_price"], 488)
        self.assertEqual(q1500["price"], 366)
        self.assertEqual(q1500["saved"], 122)

    async def test_pricing_explanation_includes_page_and_question_rates(self):
        text = await self.pcs["build_pricing_explanation_text"]()
        self.assertIn("كيف تُحسب النقاط", text)
        self.assertIn("1 نقطة", text)
        self.assertIn("1.5 نقطة", text)
        self.assertIn("90%", text)
        self.assertIn("50 نقطة يومياً", text)

    async def test_activity_detection_distinguishes_inactive_and_active_users(self):
        now = datetime.datetime.now(datetime.timezone.utc)
        self.rows = [
            {
                "free_amount": 10,
                "paid_amount": 0,
                "gifts": [],
                "refunded_ratio": 0,
                "created_at": now.isoformat(),
            }
        ]
        summary_inactive = await self.pcs["get_user_activity_summary"](101)
        self.assertFalse(summary_inactive["is_active"])

        self.rows = [
            {
                "free_amount": 15,
                "paid_amount": 0,
                "gifts": [{"id": "g1", "amount": 20}],
                "refunded_ratio": 0,
                "created_at": (now - datetime.timedelta(days=2)).isoformat(),
            },
            {
                "free_amount": 0,
                "paid_amount": 25,
                "gifts": [],
                "refunded_ratio": 0,
                "created_at": now.isoformat(),
            },
        ]
        summary_active = await self.pcs["get_user_activity_summary"](202)
        self.assertTrue(summary_active["is_active"])
        self.assertEqual(summary_active["valid_ops"], 2)
        self.assertEqual(summary_active["total_spent"], 60.0)

        rec = self.pcs["build_auto_recommendation"](summary_active, target_days=14)
        self.assertGreaterEqual(rec["quote"]["points"], 100)

    def test_parse_custom_manual_input(self):
        parse = self.pcs["parse_custom_manual_input"]
        self.assertEqual(parse("450"), {"mode": "direct_points", "points": 450})
        self.assertEqual(parse("٤٥٠ نقطة"), {"mode": "direct_points", "points": 450})
        self.assertEqual(
            parse("12 25 8"),
            {"mode": "breakdown", "pages": 12, "questions": 25, "quizzes_count": 8},
        )
        self.assertIsNone(parse("مرحبا"))
        self.assertIsNone(parse("10"))


class RechargeAndCalculatorHandlersTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.start_env = load_functions(
            "handlers/start.py",
            {"get_recharge_keyboard"},
            types=fake_types,
            Optional=object,
            RECHARGE_PACKAGES={"200": "65", "500": "130", "1000": "260"},
            ADMIN_CONTACT="@abiadd",
            urlencode=urlencode,
        )

    def test_start_recharge_keyboard_has_calculator_below_packages(self):
        kb = self.start_env["get_recharge_keyboard"]()
        callbacks = [btn.callback_data for row in kb.inline_keyboard for btn in row]
        self.assertEqual(
            callbacks,
            [
                "recharge_package:200",
                "recharge_package:500",
                "recharge_package:1000",
                "calc:menu",
                "recharge_choose_package",
            ],
        )

    async def test_calculator_menu_hides_auto_option_when_inactive_and_shows_when_active(self):
        pcs_stub = types.SimpleNamespace(
            get_user_activity_summary=AsyncMock(return_value={
                "is_active": False, "window_days": 14, "valid_ops": 0, "total_spent": 0.0,
            }),
        )
        env = load_functions(
            "handlers/payments.py",
            {
                "_build_calculator_menu_keyboard",
                "_build_calculator_menu_text",
                "_edit_or_send",
                "show_calculator_menu",
            },
            types=fake_types,
            Optional=object,
            FSMContext=object,
            CalculatorState=types.SimpleNamespace(
                waiting_for_custom_input=types.SimpleNamespace(state="calc_input")
            ),
            pcs=pcs_stub,
            TelegramBadRequest=Exception,
            logger=MagicMock(),
        )
        msg = types.SimpleNamespace(edit_text=AsyncMock(), answer=AsyncMock())
        call = types.SimpleNamespace(
            data="calc:menu",
            from_user=types.SimpleNamespace(id=55),
            message=msg,
            answer=AsyncMock(),
        )

        await env["show_calculator_menu"](call, None)
        kb_inactive = msg.edit_text.await_args.kwargs["reply_markup"]
        cbs_inactive = [b.callback_data for row in kb_inactive.inline_keyboard for b in row]
        self.assertIn("calc:explain", cbs_inactive)
        self.assertIn("calc:m:15:20:10", cbs_inactive)
        self.assertNotIn("calc:auto:14", cbs_inactive)

        msg.edit_text.reset_mock()
        pcs_stub.get_user_activity_summary.return_value = {
            "is_active": True, "window_days": 14, "valid_ops": 5, "total_spent": 120.0,
        }
        await env["show_calculator_menu"](call, None)
        kb_active = msg.edit_text.await_args.kwargs["reply_markup"]
        cbs_active = [b.callback_data for row in kb_active.inline_keyboard for b in row]
        self.assertIn("calc:auto:14", cbs_active)

    async def test_proceed_custom_package_works_in_both_support_and_shamcash_modes(self):
        pcs_stub = types.SimpleNamespace(
            build_custom_package_quote=lambda pts: {
                "id": None,
                "points": pts,
                "base_price": 244,
                "price": 195,
                "saved": 49,
                "discount_pct": 20,
                "currency": "SYP",
            }
        )
        ps_stub = types.SimpleNamespace(
            format_money=lambda a, c: f"{a} ل.س",
            get_open_order_for_user=AsyncMock(return_value=None),
            create_order=AsyncMock(return_value={"id": 99, "points": 750, "amount": 195, "currency": "SYP", "code": "123456"}),
        )
        payments_avail = AsyncMock(return_value=False)
        send_order = AsyncMock()
        env = load_functions(
            "handlers/payments.py",
            {
                "_edit_or_send",
                "_build_support_custom_charge_url",
                "proceed_custom_package",
                "create_custom_order_for_method",
            },
            types=fake_types,
            urlencode=urlencode,
            ADMIN_CONTACT="@abiadd",
            pcs=pcs_stub,
            ps=ps_stub,
            payments_available=payments_avail,
            _show_open_order=AsyncMock(),
            _send_order_screen=send_order,
            log_usage_event=AsyncMock(),
            spawn=lambda c: c.close(),
            TelegramBadRequest=Exception,
            logger=MagicMock(),
        )

        msg = types.SimpleNamespace(
            edit_text=AsyncMock(),
            answer=AsyncMock(),
            chat=types.SimpleNamespace(id=77),
        )
        call = types.SimpleNamespace(
            data="pay:custom:750",
            from_user=types.SimpleNamespace(id=77),
            message=msg,
            bot=MagicMock(),
            answer=AsyncMock(),
        )

        await env["proceed_custom_package"](call)
        rendered_text = msg.edit_text.await_args.args[0]
        rendered_kb = msg.edit_text.await_args.kwargs["reply_markup"]
        self.assertIn("750 نقطة", rendered_text)
        self.assertIn("195 ل.س", rendered_text)
        self.assertIn("49 ل.س", rendered_text)
        first_btn = rendered_kb.inline_keyboard[0][0]
        self.assertIsNotNone(first_btn.url)
        self.assertIn("t.me/abiadd", first_btn.url)

        msg.edit_text.reset_mock()
        payments_avail.return_value = True
        await env["proceed_custom_package"](call)
        shamcash_kb = msg.edit_text.await_args.kwargs["reply_markup"]
        self.assertEqual(shamcash_kb.inline_keyboard[0][0].callback_data, "pay:cm:750:sc")

        call_cm = types.SimpleNamespace(
            data="pay:cm:750:sc",
            from_user=types.SimpleNamespace(id=77),
            message=msg,
            bot=MagicMock(),
            answer=AsyncMock(),
        )
        await env["create_custom_order_for_method"](call_cm)
        ps_stub.create_order.assert_awaited_once()
        send_order.assert_awaited_once()


class ChannelJoinRewardTests(unittest.IsolatedAsyncioTestCase):
    async def test_main_menu_places_channel_button_beside_referral_until_claimed(self):
        import constants as C
        self.assertEqual(C.WELCOME_POINTS, 100)
        self.assertEqual(C.CHANNEL_JOIN_BONUS_POINTS, 50)

        kb_env = load_functions(
            "keyboards.py",
            {"get_main_menu_keyboard"},
            types=fake_types,
            OFFICIAL_CHANNEL_URL=C.OFFICIAL_CHANNEL_URL,
            SUPPORT_BOT_URL=C.SUPPORT_BOT_URL,
            WEBAPP_PUBLIC_BASE_URL="",
            get_setting=AsyncMock(side_effect=lambda k: 25 if k == "referral_bonus_points" else 50),
            logger=MagicMock(),
        )
        claimed_mock = AsyncMock(return_value=False)
        flag_mock = AsyncMock(return_value=False)
        with patch.dict(sys.modules, {
            "helpers.supabase_helper.gifts": types.SimpleNamespace(has_claimed_channel_reward=claimed_mock),
            "helpers.supabase_helper.feature_flags": types.SimpleNamespace(is_feature_enabled=flag_mock),
        }):
            # قبل تنفيذ المهمة: الزر يظهر بجانب زر الإحالة في نفس الصف ويمنح 50 نقطة
            menu_before = await kb_env["get_main_menu_keyboard"]("abiadbot", 42)
            referral_row_before = menu_before.inline_keyboard[-1]
            self.assertEqual(len(referral_row_before), 2)
            self.assertIn("25", referral_row_before[0].text)
            self.assertIn("50", referral_row_before[1].text)
            self.assertEqual(referral_row_before[1].callback_data, "channel_reward:open")

            # بعد تنفيذ المهمة لمرة واحدة: يختفي الزر تلقائياً ويبقى زر الإحالة وحده
            claimed_mock.return_value = True
            menu_after = await kb_env["get_main_menu_keyboard"]("abiadbot", 42)
            referral_row_after = menu_after.inline_keyboard[-1]
            self.assertEqual(len(referral_row_after), 1)
            self.assertIsNone(referral_row_after[0].callback_data)

    async def test_channel_reward_claim_verifies_membership_and_grants_once(self):
        has_claimed = AsyncMock(return_value=False)
        claim_reward = AsyncMock(return_value={"status": "ok", "points": 50})
        refresh_menu = AsyncMock()
        bot_stub = types.SimpleNamespace(
            get_chat_member=AsyncMock(return_value=types.SimpleNamespace(status="left")),
        )
        env = load_functions(
            "handlers/start.py",
            {
                "_official_channel_targets",
                "_is_user_subscribed_to_official_channel",
                "open_channel_reward_screen",
                "claim_channel_reward_handler",
            },
            types=fake_types,
            Optional=object,
            OFFICIAL_CHANNEL_URL="https://t.me/abiadquizmaker",
            OFFICIAL_CHANNEL_CHAT_ID=-1004376690066,
            bot=bot_stub,
            has_claimed_channel_reward=has_claimed,
            claim_channel_join_reward=claim_reward,
            _refresh_main_menu_after_channel_reward=refresh_menu,
            _show_student_navigation_screen=AsyncMock(),
            get_setting=AsyncMock(return_value=50),
            log_usage_event=AsyncMock(),
            spawn=lambda c: c.close(),
            logger=MagicMock(),
        )

        call = types.SimpleNamespace(
            data="channel_reward:claim",
            from_user=types.SimpleNamespace(id=99),
            message=types.SimpleNamespace(chat=types.SimpleNamespace(type="private")),
            answer=AsyncMock(),
        )

        # 1) غير مشترك بعد -> ينبهه ولا يمنح النقاط
        await env["claim_channel_reward_handler"](call)
        claim_reward.assert_not_awaited()
        self.assertIn("لم نتمكن من رصد اشتراكك", call.answer.await_args.args[0])

        # 2) اشترك في القناة -> يمنحه 50 نقطة ويحدث القائمة الرئيسية ليختفي الزر
        call.answer.reset_mock()
        bot_stub.get_chat_member.return_value = types.SimpleNamespace(status="member")
        await env["claim_channel_reward_handler"](call)
        claim_reward.assert_awaited_once_with(99)
        refresh_menu.assert_awaited_once_with(call)
        self.assertIn("50", call.answer.await_args.args[0])


if __name__ == "__main__":
    unittest.main()
