"""حفل التكريم بالكويز الجماعي: القواعد، الرسم (صورة + فيديو حقيقي)، السيرفر الثاني، شاشة الإعداد، الإرسال."""
import asyncio
import datetime
import html
import json
import os
import shutil
import struct
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from aiogram import types as telegram_types
from reliability_support import load_functions

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from services import ceremony_renderer as cr  # noqa: E402
from services import group_ceremony as gcm  # noqa: E402
from services import group_certificate as gc  # noqa: E402
from services import offload_worker_core as core  # noqa: E402
import test_group_quiz_setup_summary as base  # noqa: E402

SID = base.SID
PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


def png_size(data: bytes):
    assert data.startswith(PNG_MAGIC)
    return struct.unpack(">II", data[16:24])


def cfg(**kw):
    c = gcm.normalize_config({"enabled": True, "title": "اختبار الفيزياء"})
    c.update(kw)
    return c


def part(user_id, score, ms, first="طالب", last=None, username=None):
    return {"user_id": user_id, "score": score, "total_answer_time_ms": ms, "first_name": f"{first}{user_id}",
            "last_name": last, "username": username}


def winners_payload(n=3, **kw):
    rows = gcm.preview_winners(n, 20)
    p = gcm.build_payload(cfg(**kw), rows, group_name="شعبة العلمي", total=20)
    return p


def ffmpeg_available() -> bool:
    try:
        import imageio_ffmpeg
        imageio_ffmpeg.get_ffmpeg_exe()
        return True
    except Exception:
        return shutil.which("ffmpeg") is not None


# ==================== المنطق الصرف ====================
class ConfigTests(unittest.TestCase):
    def test_defaults_for_missing_or_garbage(self):
        for raw in (None, {}, "x", [], {"enabled": "yes", "count": "5", "format": 3, "music": "no"}):
            c = gcm.normalize_config(raw)
            self.assertEqual((c["enabled"], c["count"], c["format"], c["music"]), (False, 5, "video", True), raw)
            self.assertTrue(c["title"])

    def test_valid_values_are_kept_and_text_cleaned(self):
        c = gcm.normalize_config({"enabled": True, "count": 10, "format": "image", "music": False, "title": "  فيزياء 😀 \u202e"})
        self.assertEqual((c["enabled"], c["count"], c["format"], c["music"]), (True, 10, "image", False))
        self.assertEqual(c["title"], "فيزياء")

    def test_count_must_be_a_choice(self):
        self.assertEqual(gcm.normalize_config({"count": 7})["count"], gcm.DEFAULT_COUNT)
        self.assertEqual(gcm.normalize_config({"count": True})["count"], gcm.DEFAULT_COUNT)

    def test_apply_change_values(self):
        c = gcm.default_config()
        for value, key, expected in (("1", "enabled", True), ("n10", "count", 10), ("fi", "format", "image"),
                                     ("fv", "format", "video"), ("m0", "music", False), ("m1", "music", True)):
            new, err = gcm.apply_config_change(c, value)
            self.assertIsNone(err, value)
            self.assertEqual(new[key], expected, value)
        self.assertFalse(c["enabled"], "الأصل ما بيتعدّل")

    def test_apply_change_rejects_garbage(self):
        for value in ("", "n4", "n", "x", "th1", "pv", "te", "n-3"):
            new, err = gcm.apply_config_change(gcm.default_config(), value)
            self.assertIsNone(new, value)
            self.assertTrue(err)

    def test_eligibility_matrix(self):
        ok = dict(anonymous=False, names=True, pace="f30", is_channel=False)
        self.assertEqual(gcm.eligibility(**ok), (True, None))
        for override in ({"is_channel": True}, {"anonymous": True}, {"names": False}, {"pace": "b"}):
            good, reason = gcm.eligibility(**{**ok, **override})
            self.assertFalse(good, override)
            self.assertIn("حفل التكريم", reason)


class WinnersTests(unittest.TestCase):
    def rows(self):
        return [part(1, 9, 100), part(2, 8, 100), part(3, 8, 100), part(4, 7, 50), part(5, 0, 10)]

    def test_limit_by_count_and_top_n(self):
        self.assertEqual(len(gcm.select_winners(self.rows(), cfg(count=3), 20)), 3)
        self.assertEqual(len(gcm.select_winners(self.rows(), cfg(count=10), 2)), 2)

    def test_zero_score_never_celebrated(self):
        got = gcm.select_winners(self.rows(), cfg(count=10), 20)
        self.assertEqual([w["user_id"] for w in got], [1, 2, 3, 4])

    def test_ties_share_rank(self):
        got = gcm.select_winners(self.rows(), cfg(count=10), 20)
        self.assertEqual([w["rank"] for w in got], [1, 2, 2, 4])

    def test_empty_rows(self):
        self.assertEqual(gcm.select_winners([], cfg(), 5), [])


