"""Source -> import -> player presentation regressions. No network or credentials.

Uses real aiogram models and delivery functions, plus isolated database/Telegram boundaries.
"""
import asyncio
import copy
import json
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from aiogram import Bot, types
from aiogram.client.session.base import BaseSession
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError, TelegramRetryAfter
from helpers.challenge_delivery import deliver_screen
from services.challenge_content import from_existing_quiz, parse_challenge_json, ChallengeContentError
from services.challenge_presentation import PhotoText, source_image, image_option_label
from services.challenge_diagnostics import log_failure
from services import challenge_live_ui as ui
from test_challenge_live_ui import make_env, FakeLive, mstate, MID, callback, run
import test_challenge_live as repo_tests
from reliability_support import load_functions


SOURCE = {"question": "  متتالية $u_n$\nاختر:\n", "options": [" $n^2$ ", "$2n$"],
          "correct_option_id": 1, "explanation": "شرح المصدر", "is_math": True,
          "image_url": "https://example.test/original-question.png",
          "table": {"headers": ["n"], "rows": [[1]]}, "matrices": []}


class SourceTests(unittest.TestCase):
    def test_json_and_share_import_preserve_same_image_formatting_and_order(self):
        original = copy.deepcopy(SOURCE)
        a = parse_challenge_json(json.dumps({"title": "T", "is_math_quiz": True,
                                            "questions": [original]}).encode())
        b = from_existing_quiz({"id": "source-id", "source_title": "T", "is_math_quiz": True,
                                "quiz_data": [original]})
        self.assertEqual(a.questions, b.questions)
        for parsed in (a, b):
            question = parsed.questions[0]
            for field in ("question", "options", "correct_option_id", "explanation"):
                self.assertEqual(question[field], SOURCE[field])
            for field in ("image_url", "is_math", "table", "matrices"):
                self.assertEqual(question["metadata"][field], SOURCE[field])
        original["table"]["rows"][0][0] = 999
        self.assertEqual(a.questions[0]["metadata"]["table"]["rows"], [[1]])

    def test_nested_image_metadata_and_math_flag_are_supported(self):
        q = {"question": "q", "options": ["a", "b"], "correct_option_id": 0,
             "metadata": {"image_url": SOURCE["image_url"], "is_math": True}}
        p = parse_challenge_json(json.dumps([q]).encode(), "T")
        self.assertEqual(p.questions[0]["metadata"], q["metadata"])

    def test_missing_source_image_never_generates_or_silently_downgrades(self):
        q = {k: v for k, v in SOURCE.items() if k != "image_url"}
        with self.assertRaises(ChallengeContentError):
            from_existing_quiz({"quiz_data": [q], "source_title": "T"})
        q.pop("is_math")
        with self.assertRaises(ChallengeContentError):
            parse_challenge_json(json.dumps({"title": "T", "is_math_quiz": True, "questions": [q]}).encode())

    def test_reimporting_repaired_media_has_new_hash(self):
        def parse(image):
            q = {"question": "q", "options": ["a", "b"], "correct_option_id": 0}
            if image:
                q["image_url"] = image
            return parse_challenge_json(json.dumps([q]).encode(), "T")
        a, b = parse(None), parse(SOURCE["image_url"])
        self.assertNotEqual(a.content_hash, b.content_hash)
        self.assertEqual(b.content_hash, parse(SOURCE["image_url"]).content_hash)
        self.assertNotEqual(b.content_hash, parse("https://example.test/corrected.png").content_hash)

    def test_bank_snapshot_image_reaches_both_live_and_land_screens(self):
        from uuid import UUID
        import datetime as dt
        parsed = from_existing_quiz({"source_title": "T", "quiz_data": [SOURCE]})
        bank = parsed.questions[0]
        # These are the existing SQL snapshot/player-state projection fields (0009/0010).
        player_question = {"text": bank["question"], "options": bank["options"],
                           "image_url": bank["metadata"]["image_url"]}
        live = mstate(question=player_question)
        land = dict(live, attacker_id=7)
        funcs = load_functions("handlers/challenge_game.py", ["button", "attack_screen"],
                               UUID=UUID, dt=dt, types=types, PhotoText=PhotoText,
                               source_image=source_image, image_option_label=image_option_label)
        for text, rows in (ui.question_screen(live), funcs["attack_screen"](land)):
            self.assertEqual(text.image_url, SOURCE["image_url"])
            self.assertNotIn("$", text)
            self.assertNotIn(SOURCE["explanation"], text)
            self.assertTrue(any("أ / A" in b.text for row in rows for b in row))

    def test_signature_changes_when_only_source_image_changes(self):
        rows = [[ui.Btn("A", "lv:q")]]
        self.assertNotEqual(ui.signature((PhotoText("q", "https://x/a"), rows)),
                            ui.signature((PhotoText("q", "https://x/b"), rows)))


