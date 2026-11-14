"""
اختبارات شاملة لنظام اللغات الـ 6 (ar, en, tr, fr, de, fa)، شاشة اختيار اللغة بعد الموافقة،
التعرف التلقائي على لغة المستخدم، فحص رقم الهاتف (عند توفره فقط دون طلبه)،
وباقات الشحن بالليرة السورية والدولار الأمريكي مع نسب الخصم وتلوين الخيار الأوفر،
بالإضافة إلى تعريب القائمة الرئيسية (/start) وأزرارها ولوحة الإعدادات والمحفظة.
"""
import asyncio
import functools
import re
import sys
import types
import unittest
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlparse
from unittest.mock import AsyncMock, MagicMock, Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from reliability_support import load_functions  # noqa: E402


class FakeInlineKeyboardButton:
    def __init__(self, text, callback_data=None, url=None, style=None, **kwargs):
        self.text = text
        self.callback_data = callback_data
        self.url = url
        self.style = style
        self.extra = kwargs


class FakeInlineKeyboardMarkup:
    def __init__(self, inline_keyboard):
        self.inline_keyboard = inline_keyboard


fake_types = types.SimpleNamespace(
    InlineKeyboardButton=FakeInlineKeyboardButton,
    InlineKeyboardMarkup=FakeInlineKeyboardMarkup,
    CallbackQuery=object,
    Message=object,
)


def _load_i18n(redis_stub=None, supabase_stub=None):
    if redis_stub is None:
        redis_stub = types.SimpleNamespace(
            get=AsyncMock(return_value=None),
            set=AsyncMock(return_value=True),
            delete=AsyncMock(return_value=1),
        )
    env = {
        "types": fake_types,
        "TTLCache": lambda maxsize, ttl: {},
        "logger": MagicMock(),
        "functools": functools,
        "re": re,
        "sys": sys,
        "urlencode": urlencode,
        "Any": object,
        "Dict": dict,
        "List": list,
        "Optional": object,
        "Tuple": tuple,
    }
    code = (ROOT / "services/i18n.py").read_text(encoding="utf-8")
    with patch.dict(sys.modules, {
        "aiogram": types.SimpleNamespace(types=fake_types),
        "cachetools": types.SimpleNamespace(TTLCache=lambda maxsize, ttl: {}),
        "logger": types.SimpleNamespace(get_logger=lambda name: MagicMock()),
        "config": types.SimpleNamespace(redis_client=redis_stub),
        "helpers.supabase_helper": types.SimpleNamespace(supabase=supabase_stub or MagicMock()),
    }):
        exec(compile(code, "services/i18n.py", "exec"), env)
    return env


class LanguageDetectionAndPhoneTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.i18n = _load_i18n()

    def test_supported_six_languages_and_auto_detection(self):
        self.assertEqual(
            self.i18n["SUPPORTED_LANGUAGES"],
            ("ar", "en", "tr", "fr", "de", "fa"),
        )
        detect = self.i18n["detect_telegram_language"]
        self.assertEqual(detect("ar"), "ar")
        self.assertEqual(detect("en-US"), "en")
        self.assertEqual(detect("tr_TR"), "tr")
        self.assertEqual(detect("fr-CA"), "fr")
        self.assertEqual(detect("de-DE"), "de")
        self.assertEqual(detect("fa-IR"), "fa")
        self.assertEqual(detect("per"), "fa")
        self.assertEqual(detect("es-ES"), "ar")
        self.assertEqual(detect(None), "ar")

        user_obj = types.SimpleNamespace(language_code="tr-TR")
        self.assertEqual(detect(user_obj), "tr")

    def test_phone_number_only_used_when_available_and_never_requested(self):
        is_non_syrian = self.i18n["is_non_syrian_phone"]
        self.assertFalse(is_non_syrian(None))
        self.assertFalse(is_non_syrian(types.SimpleNamespace(from_user=types.SimpleNamespace(id=1))))

        self.assertFalse(is_non_syrian("+963 985 162 166"))
        self.assertFalse(is_non_syrian("00963944112233"))
        self.assertFalse(is_non_syrian("963933000111"))
        self.assertFalse(is_non_syrian("0944123456"))

        self.assertTrue(is_non_syrian("+90 555 123 4567"))
        self.assertTrue(is_non_syrian("+49 151 23456789"))
        self.assertTrue(is_non_syrian("+14155552671"))
        call_with_foreign_phone = types.SimpleNamespace(
            from_user=types.SimpleNamespace(id=7, phone_number="+33612345678")
        )
        self.assertTrue(is_non_syrian(call_with_foreign_phone))

    def test_language_screen_marks_detected_language_with_check_and_styles_continue_button(self):
        build_kb = self.i18n["build_language_screen_keyboard"]
        for code in ("ar", "en", "tr", "fr", "de", "fa"):
            kb = build_kb(code, prefix="consent:lang", is_settings=False)
            self.assertEqual(len(kb.inline_keyboard), 4)
            lang_buttons = [btn for row in kb.inline_keyboard[:3] for btn in row]
            self.assertEqual(len(lang_buttons), 6)
            checked = [btn for btn in lang_buttons if btn.text.startswith("✅ ")]
            self.assertEqual(len(checked), 1)
            self.assertEqual(checked[0].callback_data, f"consent:lang:pick:{code}")

            continue_btn = kb.inline_keyboard[3][0]
            self.assertEqual(continue_btn.callback_data, f"consent:lang:confirm:{code}")
            self.assertEqual(continue_btn.style, "success")


