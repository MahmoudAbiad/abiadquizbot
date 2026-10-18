"""Exercise practice through the existing quiz runner without Telegram/AI/DB calls."""
import ast
import asyncio
from copy import deepcopy
import html
import json
from pathlib import Path
import secrets
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch
import uuid

from aiogram import types
from reliability_support import FakeRedis, load_functions
from services.mistake_practice import (
    PracticeCallbackGuard, practice_session, practice_snapshot, practice_skipped_count,
)
from services.quiz_result_ui import build_wrong_answer_review, review_inline_button


class State:
    def __init__(self, data=None, status=None):
        self.data = deepcopy(data or {})
        self.status = status

    async def get_data(self): return deepcopy(self.data)
    async def set_data(self, data): self.data = deepcopy(data)
    async def update_data(self, **kwargs): self.data.update(deepcopy(kwargs))
    async def get_state(self): return self.status
    async def set_state(self, status): self.status = status


class Redis(FakeRedis):
    async def get(self, key):
        if self.fail:
            raise ConnectionError("Redis unavailable")
        if self.expires.get(key, float("inf")) <= self.time:
            return None
        return self.values.get(key)


def question(index=0, **extra):
    return dict(question=f"Question {index}", options=["Wrong", "Correct", "Other"],
                correct_option_id=1, hint="Saved hint", explanation="Saved explanation", **extra)


class PracticeTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.redis = Redis()
        self.state = State()
        self.bot = SimpleNamespace(send_message=AsyncMock(), send_rich_message=AsyncMock())
        self.supabase = SimpleNamespace(
            has_completed_any_quiz_before=AsyncMock(return_value=True),
            admin_get_quiz_by_id=AsyncMock(return_value={"creator_id": 42}))
        nav = load_functions("handlers/session_navigation.py", {"session_id"}, uuid=uuid)

        async def active_quiz(state):
            data = await state.get_data()
            return bool(data.get("questions") and not data.get("quiz_completed"))

        nav["active_quiz"] = active_quiz
        nav["offer_transition"] = AsyncMock(return_value=False)
        self.modules = patch.dict("sys.modules", {
            "handlers.session_navigation": SimpleNamespace(**nav),
            "helpers.supabase_helper": self.supabase,
        })
        self.modules.start()
        self.addCleanup(self.modules.stop)
        self.keyboard = types.InlineKeyboardMarkup(inline_keyboard=[[
            types.InlineKeyboardButton(text="Replay", callback_data="quiz_replay")]])
        names = {n.name for n in ast.parse(Path("handlers/quiz_runner.py").read_text(encoding="utf-8")).body
                 if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
        self.env = load_functions("handlers/quiz_runner.py", names,
            asyncio=asyncio, json=json, secrets=secrets, html=html, types=types,
            bot=self.bot, redis_client=self.redis, logger=Mock(), log_warning=Mock(), log_error=Mock(),
            QuizState=SimpleNamespace(answering_quiz="answering"), _REVIEW_TTL_SECONDS=604800,
            build_wrong_answer_review=build_wrong_answer_review, review_inline_button=review_inline_button,
            spawn=lambda coroutine: coroutine.close(),
            get_or_update_high_score=AsyncMock(return_value={"previous_score": None}),
            complete_quiz_attempt=AsyncMock(), mark_quiz_attempt_stopped=AsyncMock(),
            log_usage_event=AsyncMock(), start_quiz_attempt=Mock(return_value="new-original-attempt"), _is_valid_uuid=lambda value: bool(value),
            get_rating_keyboard=lambda *a, **kw: self.keyboard,
            get_quiz_result_keyboard=lambda **kw: self.keyboard,
            get_quiz_exit_confirmation_keyboard=lambda: self.keyboard,
            send_quiz_poll=AsyncMock(),
        )

    def call(self, data, user=42):
        return SimpleNamespace(data=data, from_user=SimpleNamespace(id=user), answer=AsyncMock(),
            message=SimpleNamespace(chat=SimpleNamespace(id=user, type="private"),
                                    delete=AsyncMock(), answer=AsyncMock()))

    async def finish(self):
        await self.env["_handle_quiz_completion"](42, 42, self.state, await self.state.get_data())
        return self.state.data.get("practice_result_token")

    async def start(self, token, user=42):
        call = self.call(f"qpractice:{token}", user)
        await self.env["start_mistake_practice"](call, self.state)
        return call

    async def seed(self, total=40, wrong=8, skipped=0):
        data = dict(questions=[question(i) for i in range(total)], total_count=total,
            current_index=total, score=total - wrong - skipped,
            answered_questions={str(i): (0 if i < wrong else 1) for i in range(total - skipped)},
            quiz_id="original-quiz", attempt_id="original-attempt", source_title="Original",
            quiz_origin="shared", stopped_early=bool(skipped))
        await self.state.set_data(data)
        await self.state.set_state("answering")
        return await self.finish()

    async def test_full_40_to_8_to_3_to_1_to_zero_chain_keeps_original_score(self):
        token = await self.seed()
        identities = {"original-attempt"}
        source_review_key = self.env["_quiz_review_key"](42, token)
        source_review = await self.redis.get(source_review_key)
        for count, remaining in [(8, 3), (3, 1), (1, 0)]:
            await self.start(token)
            self.assertEqual(len(self.state.data["questions"]), count)
            identity = self.state.data["navigation_session_id"]
            self.assertNotIn(identity, identities)
            identities.add(identity)
            self.assertEqual(self.state.data["score"], 0)
            self.assertIsNone(self.state.data["quiz_id"])
            self.assertIsNone(self.state.data["attempt_id"])
            await self.state.update_data(current_index=count, score=count - remaining,
                answered_questions={str(i): (0 if i < remaining else 1) for i in range(count)})
            token = await self.finish()
            if remaining:
                review = json.loads(await self.redis.get(self.env["_quiz_review_key"](42, token)))
                self.assertEqual(len(review), remaining)
        self.assertIsNone(token)
        self.assertIn("أتممت التدريب على جميع أخطائك", self.bot.send_message.await_args.args[1])
        self.assertEqual(await self.redis.get(source_review_key), source_review)
        self.env["get_or_update_high_score"].assert_awaited_once_with(42, "original-quiz", 32, 40)
        self.env["complete_quiz_attempt"].assert_called_once_with("original-attempt", 32)
        self.env["start_quiz_attempt"].assert_not_called()
        self.env["log_usage_event"].assert_called_once()

    async def test_stopped_original_only_explicit_errors_and_original_skips_do_not_block_mastery(self):
        token = await self.seed(total=10, wrong=2, skipped=7)
        await self.start(token)
        self.assertEqual(len(self.state.data["questions"]), 2)
        self.assertEqual(self.state.data["practice_skipped_before"], 0)
        await self.state.update_data(score=2, current_index=2, answered_questions={"0": 1, "1": 1})
        await self.finish()
        self.assertIn("أتممت التدريب على جميع أخطائك", self.bot.send_message.await_args.args[1])
        self.env["mark_quiz_attempt_stopped"].assert_called_once_with("original-attempt")

    async def test_practice_skips_are_not_errors_or_full_mastery_even_in_later_round(self):
        token = await self.seed(total=3, wrong=3)
        await self.start(token)
        await self.state.update_data(current_index=3, score=1, stopped_early=True,
                                     answered_questions={"0": 0, "1": 1})
        token = await self.finish()
        self.assertIn("توقف التدريب", self.bot.send_rich_message.await_args.kwargs["rich_message"].html)
        await self.start(token)
        self.assertEqual(len(self.state.data["questions"]), 1)
        await self.state.update_data(current_index=1, score=1, answered_questions={"0": 1})
        await self.finish()
        text = self.bot.send_message.await_args.args[1]
        self.assertNotIn("أتممت التدريب على جميع أخطائك", text)
        self.assertIn("تُرك 1 سؤالاً", text)

    async def test_zero_errors_correct_or_unanswered_has_no_training_snapshot(self):
        for answers in ({"0": 1}, {}):
            self.assertIsNone(practice_snapshot(dict(questions=[question()], answered_questions=answers), "s"))
        token = await self.seed(total=2, wrong=0)
        self.assertIsNone(token)
        self.bot.send_rich_message.assert_not_awaited()

    async def test_double_click_and_old_result_never_restart_or_replace_active_training(self):
        token = await self.seed()
        await self.start(token)
        before = await self.state.get_data()
        call = await self.start(token)
        self.assertEqual(self.state.data, before)
        self.assertTrue(call.answer.await_args.kwargs["show_alert"])
        await self.state.update_data(current_index=8, score=0, answered_questions={str(i): 0 for i in range(8)})
        next_token = await self.finish()
        before = await self.state.get_data()
        await self.start(token)
        self.assertEqual(self.state.data, before)
        self.assertNotEqual(next_token, token)

    async def test_foreign_expired_missing_and_corrupt_snapshot_leave_state_unchanged(self):
        token = await self.seed()
        before = await self.state.get_data()
        call = await self.start(token, user=99)
        self.assertIn("انتهت صلاحية", call.answer.await_args.args[0])
        self.assertEqual(self.state.data, before)
        key = self.env["_quiz_practice_key"](42, token)
        self.redis.values[key] = "invalid json"
        call = await self.start(token)
        self.assertIn("تعذر تحميل", call.answer.await_args.args[0])
        self.assertEqual(self.state.data, before)
        self.redis.time = 604801
        call = await self.start(token)
        self.assertIn("انتهت صلاحية", call.answer.await_args.args[0])
        self.assertEqual(self.state.data, before)

    async def test_expired_fsm_and_newer_quiz_or_flow_refuse_old_training(self):
        token = await self.seed()
        for data, status in [({}, None), ({"questions": [question()], "score": 7}, "answering"),
                             (await self.state.get_data(), "generating")]:
            await self.state.set_data(data)
            await self.state.set_state(status)
            await self.start(token)
            self.assertEqual(self.state.data, data)
            self.assertEqual(self.state.status, status)

    async def test_math_reuses_url_and_never_writes_subset_image_to_original_quiz(self):
        math = question(4, is_math=True, image_url="https://example.org/math.png",
                        table={"headers": ["x"], "rows": [[1]]}, matrices=[[[1, 2]]])
        snapshot = practice_snapshot(dict(questions=[question(), math], answered_questions={"1": 0}), "s")
        await self.state.set_data(practice_session(snapshot, "new-session"))
        await self.state.set_state("answering")
        await self.env["send_question_by_ids"](42, 42, self.state)
        sent = self.env["send_quiz_poll"].await_args
        self.assertEqual(sent.args[2], math)
        self.assertIsNone(sent.kwargs["quiz_id"])
        self.assertEqual(sent.kwargs["poll_meta"], {"session_id": "new-session"})
        callbacks = [b.callback_data for row in sent.args[5].inline_keyboard for b in row]
        self.assertTrue(all(data.startswith("qctl:") for data in callbacks))
        snapshot["questions"][0]["table"]["rows"][0][0] = 99
        self.assertEqual(self.state.data["questions"][0]["table"]["rows"], [[1]])

    async def test_actual_math_engine_uses_stored_image_without_render_upload_or_cache_write(self):
        bot = SimpleNamespace(send_photo=AsyncMock(), send_poll=AsyncMock(return_value=
            SimpleNamespace(poll=SimpleNamespace(id="math-poll"))))
        env = load_functions("services/quiz_engine.py", {"_send_math_image_question", "_sanitize_open_period"},
            bot=bot, redis_client=self.redis, json=json, looks_arabic=lambda _: False,
            letters_for=lambda *args: ["A", "B", "C"], latex_to_plain=lambda value: value,
            render_question_image_async=AsyncMock(), upload_quiz_question_image=AsyncMock(),
            save_question_image_url=AsyncMock(), _is_valid_uuid=Mock(), types=types)
        await env["_send_math_image_question"](42, 42,
            question(is_math=True, image_url="https://example.org/saved.png"), 0, 1, self.keyboard,
            None, poll_meta={"session_id": "practice-session"})
        bot.send_photo.assert_awaited_once_with(chat_id=42, photo="https://example.org/saved.png",
                                              caption="السؤال 1 من 1 📝", message_thread_id=None)
        env["render_question_image_async"].assert_not_awaited()
        env["upload_quiz_question_image"].assert_not_awaited()
        env["save_question_image_url"].assert_not_awaited()
        self.assertEqual(json.loads(await self.redis.get("poll:math-poll"))["session_id"], "practice-session")

        # A missing original URL can be rendered locally once, then reused by
        # later practice rounds without writing to a remapped source quiz index.
        env["render_question_image_async"].return_value = b"png"
        env["upload_quiz_question_image"].return_value = "https://example.org/new.png"
        env["_question_image_object_path"] = Mock(return_value="tmp/practice.png")
        math = question(is_math=True)
        await env["_send_math_image_question"](42, 42, math, 0, 1, self.keyboard, None)
        self.assertEqual(math["image_url"], "https://example.org/new.png")
        env["save_question_image_url"].assert_not_awaited()

    async def test_current_stop_confirmation_finishes_practice_without_database_tracking(self):
        token = await self.seed(total=2, wrong=2)
        await self.start(token)
        await self.state.update_data(answered_questions={"0": 0})
        control = self.state.data["question_control_token"]
        call = self.call(f"qctl:{control}:stop")
        await self.env["handle_question_control"](call, self.state)
        confirmation = call.message.answer.await_args.kwargs["reply_markup"]
        self.assertEqual(confirmation.inline_keyboard[0][0].callback_data, f"qctl:{control}:confirm")
        call.data = confirmation.inline_keyboard[0][0].callback_data
        await self.env["handle_question_control"](call, self.state)
        self.assertTrue(self.state.data["quiz_completed"])
        self.assertTrue(self.state.data["stopped_early"])
        snapshot = json.loads(await self.redis.get(self.env["_quiz_practice_key"](
            42, self.state.data["practice_result_token"])))
        self.assertEqual(len(snapshot["questions"]), 1)
        self.env["mark_quiz_attempt_stopped"].assert_not_called()

    async def test_loading_a_full_quiz_after_practice_resets_all_practice_flags(self):
        token = await self.seed(total=2, wrong=2)
        await self.start(token)
        call = self.call("load")
        call.chat = call.message.chat
        await self.env["start_loaded_quiz"](call, self.state, [question(), question(1)], "Full", quiz_id="full")
        self.assertFalse(self.state.data["is_mistake_practice"])
        self.assertIsNone(self.state.data["practice_result_token"])
        self.assertEqual(self.state.data["practice_round"], 0)
        self.assertEqual(self.state.data["practice_skipped_before"], 0)
        self.assertEqual(self.state.data["attempt_id"], "new-original-attempt")
        self.assertEqual(self.env["send_quiz_poll"].await_args.kwargs["quiz_id"], "full")

    async def test_snapshot_write_failure_is_disclosed_without_dead_training_button(self):
        self.redis.fail = True
        await self.seed(total=2, wrong=2)
        text = self.bot.send_message.await_args.args[1]
        self.assertIn("تعذر حفظ بيانات المراجعة والتدريب", text)
        self.assertIsNone(self.state.data["practice_result_token"])
        self.assertTrue(self.state.data["quiz_completed"])

    async def test_repeated_completion_is_idempotent(self):
        await self.seed(total=2, wrong=2)
        token = self.state.data["practice_result_token"]
        await self.finish()
        self.assertEqual(self.state.data["practice_result_token"], token)
        self.env["get_or_update_high_score"].assert_awaited_once()

    async def test_rich_and_fallback_have_two_separate_actions(self):
        token = await self.seed()
        rich = self.bot.send_rich_message.await_args.kwargs["rich_message"].html
        self.assertIn(f"qrevstart:{token}", rich)
        self.assertIn(f"qpractice:{token}", rich)
        self.bot.send_rich_message.side_effect = RuntimeError("Unsupported")
        await self.env["_send_quiz_completion"](42, "Result", self.keyboard, token, 8, token)
        buttons = [b.callback_data for row in self.bot.send_message.await_args.kwargs["reply_markup"].inline_keyboard for b in row]
        self.assertIn(f"qrev:{token}:0", buttons)
        self.assertIn(f"qpractice:{token}", buttons)

    async def test_poll_answers_are_deduplicated_and_wrong_session_answers_ignored(self):
        token = await self.seed(total=2, wrong=2)
        await self.start(token)
        identity = self.state.data["navigation_session_id"]
        poll = SimpleNamespace(poll_id="p", option_ids=[1], user=SimpleNamespace(id=42))
        metadata = dict(user_id=42, correct_option_id=1, question_index=0, session_id="original-attempt")
        await self.redis.set("poll:p", json.dumps(metadata))
        await self.env["handle_poll_answer"](poll, self.state)
        self.assertEqual(self.state.data["score"], 0)
        metadata["session_id"] = identity
        await self.redis.set("poll:p", json.dumps(metadata))
        await self.env["handle_poll_answer"](poll, self.state)
        await self.env["handle_poll_answer"](poll, self.state)
        self.assertEqual(self.state.data["score"], 1)
        self.assertEqual(self.state.data["answered_questions"], {"0": 1})

    async def test_stale_next_hint_stop_confirmation_and_legacy_buttons_cannot_change_training(self):
        token = await self.seed()
        await self.start(token)
        before = await self.state.get_data()
        for callback in ("qctl:0000000000000000:next", "qctl:0000000000000000:confirm", "next_question", "quiz_stop_confirmed"):
            call = self.call(callback)
            handler = self.env["handle_question_control"] if callback.startswith("qctl:") else self.env[
                "handle_next" if callback == "next_question" else "stop_quiz_confirmed"]
            await handler(call, self.state)
            self.assertEqual(self.state.data, before)
            self.assertTrue(call.answer.await_args.kwargs["show_alert"])
        current = self.state.data["question_control_token"]
        call = self.call(f"qctl:{current}:next")
        await self.env["handle_question_control"](call, self.state)
        self.assertEqual(self.state.data["current_index"], 1)
        before = await self.state.get_data()
        await self.env["handle_question_control"](call, self.state)
        self.assertEqual(self.state.data, before)

    async def test_redis_failure_does_not_destroy_completed_session_or_existing_review(self):
        token = await self.seed()
        before = await self.state.get_data()
        self.redis.fail = True
        call = await self.start(token)
        self.assertIn("تعذر تحميل", call.answer.await_args.args[0])
        self.assertEqual(self.state.data, before)

    async def test_practice_guard_blocks_historical_save_share_feedback_without_state_changes(self):
        await self.state.set_data({"is_mistake_practice": True})
        guard = PracticeCallbackGuard()
        handler = AsyncMock()
        for callback in ("quiz_share", "quiz_favorite", "save_name_custom", "save_to_sec_123",
                         "quiz_replay", "rate_feedback_original"):
            await guard(handler, self.call(callback), {"state": self.state})
        handler.assert_not_awaited()
        await guard(handler, self.call("qrevstart:1234567890abcdef"), {"state": self.state})
        handler.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
