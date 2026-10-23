"""Isolate upload UI and prompt construction without starting bot services."""
import ast
import asyncio
import sys
import time
import types
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch
from typing import *

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import constants as C


def load_functions(path, names, extra=None):
    tree = ast.parse((ROOT / path).read_text())
    nodes = []
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in names:
            node.decorator_list = []
            nodes.append(node)
    assert len(nodes) == len(names)
    ns = {**vars(C), **globals(), **(extra or {})}
    module = ast.Module(body=[ast.ImportFrom(module='__future__', names=[ast.alias(name='annotations')], level=0), *nodes], type_ignores=[])
    exec(compile(ast.fix_missing_locations(module), str(ROOT / path), 'exec'), ns)
    return ns


class State:
    def __init__(self, **data):
        self.data = data
        self.current = None
    async def get_data(self):
        return self.data.copy()
    async def update_data(self, **data):
        self.data.update(data)
    async def set_state(self, state):
        self.current = state


def keyboards():
    widgets = types.SimpleNamespace(InlineKeyboardButton=lambda **kw: types.SimpleNamespace(**kw),
                                    InlineKeyboardMarkup=lambda **kw: types.SimpleNamespace(**kw))
    return load_functions('keyboards.py', ['get_quiz_extraction_choice_keyboard', 'get_quiz_type_keyboard'], {'types': widgets})


