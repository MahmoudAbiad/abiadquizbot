"""شاشة ملخّص إعدادات الكويز الجماعي (gqs:) - القواعد والعرض والمعالج."""
import datetime
import json
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, Mock
from aiogram import types as telegram_types
from reliability_support import load_functions

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from services import group_certificate as gc  # noqa: E402
from services import group_ceremony as gcm  # noqa: E402

SID = "11111111-1111-1111-1111-111111111111"
CHAT = -100123
PRESETS_TOP = [3, 5, 10, 15, 20]

KB_NAMES = {
    "_setup_btn", "_mark", "_back_row", "get_group_setup_summary_keyboard", "get_group_setup_menu_keyboard",
    "get_group_setup_mode_keyboard", "get_group_setup_names_keyboard", "get_group_setup_top_keyboard",
    "get_group_setup_timer_keyboard", "get_group_setup_pace_keyboard", "get_group_setup_lock_keyboard",
    "get_group_setup_cert_keyboard", "get_group_setup_ceremony_keyboard",
}


def load_keyboards():
    return load_functions(
        "keyboards.py", KB_NAMES, types=telegram_types,
        GROUP_TOP_N_MIN=1, GROUP_TOP_N_MAX=20, GROUP_TOP_N_PRESETS=PRESETS_TOP,
        GROUP_TIMER_PRESETS=[0, 15, 30, 45, 60], GROUP_INTERVAL_PRESETS=[30, 60, 120, 300],
        _CERT_THEME_BUTTONS=(("blue", "🔵 أزرق"), ("green", "🟢 أخضر"), ("gold", "🟡 ذهبي"),
                             ("purple", "🟣 بنفسجي"), ("red", "🔴 أحمر"), ("slate", "⚫ رمادي")),
        _CERT_COUNT_CHOICES=(1, 3, 5, 10), _CEREMONY_COUNT_CHOICES=(3, 5, 10),
    )


def datas(kb):
    return [b.callback_data for row in kb.inline_keyboard for b in row]


def texts(kb):
    return [b.text for row in kb.inline_keyboard for b in row]


def load_rules(**over):
    kb = load_keyboards()
    env = dict(
        GROUP_TOP_N_MIN=1, GROUP_TOP_N_MAX=20, GROUP_TIMER_PRESETS=[0, 15, 30, 45, 60],
        GROUP_INTERVAL_PRESETS=[30, 60, 120, 300], SETUP_DEFAULT_TOP_N=5, SETUP_DEFAULT_INTERVAL_SECONDS=30,
        MIN_QUESTION_INTERVAL_SECONDS=12, BURST_GAP_SECONDS=6,
        _SETUP_PACE_LABELS={30: "كل 30 ثانية", 60: "كل دقيقة", 120: "كل دقيقتين", 300: "كل 5 دقائق"},
        _SETUP_HEADER_PREFIX="gq:setup_hdr:", _SETUP_HEADER_TTL=3600,
        _SETUP_FIELDS=("mode", "names", "top", "timer", "pace", "lock", "cert", "cer"),
        CERT_DEFAULT_TITLE=gc.DEFAULT_TITLE, CERT_THEMES=gc.THEMES, cert_apply_change=gc.apply_config_change,
        cert_eligibility=gc.eligibility, cert_normalize_config=gc.normalize_config,
        _CERT_AUTO_DISABLED_NOTE="ℹ️ عطّلت شهادات التقدير لأنها بتحتاج النمط التنافسي مع إظهار الأسماء وترتيب نهائي منشور.",
        group_cert_schema_ready=AsyncMock(return_value=False),
        ceremony_apply_change=gcm.apply_config_change, ceremony_eligibility=gcm.eligibility,
        ceremony_normalize_config=gcm.normalize_config,
        _CEREMONY_AUTO_DISABLED_NOTE="ℹ️ عطّلت حفل التكريم لأنه بيحتاج النمط التنافسي مع إظهار الأسماء وترتيب نهائي منشور.",
        group_ceremony_schema_ready=AsyncMock(return_value=False),
        get_group_setup_ceremony_keyboard=kb["get_group_setup_ceremony_keyboard"],
        _handle_ceremony_special=AsyncMock(),
        get_group_setup_cert_keyboard=kb["get_group_setup_cert_keyboard"],
        _handle_cert_special=AsyncMock(),
        _TOP_N_PROMPT="top-prompt", html=__import__("html"), json=json,
        Optional=object, Dict=dict, Any=object, Tuple=tuple, log_error=Mock(), logger=Mock(),
        quiz_lock_schema_ready=AsyncMock(return_value=True),
        get_group_setup_summary_keyboard=kb["get_group_setup_summary_keyboard"],
        get_group_setup_menu_keyboard=kb["get_group_setup_menu_keyboard"],
        get_group_setup_mode_keyboard=kb["get_group_setup_mode_keyboard"],
        get_group_setup_names_keyboard=kb["get_group_setup_names_keyboard"],
        get_group_setup_top_keyboard=kb["get_group_setup_top_keyboard"],
        get_group_setup_timer_keyboard=kb["get_group_setup_timer_keyboard"],
        get_group_setup_pace_keyboard=kb["get_group_setup_pace_keyboard"],
        get_group_setup_lock_keyboard=kb["get_group_setup_lock_keyboard"],
    )
    env.update(over)
    return load_functions(
        "handlers/group_quiz.py",
        {"_pace_to_mode", "_setup_state", "_compute_setup_update", "_compute_setup_update_core", "_cert_screen_text", "_ceremony_screen_text",
         "_setup_summary_text", "_setup_screen_view",
         "_edit_setup_screen", "setup_summary_callback", "_remember_setup_header", "_load_setup_header"},
        **env,
    )


