"""Context-overlap tests for ready-made (solved/unsolved) exam PDF extraction.

Production functions are isolated with load_functions so these deterministic
tests need neither the AI SDK nor a real API key.
"""
import asyncio
import os
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest.mock import Mock

from reliability_support import load_functions


class ReadyExamPdfContextTests(unittest.IsolatedAsyncioTestCase):
    def split_env(self):
        import fitz
        env = load_functions(
            'helpers/gemini_helper/media.py',
            {'split_ready_exam_pdf_with_context_sync',
             'split_ready_exam_pdf_without_orphans'},
            asyncio=asyncio, Path=Path, os=os, uuid=uuid, fitz=fitz,
            get_pdf_page_count_sync=lambda path: self._count_pages(fitz, path),
        )
        return fitz, env

    @staticmethod
    def _count_pages(fitz, path):
        with fitz.open(path) as doc:
            return len(doc)

    def create_pdf(self, fitz, folder, pages):
        path = os.path.join(folder, 'exam.pdf')
        with fitz.open() as doc:
            for number in range(1, pages + 1):
                page = doc.new_page()
                page.insert_text((50, 50), f'ORIGINAL PAGE {number}')
            doc.save(path)
        return path

    async def test_over_limit_two_parts_share_only_context_pages(self):
        fitz, env = self.split_env()
        with tempfile.TemporaryDirectory() as folder:
            source = self.create_pdf(fitz, folder, 18)
            chunks = env['split_ready_exam_pdf_with_context_sync'](source, 2)
            self.assertEqual(len(chunks), 2)
            self.assertEqual(
                [(c['original_start'], c['original_end']) for c in chunks],
                [(1, 9), (10, 18)]
            )
            self.assertEqual(
                [(c['owned_first'], c['owned_last']) for c in chunks],
                [(1, 9), (2, 10)]
            )
            self.assertEqual(
                [(c['leading_context'], c['trailing_context']) for c in chunks],
                [(0, 1), (1, 0)]
            )
            self.assertEqual([c['page_count'] for c in chunks], [10, 10])
            with fitz.open(chunks[0]['path']) as first, fitz.open(chunks[1]['path']) as second:
                self.assertIn('ORIGINAL PAGE 10', first[-1].get_text())
                self.assertIn('ORIGINAL PAGE 9', second[0].get_text())
            for chunk in chunks:
                os.remove(chunk['path'])
            self.assertEqual(os.listdir(folder), ['exam.pdf'])

    async def test_super_pdf_three_parts_context_both_sides(self):
        fitz, env = self.split_env()
        with tempfile.TemporaryDirectory() as folder:
            source = self.create_pdf(fitz, folder, 39)
            chunks = env['split_ready_exam_pdf_with_context_sync'](source, 3)
            self.assertEqual(
                [(c['original_start'], c['original_end']) for c in chunks],
                [(1, 13), (14, 26), (27, 39)]
            )
            self.assertEqual(
                [(c['leading_context'], c['trailing_context']) for c in chunks],
                [(0, 1), (1, 1), (1, 0)]
            )
            self.assertEqual([c['page_count'] for c in chunks], [14, 15, 14])
            self.assertEqual([c['owned_first'] for c in chunks], [1, 2, 2])
            self.assertEqual([c['owned_last'] for c in chunks], [13, 14, 14])
            for chunk in chunks:
                os.remove(chunk['path'])
            self.assertEqual(os.listdir(folder), ['exam.pdf'])

    async def test_error_cleans_partial_context_chunks(self):
        fitz, env = self.split_env()
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as folder:
            source = self.create_pdf(fitz, folder, 18)
            real_open = fitz.open
            calls = 0

            def fail_on_second_chunk(*args, **kwargs):
                nonlocal calls
                calls += 1
                if calls == 4:
                    raise ValueError('forced split failure')
                return real_open(*args, **kwargs)

            with patch.object(fitz, 'open', side_effect=fail_on_second_chunk):
                with self.assertRaises(ValueError):
                    env['split_ready_exam_pdf_with_context_sync'](source, 2)
            self.assertEqual(os.listdir(folder), ['exam.pdf'])

    async def test_cancelled_context_split_cleans_temporary_files(self):
        import threading

        started = threading.Event()
        finish = threading.Event()
        with tempfile.TemporaryDirectory() as folder:
            source = os.path.join(folder, 'source.pdf')
            Path(source).write_bytes(b'source')

            def fake_split(path, part_count):
                started.set()
                finish.wait(timeout=5)
                part_path = os.path.join(folder, 'part.pdf')
                Path(part_path).write_bytes(b'chunk')
                return [{'path': part_path}]

            env = load_functions(
                'helpers/gemini_helper/media.py',
                {'split_ready_exam_pdf_without_orphans'},
                asyncio=asyncio, os=os,
                split_ready_exam_pdf_with_context_sync=fake_split,
            )
            task = asyncio.create_task(
                env['split_ready_exam_pdf_without_orphans'](source, 2)
            )
            await asyncio.to_thread(started.wait, 5)
            task.cancel()
            finish.set()
            with self.assertRaises(asyncio.CancelledError):
                await task
            self.assertEqual(os.listdir(folder), ['source.pdf'])

    async def test_context_instruction_and_cross_chunk_duplicate_handling(self):
        env = load_functions(
            'helpers/gemini_helper/generation.py',
            {'_ready_exam_chunk_prompt', '_merge_ready_exam_chunk_questions'},
        )
        prompt = env['_ready_exam_chunk_prompt'](
            'Extract up to {count} questions.',
            40,
            {'owned_first': 2, 'owned_last': 10, 'page_count': 11},
        )
        self.assertIn('Extract up to 40 questions.', prompt)
        self.assertIn('من 2 إلى 10', prompt)
        self.assertIn('الصفحات خارج هذا النطاق صفحات سياق فقط', prompt)
        merged = env['_merge_ready_exam_chunk_questions'](
            [
                ([{'question': 'Q1'}, {'question': 'Boundary Q'}],),
                ([{'question': '  boundary   q '}, {'question': 'Q3'}],),
            ],
            40,
        )
        self.assertEqual([q['question'] for q in merged], ['Q1', 'Boundary Q', 'Q3'])

    async def test_chunk_generator_uses_two_or_three_requests_and_cleans_up(self):
        recorded_prompts = []
        cleaned = []
        model_var = Mock()
        tokens_var = Mock()
        chunks = [
            {'path': 'temp-part-1.pdf', 'owned_first': 1, 'owned_last': 10,
             'page_count': 11, 'leading_context': 0, 'trailing_context': 1},
            {'path': 'temp-part-2.pdf', 'owned_first': 2, 'owned_last': 11,
             'page_count': 12, 'leading_context': 1, 'trailing_context': 1},
            {'path': 'temp-part-3.pdf', 'owned_first': 2, 'owned_last': 10,
             'page_count': 10, 'leading_context': 1, 'trailing_context': 0},
        ]
        async def split(path, count):
            return chunks[:count]

        def build(paths, prompt):
            recorded_prompts.append(prompt)
            return paths

        excludes = []
        async def cascade(paths, exclude_vertex):
            excludes.append(exclude_vertex)
            number = len(excludes)
            return (
                [{'question': f'Q{number}', 'options': ['a', 'b', 'c', 'd'],
                  'correct_option_id': 0}],
                10, 3, 5, 2, 'mock-model', 0, 1, 1,
            )
        env = load_functions(
            'helpers/gemini_helper/generation.py',
            {'_generate_ready_exam_pdf', '_ready_exam_chunk_prompt',
             '_merge_ready_exam_chunk_questions'},
            asyncio=asyncio, API_KEYS=['key1', 'key2', 'key3'],
            split_ready_exam_pdf_without_orphans=split,
            release_memory_to_os=lambda: None,
            _generate_regular=Mock(), _execute_cascade=cascade,
            _build_super_chunk_attempt_fn=build,
            _last_model_used_var=model_var, _last_token_usage_var=tokens_var,
            log_error=Mock(), logger=Mock(),
            safe_file_cleanup=lambda path: cleaned.append(path),
        )
        two = await env['_generate_ready_exam_pdf'](
            'exam.pdf', 40, 'Extract {count}.', 2
        )
        self.assertEqual(len(two[0]), 2)
        self.assertEqual(excludes, [False, False])
        self.assertEqual(cleaned, ['temp-part-1.pdf', 'temp-part-2.pdf'])
        self.assertEqual(len(recorded_prompts), 2)
        recorded_prompts.clear()
        cleaned.clear()
        excludes.clear()

        three = await env['_generate_ready_exam_pdf'](
            'exam.pdf', 40, 'Extract {count}.', 3
        )
        self.assertEqual(len(three[0]), 3)
        self.assertEqual(excludes, [True, True, True])
        self.assertEqual(cleaned, [
            'temp-part-1.pdf', 'temp-part-2.pdf', 'temp-part-3.pdf'
        ])
        self.assertEqual(len(recorded_prompts), 3)
