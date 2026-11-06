"""شهادات التقدير بالكويز الجماعي (المرحلة ب): القواعد، الرسم، الإرسال، شاشة الإعداد، والمعاينة."""
import asyncio
import datetime
import html
import io
import json
import struct
import sys
import types
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from aiogram import types as telegram_types
from reliability_support import load_functions

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from services import group_certificate as gc  # noqa: E402
from services import certificate_renderer as cr  # noqa: E402
from services import offload_worker_core as core  # noqa: E402
import test_group_quiz_setup_summary as base  # noqa: E402

SID = base.SID
PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


def png_size(data: bytes):
    assert data.startswith(PNG_MAGIC)
    return struct.unpack(">II", data[16:24])


def cfg(**kw):
    c = gc.normalize_config({"enabled": True, "title": "اختبار الفيزياء"})
    c.update(kw)
    return c


def payload(**kw):
    p = gc.build_payload(cfg(), name="أحمد الخطيب", group_name="شعبة العلمي", rank=1, score=8, total=10, date_text="2026/11/05")
    p.update(kw)
    return p


# ==================== المنطق الصرف ====================
class TextCleaningTests(unittest.TestCase):
    def test_full_name_is_first_plus_last(self):
        self.assertEqual(gc.display_name("أحمد", "الخطيب", "ahmad", 1), "أحمد الخطيب")

    def test_emoji_and_hidden_direction_marks_are_removed(self):
        self.assertEqual(gc.display_name("أحمد 😀", "\u202eالخطيب", None, 1), "أحمد الخطيب")

    def test_falls_back_to_username_then_generic(self):
        self.assertEqual(gc.display_name("😀", "🎉", "@bob", 1), "@bob")
        self.assertEqual(gc.display_name("😀", None, None, 1), "متفوّق")
        self.assertEqual(gc.display_name(None, None, "", 5), "متفوّق")

    def test_unsupported_scripts_are_dropped_not_boxed(self):
        self.assertEqual(gc.clean_text("Ivan Иван 王", 40), "Ivan")

    def test_long_name_cut_at_word_without_ellipsis(self):
        name = gc.display_name("سارة محمد عبد الرحمن", "الأحمد الدمشقي الطويل", None, 1)
        self.assertLessEqual(len(name), gc.NAME_MAX_CHARS)
        self.assertNotIn("…", name)
        self.assertTrue(name.startswith("سارة محمد"))

    def test_title_gets_ellipsis_when_cut(self):
        out = gc.clean_text("ا" * 100, gc.TITLE_MAX_CHARS)
        self.assertEqual(len(out), gc.TITLE_MAX_CHARS)
        self.assertTrue(out.endswith("…"))

    def test_control_chars_and_newlines_collapse(self):
        self.assertEqual(gc.clean_text("a\n\n b\t\x00c", 20), "a b c")


class ConfigTests(unittest.TestCase):
    def test_garbage_gives_valid_disabled_defaults(self):
        for raw in (None, "x", [], {"count": 99, "theme": "nope", "title": "   "}):
            c = gc.normalize_config(raw)
            self.assertFalse(c["enabled"])
            self.assertTrue(c["title"])
            self.assertIn(c["theme"], gc.THEMES)
            self.assertIn(c["count"], gc.COUNT_CHOICES)

    def test_title_is_never_empty_and_defaults_to_quiz_title(self):
        self.assertEqual(gc.normalize_config({"title": "😀"}, default_title="كويز الكيمياء")["title"], "كويز الكيمياء")

    def test_enabled_must_be_exactly_true(self):
        self.assertFalse(gc.normalize_config({"enabled": "yes"})["enabled"])
        self.assertFalse(gc.normalize_config({"enabled": 1})["enabled"])

    def test_every_change_value(self):
        c = gc.normalize_config({})
        self.assertTrue(gc.apply_config_change(c, "1")[0]["enabled"])
        self.assertEqual(gc.apply_config_change(c, "thgold")[0]["theme"], "gold")
        self.assertFalse(gc.apply_config_change(c, "d0")[0]["show_date"])
        self.assertFalse(gc.apply_config_change(c, "r0")[0]["show_rank"])
        self.assertFalse(gc.apply_config_change(c, "sc0")[0]["show_score"])
        self.assertEqual(gc.apply_config_change(c, "n5")[0]["count"], 5)
        self.assertEqual(gc.apply_config_change(c, "n10")[0]["count"], 10)
        for bad in ("", "x", "thpink", "n2", "n", "n11", "d2", "pv", "1 "):
            self.assertIsNotNone(gc.apply_config_change(c, bad)[1], bad)

    def test_change_does_not_mutate_input(self):
        c = gc.normalize_config({})
        gc.apply_config_change(c, "1")
        self.assertFalse(c["enabled"])

    def test_six_themes(self):
        self.assertEqual(len(gc.THEMES), 6)


class EligibilityTests(unittest.TestCase):
    def ok(self, **kw):
        args = dict(anonymous=False, names=True, pace="f30", is_channel=False)
        args.update(kw)
        return gc.eligibility(**args)

    def test_competitive_named_is_ok(self):
        self.assertEqual(self.ok(), (True, None))
        self.assertEqual(self.ok(pace="c")[0], True)

    def test_blocked_cases_have_reasons(self):
        for kw in ({"anonymous": True}, {"names": False}, {"pace": "b"}, {"is_channel": True}):
            ok, reason = self.ok(**kw)
            self.assertFalse(ok)
            self.assertTrue(reason)