class TelegramSession(BaseSession):
    def __init__(self):
        super().__init__()
        self.calls = []
        self.errors = {}

    async def close(self):
        pass

    async def stream_content(self, *args, **kwargs):
        yield b""

    async def make_request(self, bot, method, timeout=None):
        self.calls.append(method)
        name = method.__api_method__
        if name in self.errors:
            raise self.errors[name]
        return types.Message(message_id=getattr(method, "message_id", None) or 900,
                             date=0, chat=types.Chat(id=7, type="private"))


class DeliveryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.session = TelegramSession()
        self.bot = Bot("123456:TEST_TOKEN", session=self.session)
        self.markup = types.InlineKeyboardMarkup(inline_keyboard=[[
            types.InlineKeyboardButton(text="أ / A", callback_data="lv:a:x:0:0")]])

    async def asyncTearDown(self):
        await self.bot.session.close()

    def names(self):
        return [m.__api_method__ for m in self.session.calls]

    async def test_existing_screen_becomes_source_photo_with_answer_buttons(self):
        result = await deliver_screen(self.bot, 7, 100, PhotoText("q", SOURCE["image_url"]), self.markup)
        self.assertEqual(result, 100)
        self.assertEqual(self.names(), ["editMessageMedia"])
        req = self.session.calls[0]
        self.assertEqual(req.media.media, SOURCE["image_url"])
        self.assertEqual(req.media.parse_mode, None)
        self.assertEqual(req.reply_markup, self.markup)

    async def test_no_existing_message_sends_photo_not_text(self):
        await deliver_screen(self.bot, 7, None, PhotoText("q", SOURCE["image_url"]), self.markup)
        self.assertEqual(self.names(), ["sendPhoto"])

    async def test_missing_message_recovers_photo_and_retires_old_buttons(self):
        self.session.errors["editMessageMedia"] = TelegramBadRequest(method=None, message="message to edit not found")
        result = await deliver_screen(self.bot, 7, 100, PhotoText("q", SOURCE["image_url"]), self.markup)
        self.assertEqual(result, 900)
        self.assertEqual(self.names(), ["editMessageMedia", "sendPhoto", "editMessageReplyMarkup"])

    async def test_photo_to_text_result_preserves_buttons_on_new_message(self):
        self.session.errors["editMessageText"] = TelegramBadRequest(method=None, message="there is no text in the message to edit")
        result = await deliver_screen(self.bot, 7, 100, "🏁 انتهى", self.markup)
        self.assertEqual(result, 900)
        self.assertEqual(self.names(), ["editMessageText", "sendMessage", "editMessageReplyMarkup"])

    async def test_broken_source_image_is_not_replaced_by_plain_question(self):
        error = TelegramBadRequest(method=None, message="failed to get HTTP URL content")
        self.session.errors.update(editMessageMedia=error, sendPhoto=error)
        with self.assertRaises(TelegramBadRequest):
            await deliver_screen(self.bot, 7, 100, PhotoText("q", SOURCE["image_url"]), self.markup)
        self.assertNotIn("sendMessage", self.names())
        self.assertNotIn("editMessageReplyMarkup", self.names())

    async def test_rate_limit_propagates_and_blocked_returns_none(self):
        self.session.errors["editMessageMedia"] = TelegramRetryAfter(method=None, message="slow down", retry_after=2)
        with self.assertRaises(TelegramRetryAfter):
            await deliver_screen(self.bot, 7, 100, PhotoText("q", SOURCE["image_url"]), self.markup)
        self.session.errors["editMessageMedia"] = TelegramForbiddenError(method=None, message="blocked")
        self.assertIsNone(await deliver_screen(self.bot, 7, 100, PhotoText("q", SOURCE["image_url"]), self.markup))