def base_session(**kw):
    s = dict(id=SID, chat_id=CHAT, status="waiting", poll_is_anonymous=False, show_names=True, top_n=5,
             question_timer_seconds=None, pacing_mode="fixed_interval", question_interval_seconds=30,
             lock_chat=False, total_questions=10, started_by=1)
    s.update(kw)
    return s


class StateTests(unittest.TestCase):
    def setUp(self):
        self.env = load_rules()
        self.state = self.env["_setup_state"]

    def test_defaults(self):
        st = self.state(base_session(), is_channel=False)
        self.assertEqual((st["mode"], st["names"], st["top"], st["timer"], st["pace"], st["lock"]),
                         ("c", True, 5, 0, "f30", False))

    def test_missing_columns_fall_back_to_defaults(self):
        st = self.state({"id": SID}, is_channel=False)
        self.assertEqual((st["top"], st["names"], st["pace"]), (5, True, "f30"))

    def test_channel_is_always_anonymous_and_unlocked(self):
        st = self.state(base_session(poll_is_anonymous=False, lock_chat=True), is_channel=True)
        self.assertTrue(st["anonymous"])
        self.assertFalse(st["lock"])

    def test_burst_with_timer_is_normalized_to_fixed(self):
        st = self.state(base_session(pacing_mode="burst", question_timer_seconds=30), is_channel=False)
        self.assertEqual(st["pace"], "f30")

    def test_chain_without_timer_is_normalized_to_fixed(self):
        st = self.state(base_session(pacing_mode="chain_to_timer"), is_channel=False)
        self.assertEqual(st["pace"], "f30")

    def test_lock_is_dropped_with_burst(self):
        st = self.state(base_session(pacing_mode="burst", lock_chat=True), is_channel=False)
        self.assertEqual(st["pace"], "b")
        self.assertFalse(st["lock"])

    def test_top_n_is_clamped(self):
        self.assertEqual(self.state(base_session(top_n=99), is_channel=False)["top"], 20)
        self.assertEqual(self.state(base_session(top_n=-3), is_channel=False)["top"], 1)

    def test_unknown_interval_falls_back_to_default(self):
        self.assertEqual(self.state(base_session(question_interval_seconds=77), is_channel=False)["pace"], "f30")

    def test_pace_to_mode(self):
        p = self.env["_pace_to_mode"]
        self.assertEqual(p("b"), ("burst", 6))
        self.assertEqual(p("c"), ("chain_to_timer", None))
        self.assertEqual(p("f60"), ("fixed_interval", 60))