class WinnersTests(unittest.TestCase):
    def rows(self, spec):
        return [{"user_id": i, "score": s, "total_answer_time_ms": t, "first_name": f"u{i}"} for i, (s, t) in enumerate(spec, 1)]

    def test_ranks_follow_leaderboard_order(self):
        ranked = gc.assign_ranks(self.rows([(9, 100), (8, 50), (8, 70)]))
        self.assertEqual([r["rank"] for r in ranked], [1, 2, 3])

    def test_exact_tie_shares_rank_and_next_skips(self):
        ranked = gc.assign_ranks(self.rows([(9, 100), (9, 100), (7, 10)]))
        self.assertEqual([r["rank"] for r in ranked], [1, 1, 3])

    def test_zero_score_gets_no_certificate(self):
        winners = gc.select_winners(self.rows([(3, 1), (0, 1), (0, 2)]), cfg(count=5), 20)
        self.assertEqual([w["user_id"] for w in winners], [1])

    def test_count_and_top_n_both_limit(self):
        rows = self.rows([(12, 1), (11, 1), (10, 1), (9, 1), (8, 1), (7, 1), (6, 1), (5, 1), (4, 1), (3, 1)])
        self.assertEqual(len(gc.select_winners(rows, cfg(count=5), 20)), 5)
        self.assertEqual(len(gc.select_winners(rows, cfg(count=10), 20)), 10)
        self.assertEqual(len(gc.select_winners(rows, cfg(count=3), 20)), 3)
        self.assertEqual(len(gc.select_winners(rows, cfg(count=5), 2)), 2)
        self.assertEqual(len(gc.select_winners(rows, cfg(count=1), 20)), 1)

    def test_no_rows_no_winners(self):
        self.assertEqual(gc.select_winners([], cfg(), 5), [])

    def test_input_rows_not_mutated(self):
        rows = self.rows([(9, 1)])
        gc.select_winners(rows, cfg(), 5)
        self.assertNotIn("rank", rows[0])


class PayloadTests(unittest.TestCase):
    def test_payload_has_no_disclaimer_field(self):
        self.assertFalse([k for k in payload() if "disclaimer" in k.lower()])

    def test_date_hidden_when_disabled(self):
        p = gc.build_payload(cfg(show_date=False), name="x", group_name="g", rank=1, score=1, total=2, date_text="2026/11/05")
        self.assertEqual(p["date_text"], "")

    def test_payload_is_json_serializable(self):
        json.dumps(payload(), ensure_ascii=False)

    def test_format_date_uses_syria_offset(self):
        self.assertEqual(gc.format_date(datetime.datetime(2026, 11, 5, 22, 30)), "2026/11/06")


# ==================== الرسم ====================
class RendererTests(unittest.TestCase):
    def test_every_theme_renders_a_valid_png(self):
        for theme in gc.THEMES:
            data = cr.render_certificate(payload(theme=theme))
            self.assertEqual(png_size(data), (cr.WIDTH, cr.HEIGHT), theme)

    def test_latin_arabic_and_mixed_names(self):
        for name in ("Mahmoud Al-Abiad", "سارة", "Sara سارة", "@user_name", "متفوّق", "x" * 28):
            self.assertEqual(png_size(cr.render_certificate(payload(name=name)))[0], cr.WIDTH, name)

    def test_optional_parts_can_all_be_off(self):
        data = cr.render_certificate(payload(show_rank=False, show_score=False, date_text="", issuer="", group_name=""))
        self.assertEqual(png_size(data), (cr.WIDTH, cr.HEIGHT))

    def test_long_texts_do_not_crash(self):
        data = cr.render_certificate(payload(title="ا" * 60, group_name="g" * 48, issuer="i" * 40, name="n" * 28))
        self.assertEqual(png_size(data), (cr.WIDTH, cr.HEIGHT))

    def test_preview_differs_from_final(self):
        self.assertNotEqual(cr.render_certificate(payload(preview=True)), cr.render_certificate(payload(preview=False)))

    def test_different_themes_differ(self):
        self.assertNotEqual(cr.render_certificate(payload(theme="blue")), cr.render_certificate(payload(theme="red")))

    def test_disclaimer_cannot_be_overridden_by_payload(self):
        plain = cr.render_certificate(payload())
        sneaky = cr.render_certificate({**payload(), "disclaimer": "", "disclaimer_text": "x", "footer": "y"})
        self.assertEqual(plain, sneaky)

    def test_rendering_is_deterministic(self):
        self.assertEqual(cr.render_certificate(payload()), cr.render_certificate(payload()))

    def test_logo_exists_and_is_used(self):
        self.assertTrue((ROOT / "assets" / "abiad_logo_full_badge.png").exists())
        self.assertIsNotNone(cr._logo(150))

    def test_validation_rejects_bad_payloads(self):
        bad = [
            None, [], {}, payload(theme="neon"), payload(rank=0), payload(rank=True), payload(score=-1),
            payload(total=0), payload(show_rank="yes"), payload(preview=1), payload(date_text="x" * 40), payload(name=5),
        ]
        for item in bad:
            with self.assertRaises(cr.CertificateError, msg=repr(item)[:60]):
                cr.render_certificate(item)

    def test_validation_cleans_hostile_text(self):
        out = cr.validate_payload(payload(name="\u202e😀Sara\x00", title="😀", group_name="\n"))
        self.assertEqual(out["name"], "Sara")
        self.assertEqual(out["title"], gc.DEFAULT_TITLE)
        self.assertEqual(out["group_name"], "")

    def test_production_shaper_path(self):
        """مكتبات التشكيل (arabic-reshaper/bidi) موجودة بالإنتاج فقط؛ منحاكيها لنتحقق من مسار BASIC."""
        fake_reshaper = SimpleNamespace(reshape=lambda text: text)
        with patch.object(cr, "_SHAPERS", True), \
                patch.object(cr, "arabic_reshaper", fake_reshaper, create=True), \
                patch.object(cr, "get_display", lambda text: text[::-1], create=True), \
                patch.dict(cr._FONT_CACHE, {}, clear=True):
            self.assertEqual(png_size(cr.render_certificate(payload())), (cr.WIDTH, cr.HEIGHT))

    def test_worker_core_returns_png_and_maps_bad_payload(self):
        data, media = core.render_certificate(payload())
        self.assertEqual(media, "image/png")
        self.assertTrue(data.startswith(PNG_MAGIC))
        with self.assertRaises(core.BadPayload):
            core.render_certificate(payload(theme="neon"))
        with self.assertRaises(core.BadPayload):
            core.render_certificate("nope")


