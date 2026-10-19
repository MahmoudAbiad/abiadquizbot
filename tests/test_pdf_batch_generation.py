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
            {'_question_key', '_valid_question', '_batch_prompt', '_plain_batch_prompt',
             '_Checkpoint', '_run_batch_chunks', 'generate_pdf_in_batches',
             'generate_images_in_batches', 'generate_text_in_batches'},
            hashlib=hashlib, json=json, math=math, asyncio=asyncio, logger=Mock(),
            release_memory_to_os=Mock(),
            _last_model_used_var=self.models, _last_token_usage_var=self.usage,
            GeminiFileSession=Session, Attachment=Attachment, AIRequest=Request,
            QuizResponse=object, MAX_BATCH_QUESTIONS=20, CHECKPOINT_TTL_SECONDS=7200,
            _generated_pdf_chunk_targets=allocate,
            _generated_pdf_chunk_prompt=lambda base, n, chunk, difficulty=None:
            f'{base}:generate={n}:owned={chunk["owned_first"]}:{chunk["owned_last"]}',
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



if __name__ == '__main__':
    unittest.main()