class RechargePackagesAndCurrencyTests(unittest.TestCase):
    def setUp(self):
        self.i18n = _load_i18n()

    def test_arabic_default_syp_packages_with_usd_toggle(self):
        text, kb = self.i18n["build_recharge_view"](lang_code="ar")
        self.assertIn("الليرة السورية", text)
        buttons = [btn for row in kb.inline_keyboard for btn in row]
        callbacks = [btn.callback_data for btn in buttons]
        self.assertIn("recharge_package:200", callbacks)
        self.assertIn("recharge_package:500", callbacks)
        self.assertIn("recharge_package:1000", callbacks)
        self.assertIn("recharge_currency:USD", callbacks)
        self.assertIn("calc:menu", callbacks)
        texts = [btn.text for btn in buttons]
        self.assertTrue(any("200 نقطة — 65 ل.س" in t for t in texts))
        self.assertTrue(any("500 نقطة — 130 ل.س" in t for t in texts))
        self.assertTrue(any("1000 نقطة — 260 ل.س" in t for t in texts))

    def test_arabic_toggled_to_usd_shows_fixed_usd_equivalents_and_syp_toggle(self):
        text, kb = self.i18n["build_recharge_view"](
            lang_code="ar",
            currency_override="USD",
            selected_package="200",
        )
        self.assertIn("الدولار الأمريكي", text)
        buttons = [btn for row in kb.inline_keyboard for btn in row]
        callbacks = [btn.callback_data for btn in buttons if btn.callback_data]
        self.assertIn("recharge_package:ar_usd:200", callbacks)
        self.assertIn("recharge_package:ar_usd:500", callbacks)
        self.assertIn("recharge_package:ar_usd:1000", callbacks)
        self.assertIn("recharge_currency:SYP", callbacks)

        texts = [btn.text for btn in buttons]
        self.assertTrue(any("200 نقطة — 0.5$" in t for t in texts))
        self.assertTrue(any("500 نقطة — 1$" in t for t in texts))
        self.assertTrue(any("1000 نقطة — 2$" in t for t in texts))

        support_btn = kb.inline_keyboard[-1][0]
        self.assertIsNotNone(support_btn.url)
        self.assertIn("t.me/abiadd", support_btn.url)
        parsed_q = parse_qs(urlparse(support_btn.url).query)
        self.assertIn("0.5 $", parsed_q["text"][0])

    def test_non_arabic_languages_show_usd_exclusively_with_400_900_1600_and_styled_best_option(self):
        for lang in ("en", "tr", "fr", "de", "fa"):
            with self.subTest(lang=lang):
                text, kb = self.i18n["build_recharge_view"](
                    lang_code=lang,
                    currency_override="SYP",
                    selected_package="1600",
                )
                buttons = [btn for row in kb.inline_keyboard for btn in row]
                callbacks = [btn.callback_data for btn in buttons if btn.callback_data]

                self.assertNotIn("recharge_currency:SYP", callbacks)
                self.assertNotIn("recharge_currency:USD", callbacks)

                self.assertEqual(
                    callbacks,
                    [
                        "recharge_package:intl:400",
                        "recharge_package:intl:900",
                        "recharge_package:intl:1600",
                    ],
                )
                btn_400 = kb.inline_keyboard[0][0]
                btn_900 = kb.inline_keyboard[1][0]
                btn_1600 = kb.inline_keyboard[2][0]

                self.assertIn("400", btn_400.text)
                self.assertIn("1", btn_400.text)
                self.assertIsNone(btn_400.style)

                self.assertIn("900", btn_900.text)
                self.assertIn("2", btn_900.text)
                self.assertTrue("11%" in btn_900.text or "%11" in btn_900.text)
                self.assertIsNone(btn_900.style)

                self.assertIn("1600", btn_1600.text)
                self.assertIn("3", btn_1600.text)
                self.assertTrue("25%" in btn_1600.text or "%25" in btn_1600.text)
                self.assertEqual(btn_1600.style, "success")

                support_btn = kb.inline_keyboard[3][0]
                self.assertIsNotNone(support_btn.url)
                self.assertIn("t.me/abiadd", support_btn.url)
                parsed_q = parse_qs(urlparse(support_btn.url).query)
                self.assertIn("1600", parsed_q["text"][0])