class AsyncRenderTests(unittest.IsolatedAsyncioTestCase):
    async def test_remote_result_is_used_when_available(self):
        with patch("services.offload.remote_render_certificate", AsyncMock(return_value=PNG_MAGIC + b"remote")):
            self.assertEqual(await cr.render_certificate_async(payload()), PNG_MAGIC + b"remote")

    async def test_local_fallback_when_remote_returns_none(self):
        with patch("services.offload.remote_render_certificate", AsyncMock(return_value=None)):
            data = await cr.render_certificate_async(payload())
        self.assertEqual(png_size(data), (cr.WIDTH, cr.HEIGHT))


class WiringTests(unittest.TestCase):
    def read(self, rel):
        return (ROOT / rel).read_text(encoding="utf-8")

    def test_worker_route_and_core_function_exist(self):
        self.assertIn('@app.post("/v1/render/certificate")', self.read("render_worker.py"))
        self.assertIn("core.render_certificate", self.read("render_worker.py"))

    def test_offload_client_posts_to_the_same_route(self):
        self.assertIn('"/v1/render/certificate"', self.read("services/offload.py"))

    def test_migration_adds_both_columns_idempotently(self):
        sql = self.read("db/migrations/20261105120000_group_quiz_certificates.sql")
        self.assertIn("ADD COLUMN IF NOT EXISTS cert_config jsonb NOT NULL DEFAULT '{}'", sql)
        self.assertIn("ADD COLUMN IF NOT EXISTS last_name text", sql)

    def test_cert_router_is_registered_before_general_private_handlers(self):
        main = self.read("main.py")
        self.assertLess(main.index("group_quiz_cert_router,  #"), main.index("payments_router,  # 💳"))
        self.assertIn("group_quiz_cert_router", self.read("handlers/__init__.py"))

    def test_no_new_callback_prefix_needed(self):
        self.assertIn('"gqs:"', self.read("middlewares.py"))

    def test_bot_never_imports_pil_at_module_level(self):
        # الاستيراد الكسول: PIL ما بيتحمّل ببداية البوت (ذاكرة 512MB).
        import re
        source = self.read("handlers/group_quiz.py")
        self.assertIsNone(re.search(r"^(from|import) services\.certificate_renderer", source, re.M))
        self.assertIsNone(re.search(r"^(from|import) PIL", source, re.M))
        self.assertIn("    from services.certificate_renderer import render_certificate_async", source)


# ==================== قواعد الإعداد ====================
def session(**kw):
    return base.base_session(**kw)


def with_cert(enabled=True, **kw):
    return session(cert_config=cfg(enabled=enabled), **kw)


class RulesTests(unittest.TestCase):
    def setUp(self):
        self.env = base.load_rules()
        self.state = self.env["_setup_state"]
        self.compute = self.env["_compute_setup_update"]

    def test_default_is_disabled(self):
        st = self.state(session(), is_channel=False)
        self.assertFalse(st["cert"])
        self.assertFalse(st["cert_cfg"]["enabled"])
        self.assertIsNone(st["cert_reason"])

    def test_effective_state_requires_eligibility(self):
        self.assertTrue(self.state(with_cert(), is_channel=False)["cert"])
        st = self.state(with_cert(poll_is_anonymous=True), is_channel=False)
        self.assertFalse(st["cert"])
        self.assertTrue(st["cert_reason"])

    def test_enable_saves_whole_config(self):
        updates, err = self.compute(session(), "cert", "1", is_channel=False, cert_available=True, default_title="كويز الفيزياء")
        self.assertIsNone(err)
        self.assertTrue(updates["cert_config"]["enabled"])
        self.assertEqual(updates["cert_config"]["title"], "كويز الفيزياء")
        self.assertEqual(list(updates), ["cert_config"])

    def test_enable_refused_when_not_eligible(self):
        for sess in (session(poll_is_anonymous=True), session(show_names=False), session(pacing_mode="burst")):
            updates, err = self.compute(sess, "cert", "1", is_channel=False, cert_available=True)
            self.assertIsNone(updates)
            self.assertIn("⚠️", err)

    def test_refused_for_channel_or_missing_schema(self):
        self.assertIsNotNone(self.compute(session(), "cert", "1", is_channel=True, cert_available=True)[1])
        self.assertIsNotNone(self.compute(session(), "cert", "1", is_channel=False, cert_available=False)[1])

    def test_invalid_values_rejected(self):
        for v in ("x", "thpink", "n7", ""):
            self.assertIsNotNone(self.compute(session(), "cert", v, is_channel=False, cert_available=True)[1])

    def test_style_changes_keep_enabled(self):
        updates, _ = self.compute(with_cert(), "cert", "thgold", is_channel=False, cert_available=True)
        self.assertEqual((updates["cert_config"]["theme"], updates["cert_config"]["enabled"]), ("gold", True))

    def test_disable_always_allowed(self):
        updates, err = self.compute(with_cert(poll_is_anonymous=True), "cert", "0", is_channel=False, cert_available=True)
        self.assertIsNone(err)
        self.assertFalse(updates["cert_config"]["enabled"])

    def test_conflicting_changes_auto_disable(self):
        cases = [("mode", "a"), ("names", "0"), ("pace", "b")]
        for field, value in cases:
            updates, err = self.compute(with_cert(), field, value, is_channel=False)
            self.assertIsNone(err, field)
            self.assertFalse(updates["cert_config"]["enabled"], field)

    def test_harmless_changes_keep_certificates(self):
        for field, value in (("top", "10"), ("timer", "30"), ("pace", "f60"), ("lock", "1")):
            updates, _ = self.compute(with_cert(), field, value, is_channel=False)
            self.assertNotIn("cert_config", updates, field)

    def test_no_cert_key_when_certificates_were_off(self):
        updates, _ = self.compute(session(), "mode", "a", is_channel=False)
        self.assertEqual(updates, {"poll_is_anonymous": True})

    def test_rejected_core_changes_pass_through(self):
        updates, err = self.compute(with_cert(), "names", "x", is_channel=False)
        self.assertIsNone(updates)
        self.assertTrue(err)

    def test_summary_line(self):
        def text(sess, available):
            st = self.state(sess, is_channel=False)
            return self.env["_setup_summary_text"](st, title="t", dest_title="g", total=10, is_channel=False, cert_available=available)
        self.assertNotIn("🎓", text(session(), False))
        self.assertIn("🎓 الشهادات: غير مفعّلة", text(session(), True))
        self.assertIn("🎓 الشهادات: مفعّلة (أول 3)", text(with_cert(), True))
        self.assertIn("مفعّلة (أول 2)", text(with_cert(top_n=2), True))

    def test_menu_button_only_when_available(self):
        kb = base.load_keyboards()["get_group_setup_menu_keyboard"]
        self.assertIn(f"gqs:{SID}:o:cert", base.datas(kb(SID, cert_available=True)))
        self.assertNotIn(f"gqs:{SID}:o:cert", base.datas(kb(SID, cert_available=False)))
        self.assertNotIn(f"gqs:{SID}:o:cert", base.datas(kb(SID, cert_available=True, is_channel=True)))

    def test_cert_keyboard_buttons_are_all_valid_and_short(self):
        kb = base.load_keyboards()["get_group_setup_cert_keyboard"]
        for enabled in (True, False):
            data = base.datas(kb(SID, cfg(enabled=enabled), enabled=enabled))
            for item in data:
                self.assertLessEqual(len(item.encode()), 64, item)
            cert_values = [d.split(":s:cert:")[1] for d in data if ":s:cert:" in d]
            for value in cert_values:
                if value in {"pv", "te", "ie"}:
                    continue
                _, err = self.compute(session(), "cert", value, is_channel=False, cert_available=True)
                self.assertIsNone(err, value)
            self.assertTrue({"pv", "te", "ie"} <= set(cert_values))
            self.assertEqual(sum(1 for d in data if d.endswith("s:cert:" + ("0" if enabled else "1"))), 1)

    def test_cert_screen_escapes_user_text(self):
        text = self.env["_cert_screen_text"](cfg(title="<b>x</b>", issuer="<i>y</i>"), enabled=True, reason=None, top=5)
        self.assertNotIn("<b>x</b>", text)
        self.assertIn("&lt;b&gt;x&lt;/b&gt;", text)
        self.assertIn("&lt;i&gt;y&lt;/i&gt;", text)