class RulesTests(unittest.TestCase):
    def setUp(self):
        self.env = load_rules()

    def upd(self, field, value, *, channel=False, lock_available=True, **session):
        return self.env["_compute_setup_update"](
            base_session(**session), field, value, is_channel=channel, lock_available=lock_available)

    def test_mode_saves(self):
        self.assertEqual(self.upd("mode", "a"), ({"poll_is_anonymous": True}, None))
        self.assertEqual(self.upd("mode", "c"), ({"poll_is_anonymous": False}, None))

    def test_competitive_rejected_for_channels(self):
        updates, err = self.upd("mode", "c", channel=True)
        self.assertIsNone(updates)
        self.assertTrue(err)

    def test_names_and_top_rejected_when_anonymous(self):
        for field, value in (("names", "0"), ("top", "7")):
            updates, err = self.upd(field, value, poll_is_anonymous=True)
            self.assertIsNone(updates)
            self.assertTrue(err)

    def test_names_and_top_save_when_competitive(self):
        self.assertEqual(self.upd("names", "0"), ({"show_names": False}, None))
        self.assertEqual(self.upd("top", "7"), ({"top_n": 7}, None))

    def test_invalid_values_rejected(self):
        for field, value in (("mode", "x"), ("names", "2"), ("top", "0"), ("top", "21"), ("top", "abc"),
                             ("timer", "17"), ("timer", "x"), ("pace", "f45"), ("pace", "fx"), ("pace", "z"),
                             ("lock", "2"), ("bogus", "1")):
            with self.subTest(field=field, value=value):
                updates, err = self.upd(field, value)
                self.assertIsNone(updates)
                self.assertTrue(err)

    def test_timer_saves_and_zero_clears(self):
        self.assertEqual(self.upd("timer", "30"), ({"question_timer_seconds": 30}, None))
        self.assertEqual(self.upd("timer", "0"), ({"question_timer_seconds": None}, None))

    def test_enabling_timer_during_burst_resets_pace(self):
        updates, err = self.upd("timer", "30", pacing_mode="burst", question_interval_seconds=6)
        self.assertIsNone(err)
        self.assertEqual(updates["pacing_mode"], "fixed_interval")
        self.assertEqual(updates["question_interval_seconds"], 30)

    def test_disabling_timer_during_chain_resets_pace(self):
        updates, err = self.upd("timer", "0", pacing_mode="chain_to_timer", question_timer_seconds=30,
                                question_interval_seconds=None)
        self.assertIsNone(err)
        self.assertEqual(updates["pacing_mode"], "fixed_interval")

    def test_timer_change_keeps_fixed_pace_untouched(self):
        updates, _ = self.upd("timer", "45", question_interval_seconds=120)
        self.assertEqual(updates, {"question_timer_seconds": 45})

    def test_burst_requires_no_timer_and_no_lock(self):
        self.assertEqual(self.upd("pace", "b"), ({"pacing_mode": "burst", "question_interval_seconds": 6}, None))
        self.assertIsNone(self.upd("pace", "b", question_timer_seconds=30)[0])
        self.assertIsNone(self.upd("pace", "b", lock_chat=True)[0])

    def test_chain_requires_timer(self):
        self.assertIsNone(self.upd("pace", "c")[0])
        self.assertEqual(self.upd("pace", "c", question_timer_seconds=30)[0],
                         {"pacing_mode": "chain_to_timer", "question_interval_seconds": None})

    def test_fixed_interval_values(self):
        for v in (30, 60, 120, 300):
            self.assertEqual(self.upd("pace", f"f{v}")[0],
                             {"pacing_mode": "fixed_interval", "question_interval_seconds": v})

    def test_lock_rules(self):
        self.assertEqual(self.upd("lock", "1"), ({"lock_chat": True}, None))
        self.assertEqual(self.upd("lock", "0"), ({"lock_chat": False}, None))
        self.assertIsNone(self.upd("lock", "1", pacing_mode="burst")[0])
        self.assertIsNone(self.upd("lock", "1", channel=True)[0])
        self.assertIsNone(self.upd("lock", "1", lock_available=False)[0])

    def test_min_interval_floor_is_applied(self):
        env = load_rules(MIN_QUESTION_INTERVAL_SECONDS=40)
        updates, _ = env["_compute_setup_update"](base_session(), "pace", "f30", is_channel=False)
        self.assertEqual(updates["question_interval_seconds"], 40)


