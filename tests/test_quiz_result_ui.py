"""End-of-quiz UX regressions: independent review snapshots and feedback grouping."""
import json
import unittest
from unittest.mock import AsyncMock, Mock
from types import SimpleNamespace

from aiogram import types
from reliability_support import load_functions
from services.quiz_result_ui import (
    build_wrong_answer_review, review_page_text, review_inline_button,
    review_question_rich_html, review_explanation_rich_html,
    review_question_fallback_text, review_explanation_fallback_text,
    review_expanded_fallback_text,
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
        self.assertIn(
            "🔴 <b>إجابتك:</b> B\n\n🟢 <b>الإجابة الصحيحة:</b> &lt;A&gt;",
            review_page_text(errors, 1),
        )
        self.assertNotIn("Skipped", json.dumps(errors))
        self.assertNotIn("Correct", json.dumps(errors))

    def test_no_mistakes_means_no_review_cta(self):
        self.assertEqual(build_wrong_answer_review(self.questions[:1], {"0": 1}), [])
        self.assertEqual(build_wrong_answer_review(self.questions[:1], {}), [])
        self.assertIn('data="qrevstart:abcdef1234567890"', review_inline_button('abcdef1234567890', 3))

    def test_math_snapshot_keeps_image_original_order_and_explanation(self):
        question = {
            "question": "أوجد: $x^2-4$",
            "options": ["$1$", "$2$", "$3$"],
            "correct_option_id": 2, "is_math": True,
            "image_url": "https://example.org/math-review.png",
            "explanation": "نستخدم قاعدة الفرق بين مربعين.",
            "table": {"headers": ["س"], "rows": [["1"]]},
            "matrices": [{"rows": [["1", "2"], ["3", "4"]]}],
        }
        entry = build_wrong_answer_review([
            {"question": "Earlier", "options": ["a", "b"], "correct_option_id": 0},
            question,
        ], {"1": 0})[0]
        self.assertEqual(entry["number"], 2)
        self.assertEqual(entry["total_questions"], 2)
        self.assertEqual(entry["chosen_index"], 0)
        self.assertEqual(entry["correct_index"], 2)
        self.assertEqual(entry["math_question"]["table"], question["table"])
        self.assertEqual(entry["math_question"]["matrices"], question["matrices"])
        rich = review_question_rich_html(entry, 0, 1, "abcdef1234567890",
                                          entry["image_url"])
        self.assertIn('<img src="https://example.org/math-review.png"/>', rich)
        self.assertIn("🔴 <b>إجابتك:</b> أ", rich)
        self.assertIn("🟢 <b>الإجابة الصحيحة:</b> ج", rich)
        self.assertIn("qrevexp:abcdef1234567890:0", rich)
        self.assertNotIn("نستخدم قاعدة الفرق", rich)
        text_when_missing_image = review_question_rich_html(
            entry, 0, 1, "abcdef1234567890", "")
        self.assertIn("أ:", text_when_missing_image)
        self.assertIn("ب:", text_when_missing_image)
        self.assertIn("ج:", text_when_missing_image)
        explain = review_explanation_rich_html(entry, 0, 1, "abcdef1234567890",
                                                entry["image_url"])
        self.assertIn('<img src="https://example.org/math-review.png"/>', explain)
        self.assertIn("🔴 <b>إجابتك:</b> أ", explain)
        self.assertIn("🟢 <b>الإجابة الصحيحة:</b> ج", explain)
        self.assertIn("<blockquote>نستخدم قاعدة الفرق بين مربعين.</blockquote>", explain)
        self.assertIn("qrevback:abcdef1234567890:0", explain)
        self.assertNotIn("qrevexp:abcdef1234567890:0", explain)
        self.assertLess(explain.index("🟢 <b>الإجابة الصحيحة:"), explain.index("<blockquote>"))
        self.assertLess(explain.index("<blockquote>"), explain.index("qrevback:"))

    def test_missing_explanation_is_disclosed(self):
        entry = build_wrong_answer_review(self.questions[:1], {"0": 0})[0]
        html = review_explanation_rich_html(entry, 0, 1, "abcdef1234567890")
        self.assertIn("لا يوجد شرح محفوظ لهذا السؤال.", html)

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
        await env["_send_quiz_completion"](42, "🏁 <b>نتيجتك</b>\n📊 50%\n🏆 ممتاز!", keyboard, "abcdef1234567890", 2)
        self.bot.send_rich_message.assert_awaited_once()
        rich = self.bot.send_rich_message.await_args.kwargs["rich_message"]
        self.assertIn("qrevstart:abcdef1234567890", rich.html)
        self.assertIn('<tg-button-row align="center">', rich.html)
        self.assertIn('<tg-button type="callback_data" style="primary"', rich.html)
        self.assertIn("❌ الإجابات الخاطئة: 2", rich.html)
        self.assertLess(rich.html.index("📊 50%"), rich.html.index("❌ الإجابات الخاطئة: 2"))
        self.assertLess(rich.html.index("❌ الإجابات الخاطئة: 2"), rich.html.index('<tg-button-row'))
        self.assertTrue(rich.html.endswith(review_inline_button("abcdef1234567890", 2)))
        self.assertLess(rich.html.index("🏆 ممتاز!"), rich.html.index('<tg-button-row'))
        self.assertIs(self.bot.send_rich_message.await_args.kwargs["reply_markup"], keyboard)
        self.bot.send_message.assert_not_awaited()
        self.bot.send_rich_message.side_effect = RuntimeError("Unsupported rich message")
        await env["_send_quiz_completion"](42, "Grade", keyboard, "abcdef1234567890", 2)
        fallback = self.bot.send_message.await_args.kwargs["reply_markup"]
        data = [b.callback_data for row in fallback.inline_keyboard for b in row]
        self.assertEqual(data, ["qrev:abcdef1234567890:0", "leaderboard_id"])

    async def test_review_pages_toggle_in_place_without_touching_result(self):
        class BadRequest(Exception):
            pass

        self.bot.edit_message_text = AsyncMock()
        self.redis.ttl = AsyncMock(return_value=600)
        env = load_functions("handlers/quiz_runner.py",
            {"_review_pager", "_review_show", "_review_fallback"},
            types=types, redis_client=self.redis, json=json, html=__import__("html"),
            re=__import__("re"), bot=self.bot,
            _quiz_review_key=lambda user_id, token: f"quiz:error_review:{user_id}:{token}",
            _review_resolve_image=AsyncMock(return_value=None),
            review_question_rich_html=review_question_rich_html,
            review_explanation_rich_html=review_explanation_rich_html,
            review_question_fallback_text=review_question_fallback_text,
            review_explanation_fallback_text=review_explanation_fallback_text,
            review_expanded_fallback_text=review_expanded_fallback_text,
            TelegramBadRequest=BadRequest, log_warning=Mock(), logger=self.log)

        # Pressing the button in the result sends a new review below it.
        result = SimpleNamespace(chat=SimpleNamespace(id=42), message_id=100,
            text="🏁 اكتمل الاختبار", answer=AsyncMock(), photo=None)
        call = SimpleNamespace(from_user=SimpleNamespace(id=42),
            message=result, answer=AsyncMock())
        await env["_review_show"](call, "abcdef1234567890", 0, new_message=True)
        self.bot.send_rich_message.assert_awaited_once()
        self.bot.edit_message_text.assert_not_awaited()
        opened = self.bot.send_rich_message.await_args.kwargs["rich_message"].html
        self.assertIn("qrevexp:abcdef1234567890:0", opened)

        # Opening explanation edits the already-created review message.
        review = SimpleNamespace(chat=SimpleNamespace(id=42), message_id=101,
            photo=None)
        call.message = review
        await env["_review_show"](call, "abcdef1234567890", 0, explanation=True)
        self.bot.edit_message_text.assert_awaited_once()
        # Explanation/back controls belong to the rich body, not duplicate
        # buttons below the message.
        buttons = self.bot.edit_message_text.await_args.kwargs["reply_markup"].inline_keyboard
        self.assertFalse(any(b.callback_data.startswith("qrevback:")
                             for row in buttons for b in row))
        edited = self.bot.edit_message_text.await_args.kwargs["rich_message"].html
        self.assertIn("qrevback:abcdef1234567890:0", edited)
        self.assertIn("لا يوجد شرح محفوظ", edited)
        self.assertIn("❓ <b>السؤال 2:</b>", edited)
        self.assertIn("🔴 <b>إجابتك:</b> bad", edited)
        self.assertIn("🟢 <b>الإجابة الصحيحة:</b> good", edited)
        self.assertLess(edited.index("🟢 <b>الإجابة الصحيحة:"), edited.index("<blockquote>"))

        # Back to question reuses the exact same message_id.
        await env["_review_show"](call, "abcdef1234567890", 0)
        self.assertEqual(self.bot.edit_message_text.await_count, 2)
        self.assertEqual(self.bot.edit_message_text.await_args.kwargs["message_id"], 101)
        returned = self.bot.edit_message_text.await_args.kwargs["rich_message"].html
        self.assertIn("qrevexp:abcdef1234567890:0", returned)
        self.assertIn("❓ <b>السؤال 2:</b>", returned)
        self.assertNotIn("<blockquote>", returned)
        self.redis.get.assert_awaited_with("quiz:error_review:42:abcdef1234567890")

    async def test_math_review_uses_own_original_image_in_rich_message(self):
        math_question = {"question": "أوجد الناتج", "options": ["1", "2"],
                         "correct_option_id": 1, "is_math": True,
                         "image_url": "https://example.com/math.png",
                         "explanation": "نختار ب."}
        self.redis.get.return_value = json.dumps(
            build_wrong_answer_review([math_question], {"0": 0})).encode()
        self.bot.edit_message_text = AsyncMock()
        env = load_functions("handlers/quiz_runner.py",
            {"_review_pager", "_review_show", "_review_fallback", "_review_resolve_image"},
            types=types, redis_client=self.redis, json=json, html=__import__("html"),
            re=__import__("re"), bot=self.bot,
            _quiz_review_key=lambda user_id, token: f"quiz:error_review:{user_id}:{token}",
            review_question_rich_html=review_question_rich_html,
            review_explanation_rich_html=review_explanation_rich_html,
            review_question_fallback_text=review_question_fallback_text,
            review_explanation_fallback_text=review_explanation_fallback_text,
            review_expanded_fallback_text=review_expanded_fallback_text,
            TelegramBadRequest=type("BadRequest", (Exception,), {}),
            log_warning=Mock(), logger=self.log)
        call = SimpleNamespace(from_user=SimpleNamespace(id=42),
            message=SimpleNamespace(chat=SimpleNamespace(id=42), message_id=104,
                                    photo=None),
            answer=AsyncMock())
        await env["_review_show"](call, "abcdef1234567890", 0)
        rich = self.bot.edit_message_text.await_args.kwargs["rich_message"].html
        self.assertIn('<img src="https://example.com/math.png"/>', rich)
        self.assertIn("qrevexp:abcdef1234567890:0", rich)
        self.assertIn("🔴 <b>إجابتك:</b> أ", rich)
        self.assertIn("🟢 <b>الإجابة الصحيحة:</b> ب", rich)

        # Clicking explain keeps the math image and the question visible.
        await env["_review_show"](call, "abcdef1234567890", 0, explanation=True)
        expanded = self.bot.edit_message_text.await_args.kwargs["rich_message"].html
        self.assertIn('<img src="https://example.com/math.png"/>', expanded)
        self.assertIn("❓ <b>السؤال 1:</b>", expanded)
        self.assertIn("🔴 <b>إجابتك:</b> أ", expanded)
        self.assertIn("🟢 <b>الإجابة الصحيحة:</b> ب", expanded)
        self.assertIn("<blockquote>نختار ب.</blockquote>", expanded)
        self.assertIn("qrevback:abcdef1234567890:0", expanded)
        self.assertNotIn("qrevexp:abcdef1234567890:0", expanded)

        await env["_review_show"](call, "abcdef1234567890", 0)
        collapsed = self.bot.edit_message_text.await_args.kwargs["rich_message"].html
        self.assertIn('<img src="https://example.com/math.png"/>', collapsed)
        self.assertIn("❓ <b>السؤال 1:</b>", collapsed)
        self.assertNotIn("<blockquote>", collapsed)

    async def test_photo_fallback_keeps_explanation_and_back_in_place(self):
        env = load_functions("handlers/quiz_runner.py",
            {"_review_pager", "_review_fallback"},
            types=types, html=__import__("html"), re=__import__("re"))
        message = SimpleNamespace(photo=[SimpleNamespace(file_id="photo")],
                                  edit_caption=AsyncMock(), edit_text=AsyncMock())
        call = SimpleNamespace(message=message)
        fallback = env["_review_pager"](
            "abcdef1234567890", 0, 1, explanation=True, fallback_controls=True)
        entry = {"is_math": True, "number": 4, "question": "Math",
                 "chosen": "$1$", "correct": "$2$", "chosen_index": 0,
                 "correct_index": 1, "math_question": {
                     "question": "أوجد الناتج", "options": ["$1$", "$2$"]
                 }, "explanation": "الحل هو 2"}
        expanded = review_expanded_fallback_text(entry, 0, 1)
        await env["_review_fallback"](
            call, expanded, fallback, None, False)
        message.edit_caption.assert_awaited_once()
        caption = message.edit_caption.await_args.kwargs["caption"]
        self.assertIn("السؤال 4", caption)
        self.assertIn("إجابتك:", caption)
        self.assertIn("الإجابة الصحيحة:", caption)
        self.assertIn("الحل هو 2", caption)
        action = fallback.inline_keyboard[0][0].callback_data
        self.assertEqual(action, "qrevback:abcdef1234567890:0")
        message.edit_text.assert_not_awaited()

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