class SetupCallbackTests(base.HandlerTests):
    """نفس بيئة معالج gqs: لكن مع تفعيل بوابة المخطط."""

    def build(self, session=None, **kw):
        env = super().build(session, **kw)
        env["group_cert_schema_ready"].return_value = True
        return env

    async def test_open_cert_screen(self):
        env = self.build(with_cert())
        call = await self.press(env, f"gqs:{SID}:o:cert")
        text = call.message.edit_text.await_args.args[0]
        self.assertIn("شهادات التقدير", text)
        self.assertIn("✅ مفعّلة", text)

    async def test_open_refused_when_schema_missing(self):
        env = self.build(with_cert())
        env["group_cert_schema_ready"].return_value = False
        call = await self.press(env, f"gqs:{SID}:o:cert")
        call.message.edit_text.assert_not_awaited()
        self.assertTrue(call.answer.await_args_list[0].kwargs.get("show_alert"))

    async def test_open_refused_with_reason_when_anonymous(self):
        env = self.build(session(poll_is_anonymous=True))
        call = await self.press(env, f"gqs:{SID}:o:cert")
        call.message.edit_text.assert_not_awaited()
        self.assertIn("النمط التنافسي", call.answer.await_args_list[0].args[0])

    async def test_open_hidden_for_channels(self):
        env = self.build(session(), channel=True)
        call = await self.press(env, f"gqs:{SID}:o:cert")
        call.message.edit_text.assert_not_awaited()

    async def test_enable_saves_with_header_title(self):
        env = self.build(session(), header={"title": "كويز الأحياء", "dest_title": "غروب"})
        await self.press(env, f"gqs:{SID}:s:cert:1")
        saved = self.update.await_args.args[1]["cert_config"]
        self.assertEqual((saved["enabled"], saved["title"]), (True, "كويز الأحياء"))

    async def test_enable_refused_without_saving_when_names_hidden(self):
        env = self.build(session(show_names=False))
        call = await self.press(env, f"gqs:{SID}:s:cert:1")
        self.update.assert_not_awaited()
        self.assertTrue(call.answer.await_args_list[0].kwargs.get("show_alert"))

    async def test_theme_change_redraws_cert_screen(self):
        env = self.build(with_cert())
        call = await self.press(env, f"gqs:{SID}:s:cert:thpurple")
        self.assertEqual(self.update.await_args.args[1]["cert_config"]["theme"], "purple")
        self.assertIn("شهادات التقدير", call.message.edit_text.await_args.args[0])

    async def test_conflicting_change_disables_and_tells_the_admin(self):
        env = self.build(with_cert())
        call = await self.press(env, f"gqs:{SID}:s:mode:a")
        saved = self.update.await_args.args[1]
        self.assertFalse(saved["cert_config"]["enabled"])
        self.assertIn("عطّلت شهادات التقدير", call.message.edit_text.await_args.args[0])

    async def test_special_buttons_go_to_special_handler_without_saving(self):
        env = self.build(with_cert())
        for value in ("pv", "te", "ie"):
            await self.press(env, f"gqs:{SID}:s:cert:{value}")
        self.assertEqual(env["_handle_cert_special"].await_count, 3)
        self.update.assert_not_awaited()

    async def test_go_disables_stale_certificates_with_burst(self):
        env = self.build(with_cert(pacing_mode="burst"))
        await self.press(env, f"gqs:{SID}:go")
        self.assertFalse(self.update.await_args.args[1]["cert_config"]["enabled"])
        self.launch.assert_awaited_once()

    async def test_go_keeps_valid_certificates_untouched(self):
        env = self.build(with_cert())
        await self.press(env, f"gqs:{SID}:go")
        self.update.assert_not_awaited()

    async def test_summary_shows_certificate_line(self):
        env = self.build(with_cert())
        call = await self.press(env, f"gqs:{SID}:b")
        self.assertIn("🎓 الشهادات: مفعّلة", call.message.edit_text.await_args.args[0])