class SummaryTextTests(unittest.TestCase):
    def setUp(self):
        self.env = load_rules()

    def text(self, channel=False, lock_available=True, title="كويز", dest="مجموعة", **session):
        st = self.env["_setup_state"](base_session(**session), is_channel=channel)
        return self.env["_setup_summary_text"](
            st, title=title, dest_title=dest, total=10, is_channel=channel, lock_available=lock_available)

    def test_default_summary(self):
        t = self.text()
        for part in ("تنافسي", "ظاهرة", "أول 5", "بدون", "كل 30 ثانية", "معطّل", "عدد الأسئلة: 10", "👥 الغروب"):
            self.assertIn(part, t)

    def test_titles_are_html_escaped(self):
        t = self.text(title="<b>x</b>&", dest="<i>g</i>")
        self.assertNotIn("<b>x</b>", t)
        self.assertIn("&lt;b&gt;x&lt;/b&gt;&amp;", t)
        self.assertIn("&lt;i&gt;g&lt;/i&gt;", t)

    def test_anonymous_hides_names_and_ranking(self):
        t = self.text(poll_is_anonymous=True)
        self.assertIn("مجهول", t)
        self.assertNotIn("الأسماء بالترتيب", t)
        self.assertNotIn("الترتيب النهائي", t)

    def test_burst_says_ranking_is_not_published(self):
        t = self.text(pacing_mode="burst")
        self.assertIn("دفعة واحدة", t)
        self.assertIn("ما بينشر", t)
        self.assertNotIn("أول 5", t)

    def test_channel_has_no_lock_line_and_says_channel(self):
        t = self.text(channel=True)
        self.assertNotIn("قفل", t)
        self.assertIn("📢 القناة", t)
        self.assertIn("مجهول", t)

    def test_lock_line_hidden_when_schema_not_ready(self):
        self.assertNotIn("قفل", self.text(lock_available=False))

    def test_timer_and_chain_labels(self):
        t = self.text(question_timer_seconds=30, pacing_mode="chain_to_timer")
        self.assertIn("30 ثانية", t)
        self.assertIn("فور انتهاء المؤقّت", t)

    def test_lock_enabled_label(self):
        self.assertIn("مفعّل", self.text(lock_chat=True))


class KeyboardTests(unittest.TestCase):
    def setUp(self):
        self.kb = load_keyboards()

    def test_all_callbacks_fit_telegram_limit(self):
        k = self.kb
        boards = [
            k["get_group_setup_summary_keyboard"](SID),
            k["get_group_setup_menu_keyboard"](SID),
            k["get_group_setup_mode_keyboard"](SID, anonymous=False),
            k["get_group_setup_names_keyboard"](SID, show_names=True),
            k["get_group_setup_top_keyboard"](SID, 20),
            k["get_group_setup_timer_keyboard"](SID, 60),
            k["get_group_setup_pace_keyboard"](SID, pace="f300", timer_enabled=True),
            k["get_group_setup_pace_keyboard"](SID, pace="b", timer_enabled=False),
            k["get_group_setup_lock_keyboard"](SID, lock=True),
        ]
        for board in boards:
            for data in datas(board):
                self.assertLessEqual(len(data.encode()), 64, data)
                self.assertTrue(data.startswith(("gqs:", "gqc:")), data)

    def test_summary_has_go_settings_cancel(self):
        d = datas(self.kb["get_group_setup_summary_keyboard"](SID))
        self.assertEqual(d, [f"gqs:{SID}:go", f"gqs:{SID}:m", f"gqc:{SID}"])

    def test_menu_hides_irrelevant_entries(self):
        menu = self.kb["get_group_setup_menu_keyboard"]
        self.assertNotIn(f"gqs:{SID}:o:mode", datas(menu(SID, is_channel=True)))
        self.assertNotIn(f"gqs:{SID}:o:lock", datas(menu(SID, is_channel=True)))
        anon = datas(menu(SID, anonymous=True))
        self.assertNotIn(f"gqs:{SID}:o:names", anon)
        self.assertNotIn(f"gqs:{SID}:o:top", anon)
        self.assertNotIn(f"gqs:{SID}:o:lock", datas(menu(SID, lock_available=False)))
        full = datas(menu(SID))
        for field in ("mode", "names", "top", "timer", "pace", "lock"):
            self.assertIn(f"gqs:{SID}:o:{field}", full)
        self.assertIn(f"gqs:{SID}:b", full)

    def test_check_mark_on_current_choice(self):
        k = self.kb
        t = texts(k["get_group_setup_mode_keyboard"](SID, anonymous=True))
        self.assertTrue(t[1].startswith("✅"))
        self.assertFalse(t[0].startswith("✅"))
        t = texts(k["get_group_setup_timer_keyboard"](SID, 30))
        self.assertEqual(sum(x.startswith("✅") for x in t), 1)
        self.assertTrue(any(x.startswith("✅") and "30" in x for x in t))

    def test_pace_screen_offers_only_valid_modes(self):
        pace = self.kb["get_group_setup_pace_keyboard"]
        with_timer = datas(pace(SID, pace="f30", timer_enabled=True))
        self.assertIn(f"gqs:{SID}:s:pace:c", with_timer)
        self.assertNotIn(f"gqs:{SID}:s:pace:b", with_timer)
        without = datas(pace(SID, pace="f30", timer_enabled=False))
        self.assertIn(f"gqs:{SID}:s:pace:b", without)
        self.assertNotIn(f"gqs:{SID}:s:pace:c", without)
        locked = datas(pace(SID, pace="f30", timer_enabled=False, lock=True))
        self.assertNotIn(f"gqs:{SID}:s:pace:b", locked)

    def test_top_stepper_stays_in_bounds(self):
        top = self.kb["get_group_setup_top_keyboard"]
        low = datas(top(SID, 1))
        self.assertIn(f"gqs:{SID}:s:top:1", low)
        high = datas(top(SID, 20))
        self.assertNotIn(f"gqs:{SID}:s:top:21", high)

    def test_back_buttons_lead_to_menu(self):
        for board in (self.kb["get_group_setup_lock_keyboard"](SID, lock=False),
                      self.kb["get_group_setup_names_keyboard"](SID, show_names=False)):
            self.assertEqual(datas(board)[-1], f"gqs:{SID}:m")


