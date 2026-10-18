"""End-of-quiz UX regressions: independent review snapshots and feedback grouping."""
import json
import unittest
from unittest.mock import AsyncMock, Mock
from types import SimpleNamespace

from aiogram import types
from reliability_support import load_functions
from services.quiz_result_ui import (
    build_wrong_answer_review, review_page_text, review_inline_button,
)


class ErrorReviewPresentationTests(unittest.TestCase):
    def setUp(self):
        self.questions = [
            {"question": "2 + 2", "options": ["3", "4", "5"], "correct_option_id": 1},
            {"question": "<ما الحل?>", "options": ["<A>", "B"], "correct_option_id": 0},
            {"question": "Skipped", "options": ["A", "B"], "correct_option_id": 0},
            {"question": "Correct", "options": ["A", "B"], "correct_option_id": 0},
        ]

    def test_only_wrong_answers_are_selected_and_escaped(self):
        errors = build_wrong_answer_review(self.questions, {"0": 0, "1": 1, "3": 0})
        self.assertEqual([e["number"] for e in errors], [1, 2])
        self.assertIn("2 + 2", review_page_text(errors, 0))
        self.assertIn("&lt;ما الحل?&gt;", review_page_text(errors, 1))
        self.assertIn("&lt;A&gt;", review_page_text(errors, 1))
        self.assertNotIn("Skipped", json.dumps(errors))
        self.assertNotIn("Correct", json.dumps(errors))

    def test_no_mistakes_means_no_review_cta(self):
        self.assertEqual(build_wrong_answer_review(self.questions[:1], {"0": 1}), [])
        self.assertEqual(build_wrong_answer_review(self.questions[:1], {}), [])
        self.assertIn('data="qrev:abcdef1234567890:0"', review_inline_button('abcdef1234567890', 3))

    def test_leaderboard_keeps_its_main_row_and_feedback_is_consolidated(self):
        base = types.InlineKeyboardMarkup(inline_keyboard=[
            [types.InlineKeyboardButton(text="إعادة", callback_data="quiz_replay")],
            [types.InlineKeyboardButton(text="حفظ", callback_data="quiz_favorite")],
            [types.InlineKeyboardButton(text="لوحة الشرف", callback_data="leaderboard_id")],
            [types.InlineKeyboardButton(text="الرئيسية", callback_data="student_home")],
        ])
        env = load_functions("keyboards.py", {"get_rating_keyboard"},
            types=types, get_quiz_result_keyboard=lambda **kw: base)
        keyboard = env["get_rating_keyboard"]("123")
        data = [button.callback_data for row in keyboard.inline_keyboard for button in row]
        self.assertEqual(data.index("leaderboard_id"), 2)
        self.assertEqual(data.index("quiz_feedback_123"), 3)
        self.assertNotIn("rate_like_123", data)
        self.assertNotIn("rate_dislike_123", data)


class ErrorReviewCallbackTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.entries = [
            {"number": 2, "question": "A", "chosen": "bad", "correct": "good"},
            {"number": 4, "question": "B", "chosen": "X", "correct": "Y"},
        ]
        self.redis = SimpleNamespace(
            get=AsyncMock(return_value=json.dumps(self.entries).encode()),
            set=AsyncMock(),
        )
        self.bot = SimpleNamespace(send_rich_message=AsyncMock(),
                                   send_message=AsyncMock())
        self.log = Mock()

    async def test_snapshot_written_with_user_scoped_key(self):
        env = load_functions("handlers/quiz_runner.py",
            {"_quiz_review_key", "_store_quiz_error_review"},
            redis_client=self.redis, json=json,
            build_wrong_answer_review=build_wrong_answer_review,
            secrets=SimpleNamespace(token_hex=lambda _: "abcdef1234567890"),
            _REVIEW_TTL_SECONDS=604800, log_warning=Mock(), logger=self.log)
        data = {"questions": [
            {"question": "A", "options": ["wrong", "right"], "correct_option_id": 1}],
            "answered_questions": {"0": 0}}
        token, wrong = await env["_store_quiz_error_review"](42, data)
        self.assertEqual((token, wrong), ("abcdef1234567890", 1))
        args, kwargs = self.redis.set.await_args
        self.assertEqual(args[0], "quiz:error_review:42:abcdef1234567890")
        self.assertEqual(kwargs["ex"], 604800)
        self.assertEqual(json.loads(args[1])[0]["chosen"], "wrong")

    async def test_rich_button_with_legacy_fallback(self):
        env = load_functions("handlers/quiz_runner.py", {"_send_quiz_completion"},
            types=types, bot=self.bot, review_inline_button=review_inline_button,
            log_warning=Mock(), logger=self.log)
        keyboard = types.InlineKeyboardMarkup(inline_keyboard=[[
            types.InlineKeyboardButton(text="لوحة الشرف", callback_data="leaderboard_id")]])
        await env["_send_quiz_completion"](42, "🏁 <b>نتيجتك</b>\n📊 50%", keyboard, "abcdef1234567890", 2)
        self.bot.send_rich_message.assert_awaited_once()
        rich = self.bot.send_rich_message.await_args.kwargs["rich_message"]
        self.assertIn("qrev:abcdef1234567890:0", rich.html)
        self.assertIn('<tg-button-row align="center">', rich.html)
        self.assertIn('<tg-button type="callback_data" style="primary"', rich.html)
        self.assertIn("❌ الإجابات الخاطئة: 2", rich.html)
        self.assertLess(rich.html.index("📊 50%"), rich.html.index("❌ الإجابات الخاطئة: 2"))
        self.assertLess(rich.html.index("❌ الإجابات الخاطئة: 2"), rich.html.index('<tg-button-row'))
        self.assertIs(self.bot.send_rich_message.await_args.kwargs["reply_markup"], keyboard)
        self.bot.send_message.assert_not_awaited()
        self.bot.send_rich_message.side_effect = RuntimeError("Unsupported rich message")
        await env["_send_quiz_completion"](42, "Grade", keyboard, "abcdef1234567890", 2)
        fallback = self.bot.send_message.await_args.kwargs["reply_markup"]
        data = [b.callback_data for row in fallback.inline_keyboard for b in row]
        self.assertEqual(data, ["qrev:abcdef1234567890:0", "leaderboard_id"])

    async def test_review_pages_leave_the_result_intact(self):
        env = load_functions("handlers/quiz_runner.py", {"show_quiz_error_review"},
            types=types, redis_client=self.redis, json=json,
            _quiz_review_key=lambda user_id, token: f"quiz:error_review:{user_id}:{token}",
            review_page_text=review_page_text,
            TelegramBadRequest=type("BadRequest", (Exception,), {}),
            log_warning=Mock(), logger=self.log)
        message = SimpleNamespace(text="🏁 اكتمل الاختبار",
                                  answer=AsyncMock(), edit_text=AsyncMock())
        call = SimpleNamespace(data="qrev:abcdef1234567890:0",
                               from_user=SimpleNamespace(id=42),
                               message=message, answer=AsyncMock())
        await env["show_quiz_error_review"](call)
        message.answer.assert_awaited_once()
        message.edit_text.assert_not_awaited()
        keyboard = message.answer.await_args.kwargs["reply_markup"]
        self.assertEqual(keyboard.inline_keyboard[0][0].callback_data,
                         "qrev:abcdef1234567890:1")

        message.text = "📚 مراجعة الأخطاء · 1 من 2"
        message.answer.reset_mock()
        call.data = "qrev:abcdef1234567890:1"
        await env["show_quiz_error_review"](call)
        message.edit_text.assert_awaited_once()
        message.answer.assert_not_awaited()
        self.redis.get.assert_awaited_with("quiz:error_review:42:abcdef1234567890")

    async def test_feedback_hub_preserves_rating_actions(self):
        env = load_functions("handlers/quiz_runner.py", {"open_quiz_feedback_panel"},
            types=types, _is_valid_uuid=lambda _: True)
        message = SimpleNamespace(answer=AsyncMock())
        call = SimpleNamespace(data="quiz_feedback_01234567-89ab-cdef-0123-456789abcdef",
                               message=message, answer=AsyncMock())
        await env["open_quiz_feedback_panel"](call)
        keys = [b.callback_data for row in message.answer.await_args.kwargs[
            "reply_markup"].inline_keyboard for b in row]
        self.assertIn("rate_like_01234567-89ab-cdef-0123-456789abcdef", keys)
        self.assertIn("rate_dislike_01234567-89ab-cdef-0123-456789abcdef", keys)
        self.assertIn("rate_feedback_01234567-89ab-cdef-0123-456789abcdef", keys)


if __name__ == "__main__":
    unittest.main()