# ==================== الإرسال والمعاينة وتعديل النصوص ====================
CERT_NAMES = {"_handle_cert_special", "_send_cert_preview", "_awaiting_cert_text", "receive_cert_text", "_send_certificates"}


def load_cert_handlers(**over):
    bot = SimpleNamespace(
        get_chat=AsyncMock(return_value=SimpleNamespace(title="شعبة العلمي")),
        send_photo=AsyncMock(), send_media_group=AsyncMock(), send_chat_action=AsyncMock(), edit_message_text=AsyncMock(),
    )
    redis = SimpleNamespace(set=AsyncMock(return_value=True), get=AsyncMock(return_value=None), delete=AsyncMock())

    async def safe_call(factory, **_kw):
        return await factory()

    env = dict(
        asyncio=asyncio, datetime=datetime, json=json, html=html, Optional=object, Dict=dict, Any=object,
        types=telegram_types, bot=bot, redis_client=redis, _safe_call=safe_call, logger=Mock(), log_error=Mock(),
        CERT_RENDER_TIMEOUT_SECONDS=5, _CERT_TEXT_WAIT_PREFIX="gq:cert_text_wait:", _CERT_TEXT_WAIT_TTL=300,
        _CERT_PREVIEW_COOLDOWN_PREFIX="gq:cert_preview:", _CERT_PREVIEW_COOLDOWN_SECONDS=8,
        _CERT_CANCEL_WORDS={"إلغاء", "الغاء", "cancel"},
        CERT_DEFAULT_TITLE=gc.DEFAULT_TITLE, CERT_TITLE_MAX_CHARS=gc.TITLE_MAX_CHARS, CERT_ISSUER_MAX_CHARS=gc.ISSUER_MAX_CHARS,
        cert_build_payload=gc.build_payload, cert_clean_text=gc.clean_text, cert_display_name=gc.display_name,
        cert_format_date=gc.format_date, cert_normalize_config=gc.normalize_config, cert_select_winners=gc.select_winners,
        group_cert_schema_ready=AsyncMock(return_value=True), _load_setup_header=AsyncMock(return_value={"title": "كويز", "dest_title": "غروب"}),
        get_session=AsyncMock(), update_session=AsyncMock(return_value={"ok": 1}), is_group_admin=AsyncMock(return_value=True),
        _setup_screen_view=AsyncMock(return_value=("شاشة", "kb")),
    )
    env.update(over)
    out = load_functions("handlers/group_quiz.py", CERT_NAMES, **env)
    out["_bot"], out["_redis"] = env["bot"], env["redis_client"]
    return out


def part(user_id, score, ms, first="طالب", last=None, username=None):
    return {"user_id": user_id, "score": score, "total_answer_time_ms": ms, "first_name": f"{first}{user_id}", "last_name": last, "username": username}


def finished_session(**kw):
    s = dict(id=SID, chat_id=-1001, poll_is_anonymous=False, show_names=True, total_questions=10, message_thread_id=None,
             cert_config=cfg(enabled=True))
    s.update(kw)
    return s


class SendCertificatesTests(unittest.IsolatedAsyncioTestCase):
    def build(self, **over):
        self.render = AsyncMock(return_value=PNG_MAGIC + b"x")
        env = load_cert_handlers(**{"_render_cert": self.render, **over})
        return env, env["_bot"]

    async def test_album_for_several_winners(self):
        env, bot = self.build()
        rows = [part(1, 9, 5, last="الخطيب"), part(2, 8, 5), part(3, 7, 5), part(4, 6, 5)]
        await env["_send_certificates"](finished_session(), rows, 10)
        self.assertEqual(self.render.await_count, 3)
        media = bot.send_media_group.await_args.kwargs["media"]
        self.assertEqual(len(media), 3)
        self.assertIn("غير رسمية", media[0].caption)
        self.assertIsNone(media[1].caption)
        bot.send_photo.assert_not_awaited()

    async def test_payload_has_full_name_group_title_and_rank(self):
        env, _ = self.build()
        await env["_send_certificates"](finished_session(), [part(1, 9, 5, first="أحمد", last="الخطيب")], 10)
        sent = self.render.await_args.args[0]
        self.assertEqual((sent["name"], sent["group_name"], sent["title"], sent["rank"], sent["score"], sent["total"]),
                         ("أحمد1 الخطيب", "شعبة العلمي", "اختبار الفيزياء", 1, 9, 10))
        self.assertFalse(sent["preview"])

    async def test_single_winner_uses_send_photo(self):
        env, bot = self.build()
        await env["_send_certificates"](finished_session(cert_config=cfg(enabled=True, count=1)), [part(1, 9, 5), part(2, 8, 5)], 10)
        bot.send_photo.assert_awaited_once()
        bot.send_media_group.assert_not_awaited()

    async def test_thread_id_is_forwarded(self):
        env, bot = self.build()
        await env["_send_certificates"](finished_session(message_thread_id=77), [part(1, 9, 5), part(2, 8, 5)], 10)
        self.assertEqual(bot.send_media_group.await_args.kwargs["message_thread_id"], 77)

    async def test_tied_students_share_rank(self):
        env, _ = self.build()
        await env["_send_certificates"](finished_session(), [part(1, 9, 5), part(2, 9, 5), part(3, 5, 1)], 10)
        self.assertEqual([c.args[0]["rank"] for c in self.render.await_args_list], [1, 1, 3])

    async def test_zero_scores_get_nothing(self):
        env, bot = self.build()
        await env["_send_certificates"](finished_session(), [part(1, 0, 5), part(2, 0, 6)], 10)
        self.render.assert_not_awaited()
        bot.send_photo.assert_not_awaited()
        bot.send_media_group.assert_not_awaited()

    async def test_disabled_anonymous_or_hidden_names_send_nothing(self):
        for sess in (finished_session(cert_config=cfg(enabled=False)), finished_session(cert_config={}),
                     finished_session(poll_is_anonymous=True), finished_session(show_names=False)):
            env, bot = self.build()
            await env["_send_certificates"](sess, [part(1, 9, 5)], 10)
            self.render.assert_not_awaited()
            bot.send_photo.assert_not_awaited()

    async def test_one_failed_render_does_not_block_the_others(self):
        env, bot = self.build()
        self.render.side_effect = [PNG_MAGIC, RuntimeError("worker busy"), PNG_MAGIC]
        await env["_send_certificates"](finished_session(), [part(1, 9, 5), part(2, 8, 5), part(3, 7, 5)], 10)
        self.assertEqual(len(bot.send_media_group.await_args.kwargs["media"]), 2)

    async def test_all_renders_failing_is_silent(self):
        env, bot = self.build()
        self.render.side_effect = RuntimeError("503")
        await env["_send_certificates"](finished_session(), [part(1, 9, 5)], 10)
        bot.send_photo.assert_not_awaited()
        bot.send_media_group.assert_not_awaited()

    async def test_render_timeout_is_contained(self):
        async def slow(_payload):
            await asyncio.sleep(10)
        env, bot = self.build(CERT_RENDER_TIMEOUT_SECONDS=0.05, _render_cert=slow)
        await env["_send_certificates"](finished_session(), [part(1, 9, 5)], 10)
        bot.send_photo.assert_not_awaited()

    async def test_telegram_failure_never_raises(self):
        env, bot = self.build()
        bot.send_media_group.side_effect = RuntimeError("chat not found")
        await env["_send_certificates"](finished_session(), [part(1, 9, 5), part(2, 8, 5)], 10)

    async def test_get_chat_failure_still_sends_without_group_name(self):
        env, bot = self.build()
        bot.get_chat.side_effect = RuntimeError("kicked")
        await env["_send_certificates"](finished_session(), [part(1, 9, 5)], 10)
        self.assertEqual(self.render.await_args.args[0]["group_name"], "")
        bot.send_photo.assert_awaited_once()

    async def test_score_never_exceeds_total_on_the_certificate(self):
        env, _ = self.build()
        await env["_send_certificates"](finished_session(total_questions=5), [part(1, 8, 5)], 10)
        sent = self.render.await_args.args[0]
        self.assertGreaterEqual(sent["total"], sent["score"])


