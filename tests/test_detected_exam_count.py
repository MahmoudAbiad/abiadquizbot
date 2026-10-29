"""Detected/manual exam counts govern the review, request and persistence."""
import asyncio
import types
import unittest
from unittest.mock import AsyncMock, Mock

import constants as C
from reliability_support import load_functions
from test_similar_math_mode import State
import test_generation_regressions as regressions


WIDGETS = types.SimpleNamespace(
    InlineKeyboardButton=lambda **kw: types.SimpleNamespace(**kw),
    InlineKeyboardMarkup=lambda **kw: types.SimpleNamespace(**kw),
)


class ExamCountTests(unittest.IsolatedAsyncioTestCase):
    def ui(self):
        self.draw = AsyncMock()
        return load_functions('handlers/files.py', {
            'show_question_count_screen', 'handle_count_custom_prompt',
            'process_count_custom_text',
        }, **vars(C), types=WIDGETS, _render_question_count_screen=self.draw,
            QuizState=types.SimpleNamespace(waiting_for_count='count'),
            DEFAULT_COUNT_SUGGESTIONS=[5, 10, 15, 20],
            validate_question_count=lambda n: (1 <= n <= 120, 'invalid'),
            logger=Mock(), log_error=Mock())

    async def test_detected_count_and_manual_correction_survive_review(self):
        env = self.ui()
        state = State(subject_type=C.SUBJECT_QUIZ_UNSOLVED, detected_question_count=17)
        msg = types.SimpleNamespace(bot=object(), chat=types.SimpleNamespace(id=1),
                                    message_id=2, text='21', delete=AsyncMock(),
                                    edit_text=AsyncMock(), answer=AsyncMock())
        await env['show_question_count_screen'](msg, state, edit=True)
        self.assertEqual(state.data['selected_question_count'], 17)
        call = types.SimpleNamespace(message=msg, answer=AsyncMock())
        await env['handle_count_custom_prompt'](call, state)
        msg.edit_text.assert_awaited_once()
        await env['process_count_custom_text'](msg, state)
        self.assertEqual(state.data['selected_question_count'], 21)
        await env['show_question_count_screen'](msg, state, edit=True)
        self.assertEqual(state.data['selected_question_count'], 21)
        msg.text = '121'
        await env['process_count_custom_text'](msg, state)
        self.assertEqual(state.data['selected_question_count'], 21)

    async def test_missing_detection_does_not_invent_ten(self):
        env = self.ui()
        state = State(subject_type=C.SUBJECT_QUIZ_SOLVED)
        msg = types.SimpleNamespace(bot=object(), chat=types.SimpleNamespace(id=1), message_id=2)
        await env['show_question_count_screen'](msg, state)
        self.assertEqual(state.data['selected_question_count'], 0)
        keyboard = load_functions('keyboards.py', {'get_quiz_detected_count_keyboard'},
                                  **vars(C), types=WIDGETS)['get_quiz_detected_count_keyboard']
        actions = [b.callback_data for row in keyboard(0).inline_keyboard for b in row]
        self.assertIn('qcount_custom', actions)
        self.assertNotIn('qcount_start', actions)
        actions = [b.callback_data for row in keyboard(17).inline_keyboard for b in row]
        self.assertIn('qcount_start', actions)

    async def test_render_uses_current_number_after_edit(self):
        keyboard = load_functions('keyboards.py', {'get_quiz_detected_count_keyboard'},
                                  **vars(C), types=WIDGETS)['get_quiz_detected_count_keyboard']
        price = AsyncMock(return_value=22)
        env = load_functions('handlers/files.py', {'_render_question_count_screen'},
            **vars(C), DEFAULT_COUNT_SUGGESTIONS=[5,10,15,20],
            determine_execution_mode=lambda *a, **k: 'Standard',
            calculate_quiz_points_cost=price, build_question_type_label=lambda *a, **k: 'exam',
            build_transparency_text=lambda *a: '', get_quiz_detected_count_keyboard=keyboard)
        state = State(subject_type=C.SUBJECT_QUIZ_SOLVED, selected_question_count=21,
                      count_prompt_text='old count: 17', items_count=1)
        bot = types.SimpleNamespace(edit_message_text=AsyncMock())
        await env['_render_question_count_screen'](bot, 1, 2, state)
        text = bot.edit_message_text.await_args.args[0]
        self.assertIn('21', text)
        self.assertNotIn('17', text)
        self.assertNotIn('تقدير', text)
        self.assertNotIn('المقدّر', text)
        price.assert_awaited_once_with(1, 21, False)

    async def test_service_uses_reviewed_count_and_rejects_short_exam_before_save(self):
        for actual, expected_error in ((1, 'extraction_count_mismatch'), (10, ''), (12, '')):
            with self.subTest(actual=actual):
                questions = [dict(question=str(i), options=['a','b'], correct_option_id=0)
                             for i in range(actual)]
                save = AsyncMock(return_value='quiz-id')
                env = load_functions('services/quiz_service.py', {
                    'execute_quiz_generation_workflow', 'normalize_quiz_option_count'},
                    **vars(C), asyncio=asyncio,
                    _HEAVY_PROCESSING_SEMAPHORE=asyncio.Semaphore(1),
                    get_file_quizzes=AsyncMock(return_value=[]),
                    resolve_generation_question_type_text=lambda *a: None,
                    resolve_quiz_extraction_instruction=lambda *a: 'Extract {count}',
                    determine_execution_mode=lambda *a, **k: 'Standard',
                    generate_quiz_smart=AsyncMock(return_value=questions),
                    get_last_generation_metadata=lambda: {'model': 'test'},
                    spawn=lambda coro: coro.close(), log_ai_generation=AsyncMock(),
                    save_file_quiz_multiple=save, build_question_type_label=lambda *a, **k: 'exam')
                quiz, _, error, meta = await env['execute_quiz_generation_workflow'](
                    1, dict(input_type='text', pure_text='exam', file_hash='source',
                            subject_type=C.SUBJECT_QUIZ_UNSOLVED), 10, None)
                self.assertEqual(error, expected_error)
                self.assertEqual(env['generate_quiz_smart'].await_args.kwargs['count'], 10)
                self.assertFalse(env['generate_quiz_smart'].await_args.kwargs['batch_enabled'])
                if error:
                    self.assertIsNone(quiz)
                    self.assertEqual(meta['extracted_count'], 1)
                    save.assert_not_awaited()
                else:
                    self.assertEqual(len(quiz), 10)
                    self.assertEqual(quiz[0]['options'], ['a','b'])
                    self.assertEqual(len(save.await_args.kwargs['quiz_data']), 10)

    async def test_incomplete_exam_refunds_once_and_retains_upload_for_count_edit(self):
        fixture = regressions.DebitTests()
        fn = fixture.make()
        fixture.call.bot = object()
        fixture.call.message.chat = types.SimpleNamespace(id=1)
        fixture.state.data['file_paths'] = ['source.pdf']
        fixture.env['_missing_upload_paths'] = lambda data: []
        fixture.env['_render_question_count_screen'] = AsyncMock()
        fixture.env['execute_quiz_generation_workflow'].return_value = (
            None, None, 'extraction_count_mismatch', {'extracted_count':1})
        await fn(fixture.call, fixture.state)
        fixture.env['refund_user_on_failure'].assert_awaited_once()
        fixture.env['safe_file_cleanup'].assert_not_called()
        fixture.env['_render_question_count_screen'].assert_awaited_once()
        self.assertEqual(fixture.state.status, 'waiting_for_count')
        self.assertEqual(fixture.state.data['selected_question_count'], 5)
        self.assertNotIn('quiz_data', fixture.state.data)


if __name__ == '__main__':
    unittest.main()