class PostConsentLanguageFlowTests(unittest.IsolatedAsyncioTestCase):
    async def test_accept_consent_shows_language_screen_then_confirm_resumes_pending_messages(self):
        i18n_mod = _load_i18n()
        saved_langs = {}

        async def save_lang(uid, code):
            saved_langs[uid] = code
            return code

        svc_stub = types.SimpleNamespace(
            _redis=lambda: types.SimpleNamespace(
                set=AsyncMock(return_value=True),
                delete=AsyncMock(return_value=1),
            ),
            CONSENT_ACCEPT="consent:accept",
            CONSENT_DECLINE="consent:decline",
            LANGUAGE_STEP_ENABLED=True,
            current_versions=AsyncMock(return_value=(1, 1)),
            remember_accepted=AsyncMock(),
            forget_denied=AsyncMock(),
            detect_initial_language=AsyncMock(return_value="tr"),
            save_user_language=AsyncMock(side_effect=save_lang),
            build_language_prompt_text=i18n_mod["build_language_screen_text"],
            build_language_keyboard=lambda code: i18n_mod["build_language_screen_keyboard"](code, prefix="consent:lang"),
            build_localized_accepted_message=i18n_mod["build_localized_accepted_text"],
            get_continue_button_label=lambda code: i18n_mod["DEEP_LINK_CONTINUE_LABELS"][code],
            get_consent_confirmed_toast=lambda code: i18n_mod["CONSENT_CONFIRMED_TOASTS"][code],
            pop_pending_messages=AsyncMock(return_value=["msg_pdf"]),
            pop_pending_callback=AsyncMock(return_value=None),
            pop_start_payload=AsyncMock(return_value=None),
            replay_pending_messages=AsyncMock(),
            build_accepted_text=Mock(return_value="accepted"),
        )

        edits = AsyncMock()
        spawned = []
        env = load_functions(
            "handlers/consent.py",
            {"_decide", "consent_language_pick", "consent_language_confirm"},
            svc=svc_stub,
            logger=MagicMock(),
            spawn=spawned.append,
            _edit_or_send=edits,
            types=fake_types,
        )

        rpc_mock = Mock()
        rpc_mock.return_value.execute = AsyncMock(return_value=types.SimpleNamespace(data=[{"changed": True}]))
        supabase_stub = types.SimpleNamespace(rpc=rpc_mock)
        log_event = AsyncMock()

        call = types.SimpleNamespace(
            from_user=types.SimpleNamespace(id=42, language_code="tr-TR"),
            bot=types.SimpleNamespace(get_me=AsyncMock(return_value=types.SimpleNamespace(username="abiadbot"))),
            answer=AsyncMock(),
            data="consent:accept",
        )

        with patch.dict("sys.modules", {
            "helpers.supabase_helper": types.SimpleNamespace(supabase=supabase_stub),
            "helpers.supabase_helper.analytics": types.SimpleNamespace(log_usage_event=log_event),
        }):
            await env["_decide"](call, "accepted")

        svc_stub.pop_pending_messages.assert_not_awaited()
        edits.assert_awaited_once()
        lang_text, lang_kb = edits.await_args.args[1], edits.await_args.args[2]
        self.assertIn("Türkçe", lang_text)
        continue_btn = lang_kb.inline_keyboard[3][0]
        self.assertEqual(continue_btn.callback_data, "consent:lang:confirm:tr")
        self.assertEqual(continue_btn.style, "success")

        edits.reset_mock()
        call_pick = types.SimpleNamespace(
            from_user=types.SimpleNamespace(id=42),
            data="consent:lang:pick:de",
            answer=AsyncMock(),
        )
        await env["consent_language_pick"](call_pick)
        picked_kb = edits.await_args.args[2]
        self.assertEqual(picked_kb.inline_keyboard[3][0].callback_data, "consent:lang:confirm:de")
        self.assertEqual(picked_kb.inline_keyboard[3][0].style, "success")

        edits.reset_mock()
        call_confirm = types.SimpleNamespace(
            from_user=types.SimpleNamespace(id=42),
            bot=call.bot,
            data="consent:lang:confirm:de",
            answer=AsyncMock(),
        )
        await env["consent_language_confirm"](call_confirm)
        self.assertEqual(saved_langs[42], "de")
        svc_stub.pop_pending_messages.assert_awaited_once_with(42, bot=call.bot)
        self.assertEqual(len(spawned), 1)
        await spawned[0]
        svc_stub.replay_pending_messages.assert_awaited_once_with(call.bot, ["msg_pdf"])
        self.assertIn("Vielen Dank", edits.await_args.args[1])