class LiveRegressions(unittest.TestCase):
    def test_worker_updates_opponent_answered_within_same_question(self):
        live = FakeLive(match_state=mstate(), attach_message=None)
        env = make_env(live)
        row = {"id": MID, "status": "in_progress", "question_index": 0}
        run(env["render_for"](env["bot"], row, 7, 100))
        live.script["match_state"] = mstate(opp_answered=True)
        run(env["render_for"](env["bot"], row, 7, 100))
        self.assertIn("أجاب بالفعل", env["bot"].edits[-1].text)

    def test_slow_old_response_cannot_overwrite_new_question(self):
        env = make_env(FakeLive(attach_message=None))
        run(env["present"](env["bot"], 7, 100, mstate(index=1, server_now="2026-10-05T10:00:12Z")))
        run(env["present"](env["bot"], 7, 100, mstate(index=0)))
        self.assertEqual(len(env["bot"].edits), 1)
        self.assertIn("السؤال 2", env["bot"].edits[-1].text)

    def test_worker_keeps_selected_options_page(self):
        st = mstate(question={"text": "q", "options": list("abcdef")})
        env = make_env(FakeLive(match_state=st, attach_message=None))
        run(env["present"](env["bot"], 7, 100, st, page=2))
        run(env["render_for"](env["bot"], {"id": MID, "status": "in_progress", "question_index": 0}, 7, 100))
        self.assertEqual(len(env["bot"].edits), 1)
        self.assertIn("5) e", env["bot"].edits[-1].text)

    def test_one_failed_pair_or_tick_does_not_skip_others(self):
        funcs, _ = repo_tests.RepoTests().env({})
        funcs["try_match"] = AsyncMock(side_effect=[RuntimeError("private details"), {"status": "matched"}])
        self.assertEqual(run(funcs["pair_waiting"]([7, 8])), [{"status": "matched"}])
        funcs["expire_queue"] = AsyncMock(return_value=[])
        funcs["due_matches"] = AsyncMock(return_value=["bad", "good"])
        funcs["tick"] = AsyncMock(side_effect=[RuntimeError("private details"), {"id": "good", "status": "completed"}])
        self.assertEqual(run(funcs["sweep"]())["matches"], [{"id": "good", "status": "completed"}])

    def test_diagnostics_include_location_but_not_sensitive_error_message(self):
        logger = Mock()
        try:
            raise TypeError("TOKEN_secret correct_option=1")
        except TypeError as exc:
            log_failure(logger, "event", exc, match="m")
        args = logger.warning.call_args.args
        line = args[0] % args[1:]
        self.assertIn("TypeError", line)
        self.assertIn("test_diagnostics_include", line)
        self.assertNotIn("TOKEN_secret", line)
        self.assertNotIn("correct_option", line)

    def test_real_wake_event_interrupts_worker_wait(self):
        async def scenario():
            wake = asyncio.Event()
            env = load_functions("handlers/challenge_live.py", ["wake_live", "wait_for_work"], asyncio=asyncio, _wake=wake)
            task = asyncio.create_task(env["wait_for_work"](20))
            await asyncio.sleep(0)
            env["wake_live"]()
            await asyncio.wait_for(task, timeout=0.2)
            self.assertFalse(wake.is_set())
        run(scenario())

    def test_generic_error_offers_real_resume_button(self):
        _, rows = ui.error_screen(TypeError("internal"))
        self.assertTrue(any(b.data == "lv:q" for row in rows for b in row))


if __name__ == "__main__":
    unittest.main()