class PreviewTests(unittest.IsolatedAsyncioTestCase):
    def call(self, user_id=1):
        call = Mock()
        call.answer = AsyncMock()
        call.from_user = SimpleNamespace(id=user_id, first_name="محمود", last_name="العبيد", username="m")
        call.message = Mock()
        call.message.chat = SimpleNamespace(id=user_id, type="private")
        call.message.message_id = 55
        call.message.answer = AsyncMock()
        return call

    def build(self, **over):
        self.render = AsyncMock(return_value=PNG_MAGIC + b"p")
        return load_cert_handlers(**{"_render_cert": self.render, **over})

    async def test_preview_goes_to_admin_private_chat_only(self):
        env = self.build()
        call = self.call(7)
        await env["_send_cert_preview"](call, session(cert_config=cfg(theme="gold", issuer="الأستاذ")), SID)
        sent = self.render.await_args.args[0]
        self.assertTrue(sent["preview"])
        self.assertEqual((sent["theme"], sent["issuer"], sent["name"], sent["group_name"]), ("gold", "الأستاذ", "محمود العبيد", "غروب"))
        self.assertEqual(env["_bot"].send_photo.await_args.kwargs["chat_id"], 7)

    async def test_preview_works_even_if_certificates_are_off(self):
        env = self.build()
        await env["_send_cert_preview"](self.call(), session(), SID)
        env["_bot"].send_photo.assert_awaited_once()

    async def test_cooldown_blocks_rapid_repeats(self):
        env = self.build()
        env["_redis"].set.return_value = False
        call = self.call()
        await env["_send_cert_preview"](call, session(), SID)
        self.render.assert_not_awaited()
        self.assertTrue(call.answer.await_args.kwargs.get("show_alert"))

    async def test_redis_down_does_not_block_preview(self):
        env = self.build()
        env["_redis"].set.side_effect = ConnectionError("down")
        await env["_send_cert_preview"](self.call(), session(), SID)
        env["_bot"].send_photo.assert_awaited_once()

    async def test_render_failure_tells_the_admin_and_does_not_raise(self):
        env = self.build()
        self.render.side_effect = RuntimeError("busy")
        call = self.call()
        await env["_send_cert_preview"](call, session(), SID)
        env["_bot"].send_photo.assert_not_awaited()
        self.assertIn("تعذّر", call.message.answer.await_args.args[0])


class SpecialButtonTests(unittest.IsolatedAsyncioTestCase):
    def call(self):
        call = Mock()
        call.answer = AsyncMock()
        call.from_user = SimpleNamespace(id=9, first_name="a", last_name=None, username=None)
        call.message = Mock()
        call.message.message_id = 31
        call.message.chat = SimpleNamespace(id=9, type="private")
        call.message.answer = AsyncMock()
        return call

    async def test_title_button_arms_wait_and_prompts(self):
        env = load_cert_handlers()
        call = self.call()
        await env["_handle_cert_special"](call, session(), SID, "te", {}, is_channel=False)
        key, raw = env["_redis"].set.await_args.args[:2]
        self.assertEqual(key, "gq:cert_text_wait:9")
        self.assertEqual(json.loads(raw), {"sid": SID, "field": "title", "message_id": 31})
        self.assertEqual(env["_redis"].set.await_args.kwargs["ex"], 300)
        self.assertIn("اسم الاختبار", call.message.answer.await_args.args[0])

    async def test_issuer_button_arms_issuer_field(self):
        env = load_cert_handlers()
        await env["_handle_cert_special"](self.call(), session(), SID, "ie", {}, is_channel=False)
        self.assertEqual(json.loads(env["_redis"].set.await_args.args[1])["field"], "issuer")

    async def test_channel_or_missing_schema_refused(self):
        env = load_cert_handlers()
        call = self.call()
        await env["_handle_cert_special"](call, session(), SID, "te", {}, is_channel=True)
        env["_redis"].set.assert_not_awaited()
        env = load_cert_handlers(group_cert_schema_ready=AsyncMock(return_value=False))
        await env["_handle_cert_special"](call, session(), SID, "pv", {}, is_channel=False)
        env["_bot"].send_photo.assert_not_awaited()