class PayloadTests(unittest.TestCase):
    def test_build_payload_cleans_names_and_maps_format(self):
        rows = gc.assign_ranks([
            {"user_id": 1, "score": 5, "total_answer_time_ms": 1, "first_name": "أحمد 😀", "last_name": "\u202eالخطيب", "username": None},
            {"user_id": 2, "score": 4, "total_answer_time_ms": 1, "first_name": "😀", "last_name": None, "username": "bob"},
        ])
        p = gcm.build_payload(cfg(format="image", music=False), rows, group_name="غروب", total=10)
        self.assertEqual((p["fmt"], p["music"], p["preview"]), ("png", False, False))
        self.assertEqual([w["name"] for w in p["winners"]], ["أحمد الخطيب", "@bob"])
        self.assertEqual(gcm.build_payload(cfg(), rows, group_name="", total=10)["fmt"], "mp4")

    def test_total_never_below_score(self):
        rows = gc.assign_ranks([part(1, 12, 1)])
        self.assertEqual(gcm.build_payload(cfg(), rows, group_name="", total=10)["winners"][0]["total"], 12)

    def test_payload_is_json_serialisable_and_capped(self):
        rows = gc.assign_ranks([part(i, 20 - i // 3, i) for i in range(1, 30)])
        p = gcm.build_payload(cfg(), rows, group_name="غ" * 200, total=20)
        json.dumps(p, ensure_ascii=False)
        self.assertLessEqual(len(p["winners"]), gcm.MAX_WINNERS)
        self.assertLessEqual(len(p["group_name"]), 48)

    def test_preview_winners_show_a_tie_and_respect_count(self):
        rows = gcm.preview_winners(10, 20)
        self.assertEqual(len(rows), 10)
        self.assertEqual([r["rank"] for r in rows][:4], [1, 2, 3, 3])
        self.assertEqual(len(gcm.preview_winners(3, 20)), 3)


# ==================== الرسم ====================
class ValidateTests(unittest.TestCase):
    def test_valid_payload_roundtrip(self):
        data = cr.validate_payload(winners_payload())
        self.assertEqual(data["fmt"], "mp4")
        self.assertEqual(len(data["winners"]), 3)

    def test_rejects_bad_shapes(self):
        good = winners_payload()
        bad_cases = [
            "x", {**good, "fmt": "gif"}, {**good, "music": "yes"}, {**good, "preview": 1}, {**good, "winners": []},
            {**good, "winners": "x"}, {**good, "winners": [{"rank": 1}]},
            {**good, "winners": [{"rank": 0, "name": "a", "score": 1, "total": 2}]},
            {**good, "winners": [{"rank": True, "name": "a", "score": 1, "total": 2}]},
            {**good, "winners": [{"rank": 2, "name": "a", "score": 1, "total": 2}, {"rank": 1, "name": "b", "score": 1, "total": 2}]},
            {**good, "winners": [{"rank": 1, "name": "a", "score": 1, "total": 2}] * (cr.MAX_WINNERS + 1)},
            {**good, "title": 5},
        ]
        for case in bad_cases:
            with self.assertRaises(cr.CeremonyError, msg=str(case)[:60]):
                cr.validate_payload(case)

    def test_sanitises_text(self):
        p = winners_payload()
        p["title"] = "<b>اختبار</b> 😀"
        p["winners"][0]["name"] = "\u202e" + "ن" * 100
        data = cr.validate_payload(p)
        self.assertNotIn("😀", data["title"])
        self.assertLessEqual(len(data["winners"][0]["name"]), gc.NAME_MAX_CHARS)


class StaticRenderTests(unittest.TestCase):
    def test_png_dimensions_and_magic(self):
        png = cr.render_ceremony_png({**winners_payload(5), "fmt": "png"})
        self.assertEqual(png_size(png), (cr.FRAME_W, cr.FRAME_H))

    def test_layout_edge_cases_all_render(self):
        many_ties = [{"rank": 1, "name": f"طالب {i}", "score": 20, "total": 20} for i in range(12)]
        cases = {
            "single": [{"rank": 1, "name": "وحيد", "score": 5, "total": 5}],
            "two": [{"rank": 1, "name": "أ", "score": 5, "total": 5}, {"rank": 2, "name": "ب", "score": 4, "total": 5}],
            "twelve_tied": many_ties,
            "long_names": [{"rank": i, "name": "عبد الرحمن بن عبد العزيز " * 3, "score": 9, "total": 10} for i in range(1, 8)],
            "latin": [{"rank": 1, "name": "Alexander the Great of Macedonia", "score": 9, "total": 10}],
        }
        for label, winners in cases.items():
            png = cr.render_ceremony_png({**winners_payload(), "fmt": "png", "winners": winners})
            self.assertEqual(png_size(png), (cr.FRAME_W, cr.FRAME_H), label)

    def test_preview_flag_changes_the_footer_text(self):
        data = cr.validate_payload({**winners_payload(), "preview": True})
        self.assertTrue(data["preview"])
        self.assertIn("معاينة", cr.PREVIEW_DISCLAIMER)

    def test_missing_assets_raise_clean_error(self):
        with patch.object(cr, "FLAMES_STILL_PATH", "/nonexistent/flames.png"):
            with self.assertRaises(cr.CeremonyError):
                cr.render_ceremony_png({**winners_payload(), "fmt": "png"})

    def test_assets_are_committed(self):
        for path in (cr.FLAMES_STILL_PATH, cr.FLAMES_LOOP_PATH, cr.THEME_AUDIO_PATH):
            self.assertTrue(os.path.isfile(path), path)


class FfmpegCommandTests(unittest.TestCase):
    def test_with_music_maps_audio_and_trims_to_song(self):
        cmd = cr.build_ffmpeg_command("ffmpeg", "/t/o.png", "/t/out.mp4", music=True)
        self.assertIn(cr.THEME_AUDIO_PATH, cmd)
        self.assertIn("2:a", cmd)
        self.assertNotIn("-an", cmd)
        self.assertEqual(cmd[cmd.index("-t") + 1], str(gcm.MUSIC_SECONDS))
        self.assertEqual(cmd[-1], "/t/out.mp4")

    def test_without_music_is_silent_and_shorter(self):
        cmd = cr.build_ffmpeg_command("ffmpeg", "/t/o.png", "/t/out.mp4", music=False)
        self.assertNotIn(cr.THEME_AUDIO_PATH, cmd)
        self.assertIn("-an", cmd)
        self.assertEqual(cmd[cmd.index("-t") + 1], str(gcm.SILENT_SECONDS))

    def test_flames_loop_is_repeated_forever(self):
        cmd = cr.build_ffmpeg_command("ffmpeg", "/t/o.png", "/t/out.mp4", music=True)
        i = cmd.index(cr.FLAMES_LOOP_PATH)
        self.assertEqual(cmd[i - 3:i], ["-stream_loop", "-1", "-i"])

    def test_video_dimensions_are_even_for_telegram(self):
        self.assertEqual((gcm.VIDEO_WIDTH % 2, gcm.VIDEO_HEIGHT % 2), (0, 0))
        self.assertEqual(round(cr.FRAME_W * gcm.VIDEO_HEIGHT / gcm.VIDEO_WIDTH), cr.FRAME_H)


@unittest.skipUnless(ffmpeg_available(), "ffmpeg غير متوفر")
class VideoRenderTests(unittest.TestCase):
    def probe(self, data: bytes) -> str:
        import imageio_ffmpeg
        with tempfile.NamedTemporaryFile(suffix=".mp4", delete=False) as fh:
            fh.write(data)
            path = fh.name
        try:
            out = subprocess.run([imageio_ffmpeg.get_ffmpeg_exe(), "-i", path, "-f", "null", "-"], capture_output=True, text=True, timeout=60)
            return out.stderr
        finally:
            os.unlink(path)

    def test_video_with_music(self):
        data, kind = cr.render_ceremony(winners_payload(5))
        self.assertEqual((kind, data[4:8]), ("mp4", b"ftyp"))
        info = self.probe(data)
        self.assertIn("720x900", info)
        self.assertIn("Audio: aac", info)
        self.assertRegex(info, r"Duration: 00:00:18\.[89]")
        self.assertLess(len(data), 4 * 1024 * 1024)

    def test_silent_video_has_no_audio_stream(self):
        data, _ = cr.render_ceremony(winners_payload(3, music=False))
        info = self.probe(data)
        self.assertNotIn("Audio:", info)
        self.assertRegex(info, r"Duration: 00:00:1[12]\.")


# ==================== السيرفر الثاني ====================
class WorkerCoreTests(unittest.TestCase):
    def test_png_goes_through_core_with_media_type(self):
        data, media = core.render_ceremony({**winners_payload(), "fmt": "png"})
        self.assertEqual(media, "image/png")
        self.assertEqual(png_size(data), (cr.FRAME_W, cr.FRAME_H))

    def test_video_media_type(self):
        with patch.object(cr, "render_ceremony", return_value=(b"\x00\x00\x00\x18ftypmp42", "mp4")):
            data, media = core.render_ceremony(winners_payload())
        self.assertEqual(media, "video/mp4")

    def test_bad_payload_becomes_badpayload(self):
        for bad in ({}, {"fmt": "zip"}, {**winners_payload(), "winners": []}, "x"):
            with self.assertRaises(core.BadPayload):
                core.render_ceremony(bad)


class OffloadClientTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        from services import offload
        self.offload = offload

    def test_mp4_magic_is_ftyp_at_offset_4(self):
        self.assertTrue(self.offload._looks_like("mp4", b"\x00\x00\x00\x18ftypisom"))
        self.assertFalse(self.offload._looks_like("mp4", PNG_MAGIC))
        self.assertTrue(self.offload._looks_like("png", PNG_MAGIC + b"x"))
        self.assertFalse(self.offload._looks_like("png", b"\x00\x00\x00\x18ftyp"))

    async def test_disabled_returns_none_without_calling(self):
        with patch.object(self.offload, "ENABLED", False), patch.object(self.offload, "_call", new=AsyncMock()) as call:
            self.assertIsNone(await self.offload.remote_render_ceremony(winners_payload()))
        call.assert_not_awaited()

    async def test_video_uses_long_timeout_and_mp4_kind(self):
        with patch.object(self.offload, "ENABLED", True), patch.object(self.offload, "_call", new=AsyncMock(return_value=b"v")) as call:
            await self.offload.remote_render_ceremony(winners_payload())
        args = call.await_args.args
        self.assertEqual((args[0], args[3]), ("/v1/render/ceremony", "mp4"))
        self.assertEqual(args[2], self.offload.CEREMONY_VIDEO_READ_TIMEOUT)
        self.assertGreater(args[2], self.offload.RENDER_READ_TIMEOUT)

    async def test_image_uses_render_timeout_and_png_kind(self):
        with patch.object(self.offload, "ENABLED", True), patch.object(self.offload, "_call", new=AsyncMock(return_value=b"i")) as call:
            await self.offload.remote_render_ceremony({**winners_payload(), "fmt": "png"})
        args = call.await_args.args
        self.assertEqual((args[3], args[2]), ("png", self.offload.RENDER_READ_TIMEOUT))


# ==================== شاشة الإعداد (قواعد + callbacks) ====================
def session(**kw):
    s = base.base_session()
    s.update(kw)
    return s


def with_cer(**cer_kw):
    return session(ceremony_config=cfg(**cer_kw))


class SetupRulesTests(unittest.TestCase):
    def setUp(self):
        self.env = base.load_rules()
        self.update = self.env["_compute_setup_update"]

    def go(self, sess, field, value, **kw):
        kw.setdefault("ceremony_available", True)
        return self.update(sess, field, value, is_channel=kw.pop("is_channel", False), **kw)

    def test_enable_saves_config_with_default_title(self):
        updates, err = self.go(session(), "cer", "1", ceremony_default_title="كويز الأحياء")
        self.assertIsNone(err)
        self.assertEqual((updates["ceremony_config"]["enabled"], updates["ceremony_config"]["title"]), (True, "كويز الأحياء"))
        self.assertNotIn("cert_config", updates)

    def test_each_setting_changes_only_ceremony_config(self):
        for value, key, expected in (("fi", "format", "image"), ("m0", "music", False), ("n10", "count", 10), ("0", "enabled", False)):
            updates, err = self.go(with_cer(), "cer", value)
            self.assertIsNone(err, value)
            self.assertEqual(list(updates), ["ceremony_config"])
            self.assertEqual(updates["ceremony_config"][key], expected)

    def test_unavailable_schema_or_channel_is_invalid(self):
        self.assertIsNotNone(self.go(session(), "cer", "1", ceremony_available=False)[1])
        self.assertIsNotNone(self.go(session(), "cer", "1", is_channel=True)[1])

    def test_enable_refused_when_not_eligible(self):
        for sess in (session(poll_is_anonymous=True), session(show_names=False), session(pacing_mode="burst")):
            updates, err = self.go(sess, "cer", "1")
            self.assertIsNone(updates)
            self.assertIn("حفل التكريم", err)

    def test_garbage_value_rejected(self):
        self.assertIsNotNone(self.go(session(), "cer", "zzz")[1])

    def test_other_changes_auto_disable_ceremony(self):
        for field, value in (("mode", "a"), ("names", "0"), ("pace", "b")):
            updates, err = self.go(with_cer(), field, value)
            self.assertIsNone(err, (field, value))
            self.assertFalse(updates["ceremony_config"]["enabled"], (field, value))

    def test_harmless_change_leaves_ceremony_alone(self):
        updates, _ = self.go(with_cer(), "top", "10")
        self.assertNotIn("ceremony_config", updates)

    def test_disabled_ceremony_is_not_touched_by_other_changes(self):
        updates, _ = self.go(session(ceremony_config=cfg(enabled=False)), "mode", "a")
        self.assertNotIn("ceremony_config", updates)

    def test_both_features_disable_together(self):
        s = session(ceremony_config=cfg(), cert_config=gc.normalize_config({"enabled": True}))
        updates, _ = self.go(s, "mode", "a")
        self.assertFalse(updates["ceremony_config"]["enabled"])
        self.assertFalse(updates["cert_config"]["enabled"])

    def test_state_exposes_ceremony(self):
        st = self.env["_setup_state"](with_cer(count=3), is_channel=False)
        self.assertTrue(st["cer"])
        self.assertEqual(st["cer_cfg"]["count"], 3)
        st = self.env["_setup_state"](with_cer(), is_channel=True)
        self.assertFalse(st["cer"])
        self.assertTrue(st["cer_reason"])
        st = self.env["_setup_state"]({"id": SID}, is_channel=False)
        self.assertFalse(st["cer"])


class SummaryAndScreensTests(unittest.TestCase):
    def setUp(self):
        self.env = base.load_rules()

    def summary(self, sess, **kw):
        state = self.env["_setup_state"](sess, is_channel=False)
        return self.env["_setup_summary_text"](state, title="كويز", dest_title="غروب", total=10, is_channel=False, **kw)

    def test_summary_line_only_when_available(self):
        self.assertNotIn("حفل التكريم", self.summary(with_cer()))
        self.assertIn("حفل التكريم: غير مفعّل", self.summary(session(), ceremony_available=True))

    def test_summary_describes_enabled_ceremony(self):
        text = self.summary(with_cer(count=3, format="image", music=True), ceremony_available=True)
        self.assertIn("مفعّل (أول 3، صورة + أغنية)", text)
        text = self.summary(with_cer(count=10, format="video", music=False), ceremony_available=True)
        self.assertIn("(أول 5، فيديو)", text)

    def test_screen_text_escapes_title(self):
        text = self.env["_ceremony_screen_text"](cfg(title="<b>x</b>"), enabled=True, reason=None, top=5)
        self.assertNotIn("<b>x</b>", text)
        self.assertIn("&lt;b&gt;x&lt;/b&gt;", text)

    def test_screen_caps_count_by_top(self):
        text = self.env["_ceremony_screen_text"](cfg(count=10), enabled=True, reason=None, top=3)
        self.assertIn("أول 3", text)

    def test_menu_button_visibility(self):
        kb = base.load_keyboards()["get_group_setup_menu_keyboard"]
        shown = base.datas(kb(SID, ceremony_available=True))
        hidden = base.datas(kb(SID, ceremony_available=False))
        channel = base.datas(kb(SID, is_channel=True, ceremony_available=True))
        self.assertIn(f"gqs:{SID}:o:cer", shown)
        self.assertNotIn(f"gqs:{SID}:o:cer", hidden)
        self.assertNotIn(f"gqs:{SID}:o:cer", channel)

    def test_keyboard_callbacks_valid_and_within_telegram_limit(self):
        kb = base.load_keyboards()["get_group_setup_ceremony_keyboard"]
        for enabled in (True, False):
            data = base.datas(kb(SID, cfg(), enabled=enabled))
            self.assertTrue(all(len(d.encode()) <= 64 for d in data), data)
            values = {d.split(":")[-1] for d in data if ":s:cer:" in d}
            self.assertTrue({"fv", "fi", "m0", "pv", "te", "n3", "n5", "n10"} <= values)
            for v in values - {"pv", "te"}:
                new, err = gcm.apply_config_change(gcm.default_config(), v)
                self.assertIsNone(err, v)
            self.assertEqual(sum(1 for d in data if d.endswith("s:cer:" + ("0" if enabled else "1"))), 1)

    def test_keyboard_marks_current_choices(self):
        kb = base.load_keyboards()["get_group_setup_ceremony_keyboard"]
        labels = base.texts(kb(SID, cfg(format="image", count=10, music=False), enabled=True))
        self.assertTrue(any(t.startswith("✅") and "صورة" in t for t in labels))
        self.assertTrue(any(t.startswith("✅") and "أول 10" in t for t in labels))
        self.assertFalse(any(t.startswith("✅") and "فيديو" in t for t in labels))
        self.assertTrue(any("مطفية" in t for t in labels))


class SetupCallbackTests(base.HandlerTests):
    def build(self, sess=None, **kw):
        env = super().build(sess, **kw)
        env["group_ceremony_schema_ready"].return_value = True
        return env

    async def test_open_screen(self):
        env = self.build(with_cer())
        call = await self.press(env, f"gqs:{SID}:o:cer")
        text = call.message.edit_text.await_args.args[0]
        self.assertIn("حفل التكريم", text)
        self.assertIn("✅ مفعّل", text)

    async def test_open_refused_when_schema_missing(self):
        env = self.build(with_cer())
        env["group_ceremony_schema_ready"].return_value = False
        call = await self.press(env, f"gqs:{SID}:o:cer")
        call.message.edit_text.assert_not_awaited()
        self.assertTrue(call.answer.await_args_list[0].kwargs.get("show_alert"))

    async def test_open_refused_with_reason_when_anonymous(self):
        env = self.build(session(poll_is_anonymous=True))
        call = await self.press(env, f"gqs:{SID}:o:cer")
        call.message.edit_text.assert_not_awaited()
        self.assertIn("النمط التنافسي", call.answer.await_args_list[0].args[0])

    async def test_open_hidden_for_channels(self):
        env = self.build(session(), channel=True)
        call = await self.press(env, f"gqs:{SID}:o:cer")
        call.message.edit_text.assert_not_awaited()

    async def test_enable_saves_with_header_title(self):
        env = self.build(session(), header={"title": "كويز الأحياء", "dest_title": "غروب"})
        await self.press(env, f"gqs:{SID}:s:cer:1")
        saved = self.update.await_args.args[1]["ceremony_config"]
        self.assertEqual((saved["enabled"], saved["title"]), (True, "كويز الأحياء"))

    async def test_enable_refused_without_saving_when_names_hidden(self):
        env = self.build(session(show_names=False))
        call = await self.press(env, f"gqs:{SID}:s:cer:1")
        self.update.assert_not_awaited()
        self.assertTrue(call.answer.await_args_list[0].kwargs.get("show_alert"))

    async def test_format_change_redraws_ceremony_screen(self):
        env = self.build(with_cer())
        call = await self.press(env, f"gqs:{SID}:s:cer:fi")
        self.assertEqual(self.update.await_args.args[1]["ceremony_config"]["format"], "image")
        self.assertIn("حفل التكريم", call.message.edit_text.await_args.args[0])

    async def test_preview_and_title_buttons_dispatch_without_saving(self):
        env = self.build(with_cer())
        for value in ("pv", "te"):
            await self.press(env, f"gqs:{SID}:s:cer:{value}")
        self.assertEqual([c.args[3] for c in env["_handle_ceremony_special"].await_args_list], ["pv", "te"])
        self.update.assert_not_awaited()

    async def test_changing_mode_disables_ceremony_and_says_so(self):
        env = self.build(with_cer())
        call = await self.press(env, f"gqs:{SID}:s:mode:a")
        self.assertFalse(self.update.await_args.args[1]["ceremony_config"]["enabled"])
        self.assertIn("عطّلت حفل التكريم", call.message.edit_text.await_args.args[0])

    async def test_go_normalizes_stale_enabled_ceremony(self):
        env = self.build(session(pacing_mode="burst", ceremony_config=cfg()))
        await self.press(env, f"gqs:{SID}:go")
        fixes = self.update.await_args.args[1]
        self.assertFalse(fixes["ceremony_config"]["enabled"])
        self.launch.assert_awaited_once()

    async def test_unknown_field_still_rejected(self):
        env = self.build(session())
        call = await self.press(env, f"gqs:{SID}:s:zzz:1")
        self.update.assert_not_awaited()


# ==================== الإرسال والمعاينة وتعديل العنوان ====================
CER_NAMES = {"_handle_ceremony_special", "_send_ceremony_preview", "_send_ceremony_audio", "_send_ceremony",
             "_send_recognitions", "_awaiting_cert_text", "receive_cert_text"}


def load_ceremony_handlers(**over):
    bot = SimpleNamespace(
        get_chat=AsyncMock(return_value=SimpleNamespace(title="شعبة العلمي")),
        send_photo=AsyncMock(), send_video=AsyncMock(), send_audio=AsyncMock(), send_chat_action=AsyncMock(),
        edit_message_text=AsyncMock(),
    )
    redis = SimpleNamespace(set=AsyncMock(return_value=True), get=AsyncMock(return_value=None), delete=AsyncMock())

    async def safe_call(factory, **_kw):
        return await factory()

    env = dict(
        asyncio=asyncio, datetime=datetime, json=json, html=html, Optional=object, Dict=dict, Any=object,
        types=telegram_types, bot=bot, redis_client=redis, _safe_call=safe_call, logger=Mock(), log_error=Mock(),
        CEREMONY_RENDER_TIMEOUT_SECONDS=5, _CEREMONY_PREVIEW_COOLDOWN_PREFIX="gq:ceremony_preview:",
        _CEREMONY_PREVIEW_COOLDOWN_SECONDS=10, _CEREMONY_AUDIO_FID_KEY="gq:ceremony_audio_file_id",
        _CEREMONY_AUDIO_FID_TTL=100, _CERT_TEXT_WAIT_PREFIX="gq:cert_text_wait:", _CERT_TEXT_WAIT_TTL=300,
        _CERT_CANCEL_WORDS={"إلغاء", "الغاء", "cancel"},
        CERT_DEFAULT_TITLE=gc.DEFAULT_TITLE, CERT_TITLE_MAX_CHARS=gc.TITLE_MAX_CHARS, CERT_ISSUER_MAX_CHARS=gc.ISSUER_MAX_CHARS,
        cert_clean_text=gc.clean_text, cert_normalize_config=gc.normalize_config,
        CEREMONY_MUSIC_SECONDS=gcm.MUSIC_SECONDS, CEREMONY_AUDIO_PATH=gcm.THEME_AUDIO_PATH, CEREMONY_AUDIO_TITLE=gcm.THEME_AUDIO_TITLE,
        CEREMONY_VIDEO_WIDTH=gcm.VIDEO_WIDTH, CEREMONY_VIDEO_HEIGHT=gcm.VIDEO_HEIGHT,
        ceremony_build_payload=gcm.build_payload, ceremony_normalize_config=gcm.normalize_config,
        ceremony_preview_winners=gcm.preview_winners, ceremony_select_winners=gcm.select_winners,
        group_ceremony_schema_ready=AsyncMock(return_value=True),
        _load_setup_header=AsyncMock(return_value={"title": "كويز", "dest_title": "غروب"}),
        get_session=AsyncMock(), update_session=AsyncMock(return_value={"ok": 1}), is_group_admin=AsyncMock(return_value=True),
        _setup_screen_view=AsyncMock(return_value=("شاشة", "kb")), _render_ceremony=AsyncMock(return_value=b"BYTES"),
        _send_certificates=AsyncMock(),
    )
    env.update(over)
    out = load_functions("handlers/group_quiz.py", CER_NAMES, **env)
    out["_bot"], out["_redis"] = env["bot"], env["redis_client"]
    return out


def finished_session(**kw):
    s = dict(id=SID, chat_id=-1001, poll_is_anonymous=False, show_names=True, total_questions=10, message_thread_id=None,
             ceremony_config=cfg())
    s.update(kw)
    return s


ROWS = [part(1, 9, 5), part(2, 8, 5), part(3, 7, 5)]


class SendCeremonyTests(unittest.IsolatedAsyncioTestCase):
    def build(self, **over):
        self.env = load_ceremony_handlers(**over)
        return self.env["_send_ceremony"], self.env["_bot"]

    async def test_video_sent_with_dimensions_and_no_separate_audio(self):
        send, bot = self.build()
        await send(finished_session(), ROWS, 5)
        bot.send_video.assert_awaited_once()
        kw = bot.send_video.await_args.kwargs
        self.assertEqual((kw["chat_id"], kw["width"], kw["height"]), (-1001, gcm.VIDEO_WIDTH, gcm.VIDEO_HEIGHT))
        self.assertTrue(kw["supports_streaming"])
        self.assertEqual(kw["duration"], int(gcm.MUSIC_SECONDS))
        bot.send_audio.assert_not_awaited()
        bot.send_photo.assert_not_awaited()
        payload = self.env["_render_ceremony"].await_args.args[0]
        self.assertEqual((payload["fmt"], payload["music"]), ("mp4", True))
        self.assertEqual(payload["group_name"], "شعبة العلمي")
        self.assertEqual([w["rank"] for w in payload["winners"]], [1, 2, 3])

    async def test_silent_video_duration(self):
        send, bot = self.build()
        await send(finished_session(ceremony_config=cfg(music=False)), ROWS, 5)
        self.assertEqual(bot.send_video.await_args.kwargs["duration"], 12)

    async def test_image_mode_sends_photo_then_song(self):
        send, bot = self.build()
        await send(finished_session(ceremony_config=cfg(format="image")), ROWS, 5)
        bot.send_photo.assert_awaited_once()
        bot.send_audio.assert_awaited_once()
        bot.send_video.assert_not_awaited()
        self.assertEqual(bot.send_audio.await_args.kwargs["title"], gcm.THEME_AUDIO_TITLE)

    async def test_image_mode_without_music_sends_no_audio(self):
        send, bot = self.build()
        await send(finished_session(ceremony_config=cfg(format="image", music=False)), ROWS, 5)
        bot.send_photo.assert_awaited_once()
        bot.send_audio.assert_not_awaited()

    async def test_audio_file_id_is_cached_then_reused(self):
        send, bot = self.build()
        bot.send_audio.return_value = SimpleNamespace(audio=SimpleNamespace(file_id="FID123"))
        await send(finished_session(ceremony_config=cfg(format="image")), ROWS, 5)
        first = bot.send_audio.await_args.kwargs["audio"]
        self.assertIsInstance(first, telegram_types.FSInputFile)
        self.env["_redis"].set.assert_awaited_once()
        self.assertEqual(self.env["_redis"].set.await_args.args[:2], ("gq:ceremony_audio_file_id", "FID123"))
        self.env["_redis"].get.return_value = b"FID123"
        await send(finished_session(ceremony_config=cfg(format="image")), ROWS, 5)
        self.assertEqual(bot.send_audio.await_args.kwargs["audio"], "FID123")

    async def test_thread_id_is_forwarded(self):
        send, bot = self.build()
        await send(finished_session(message_thread_id=77), ROWS, 5)
        self.assertEqual(bot.send_video.await_args.kwargs["message_thread_id"], 77)

    async def test_nothing_sent_when_disabled_anonymous_hidden_names_or_nobody_scored(self):
        for sess, rows in (
            (finished_session(ceremony_config=cfg(enabled=False)), ROWS),
            (finished_session(poll_is_anonymous=True), ROWS),
            (finished_session(show_names=False), ROWS),
            (finished_session(), [part(1, 0, 5)]),
            (finished_session(), []),
            ({"id": SID, "chat_id": -1001}, ROWS),
        ):
            send, bot = self.build()
            await send(sess, rows, 5)
            bot.send_video.assert_not_awaited()
            bot.send_photo.assert_not_awaited()
            self.env["_render_ceremony"].assert_not_awaited()

    async def test_render_failure_is_swallowed_and_nothing_sent(self):
        send, bot = self.build(_render_ceremony=AsyncMock(side_effect=RuntimeError("ffmpeg died")))
        await send(finished_session(), ROWS, 5)
        bot.send_video.assert_not_awaited()
        self.env["log_error"].assert_called()

    async def test_render_timeout_is_swallowed(self):
        async def slow(_payload):
            await asyncio.sleep(1)
        send, bot = self.build(_render_ceremony=slow, CEREMONY_RENDER_TIMEOUT_SECONDS=0.01)
        await send(finished_session(), ROWS, 5)
        bot.send_video.assert_not_awaited()

    async def test_get_chat_failure_does_not_block(self):
        send, bot = self.build()
        bot.get_chat.side_effect = RuntimeError("no")
        await send(finished_session(), ROWS, 5)
        bot.send_video.assert_awaited_once()
        self.assertEqual(self.env["_render_ceremony"].await_args.args[0]["group_name"], "")

    async def test_unexpected_errors_never_propagate(self):
        send, bot = self.build()
        bot.send_chat_action.side_effect = RuntimeError("boom")
        await send(finished_session(), ROWS, 5)

    async def test_winners_limited_by_top_n_and_count(self):
        send, _ = self.build()
        rows = [part(i, 20 - i, i) for i in range(1, 12)]
        await send(finished_session(ceremony_config=cfg(count=10)), rows, 4)
        self.assertEqual(len(self.env["_render_ceremony"].await_args.args[0]["winners"]), 4)


class RecognitionsOrderTests(unittest.IsolatedAsyncioTestCase):
    async def test_ceremony_before_certificates(self):
        order = []
        env = load_ceremony_handlers()
        env["_send_ceremony"].__globals__["_send_certificates"] = AsyncMock(side_effect=lambda *a: order.append("certs"))

        async def ceremony(*a):
            order.append("ceremony")
        env["_send_recognitions"].__globals__["_send_ceremony"] = ceremony
        await env["_send_recognitions"](finished_session(), ROWS, 5)
        self.assertEqual(order, ["ceremony", "certs"])

    async def test_certificates_still_sent_when_ceremony_render_fails(self):
        env = load_ceremony_handlers(_render_ceremony=AsyncMock(side_effect=RuntimeError("x")))
        certs = AsyncMock()
        env["_send_recognitions"].__globals__["_send_certificates"] = certs
        await env["_send_recognitions"](finished_session(), ROWS, 5)
        certs.assert_awaited_once()


class PreviewAndTitleTests(unittest.IsolatedAsyncioTestCase):
    def call(self, user_id=5):
        call = Mock()
        call.answer = AsyncMock()
        call.from_user = SimpleNamespace(id=user_id)
        call.message = Mock()
        call.message.chat = SimpleNamespace(id=user_id)
        call.message.message_id = 99
        call.message.answer = AsyncMock()
        return call

    async def test_preview_renders_static_png_with_fake_names_and_marks_preview(self):
        env = load_ceremony_handlers()
        call = self.call()
        await env["_send_ceremony_preview"](call, {"ceremony_config": cfg(count=5), "total_questions": 10}, SID)
        payload = env["_render_ceremony"].await_args.args[0]
        self.assertEqual((payload["fmt"], payload["music"], payload["preview"]), ("png", False, True))
        self.assertEqual(len(payload["winners"]), 5)
        self.assertIn(payload["winners"][0]["name"], gcm.PREVIEW_NAMES)
        env["_bot"].send_photo.assert_awaited_once()
        self.assertEqual(env["_bot"].send_photo.await_args.kwargs["chat_id"], 5)

    async def test_preview_cooldown(self):
        env = load_ceremony_handlers()
        env["_redis"].set.return_value = False
        call = self.call()
        await env["_send_ceremony_preview"](call, {"ceremony_config": cfg()}, SID)
        env["_render_ceremony"].assert_not_awaited()
        self.assertTrue(call.answer.await_args.kwargs.get("show_alert"))

    async def test_preview_failure_tells_the_admin(self):
        env = load_ceremony_handlers(_render_ceremony=AsyncMock(side_effect=RuntimeError("x")))
        call = self.call()
        await env["_send_ceremony_preview"](call, {"ceremony_config": cfg()}, SID)
        env["_bot"].send_photo.assert_not_awaited()
        call.message.answer.assert_awaited()

    async def test_special_title_arms_the_text_wait(self):
        env = load_ceremony_handlers()
        call = self.call()
        await env["_handle_ceremony_special"](call, {}, SID, "te", is_channel=False)
        key, raw = env["_redis"].set.await_args.args[:2]
        self.assertTrue(key.endswith("5"))
        self.assertEqual(json.loads(raw)["field"], "cer_title")
        call.message.answer.assert_awaited_once()

    async def test_special_refused_for_channels_and_missing_schema(self):
        env = load_ceremony_handlers()
        call = self.call()
        await env["_handle_ceremony_special"](call, {}, SID, "pv", is_channel=True)
        env2 = load_ceremony_handlers(group_ceremony_schema_ready=AsyncMock(return_value=False))
        call2 = self.call()
        await env2["_handle_ceremony_special"](call2, {}, SID, "pv", is_channel=False)
        for c in (call, call2):
            self.assertTrue(c.answer.await_args.kwargs.get("show_alert"))

    async def test_wait_filter_accepts_ceremony_title(self):
        env = load_ceremony_handlers()
        env["_redis"].get.return_value = json.dumps({"sid": SID, "field": "cer_title", "message_id": 1})
        msg = SimpleNamespace(text="عنوان", from_user=SimpleNamespace(id=5))
        self.assertEqual((await env["_awaiting_cert_text"](msg))["cert_wait"]["field"], "cer_title")

    async def test_receiving_title_saves_ceremony_config_and_redraws_ceremony_screen(self):
        env = load_ceremony_handlers()
        env["get_session"].return_value = {"id": SID, "status": "waiting", "started_by": 5, "chat_id": -1001,
                                           "ceremony_config": cfg(count=10)}
        msg = SimpleNamespace(text="  امتحان الكيمياء 😀 ", from_user=SimpleNamespace(id=5), chat=SimpleNamespace(id=5), answer=AsyncMock())
        await env["receive_cert_text"](msg, {"sid": SID, "field": "cer_title", "message_id": 42})
        column, saved = list(env["update_session"].await_args.args[1].items())[0]
        self.assertEqual((column, saved["title"], saved["count"]), ("ceremony_config", "امتحان الكيمياء", 10))
        self.assertEqual(env["_setup_screen_view"].await_args.args[1], "cer")
        env["_redis"].delete.assert_awaited()

    async def test_empty_title_is_refused_and_nothing_saved(self):
        env = load_ceremony_handlers()
        env["get_session"].return_value = {"id": SID, "status": "waiting", "started_by": 5, "chat_id": -1001}
        msg = SimpleNamespace(text="😀😀", from_user=SimpleNamespace(id=5), chat=SimpleNamespace(id=5), answer=AsyncMock())
        await env["receive_cert_text"](msg, {"sid": SID, "field": "cer_title", "message_id": 42})
        env["update_session"].assert_not_awaited()

    async def test_cert_title_flow_unchanged(self):
        env = load_ceremony_handlers()
        env["get_session"].return_value = {"id": SID, "status": "waiting", "started_by": 5, "chat_id": -1001,
                                           "cert_config": gc.normalize_config({"enabled": True})}
        msg = SimpleNamespace(text="شكراً", from_user=SimpleNamespace(id=5), chat=SimpleNamespace(id=5), answer=AsyncMock())
        await env["receive_cert_text"](msg, {"sid": SID, "field": "issuer", "message_id": 42})
        column, saved = list(env["update_session"].await_args.args[1].items())[0]
        self.assertEqual((column, saved["issuer"]), ("cert_config", "شكراً"))
        self.assertEqual(env["_setup_screen_view"].await_args.args[1], "cert")


# ==================== أصول البناء ====================
class AssetScriptTests(unittest.TestCase):
    def test_build_script_constants_match_renderer(self):
        text = (ROOT / "scripts" / "build_ceremony_assets.py").read_text(encoding="utf-8")
        for name in ("FLAME_W", "FRAME_H", "FRAME_W", "VIDEO_FPS", "VIDEO_H", "VIDEO_W"):
            self.assertIn(name, text)
            self.assertTrue(hasattr(cr, name), name)

    def test_production_modules_do_not_need_numpy(self):
        for module in ("services/ceremony_renderer.py", "services/group_ceremony.py"):
            src = (ROOT / module).read_text(encoding="utf-8")
            self.assertNotIn("import numpy", src, module)

    def test_migration_adds_only_the_ceremony_column(self):
        sql = next((ROOT / "db" / "migrations").glob("*group_quiz_ceremony.sql")).read_text(encoding="utf-8")
        self.assertIn("ADD COLUMN IF NOT EXISTS ceremony_config jsonb", sql)
        self.assertIn("DEFAULT '{}'::jsonb", sql)


if __name__ == "__main__":
    unittest.main()
