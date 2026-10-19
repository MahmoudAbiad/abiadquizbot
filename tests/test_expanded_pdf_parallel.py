"""Expanded PDFs: two concurrent inline requests and independent fallback."""
import asyncio
import unittest
from collections import Counter
from unittest.mock import AsyncMock, patch

from helpers.gemini_helper import batch_generation as batches
from helpers.gemini_helper.quiz_generator import _expanded_pdf_part_count


def question(number):
    return {
        "question": f"Question {number}?",
        "options": ["A", "B", "C", "D"],
        "correct_option_id": 0,
        "explanation": "",
    }


def completed_result(questions, *, model="fast", provider="gemini"):
    return (questions, 123, 80, 30, 13, model, 0, 1, 2, provider)


def page_chunks():
    return [
        {"paths": ["first.pdf"], "weight": 1,
         "pdf_chunk": {"owned_first": 1, "owned_last": 1, "page_count": 2}},
        {"paths": ["second.pdf"], "weight": 2,
         "pdf_chunk": {"owned_first": 2, "owned_last": 3, "page_count": 3}},
    ]


class ExpandedPartCountTests(unittest.TestCase):
    def test_question_heavy_three_page_exam_uses_two_parts(self):
        self.assertEqual(_expanded_pdf_part_count(3, 55), 2)

    def test_one_page_stays_single_and_super_stays_three(self):
        self.assertEqual(_expanded_pdf_part_count(1, 55), 1)
        self.assertEqual(_expanded_pdf_part_count(36, 55), 3)


class ExpandedParallelTests(unittest.IsolatedAsyncioTestCase):
    async def test_generation_starts_both_parts_together_on_same_model_and_key(self):
        arrived = asyncio.Event()
        requests = []

        async def cascade(request, **kwargs):
            requests.append(request)
            if len(requests) == 2:
                arrived.set()
            await asyncio.wait_for(arrived.wait(), timeout=1)
            if request.contents[-1].path == "first.pdf":
                return completed_result([question(1)])
            return completed_result([question(2), question(3)])

        with (patch.object(batches, "_execute_cascade", new=cascade),
              patch("services.ai.runtime.choose_parallel_preference",
                    new=AsyncMock(return_value=("gemini", "fast", 1)))):
            response = await batches._run_expanded_pdf_parallel(
                page_chunks(), 3, "Generate {count}", False, None,
                None, None, False,
            )

        self.assertEqual([q["question"] for q in response[0]],
                         ["Question 1?", "Question 2?", "Question 3?"])
        self.assertEqual(len(requests), 2)
        self.assertTrue(all(req.preferred_attempt == ("gemini", "fast", 1) for req in requests))
        self.assertTrue(all(req.inline_only and req.file_session is None for req in requests))

    async def test_only_failed_part_enters_small_continuation(self):
        calls = Counter()
        requests = []

        async def cascade(request, **kwargs):
            path = request.contents[-1].path
            calls[path] += 1
            requests.append(request)
            if path == "first.pdf":
                return completed_result([question(1)])
            if calls[path] == 1:
                return None  # The entire cascade exhausted on the large part.
            return completed_result([question(2), question(3)])

        with (patch.object(batches, "_execute_cascade", new=cascade),
              patch("services.ai.runtime.choose_parallel_preference",
                    new=AsyncMock(return_value=("gemini", "fast", 0)))):
            response = await batches._run_expanded_pdf_parallel(
                page_chunks(), 3, "Generate {count}", False, None,
                None, None, False,
            )
        self.assertEqual(len(response[0]), 3)
        self.assertEqual(calls, {"first.pdf": 1, "second.pdf": 2})
        self.assertEqual(requests[1].preferred_attempt, ("gemini", "fast", 0))
        self.assertIsNone(requests[2].preferred_attempt)
        self.assertTrue(all(req.inline_only for req in requests))

    async def test_ready_exam_two_parts_do_not_need_sequential_anchors(self):
        seen = []

        async def cascade(request, **kwargs):
            seen.append(request)
            if request.contents[-1].path == "first.pdf":
                return completed_result([question(1)])
            return completed_result([question(2), question(3), question(4)])

        with (patch.object(batches, "_execute_cascade", new=cascade),
              patch("services.ai.runtime.choose_parallel_preference",
                    new=AsyncMock(return_value=("gemini", "fast", 0)))):
            response = await batches._run_expanded_pdf_parallel(
                page_chunks(), 4, "Extract {count}", False, None,
                None, None, True,
            )
        self.assertEqual(len(seen), 2)
        self.assertEqual([q["question"] for q in response[0]],
                         [f"Question {i}?" for i in range(1, 5)])
        self.assertTrue(all("صفحات السياق" in req.system_instruction for req in seen))

    async def test_exam_does_not_return_partial_when_part_never_reaches_eof(self):
        calls = Counter()

        async def cascade(request, **kwargs):
            path = request.contents[-1].path
            calls[path] += 1
            if path == "first.pdf":
                return completed_result([question(1)]) if calls[path] == 1 else None
            return completed_result([question(2)]) if calls[path] == 1 else None

        with (patch.object(batches, "_execute_cascade", new=cascade),
              patch("services.ai.runtime.choose_parallel_preference",
                    new=AsyncMock(return_value=("gemini", "fast", 0)))):
            result = await batches._run_expanded_pdf_parallel(
                page_chunks(), 4, "Extract {count}", False, None,
                None, None, True,
            )
        self.assertIsNone(result)


if __name__ == "__main__":
    unittest.main()
