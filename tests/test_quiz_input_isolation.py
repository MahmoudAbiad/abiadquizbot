"""New quiz inputs must never inherit another input's text or cache identity."""
import ast
import asyncio
import json
import os
import types
import typing
import unittest
from unittest.mock import AsyncMock, Mock

from pydantic import BaseModel, Field

import constants
from reliability_support import ROOT, load_functions


class State:
    def __init__(self, data=None):
        self.data = dict(data or {})
        self.current = None

    async def get_data(self):
        return self.data.copy()

    async def set_data(self, data):
        self.data = dict(data)

    async def update_data(self, **data):
        self.data.update(data)

    async def set_state(self, value):
        self.current = value


class QuizInputIsolationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        deps = {key: getattr(constants, key) for key in dir(constants) if key.isupper()}
        deps['CLASSIFY_CACHE_TTL_SECONDS'] = 604800
        source = ast.parse((ROOT / 'services/subject_classifier.py').read_text(encoding='utf-8'))
        for node in source.body:
            if isinstance(node, ast.Assign):
                for target in node.targets:
                    if isinstance(target, ast.Name) and target.id == '_CLASSIFY_CACHE_PREFIX':
                        deps[target.id] = ast.literal_eval(node.value)
        self.cache = {}
        async def get(key):
            return self.cache.get(key)
        async def put(key, value, **kwargs):
            self.cache[key] = value
        deps.update(BaseModel=BaseModel, Field=Field, Literal=typing.Literal,
                    List=typing.List, Optional=typing.Optional, logger=Mock(), log_warning=Mock(),
                    redis_client=types.SimpleNamespace(get=AsyncMock(side_effect=get), set=AsyncMock(side_effect=put)),
                    get_classification_lock=AsyncMock(return_value=None))
        self.classifier = load_functions('services/subject_classifier.py', {
            'SubjectClassification', '_get_cached_classification', '_store_cached_classification',
            'classify_subject', '_fallback',
        }, **deps)
        schema = self.classifier['SubjectClassification']
        schema.model_rebuild(_types_namespace=self.classifier)
        self.classifier.update(
            _classify_text=AsyncMock(return_value=schema(subject='english')),
            _classify_media=AsyncMock(return_value=schema(subject='math')),
        )
        deps.update(SubjectClassification=schema, classify_subject=self.classifier['classify_subject'],
                    QuizState=types.SimpleNamespace(waiting_for_cache_decision='cache',
                        waiting_for_translation_choice='translation'),
                    os=os, log_error=Mock(), spawn=lambda coroutine: coroutine.close(),
                    log_usage_event=AsyncMock(), get_file_quizzes=AsyncMock(return_value=[]),
                    is_feature_enabled=AsyncMock(return_value=False),
                    estimate_pages_from_text=lambda text: (1, len(text.split())),
                    extract_office_text_if_needed=AsyncMock(),
                    get_translation_choice_keyboard=Mock(), _show_quiz_options_screen=AsyncMock(),
                    _render_cache_decision_screen=AsyncMock(), safe_file_cleanup=Mock(),
                    offer_transition=AsyncMock(return_value=False), message_action=Mock())
        self.env = load_functions('handlers/files.py', {
            '_finalize_media_processing', 'ask_question_count', 'handle_pure_text',
        }, **deps)
        self.message = Mock(from_user=types.SimpleNamespace(id=101),
                            chat=types.SimpleNamespace(id=101))
        self.message.answer = AsyncMock(return_value=self.message)
        self.message.edit_text = AsyncMock()

    async def test_english_lesson_and_english_exam_both_show_translation_choice(self):
        schema = self.classifier['SubjectClassification']
        for subject in ('english', 'quiz_solved', 'quiz_unsolved'):
            with self.subTest(subject=subject):
                self.classifier['_classify_media'].return_value = schema(
                    subject=subject, content_subject='english', question_count=10,
                    suggested_types=['مفردات الدرس', 'فهم المقروء'])
                state = State()
                self.message.edit_text.reset_mock()
                await self.finalize(state, file_hash='english-' + subject)
                self.assertEqual(state.current, 'translation')
                self.assertEqual(self.message.edit_text.await_args.args[0],
                                 constants.MSG_ENGLISH_CONTENT_DETECTED)
                self.env['_show_quiz_options_screen'].assert_not_awaited()

    def completed(self):
        return State({'pure_text': 'Old English lesson about grammar and verbs.',
            'file_hash': 'previous-document', 'cached_office_text': 'Old office document',
            'questions': [{'question': 'old question'}], 'quiz_completed': True,
            'attempt_id': 'old-attempt', 'practice_result_token': 'old-practice',
            'subject_type': 'english', 'input_type': 'text', 'debited_cost': 9,
            'debit_id': 'old-debit', 'count_screen_message_id': 123,
            'available_quizzes': [{'id': 'old-quiz'}]})

    async def finalize(self, state, paths=None, file_hash='new-pdf', **kwargs):
        await self.env['_finalize_media_processing'](
            self.message, state, paths or ['new.pdf'], 'New content', 3,
            False, file_hash, content_source='pdf', **kwargs)
        self.env['log_error'].assert_not_called()

    async def test_completed_text_then_pdf_and_second_user_share_only_pdf_classification(self):
        first = self.completed()
        await self.finalize(first)
        second = State()
        await self.finalize(second, user_id=202, input_channel='web_upload')
        self.assertEqual([first.data['subject_type'], second.data['subject_type']], ['math', 'math'])
        self.classifier['_classify_media'].assert_awaited_once_with(['new.pdf'])
        self.classifier['_classify_text'].assert_not_awaited()
        for key in ('pure_text', 'cached_office_text', 'questions', 'quiz_completed',
                    'attempt_id', 'practice_result_token', 'debited_cost', 'debit_id',
                    'count_screen_message_id', 'available_quizzes'):
            self.assertNotIn(key, first.data)
        self.assertEqual(second.data['input_channel'], 'web_upload')
        self.assertEqual(self.classifier['_classify_media'].await_count, 1)

    async def test_new_pdf_ignores_old_cache_version(self):
        self.cache['subject_classify:v2:new-pdf'] = json.dumps({'subject': 'english'})
        await self.finalize(State())
        self.classifier['_classify_media'].assert_awaited_once()
        self.assertEqual(json.loads(self.cache['subject_classify:v2:new-pdf'])['subject'], 'english')

    async def test_dirty_media_state_cannot_send_retained_text_to_classifier(self):
        state = self.completed()
        state.data.update(input_type='media', file_paths=['new.pdf'], file_hash='new-pdf')
        await self.env['ask_question_count'](self.message, state, 'Choose count')
        self.assertEqual(state.data['subject_type'], 'math')
        self.classifier['_classify_text'].assert_not_awaited()

    async def test_classifier_prefers_explicit_file_over_stale_text(self):
        result = await self.classifier['classify_subject'](['new.pdf'], 'Old English lesson', 'new-pdf')
        self.assertEqual(result.subject, 'math')
        self.classifier['_classify_text'].assert_not_awaited()

    async def test_photo_after_text_uses_the_photo(self):
        state = self.completed()
        await self.finalize(state, paths=['new.jpg'], file_hash='new-image')
        self.classifier['_classify_media'].assert_awaited_once_with(['new.jpg'])
        self.classifier['_classify_text'].assert_not_awaited()
        self.assertEqual(state.data['subject_type'], 'math')

    async def test_empty_or_failed_input_does_not_poison_shared_cache(self):
        await self.classifier['classify_subject'](None, None, 'empty-file')
        self.assertFalse(self.cache)
        self.classifier['_classify_media'].side_effect = RuntimeError('Detection unavailable')
        await self.classifier['classify_subject'](['new.pdf'], None, 'new-pdf')
        self.assertFalse(self.cache)

    async def test_confirmed_community_classifications_are_preserved(self):
        self.classifier['get_classification_lock'].return_value = {
            'classification_data': {'subject': 'french'}}
        result = await self.classifier['classify_subject'](['new.pdf'], None, 'new-pdf')
        self.assertEqual(result.subject, 'french')
        self.classifier['_classify_media'].assert_not_awaited()
        self.assertFalse(self.cache)

    async def test_active_quiz_is_preserved_until_student_confirms_transition(self):
        state = self.completed()
        state.data['quiz_completed'] = False
        original = state.data.copy()
        self.env['offer_transition'].return_value = True
        self.message.text = 'An unrelated new lesson with enough text to create a quiz.'
        await self.env['handle_pure_text'](self.message, state)
        self.assertEqual(state.data, original)
        self.classifier['_classify_text'].assert_not_awaited()

    async def test_completed_media_then_text_drops_file_hash_and_old_result(self):
        state = self.completed()
        state.data.update(input_type='media', file_paths=['old.pdf'])
        self.message.text = 'New English lesson about grammar and verbs, unrelated to the PDF.'
        await self.env['handle_pure_text'](self.message, state)
        self.assertEqual(state.data['pure_text'], self.message.text)
        self.assertEqual(state.data['subject_type'], 'english')
        for key in ('file_paths', 'file_hash', 'cached_office_text', 'questions', 'debit_id'):
            self.assertNotIn(key, state.data)
        self.assertFalse(self.cache)

    async def test_cached_quiz_choice_also_starts_with_clean_input(self):
        state = self.completed()
        self.env['get_file_quizzes'].return_value = [{'id': 'matching-quiz'}]
        await self.finalize(state)
        self.assertEqual(state.current, 'cache')
        self.assertEqual(state.data['available_quizzes'], [{'id': 'matching-quiz'}])
        self.assertNotIn('pure_text', state.data)
        self.assertNotIn('cached_office_text', state.data)
        self.assertNotIn('debit_id', state.data)
        self.classifier['_classify_media'].assert_not_awaited()

    async def test_new_office_document_does_not_generate_from_previous_office_text(self):
        state = self.completed()
        self.env['extract_office_text_if_needed'].return_value = ('New document text', True)
        await self.finalize(state, paths=['new.docx'], file_hash='new-office')
        self.assertEqual(state.data['cached_office_text'], 'New document text')
        deps = {**self.env, '_HEAVY_PROCESSING_SEMAPHORE': asyncio.Semaphore(1),
                'generate_quiz_smart': AsyncMock(return_value=None),
                'resolve_generation_question_type_text': Mock(return_value=None),
                'resolve_quiz_extraction_instruction': Mock(return_value=None),
                'determine_execution_mode': lambda *args, **kw: 'Standard'}
        generation = load_functions('services/quiz_service.py', {'execute_quiz_generation_workflow'}, **deps)
        await generation['execute_quiz_generation_workflow'](101, state.data, 5, self.message)
        sent = deps['generate_quiz_smart'].await_args.kwargs
        self.assertEqual(sent['pure_text'], 'New document text')
        self.assertIsNone(sent['file_paths'])

    async def test_audio_transcript_quiz_cannot_inherit_previous_file_identity(self):
        state = self.completed()
        state.data.update(pure_text='Current lecture transcript with grammar examples.',
                          source_title='Lecture', input_channel='web_upload')
        env = load_functions('handlers/audio.py', {'handle_audio_to_quiz'}, **self.env)
        call = Mock(message=self.message, answer=AsyncMock())
        await env['handle_audio_to_quiz'](call, state)
        self.env['log_error'].assert_not_called()
        self.assertEqual(state.data['content_source'], 'audio_transcript')
        self.assertEqual(state.data['input_channel'], 'web_upload')
        self.assertEqual(state.data['pure_text'], 'Current lecture transcript with grammar examples.')
        self.assertNotIn('file_hash', state.data)
        self.assertNotIn('cached_office_text', state.data)
        self.assertFalse(self.cache)


if __name__ == '__main__':
    unittest.main()