class SimilarMathTests(unittest.IsolatedAsyncioTestCase):
    def test_buttons_are_explicit_math_only(self):
        ns = keyboards()
        for subject in ('math', 'english', 'french', 'other'):
            kb = ns['get_quiz_type_keyboard'](subject, [], 'general')
            callbacks = [b.callback_data for row in kb.inline_keyboard for b in row]
            self.assertEqual('qtype_similar_math' in callbacks, subject == 'math')
        for solved in (True, False):
            kb = ns['get_quiz_extraction_choice_keyboard'](is_math=True, solved=solved)
            callbacks = [b.callback_data for row in kb.inline_keyboard for b in row]
            self.assertIn('quiz_generate_similar_math', callbacks)
            self.assertEqual('quiz_extract_as_is' in callbacks, solved)
            self.assertIn('quiz_extract_ai_solve', callbacks)
        self.assertNotIn('quiz_generate_similar_math', [b.callback_data for row in
            ns['get_quiz_extraction_choice_keyboard']().inline_keyboard for b in row])

    async def test_ready_quiz_similarity_clears_extraction_and_opens_difficulty(self):
        render = AsyncMock()
        fake_module = types.SimpleNamespace(_render_difficulty_screen=render)
        states = types.SimpleNamespace(waiting_for_quiz_options='options')
        ns = load_functions('handlers/files.py', ['handle_quiz_generate_similar_math'], {'QuizState':states})
        for subject in ('quiz_solved', 'quiz_unsolved'):
            state = State(subject_type=subject, content_subject_type='math', quiz_extraction_mode='as_is')
            call = types.SimpleNamespace(message=object(), answer=AsyncMock())
            with patch.dict(sys.modules, {'handlers.quiz_options':fake_module}):
                await ns['handle_quiz_generate_similar_math'](call,state)
            self.assertEqual(state.data['question_type'], 'similar_math')
            self.assertIsNone(state.data['quiz_extraction_mode'])
            self.assertEqual(state.current, 'options')
        state = State(subject_type='quiz_solved', content_subject_type='english')
        call = types.SimpleNamespace(message=object(), answer=AsyncMock())
        await ns['handle_quiz_generate_similar_math'](call,state)
        self.assertNotIn('question_type',state.data)

    async def test_extracting_after_going_back_resets_similar_mode(self):
        show = AsyncMock()
        ns = load_functions('handlers/files.py', ['_apply_quiz_extraction_choice'], {'show_question_count_screen':show})
        state = State(subject_type='quiz_solved',question_type='similar_math')
        await ns['_apply_quiz_extraction_choice'](types.SimpleNamespace(message=object()),state,'as_is')
        self.assertEqual(state.data['question_type'], 'general')
        self.assertEqual(state.data['quiz_extraction_mode'], 'as_is')
        show.assert_awaited_once()

    def test_cache_counts_and_labels_distinguish_similar_from_extraction(self):
        ns = load_functions('services/quiz_service.py', ['combo_quiz_count','build_question_type_label'])
        rows = [dict(subject_type='quiz_solved',question_type='general',difficulty='medium'),
                dict(subject_type='quiz_solved',question_type='similar_math',difficulty='medium')]
        self.assertEqual(ns['combo_quiz_count'](rows,'quiz_solved','similar_math','medium'),1)
        self.assertEqual(ns['build_question_type_label']('quiz_solved','similar_math'), C.BTN_SIMILAR_MATH)
        self.assertNotEqual(ns['build_question_type_label']('quiz_solved','general'), C.BTN_SIMILAR_MATH)

    async def test_new_question_count_is_independent_of_source_exam_count(self):
        draw = AsyncMock()
        ns = load_functions('handlers/files.py', ['show_question_count_screen'], {
            '_render_question_count_screen':draw,
            'DEFAULT_COUNT_SUGGESTIONS':[5,10,15,20],
            'DEFAULT_DETECTED_QUIZ_COUNT_FALLBACK':10,
            'QuizState':types.SimpleNamespace(waiting_for_count='count')})
        message=types.SimpleNamespace(bot=object(),chat=types.SimpleNamespace(id=1),message_id=2)
        state=State(subject_type='quiz_solved',question_type='similar_math',
                    detected_question_count=37,count_prompt_text='كم مسألة جديدة؟')
        await ns['show_question_count_screen'](message,state,edit=True)
        self.assertEqual(state.data['selected_question_count'],10)
        self.assertEqual(state.data['count_prompt_text'],'كم مسألة جديدة؟')
        state=State(subject_type='quiz_solved',question_type='general',detected_question_count=37)
        await ns['show_question_count_screen'](message,state,edit=True)
        self.assertEqual(state.data['selected_question_count'],37)

    async def test_all_unsolved_exams_offer_generation_or_extraction(self):
        show=AsyncMock()
        ns=load_functions('handlers/files.py', ['_proceed_to_quiz_unsolved_screen'], {
            **keyboards(), 'show_question_count_screen':show,
            'QuizState':types.SimpleNamespace(waiting_for_quiz_extraction_choice='choice')})
        message=types.SimpleNamespace(edit_text=AsyncMock(),answer=AsyncMock())
        state=State(subject_type='quiz_unsolved',content_subject_type='math')
        await ns['_proceed_to_quiz_unsolved_screen'](message,state)
        self.assertEqual(state.current,'choice')
        show.assert_not_awaited()
        state=State(subject_type='quiz_unsolved',content_subject_type='english')
        await ns['_proceed_to_quiz_unsolved_screen'](message,state)
        self.assertEqual(state.current, 'choice')
        show.assert_not_awaited()

    async def test_new_generation_reuses_suggestions_and_clears_extraction_state(self):
        show = AsyncMock()
        ns = load_functions('handlers/files.py', ['handle_quiz_generate_new'],
                            {'_show_quiz_options_screen': show})
        for subject in ('math', 'english', 'french', 'other', None):
            state = State(subject_type='quiz_solved', content_subject_type=subject,
                          suggested_question_types=['مفردات الدرس'], english_mode='translated',
                          quiz_extraction_mode='as_is', detected_question_count=37,
                          selected_question_count=37, review_count_to_restore=37)
            call = types.SimpleNamespace(message=object(), answer=AsyncMock())
            await ns['handle_quiz_generate_new'](call, state)
            self.assertEqual(state.data['subject_type'], subject or 'other')
            self.assertIsNone(state.data['quiz_extraction_mode'])
            self.assertIsNone(state.data['detected_question_count'])
            self.assertIsNone(state.data['review_count_to_restore'])
            self.assertEqual(state.data['suggested_question_types'], ['مفردات الدرس'])
            self.assertEqual(state.data['english_mode'], 'translated')
        self.assertEqual(show.await_count, 5)

    def test_new_generation_button_and_dynamic_suggestions_for_every_subject(self):
        ns = keyboards()
        for solved in (True, False):
            kb = ns['get_quiz_extraction_choice_keyboard'](solved=solved)
            self.assertIn('quiz_generate_new', [b.callback_data for row in kb.inline_keyboard for b in row])
        for subject in ('math', 'english', 'french', 'other'):
            kb = ns['get_quiz_type_keyboard'](subject, ['مفردات الدرس'], 'general')
            buttons = [b for row in kb.inline_keyboard for b in row]
            self.assertIn('qtype_other_0', [b.callback_data for b in buttons])
            self.assertTrue(any('أسئلة شاملة عن الدرس' in b.text for b in buttons))

    def test_two_and_three_original_options_are_valid_in_regular_and_batch_extraction(self):
        normalize = load_functions('services/quiz_service.py', ['normalize_quiz_option_count'])['normalize_quiz_option_count']
        batch = load_functions('helpers/gemini_helper/batch_generation.py',
                               ['_question_key', '_valid_extracted_question', '_valid_question'])
        for count in (2, 3, 4, 5):
            question = dict(question='Original', options=list('ABCDE')[:count], correct_option_id=count-1)
            original = question.copy()
            self.assertEqual(normalize([question]), [original])
            self.assertTrue(batch['_valid_extracted_question'](question))
            self.assertEqual(batch['_valid_question'](question), count == 4)

    async def test_generation_uses_new_prompt_and_ignores_extraction_only_when_selected(self):
        class Var:
            def set(self, value): self.value=value
            def get(self): return getattr(self,'value',None)
        prompt_helpers = load_functions('helpers/gemini_helper/prompts.py',
            ['_resolve_difficulty_instruction','_resolve_question_type_instruction'])
        generate = AsyncMock(return_value=[{'question':'new'}])
        ns = load_functions('helpers/gemini_helper/quiz_generator.py', ['generate_quiz_smart'], {
            **prompt_helpers, '_generate_text_quiz_with_gemini':generate,
            '_last_model_used_var':Var(), '_last_token_usage_var':Var(),
            '_groq_skip_reason_var':Var(), '_last_generation_metadata_var':Var()})
        await ns['generate_quiz_smart'](pure_text='x squared',count=4,is_math_mode=True,
            question_type='similar_math',quiz_extraction_instruction='EXTRACT_ORIGINAL_SENTINEL',
            previous_questions=[{'question':'old'}])
        prompt = generate.await_args.args[1]
        self.assertNotIn('EXTRACT_ORIGINAL_SENTINEL',prompt)
        self.assertIn('حلّ المسألة الجديدة كاملة',prompt)
        self.assertIn('أربعة خيارات مختلفة رياضياً',prompt)
        self.assertIn('معطيات جديدة مختلفة',prompt)
        self.assertIn('إنشاء 4 مسائل',prompt)
        self.assertNotIn('يُمنع إضافة معلومات خارجية',prompt)
        await ns['generate_quiz_smart'](pure_text='x squared',count=4,is_math_mode=True,
            question_type='problems',quiz_extraction_instruction='EXTRACT_ORIGINAL_SENTINEL')
        extraction_prompt = generate.await_args.args[1]
        self.assertIn('EXTRACT_ORIGINAL_SENTINEL', extraction_prompt)
        self.assertIn('مهمة استخراج اختبار جاهز', extraction_prompt)
        self.assertNotIn('مهمتك تحويل النص/المستند', extraction_prompt)
        self.assertNotIn('مستوى الصعوبة المطلوب', extraction_prompt)
        self.assertNotIn('الأسئلة السابقة', extraction_prompt)


if __name__ == '__main__':
    unittest.main()