def make_call(data, chat_type="private", user_id=1):
    call = Mock()
    call.data = data
    call.answer = AsyncMock()
    call.from_user = types.SimpleNamespace(id=user_id)
    call.message = Mock()
    call.message.chat = types.SimpleNamespace(type=chat_type, id=user_id)
    call.message.edit_text = AsyncMock()
    return call


class HandlerTests(unittest.IsolatedAsyncioTestCase):
    def build(self, session=None, *, channel=False, lock_perm=True, save_ok=True, header=None):
        self.session = session or base_session()
        self.guard = AsyncMock(return_value=self.session)
        self.update = AsyncMock(return_value=(self.session if save_ok else None))
        self.launch = AsyncMock()
        self.ensure = AsyncMock(return_value=lock_perm)
        self.redis = Mock(get=AsyncMock(return_value=json.dumps(header or {"title": "اختبار", "dest_title": "غروب"})))
        return load_rules(
            _guard_config_callback=self.guard, update_session=self.update, _launch_group_session=self.launch,
            _ensure_group_lock_permission=self.ensure, _is_channel_chat=AsyncMock(return_value=channel),
            redis_client=self.redis,
        )

    async def press(self, env, data, **kw):
        call = make_call(data, **kw)
        await env["setup_summary_callback"](call)
        return call

    async def test_go_launches_with_current_settings(self):
        env = self.build(base_session(question_interval_seconds=60))
        call = await self.press(env, f"gqs:{SID}:go")
        self.launch.assert_awaited_once_with(call, self.session, SID, "fixed_interval", 60)
        self.ensure.assert_not_awaited()

    async def test_go_with_burst_and_chain(self):
        env = self.build(base_session(pacing_mode="burst"))
        call = await self.press(env, f"gqs:{SID}:go")
        self.launch.assert_awaited_once_with(call, self.session, SID, "burst", 6)
        env = self.build(base_session(pacing_mode="chain_to_timer", question_timer_seconds=30))
        call = await self.press(env, f"gqs:{SID}:go")
        self.launch.assert_awaited_once_with(call, self.session, SID, "chain_to_timer", None)

    async def test_go_with_lock_checks_permission_first_with_gqs_buttons(self):
        env = self.build(base_session(lock_chat=True), lock_perm=False)
        await self.press(env, f"gqs:{SID}:go")
        self.launch.assert_not_awaited()
        kwargs = self.ensure.await_args.kwargs
        self.assertEqual(kwargs["retry_data"], f"gqs:{SID}:go")
        self.assertEqual(kwargs["skip_data"], f"gqs:{SID}:s:lock:0")
        self.assertLessEqual(len(kwargs["skip_data"].encode()), 64)

    async def test_go_with_lock_and_permission_launches(self):
        env = self.build(base_session(lock_chat=True))
        await self.press(env, f"gqs:{SID}:go")
        self.launch.assert_awaited_once()

    async def test_go_normalizes_stale_lock_with_burst(self):
        env = self.build(base_session(pacing_mode="burst", lock_chat=True))
        await self.press(env, f"gqs:{SID}:go")
        self.update.assert_awaited_once_with(SID, {"lock_chat": False})
        self.ensure.assert_not_awaited()
        self.launch.assert_awaited_once()

    async def test_go_forces_anonymous_for_channel_sessions(self):
        env = self.build(base_session(poll_is_anonymous=False), channel=True)
        await self.press(env, f"gqs:{SID}:go")
        self.update.assert_awaited_once_with(SID, {"poll_is_anonymous": True})
        self.launch.assert_awaited_once()

    async def test_go_stops_when_normalization_cannot_be_saved(self):
        env = self.build(base_session(pacing_mode="burst", lock_chat=True), save_ok=False)
        call = await self.press(env, f"gqs:{SID}:go")
        self.launch.assert_not_awaited()
        self.assertTrue(any(c.kwargs.get("show_alert") for c in call.answer.await_args_list))

    async def test_unauthorized_guard_blocks_everything(self):
        env = self.build()
        self.guard.return_value = None
        for tail in ("go", "b", "m", "o:mode", "s:top:7"):
            call = await self.press(env, f"gqs:{SID}:{tail}")
            call.message.edit_text.assert_not_awaited()
        self.launch.assert_not_awaited()
        self.update.assert_not_awaited()

    async def test_summary_and_menu_render_without_writes(self):
        env = self.build()
        call = await self.press(env, f"gqs:{SID}:b")
        text = call.message.edit_text.await_args.args[0]
        self.assertIn("اختبار", text)
        self.assertEqual(call.message.edit_text.await_args.kwargs["parse_mode"], "HTML")
        call = await self.press(env, f"gqs:{SID}:m")
        self.assertIn(f"gqs:{SID}:o:timer", datas(call.message.edit_text.await_args.kwargs["reply_markup"]))
        self.update.assert_not_awaited()

    async def test_missing_redis_header_falls_back_to_generic_title(self):
        env = self.build()
        self.redis.get = AsyncMock(return_value=None)
        call = await self.press(env, f"gqs:{SID}:b")
        self.assertIn("كويز", call.message.edit_text.await_args.args[0])

    async def test_redis_failure_does_not_break_summary(self):
        env = self.build()
        self.redis.get = AsyncMock(side_effect=ConnectionError("down"))
        call = await self.press(env, f"gqs:{SID}:b")
        call.message.edit_text.assert_awaited_once()

    async def test_setting_saves_once_and_redraws_same_screen_with_check(self):
        env = self.build()
        call = await self.press(env, f"gqs:{SID}:s:top:7")
        self.update.assert_awaited_once_with(SID, {"top_n": 7})
        kb = call.message.edit_text.await_args.kwargs["reply_markup"]
        # الخيار الجديد (7) مو من الأزرار الجاهزة، فالمؤشّر بيظهر بعدّاد الستيبر
        self.assertIn("🏅 7", texts(kb))
        self.assertIn(f"gqs:{SID}:m", datas(kb))

    async def test_timer_conflict_resets_pace_and_tells_the_admin(self):
        env = self.build(base_session(pacing_mode="burst", question_interval_seconds=6))
        call = await self.press(env, f"gqs:{SID}:s:timer:30")
        saved = self.update.await_args.args[1]
        self.assertEqual(saved["pacing_mode"], "fixed_interval")
        self.assertIn("ℹ️", call.message.edit_text.await_args.args[0])

    async def test_rejected_setting_writes_nothing(self):
        env = self.build(base_session(question_timer_seconds=30))
        call = await self.press(env, f"gqs:{SID}:s:pace:b")
        self.update.assert_not_awaited()
        self.assertTrue(any(c.kwargs.get("show_alert") for c in call.answer.await_args_list))

    async def test_save_failure_alerts_and_does_not_redraw(self):
        env = self.build(save_ok=False)
        call = await self.press(env, f"gqs:{SID}:s:top:7")
        call.message.edit_text.assert_not_awaited()
        self.assertTrue(any(c.kwargs.get("show_alert") for c in call.answer.await_args_list))

    async def test_enabling_lock_without_permission_keeps_state_and_uses_gqs_buttons(self):
        env = self.build(lock_perm=False)
        await self.press(env, f"gqs:{SID}:s:lock:1")
        self.update.assert_not_awaited()
        kwargs = self.ensure.await_args.kwargs
        self.assertEqual(kwargs["retry_data"], f"gqs:{SID}:s:lock:1")
        self.assertEqual(kwargs["skip_data"], f"gqs:{SID}:s:lock:0")

    async def test_enabling_lock_with_permission_saves(self):
        env = self.build()
        await self.press(env, f"gqs:{SID}:s:lock:1")
        self.update.assert_awaited_once_with(SID, {"lock_chat": True})

    async def test_disabling_lock_needs_no_permission_check(self):
        env = self.build(base_session(lock_chat=True))
        await self.press(env, f"gqs:{SID}:s:lock:0")
        self.ensure.assert_not_awaited()
        self.update.assert_awaited_once_with(SID, {"lock_chat": False})

    async def test_hidden_screens_are_refused_server_side(self):
        env = self.build(channel=True)
        for field in ("mode", "lock"):
            call = await self.press(env, f"gqs:{SID}:o:{field}")
            call.message.edit_text.assert_not_awaited()
        env = self.build(base_session(poll_is_anonymous=True))
        for field in ("names", "top"):
            call = await self.press(env, f"gqs:{SID}:o:{field}")
            call.message.edit_text.assert_not_awaited()

    async def test_lock_screen_refused_when_schema_not_ready(self):
        env = self.build()
        env["quiz_lock_schema_ready"].return_value = False
        call = await self.press(env, f"gqs:{SID}:o:lock")
        call.message.edit_text.assert_not_awaited()

    async def test_malformed_callbacks_are_rejected(self):
        env = self.build()
        for data in (f"gqs:{SID}", f"gqs:{SID}:zzz", f"gqs:{SID}:o", f"gqs:{SID}:o:bogus",
                     f"gqs:{SID}:s:top", f"gqs:{SID}:s:bogus:1", f"gqs:{SID}:s:top:5:extra", "gqs:"):
            with self.subTest(data=data):
                self.guard.reset_mock()
                call = await self.press(env, data)
                self.guard.assert_not_awaited()
                self.update.assert_not_awaited()
                self.assertTrue(any(c.kwargs.get("show_alert") for c in call.answer.await_args_list))

    async def test_noop_does_nothing(self):
        env = self.build()
        call = await self.press(env, f"gqs:{SID}:noop")
        self.guard.assert_not_awaited()
        call.message.edit_text.assert_not_awaited()

    async def test_unchanged_message_error_is_swallowed(self):
        env = self.build()
        call = make_call(f"gqs:{SID}:s:top:5")
        call.message.edit_text.side_effect = RuntimeError("Bad Request: message is not modified")
        await env["setup_summary_callback"](call)
        env["log_error"].assert_not_called()

    async def test_other_edit_errors_are_logged_not_raised(self):
        env = self.build()
        call = make_call(f"gqs:{SID}:s:top:5")
        call.message.edit_text.side_effect = RuntimeError("boom")
        await env["setup_summary_callback"](call)
        env["log_error"].assert_called_once()


