"""Extended/super PDF batches: recovery, source reuse and bounded retries."""
import asyncio
import hashlib
import json
import math
import sys
import types
import unittest
from unittest.mock import Mock, patch

from reliability_support import load_functions


class FakeRedis:
    def __init__(self):
        self.values = {}

    async def get(self, key):
        return self.values.get(key)

    async def setex(self, key, ttl, value):
        assert ttl <= 7200
        self.values[key] = value

    async def delete(self, key):
        self.values.pop(key, None)


class Value:
    def __init__(self):
        self.value = None

    def set(self, value):
        self.value = value


class Attachment:
    def __init__(self, path):
        self.path = path

    @classmethod
    def from_path(cls, path):
        return cls(path)


class Request:
    def __init__(self, parts, schema, system_instruction=None, file_session=None):
        self.parts = parts
        self.instruction = system_instruction
        self.file_session = file_session


class Session:
    def __init__(self):
        self.closed = False

    async def aclose(self):
        self.closed = True


def allocate(count, chunks):
    owned = [c['owned_last'] - c['owned_first'] + 1 for c in chunks]
    targets = [(count * n) // sum(owned) for n in owned]
    for i in range(count - sum(targets)):
        targets[i] += 1
    return targets


def questions(start, end):
    return [dict(question=f'Question {n}', options=['a', 'b', 'c', 'd'],
                 correct_option_id=0) for n in range(start, end)]


def result(start, end):
    return (questions(start, end), 120, 50, 70, 0, 'test-model', 0, 1, 2, 'gemini')


class BatchTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.redis = FakeRedis()
        self.models = Value()
        self.usage = Value()
        self.calls = []
        self.env = load_functions(
            'helpers/gemini_helper/batch_generation.py',
            {'_question_key', '_valid_question', '_valid_extracted_question',
             '_batch_prompt', '_plain_batch_prompt', '_extraction_prompt',
             '_Checkpoint', '_run_batch_chunks', '_run_extraction_chunks',
             '_ordered_image_groups', 'generate_pdf_in_batches',
             'generate_images_in_batches', 'generate_text_in_batches'},
            hashlib=hashlib, json=json, math=math, asyncio=asyncio, logger=Mock(),
            release_memory_to_os=Mock(),
            _last_model_used_var=self.models, _last_token_usage_var=self.usage,
            GeminiFileSession=Session, Attachment=Attachment, AIRequest=Request,
            QuizResponse=object, MAX_BATCH_QUESTIONS=20, CHECKPOINT_TTL_SECONDS=7200,
            _generated_pdf_chunk_targets=allocate,
            _generated_pdf_chunk_prompt=lambda base, n, chunk, difficulty=None:
            f'{base}:generate={n}:owned={chunk["owned_first"]}:{chunk["owned_last"]}',
            _ready_exam_chunk_prompt=lambda base, n, chunk:
            f'{base}:extract={n}:owned={chunk["owned_first"]}:{chunk["owned_last"]}',
            split_pdf_with_context_without_orphans=None,
            split_images_into_three_sync=lambda paths:
            [paths[:len(paths)//3], paths[len(paths)//3:2*len(paths)//3], paths[2*len(paths)//3:]],
            safe_file_cleanup=Mock(),
            _execute_cascade=None,
        )
        self.config_patch = patch.dict(
            sys.modules, {'config': types.SimpleNamespace(redis_client=self.redis)})
        self.config_patch.start()
        self.addCleanup(self.config_patch.stop)

    async def test_short_pdf_not_split_and_file_session_reused(self):
        async def cascade(req, **kw):
            self.calls.append(req)
            start = (len(self.calls) - 1) * 20 + 1
            return result(start, start + 20)
        self.env['_execute_cascade'] = cascade
        generated = await self.env['generate_pdf_in_batches'](
            'source.pdf', 4, 60, 'prompt', 1, user_id=9, file_hash='hash')
        self.assertEqual(len(generated[0]), 60)
        self.assertEqual([req.parts[0].path for req in self.calls], ['source.pdf'] * 3)
        self.assertEqual(len({id(req.file_session) for req in self.calls}), 1)
        self.assertEqual(self.redis.values, {})
        self.assertEqual(self.usage.value['total_tokens'], 360)

    async def test_resume_only_unfinished_batch_after_cascade_failure(self):
        async def fail_last(req, **kw):
            self.calls.append(req)
            if len(self.calls) == 3:
                return None
            start = (len(self.calls) - 1) * 20 + 1
            return result(start, start + 20)
        self.env['_execute_cascade'] = fail_last
        generate = self.env['generate_pdf_in_batches']
        self.assertIsNone(await generate(
            'source.pdf', 4, 60, 'prompt', 1, user_id=9, file_hash='hash'))
        self.assertEqual(len(json.loads(
            next(iter(self.redis.values.values())))['parts'][0]), 40)
        resumed = []

        async def finish(req, **kw):
            resumed.append(req)
            self.assertIn('Question 40', req.instruction)
            return result(41, 61)
        self.env['_execute_cascade'] = finish
        generated = await generate(
            'source.pdf', 4, 60, 'prompt', 1, user_id=9, file_hash='hash')
        self.assertEqual(len(generated[0]), 60)
        self.assertEqual(len(resumed), 1)
        self.assertFalse(self.redis.values)

    async def test_duplicate_only_batches_stop_without_losing_checkpoint(self):
        async def duplicate(req, **kw):
            self.calls.append(req)
            return result(1, 21)
        self.env['_execute_cascade'] = duplicate
        generated = await self.env['generate_pdf_in_batches'](
            'source.pdf', 4, 40, 'prompt', 1, user_id=9, file_hash='hash')
        self.assertIsNone(generated)
        self.assertEqual(len(self.calls), 3)
        self.assertEqual(len(json.loads(
            next(iter(self.redis.values.values())))['parts'][0]), 20)

    async def test_large_pdf_retains_page_ownership(self):
        async def split(path, parts):
            return [dict(path='first.pdf', owned_first=1, owned_last=10, page_count=11),
                    dict(path='second.pdf', owned_first=2, owned_last=11, page_count=12)]
        async def cascade(req, **kw):
            self.calls.append(req)
            start = (len(self.calls) - 1) * 20 + 1
            return result(start, start + 20)
        self.env['_execute_cascade'] = cascade
        self.env['split_pdf_with_context_without_orphans'] = split
        generated = await self.env['generate_pdf_in_batches'](
            'source.pdf', 20, 40, 'prompt', 2)
        self.assertEqual(len(generated[0]), 40)
        self.assertEqual([req.parts[0].path for req in self.calls],
                         ['first.pdf', 'second.pdf'])


    async def test_medium_album_generates_more_than_thirty_questions(self):
        async def cascade(req, **kw):
            self.calls.append(req)
            start = (len(self.calls) - 1) * 20 + 1
            return result(start, start + 20)
        self.env['_execute_cascade'] = cascade
        paths = [f'photo_{i}.jpg' for i in range(8)]
        generated = await self.env['generate_images_in_batches'](
            paths, 45, 'prompt {count}', user_id=9, file_hash='album-hash')
        self.assertEqual(len(generated[0]), 45)
        self.assertEqual(len(self.calls), 3)
        self.assertEqual([part.path for part in self.calls[0].parts], paths)
        self.assertTrue(all(len(req.parts) == 8 for req in self.calls))
        self.assertIn('Question 20', self.calls[1].instruction)

    async def test_super_album_keeps_three_separate_groups(self):
        async def cascade(req, **kw):
            self.calls.append(req)
            start = (len(self.calls) - 1) * 20 + 1
            return result(start, start + 20)
        self.env['_execute_cascade'] = cascade
        paths = [f'photo_{i}.jpg' for i in range(14)]
        generated = await self.env['generate_images_in_batches'](
            paths, 60, 'prompt {count}', user_id=9, file_hash='large-album')
        self.assertEqual(len(generated[0]), 60)
        self.assertEqual(len(self.calls), 5)
        self.assertEqual(len({req.parts[0].path for req in self.calls}), 3)
        self.assertEqual(set(part.path for req in self.calls for part in req.parts),
                         set(paths))

    async def test_long_text_uses_same_unmodified_source_on_each_batch(self):
        async def cascade(req, **kw):
            self.calls.append(req)
            start = (len(self.calls) - 1) * 20 + 1
            return result(start, start + 20)
        self.env['_execute_cascade'] = cascade
        source = 'Long Office or lecture transcript with lots of useful source words'
        generated = await self.env['generate_text_in_batches'](
            source, 45, 'prompt {count}', user_id=9)
        self.assertEqual(len(generated[0]), 45)
        self.assertEqual(len(self.calls), 3)
        self.assertTrue(all(req.parts == (source,) for req in self.calls))
        self.assertFalse(self.redis.values)

    async def test_text_resume_uses_only_remaining_questions(self):
        async def cascade(req, **kw):
            self.calls.append(req)
            if len(self.calls) == 2:
                return None
            return result(1, 21)
        self.env['_execute_cascade'] = cascade
        generate = self.env['generate_text_in_batches']
        self.assertIsNone(await generate('transcript', 40, 'prompt {count}', user_id=9))
        self.assertEqual(len(self.redis.values), 1)
        resumed = []
        async def finish(req, **kw):
            resumed.append(req)
            return result(21, 41)
        self.env['_execute_cascade'] = finish
        finished = await generate('transcript', 40, 'prompt {count}', user_id=9)
        self.assertEqual(len(finished[0]), 40)
        self.assertEqual(len(resumed), 1)
        self.assertEqual(self.redis.values, {})



    async def test_ready_exam_short_pdf_extracts_only_available_questions(self):
        async def cascade(req, **kw):
            self.calls.append(req)
            self.assertTrue(kw.get('allow_empty'))
            if len(self.calls) == 1:
                return result(1, 21)
            if len(self.calls) == 2:
                self.assertIn('Question 20', req.instruction)
                return result(21, 41)
            self.assertIn('Question 40', req.instruction)
            return result(41, 44)  # Only three original questions remain.
        self.env['_execute_cascade'] = cascade
        extracted = await self.env['generate_pdf_in_batches'](
            'original.pdf', 2, 60, 'Extract {count}', 1,
            user_id=9, file_hash='original', extraction=True,
        )
        self.assertEqual(len(extracted[0]), 43)
        self.assertEqual([q['question'] for q in extracted[0]],
                         [f'Question {i}' for i in range(1, 44)])
        self.assertEqual(len(self.calls), 3)
        self.assertEqual({req.parts[0].path for req in self.calls}, {'original.pdf'})
        self.assertFalse(self.redis.values)
        self.assertEqual(self.models.value['mode'], 'batched_exam_pdf')

    async def test_ready_exam_uses_full_source_and_can_report_empty_end(self):
        async def cascade(req, **kw):
            self.calls.append(req)
            if len(self.calls) == 1:
                return result(1, 21)
            self.assertIn('Question 20', req.instruction)
            return ([], 25, 10, 15, 0, 'test-model', 0, 1, 2, 'gemini')
        self.env['_execute_cascade'] = cascade
        extracted = await self.env['generate_text_in_batches'](
            'Source exam with 20 questions', 40, 'Extract {count}',
            user_id=9, extraction=True,
        )
        self.assertEqual(len(extracted[0]), 20)
        self.assertEqual(len(self.calls), 2)
        self.assertTrue(all(req.parts == ('Source exam with 20 questions',) for req in self.calls))

    async def test_ready_exam_keeps_five_options_for_existing_normalization(self):
        async def cascade(req, **kw):
            q = dict(question='Original Q1', options=['a', 'b', 'c', 'd', 'e'],
                     correct_option_id=4)
            return ([q], 33, 10, 23, 0, 'test-model', 0, 1, 2, 'gemini')
        self.env['_execute_cascade'] = cascade
        extracted = await self.env['generate_text_in_batches'](
            'Original source', 40, 'Extract {count}',
            user_id=9, extraction=True,
        )
        self.assertEqual(extracted[0][0]['options'], ['a', 'b', 'c', 'd', 'e'])
        self.assertEqual(extracted[0][0]['correct_option_id'], 4)

    async def test_ready_exam_failed_batch_resumes_using_last_question(self):
        async def fail(req, **kw):
            self.calls.append(req)
            return result(1, 21) if len(self.calls) == 1 else None
        self.env['_execute_cascade'] = fail
        fn = self.env['generate_text_in_batches']
        self.assertIsNone(await fn(
            'Same exam', 60, 'Extract {count}', user_id=9, extraction=True))
        state = json.loads(next(iter(self.redis.values.values())))
        self.assertEqual(len(state['parts'][0]), 20)
        self.assertEqual(state['completed_parts'], [False])
        resumed = []
        async def succeed(req, **kw):
            resumed.append(req)
            self.assertIn('Question 20', req.instruction)
            return result(21, 24)
        self.env['_execute_cascade'] = succeed
        outcome = await fn('Same exam', 60, 'Extract {count}', user_id=9, extraction=True)
        self.assertEqual(len(outcome[0]), 23)
        self.assertEqual(len(resumed), 1)
        self.assertFalse(self.redis.values)

    async def test_ready_exam_contiguous_image_groups_keep_source_order(self):
        async def cascade(req, **kw):
            self.calls.append(req)
            index = len(self.calls)
            return result((index - 1) * 3 + 1, index * 3 + 1)
        self.env['_execute_cascade'] = cascade
        paths = [f'photo_{i}.jpg' for i in range(12)]
        outcome = await self.env['generate_images_in_batches'](
            paths, 45, 'Extract {count}', file_hash='exam', user_id=9,
            extraction=True,
        )
        self.assertEqual(len(outcome[0]), 9)
        self.assertEqual(
            [[p.path for p in req.parts] for req in self.calls],
            [paths[:4], paths[4:8], paths[8:]],
        )

    async def test_ready_exam_five_option_checkpoint_remains_valid_after_failure(self):
        calls = []
        async def first(req, **kw):
            calls.append(req)
            if len(calls) > 1:
                return None
            five = dict(question='Original Q1', options=['a','b','c','d','e'],
                        correct_option_id=4)
            extra = questions(2, 21)
            return ([five] + extra, 60, 20, 40, 0, 'test-model', 0, 1, 2, 'gemini')
        self.env['_execute_cascade'] = first
        self.assertIsNone(await self.env['generate_text_in_batches'](
            'An exam', 40, 'Extract {count}', user_id=9, extraction=True))
        self.assertEqual(len(self.redis.values), 1)
        async def last(req, **kw):
            return result(21, 23)
        self.env['_execute_cascade'] = last
        outcome = await self.env['generate_text_in_batches'](
            'An exam', 40, 'Extract {count}', user_id=9, extraction=True)
        self.assertEqual(len(outcome[0]), 22)
        self.assertEqual(outcome[0][0]['options'][-1], 'e')

    async def test_ready_exam_duplicate_only_full_batch_fails_without_faking_end(self):
        async def cascade(req, **kw):
            self.calls.append(req)
            return result(1, 21)
        self.env['_execute_cascade'] = cascade
        extracted = await self.env['generate_text_in_batches'](
            'Same source', 40, 'Extract {count}', user_id=9, extraction=True)
        self.assertIsNone(extracted)
        self.assertGreaterEqual(len(self.calls), 3)
        self.assertEqual(len(json.loads(
            next(iter(self.redis.values.values())))['parts'][0]), 20)

    async def test_ready_exam_checkpoint_is_separate_from_question_generation(self):
        async def cascade(req, **kw):
            if 'دفعة استخراج' in req.instruction:
                return result(1, 4)
            return result(1, 21)
        self.env['_execute_cascade'] = cascade
        self.assertIsNotNone(await self.env['generate_text_in_batches'](
            'A test', 40, 'Extract {count}', user_id=9, extraction=True))
        self.assertIsNotNone(await self.env['generate_text_in_batches'](
            'A test', 20, 'Extract {count}', user_id=9))


if __name__ == '__main__':
    unittest.main()