def message(text, user_id=1):
    msg = Mock()
    msg.text = text
    msg.from_user = SimpleNamespace(id=user_id)
    msg.chat = SimpleNamespace(id=user_id, type="private")
    msg.answer = AsyncMock()
    return msg


WAIT_TITLE = {"sid": SID, "field": "title", "message_id": 31}
WAIT_ISSUER = {"sid": SID, "field": "issuer", "message_id": 31}


class TextFilterTests(unittest.IsolatedAsyncioTestCase):
    async def test_no_wait_key_means_not_ours(self):
        env = load_cert_handlers()
        self.assertFalse(await env["_awaiting_cert_text"](message("اسم")))

    async def test_commands_and_empty_texts_are_never_captured(self):
        env = load_cert_handlers()
        env["_redis"].get.return_value = json.dumps(WAIT_TITLE)
        self.assertFalse(await env["_awaiting_cert_text"](message("/start")))
        self.assertFalse(await env["_awaiting_cert_text"](message("")))
        self.assertFalse(await env["_awaiting_cert_text"](message(None)))

    async def test_waiting_user_text_is_captured(self):
        env = load_cert_handlers()
        env["_redis"].get.return_value = json.dumps(WAIT_TITLE)
        self.assertEqual(await env["_awaiting_cert_text"](message("اسم")), {"cert_wait": WAIT_TITLE})

    async def test_corrupt_or_foreign_values_and_redis_errors_are_ignored(self):
        env = load_cert_handlers()
        for raw in ("not json", json.dumps({"sid": SID, "field": "other"}), json.dumps([1]), json.dumps({"field": "title"})):
            env["_redis"].get.return_value = raw
            self.assertFalse(await env["_awaiting_cert_text"](message("x")), raw)
        env["_redis"].get.side_effect = ConnectionError("down")
        self.assertFalse(await env["_awaiting_cert_text"](message("x")))


class ReceiveTextTests(unittest.IsolatedAsyncioTestCase):
    def build(self, sess=None, **over):
        sess = sess if sess is not None else session(cert_config=cfg(enabled=True, title="القديم"))
        return load_cert_handlers(**{"get_session": AsyncMock(return_value=sess), **over})

    async def test_title_saved_and_screen_refreshed(self):
        env = self.build()
        msg = message("  اختبار الكيمياء 😀 ")
        await env["receive_cert_text"](msg, WAIT_TITLE)
        saved = env["update_session"].await_args.args[1]["cert_config"]
        self.assertEqual((saved["title"], saved["enabled"]), ("اختبار الكيمياء", True))
        env["_redis"].delete.assert_awaited()
        self.assertIn("تم حفظ اسم الاختبار بنجاح", msg.answer.await_args.args[0])
        edit = env["_bot"].edit_message_text.await_args.kwargs
        self.assertEqual((edit["message_id"], edit["chat_id"]), (31, 1))

    async def test_title_is_mandatory_and_wait_stays_armed(self):
        env = self.build()
        msg = message("😀😀")
        await env["receive_cert_text"](msg, WAIT_TITLE)
        env["update_session"].assert_not_awaited()
        env["_redis"].delete.assert_not_awaited()
        self.assertIn("إجباري", msg.answer.await_args.args[0])

    async def test_long_title_is_cut(self):
        env = self.build()
        await env["receive_cert_text"](message("ا" * 200), WAIT_TITLE)
        self.assertLessEqual(len(env["update_session"].await_args.args[1]["cert_config"]["title"]), gc.TITLE_MAX_CHARS)

    async def test_issuer_saved_and_dash_clears_it(self):
        env = self.build()
        msg = message("الأستاذ محمد")
        await env["receive_cert_text"](msg, WAIT_ISSUER)
        self.assertEqual(env["update_session"].await_args.args[1]["cert_config"]["issuer"], "الأستاذ محمد")
        self.assertIn("تم حفظ الجهة / المعلّم بنجاح", msg.answer.await_args.args[0])
        env = self.build(session(cert_config=cfg(issuer="قديم")))
        msg = message("-")
        await env["receive_cert_text"](msg, WAIT_ISSUER)
        self.assertEqual(env["update_session"].await_args.args[1]["cert_config"]["issuer"], "")
        self.assertIn("تم حفظ الجهة / المعلّم بنجاح", msg.answer.await_args.args[0])

    async def test_confirmation_failure_does_not_hide_updated_screen(self):
        env = self.build()
        msg = message("الأستاذ محمد")
        msg.answer.side_effect = RuntimeError("telegram unavailable")
        await env["receive_cert_text"](msg, WAIT_ISSUER)
        env["update_session"].assert_awaited_once()
        env["_bot"].edit_message_text.assert_awaited_once()

    async def test_cancel_word_changes_nothing(self):
        env = self.build()
        await env["receive_cert_text"](message("إلغاء"), WAIT_TITLE)
        env["update_session"].assert_not_awaited()
        env["_redis"].delete.assert_awaited()

    async def test_not_the_session_owner_or_not_admin_is_refused(self):
        for env in (self.build(session(started_by=1234)), self.build(is_group_admin=AsyncMock(return_value=False))):
            msg = message("اسم")
            await env["receive_cert_text"](msg, WAIT_TITLE)
            env["update_session"].assert_not_awaited()
            self.assertIn("ما عادت متاحة", msg.answer.await_args.args[0])

    async def test_session_already_started_is_refused(self):
        env = self.build(session(status="active"))
        await env["receive_cert_text"](message("اسم"), WAIT_TITLE)
        env["update_session"].assert_not_awaited()

    async def test_missing_session_is_refused(self):
        env = load_cert_handlers(get_session=AsyncMock(return_value=None))
        await env["receive_cert_text"](message("اسم"), WAIT_TITLE)
        env["update_session"].assert_not_awaited()

    async def test_save_failure_keeps_wait_and_tells_the_admin(self):
        env = self.build(update_session=AsyncMock(return_value=None))
        msg = message("اسم")
        await env["receive_cert_text"](msg, WAIT_TITLE)
        env["_redis"].delete.assert_not_awaited()
        self.assertIn("تعذر", msg.answer.await_args.args[0])

    async def test_edit_failure_falls_back_to_a_new_message(self):
        env = self.build()
        env["_bot"].edit_message_text.side_effect = RuntimeError("message to edit not found")
        msg = message("اسم")
        await env["receive_cert_text"](msg, WAIT_TITLE)
        msg.answer.assert_awaited()

    async def test_unexpected_errors_never_escape(self):
        env = self.build(get_session=AsyncMock(side_effect=RuntimeError("db down")))
        await env["receive_cert_text"](message("اسم"), WAIT_TITLE)