class StartSetupTests(unittest.IsolatedAsyncioTestCase):
    async def run_setup(self, *, is_channel=False, defaults_ok=True, dest="غروب <x>"):
        kb = load_keyboards()
        env = load_rules()
        extra = load_functions(
            "handlers/group_quiz.py", {"_start_session_setup"},
            is_group_admin=AsyncMock(return_value=True), get_open_session_for_chat=AsyncMock(return_value=None),
            get_shared_quiz=AsyncMock(return_value={"id": "q1", "source_title": "عنوان <b>", "quiz_data": [1, 2, 3]}),
            check_or_add_user=AsyncMock(),
            create_session=AsyncMock(return_value={"id": SID, "total_questions": 3, "chat_id": CHAT}),
            update_session=AsyncMock(return_value={"id": SID} if defaults_ok else None),
            claim_session_cancel=AsyncMock(), _safe_call=self._safe,
            _remember_setup_header=AsyncMock(), _setup_state=env["_setup_state"],
            _setup_summary_text=env["_setup_summary_text"], quiz_lock_schema_ready=AsyncMock(return_value=True),
            get_group_setup_summary_keyboard=kb["get_group_setup_summary_keyboard"],
            SETUP_DEFAULT_TOP_N=5, SETUP_DEFAULT_INTERVAL_SECONDS=30, html=__import__("html"),
            Optional=object, Dict=dict, Any=object,
        )
        ui = Mock(reply=AsyncMock(), answer=AsyncMock())
        user = types.SimpleNamespace(id=1, username="u", first_name="f", last_name="l")
        await extra["_start_session_setup"](ui_msg=ui, chat_id=CHAT, user=user, share_id="s",
                                            is_channel=is_channel, dest_title=dest)
        return extra, ui

    @staticmethod
    async def _safe(factory, **kwargs):
        return await factory()

    async def test_defaults_are_written_and_summary_is_shown(self):
        extra, ui = await self.run_setup()
        defaults = extra["update_session"].await_args.args[1]
        self.assertEqual(defaults["top_n"], 5)
        self.assertEqual(defaults["pacing_mode"], "fixed_interval")
        self.assertEqual(defaults["question_interval_seconds"], 30)
        self.assertIsNone(defaults["question_timer_seconds"])
        self.assertTrue(defaults["show_names"])
        self.assertFalse(defaults["poll_is_anonymous"])
        text = ui.answer.await_args.args[0]
        self.assertIn("&lt;b&gt;", text)
        self.assertIn("&lt;x&gt;", text)
        self.assertEqual(datas(ui.answer.await_args.kwargs["reply_markup"])[0], f"gqs:{SID}:go")
        extra["_remember_setup_header"].assert_awaited_once()

    async def test_channel_defaults_are_anonymous(self):
        extra, ui = await self.run_setup(is_channel=True)
        self.assertTrue(extra["update_session"].await_args.args[1]["poll_is_anonymous"])
        self.assertIn("📢 القناة", ui.answer.await_args.args[0])

    async def test_failed_defaults_cancel_the_session(self):
        extra, ui = await self.run_setup(defaults_ok=False)
        extra["claim_session_cancel"].assert_awaited_once_with(SID)
        ui.answer.assert_not_awaited()