class PaymentsAndSettingsHandlersTests(unittest.IsolatedAsyncioTestCase):
    async def test_payments_handler_switches_between_arabic_syp_usd_and_intl_usd(self):
        i18n_env = _load_i18n()
        user_langs = {10: "ar", 20: "fr"}

        async def get_lang(uid, fallback_user_or_code=None):
            return user_langs.get(uid, "ar")

        i18n_stub = types.SimpleNamespace(
            get_user_language=AsyncMock(side_effect=get_lang),
            is_non_syrian_phone=i18n_env["is_non_syrian_phone"],
            resolve_recharge_currency=i18n_env["resolve_recharge_currency"],
            CURRENCY_TOGGLE_TO_USD_TEXT=i18n_env["CURRENCY_TOGGLE_TO_USD_TEXT"],
            build_recharge_view=i18n_env["build_recharge_view"],
            get_choose_package_alert=i18n_env["get_choose_package_alert"],
        )
        ps_stub = types.SimpleNamespace(
            get_active_packages=AsyncMock(return_value=[]),
            get_open_order_for_user=AsyncMock(return_value=None),
            format_money=lambda a, c: f"{a} {c}",
        )
        env = load_functions(
            "handlers/payments.py",
            {
                "_edit_or_send",
                "_calculator_button_row",
                "_support_row",
                "try_show_payment_packages",
                "toggle_recharge_currency",
                "select_Localized_recharge_package",
                "remind_to_choose_recharge_package",
            },
            types=fake_types,
            Optional=object,
            ADMIN_CONTACT="@abiadd",
            i18n=i18n_stub,
            ps=ps_stub,
            payments_available=AsyncMock(return_value=False),
            _show_open_order=AsyncMock(),
            log_usage_event=AsyncMock(),
            spawn=lambda c: c.close(),
            TelegramBadRequest=Exception,
            logger=MagicMock(),
        )

        msg_ar = types.SimpleNamespace(edit_text=AsyncMock(), answer=AsyncMock())
        call_ar = types.SimpleNamespace(
            data="recharge_info",
            from_user=types.SimpleNamespace(id=10, language_code="ar"),
            message=msg_ar,
            answer=AsyncMock(),
        )
        handled = await env["try_show_payment_packages"](call_ar)
        self.assertTrue(handled)
        kb_ar_syp = msg_ar.answer.await_args.kwargs["reply_markup"]
        cbs_ar_syp = [b.callback_data for r in kb_ar_syp.inline_keyboard for b in r]
        self.assertIn("recharge_currency:USD", cbs_ar_syp)
        self.assertIn("recharge_package:200", cbs_ar_syp)

        call_toggle = types.SimpleNamespace(
            data="recharge_currency:USD",
            from_user=types.SimpleNamespace(id=10, language_code="ar"),
            message=msg_ar,
            answer=AsyncMock(),
        )
        await env["toggle_recharge_currency"](call_toggle)
        kb_ar_usd = msg_ar.edit_text.await_args.kwargs["reply_markup"]
        cbs_ar_usd = [b.callback_data for r in kb_ar_usd.inline_keyboard for b in r]
        self.assertIn("recharge_currency:SYP", cbs_ar_usd)
        self.assertIn("recharge_package:ar_usd:200", cbs_ar_usd)

        msg_fr = types.SimpleNamespace(edit_text=AsyncMock(), answer=AsyncMock())
        call_fr = types.SimpleNamespace(
            data="recharge_info",
            from_user=types.SimpleNamespace(id=20, language_code="fr"),
            message=msg_fr,
            answer=AsyncMock(),
        )
        await env["try_show_payment_packages"](call_fr)
        kb_fr = msg_fr.answer.await_args.kwargs["reply_markup"]
        cbs_fr = [b.callback_data for r in kb_fr.inline_keyboard for b in r if b.callback_data]
        self.assertNotIn("recharge_currency:SYP", cbs_fr)
        self.assertEqual(kb_fr.inline_keyboard[2][0].style, "success")

    async def test_settings_panel_preserves_existing_buttons_and_adds_language_button(self):
        client = types.SimpleNamespace()
        client.table = lambda *_: client
        client.select = lambda *_: client
        client.eq = lambda *_: client
        client.single = lambda: client
        client.execute = AsyncMock(return_value=types.SimpleNamespace(data={"renewal_notifications": True}))

        env = load_functions("handlers/settings.py", {"settings_panel"}, supabase=client, types=fake_types)
        text, kb = await env["settings_panel"](42)
        self.assertIn("توقيت دمشق", text)
        self.assertEqual(kb.inline_keyboard[0][0].callback_data, "renewal:off")
        self.assertEqual(kb.inline_keyboard[1][0].callback_data, "student_home")
        self.assertEqual(kb.inline_keyboard[2][0].callback_data, "settings:lang")

    async def test_main_menu_settings_and_wallet_six_languages(self):
        i18n_env = _load_i18n()
        build_settings = i18n_env["build_settings_panel_content"]
        get_wallet = i18n_env["get_wallet_strings"]
        translate_kb = i18n_env["translate_main_menu_keyboard"]

        sample_kb = FakeInlineKeyboardMarkup([
            [FakeInlineKeyboardButton(text="❓ كيف أستخدم البوت؟", callback_data="student_help")],
            [FakeInlineKeyboardButton(text="⭐ اختباراتي المحفوظة", callback_data="favorites_menu")],
            [FakeInlineKeyboardButton(text="💰 رصيدي وهداياي", callback_data="gift:wallet")],
            [FakeInlineKeyboardButton(text="⚙️ الإعدادات", callback_data="student_settings")],
        ])

        for lang in ("ar", "en", "tr", "fr", "de", "fa"):
            s_text, s_kb = build_settings(lang, enabled=True)
            self.assertTrue(len(s_text) > 15)
            self.assertEqual(s_kb.inline_keyboard[0][0].callback_data, "renewal:off")
            self.assertEqual(s_kb.inline_keyboard[1][0].callback_data, "student_home")
            self.assertEqual(s_kb.inline_keyboard[2][0].callback_data, "settings:lang")

            ws = get_wallet(lang)
            self.assertIn("btn_recharge", ws)
            self.assertIn("redeem_errors", ws)

            if lang != "ar":
                t_kb = translate_kb(sample_kb, lang)
                self.assertNotEqual(t_kb.inline_keyboard[0][0].text, "❓ كيف أستخدم البوت؟")
                self.assertNotEqual(t_kb.inline_keyboard[2][0].text, "💰 رصيدي وهداياي")
                self.assertNotEqual(t_kb.inline_keyboard[3][0].text, "⚙️ الإعدادات")


if __name__ == "__main__":
    unittest.main()