# ==================== التخزين ====================
class FakeQuery:
    def __init__(self, db, table):
        self.db, self.table, self.op, self.cols = db, table, None, ()

    def select(self, *cols):
        self.op, self.cols = "select", cols
        return self

    def eq(self, *_a):
        return self

    def limit(self, *_a):
        return self

    def upsert(self, payload, on_conflict=None):
        self.op = "upsert"
        self.db.upserts.append(payload)
        return self

    async def execute(self):
        if self.op == "select" and any(c in self.db.missing for c in self.cols):
            raise RuntimeError("column does not exist")
        return SimpleNamespace(data=[])


class FakeDb:
    def __init__(self, missing=()):
        self.missing, self.upserts = set(missing), []

    def table(self, name):
        return FakeQuery(self, name)


class StoreTests(unittest.IsolatedAsyncioTestCase):
    def load(self, db, now=1000.0):
        clock = SimpleNamespace(monotonic=lambda: self.now)
        self.now = now
        return load_functions(
            "services/group_quiz_store.py", {"cert_schema_ready", "bump_participant"},
            supabase=db, time=clock, Dict=dict, Any=object, Optional=object,
            _CERT_SCHEMA_CACHE={"ready": False, "until": 0.0}, log_error=Mock(), logger=Mock(),
        )

    async def bump(self, env, **kw):
        args = dict(session_id=SID, user_id=5, username="u", first_name="أحمد", is_correct=True, answer_time_ms=10)
        args.update(kw)
        await env["bump_participant"](**args)

    async def test_last_name_is_written_when_columns_exist(self):
        db = FakeDb()
        env = self.load(db)
        await self.bump(env, last_name="الخطيب")
        self.assertEqual(db.upserts[0]["last_name"], "الخطيب")
        self.assertEqual(db.upserts[0]["first_name"], "أحمد")

    async def test_last_name_skipped_before_migration_so_scoring_never_breaks(self):
        db = FakeDb(missing={"last_name"})
        env = self.load(db)
        await self.bump(env, last_name="الخطيب")
        self.assertEqual(len(db.upserts), 1)
        self.assertNotIn("last_name", db.upserts[0])
        self.assertEqual(db.upserts[0]["score"], 1)

    async def test_no_last_name_means_no_column_written(self):
        db = FakeDb()
        env = self.load(db)
        await self.bump(env)
        self.assertNotIn("last_name", db.upserts[0])

    async def test_schema_ready_needs_both_columns_and_is_cached(self):
        for missing in ({"cert_config"}, {"last_name"}):
            self.assertFalse(await self.load(FakeDb(missing=missing))["cert_schema_ready"](), missing)
        env = self.load(FakeDb())
        self.assertTrue(await env["cert_schema_ready"]())
        env["supabase"].missing.add("cert_config")
        self.assertTrue(await env["cert_schema_ready"]())   # من الكاش
        self.now += 61
        self.assertFalse(await env["cert_schema_ready"]())


class FinishHookTests(unittest.IsolatedAsyncioTestCase):
    """finish_group_session بيجدول (حفل التكريم ثم الشهادات) بعد الترتيب النصي فقط، وفشلها ما بيأثّر عليه."""

    def build(self, rows, **sess):
        bot = SimpleNamespace(send_message=AsyncMock(), stop_poll=AsyncMock())
        self.spawn, self.send_certs = Mock(side_effect=lambda coro: coro.close() if hasattr(coro, "close") else None), Mock(return_value="coro")

        async def safe_call(factory, **_kw):
            return await factory()

        env = load_functions(
            "handlers/group_quiz.py", {"finish_group_session"},
            claim_session_finish=AsyncMock(return_value=True), restore_quiz_lock=AsyncMock(),
            clear_session_questions_cache=AsyncMock(), _pop_open_poll=AsyncMock(return_value=None),
            get_leaderboard=AsyncMock(return_value=rows), bot=bot, redis_client=SimpleNamespace(set=AsyncMock()), html=html,
            _safe_call=safe_call, GROUP_TOP_N_MIN=1, GROUP_TOP_N_MAX=20, _FINISHED_SESSION_PREFIX="gq:finished:",
            _FINISHED_SESSION_TTL=7200, logger=Mock(), log_error=Mock(), spawn=self.spawn, _send_recognitions=self.send_certs,
            Optional=object,
        )
        s = dict(id=SID, chat_id=-1001, poll_is_anonymous=False, show_names=True, top_n=5, pacing_mode="fixed_interval",
                 question_timer_seconds=None, total_questions=10, cert_config=cfg(enabled=True))
        s.update(sess)
        return env["finish_group_session"], bot, s

    async def test_certificates_scheduled_after_ranking(self):
        rows = [part(1, 9, 5)]
        finish, bot, s = self.build(rows)
        await finish(s)
        bot.send_message.assert_awaited_once()
        self.send_certs.assert_called_once_with(s, rows, 5)
        self.spawn.assert_called_once()

    async def test_nothing_scheduled_when_nobody_answered(self):
        finish, bot, s = self.build([])
        await finish(s)
        self.send_certs.assert_not_called()

    async def test_nothing_scheduled_for_anonymous_or_burst(self):
        for extra in ({"poll_is_anonymous": True}, {"pacing_mode": "burst"}):
            finish, bot, s = self.build([part(1, 9, 5)], **extra)
            await finish(s)
            self.send_certs.assert_not_called()


if __name__ == "__main__":
    unittest.main()