class LaunchRefactorTests(unittest.TestCase):
    def test_old_pacing_handler_delegates_to_shared_launch(self):
        import ast
        from pathlib import Path
        src = Path(__file__).resolve().parents[1].joinpath("handlers", "group_quiz.py").read_text(encoding="utf-8")
        tree = ast.parse(src)
        fns = {n.name: n for n in tree.body if isinstance(n, (ast.AsyncFunctionDef, ast.FunctionDef))}
        self.assertIn("_launch_group_session", fns)
        old = ast.get_source_segment(src, fns["choose_group_pacing"])
        self.assertIn("_launch_group_session(", old)
        self.assertNotIn("start_waiting_session(", old)
        new = ast.get_source_segment(src, fns["setup_summary_callback"])
        self.assertIn("_launch_group_session(", new)

    def test_lock_helper_keeps_old_defaults(self):
        import inspect  # noqa: F401
        from pathlib import Path
        src = Path(__file__).resolve().parents[1].joinpath("handlers", "group_quiz.py").read_text(encoding="utf-8")
        self.assertIn('retry_data or f"gql:{sid}:1"', src)
        self.assertIn('skip_data or f"gql:{sid}:0"', src)

    def test_middleware_lets_gqs_through(self):
        from pathlib import Path
        src = Path(__file__).resolve().parents[1].joinpath("middlewares.py").read_text(encoding="utf-8")
        self.assertIn('"gqs:"', src)


if __name__ == "__main__":
    unittest.main()
