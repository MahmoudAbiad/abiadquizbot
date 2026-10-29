"""Deadline, fallback, cancellation and refund behavior without vendor calls."""
import asyncio
from collections import Counter
import time
import types
import unittest
from unittest.mock import AsyncMock, Mock, patch

import constants as C
from reliability_support import load_functions
from services.ai.contracts import AIRequest
from services.ai import time_budget as tb
from services.ai.state import _last_model_used_var
import test_generation_regressions as regressions


class QuizBudgetTests(unittest.IsolatedAsyncioTestCase):
    def test_limits_scale_with_pages_and_output_and_have_caps(self):
        for pages, questions, expected in [
            (1, 10, (50, 150)), (5, 10, (90, 270)),
            (15, 10, (190, 570)), (1, 30, (90, 270)),
            (150, 120, (300, 600)),
        ]:
            with self.subTest(pages=pages, questions=questions):
                self.assertEqual(tb.quiz_timeout_limits(pages, questions), expected)

    async def test_parallel_and_nested_work_share_deadline_and_metadata_stays_in_task(self):
        self.assertIsNone(tb.current_quiz_budget())
        async with tb.quiz_ai_budget(1, 10) as parent:
            async def child():
                async with tb.quiz_ai_budget(150, 120) as nested:
                    return nested
            children = await asyncio.gather(child(), child())
            self.assertTrue(all(child is parent for child in children))
            _last_model_used_var.set({'model': 'budget-test'})
        self.assertEqual(_last_model_used_var.get()['model'], 'budget-test')
        self.assertIsNone(tb.current_quiz_budget())

    async def test_expiry_cancels_parallel_work_and_cleans_context(self):
        cancelled = []
        async def child():
            try:
                await asyncio.sleep(10)
            finally:
                cancelled.append(True)
        with patch.object(tb, 'quiz_timeout_limits', return_value=(0.01, 0.03)):
            with self.assertRaises(tb.QuizAITimeout):
                async with tb.quiz_ai_budget(1, 10):
                    await asyncio.gather(child(), child())
        self.assertEqual(len(cancelled), 2)
        self.assertIsNone(tb.current_quiz_budget())

    async def test_external_cancellation_is_not_converted_to_timeout(self):
        async def task():
            async with tb.quiz_ai_budget(1, 10):
                await asyncio.sleep(10)
        job = asyncio.create_task(task())
        await asyncio.sleep(0)
        job.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await job
        self.assertIsNone(tb.current_quiz_budget())

    async def test_remote_cleanup_has_one_allowance_for_all_files(self):
        async with tb.quiz_ai_budget(1,10):
            started=time.monotonic()
            async with tb.quiz_cleanup_deadline(0.02):
                for _ in range(50):
                    await asyncio.sleep(10)
            self.assertLess(time.monotonic()-started,1)


class RuntimeBudgetTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.calls = []
        self.adapter = types.SimpleNamespace(capabilities={'text'}, generate=AsyncMock())
        self.settings = types.SimpleNamespace(
            get_ai_thinking_level=AsyncMock(return_value=None),
            get_ai_request_timeout=AsyncMock(return_value=300),
        )
        self.provider = dict(id='test', transport='test', base_url='', is_enabled=True)
        self.result = types.SimpleNamespace(usage={}, input_tokens=0, output_tokens=0,
                                           total_tokens=0, truncated=False)
        async def run_attempt(coro, *, timeout, **kwargs):
            self.calls.append(timeout)
            return await asyncio.wait_for(coro, timeout)
        env = load_functions('services/ai/runtime.py', {'execute'},
            asyncio=asyncio, time=time, uuid=__import__('uuid'), Counter=Counter,
            get_providers=AsyncMock(return_value={'test':self.provider}),
            get_enabled_models=AsyncMock(return_value=[
                dict(provider='test', model_name='first', capabilities=['text']),
                dict(provider='test', model_name='second', capabilities=['text']),
            ]), ADAPTERS={'test':self.adapter}, logger=Mock(),
            credentials_for=lambda p:['a','b'],
            _attempt_identity=lambda p,k,m:(p['id'],k,m),
            _free_pool_identity=lambda *a:None,
            _blocked={}, _shared_blocked={}, _pointers={},
            chat_service=lambda url:'generic', run_attempt=run_attempt,
            ProviderError=type('ProviderError',(Exception,),{}),
            AIResponseDiagnosticError=type('DiagnosticError',(Exception,),{}),
            RateLimitInfo=lambda *a:None,
            decide_cooldown=lambda *a,**kw:types.SimpleNamespace(
                seconds=0,scope='model_credential',basis='test'))
        self.execute = env['execute']
        self.adapter.generate.return_value = self.result
        self.patch = patch.dict('sys.modules', {'helpers.settings_helper':self.settings})
        self.patch.start()
        self.addCleanup(self.patch.stop)

    async def test_small_quiz_caps_300_second_setting_at_50(self):
        async with tb.quiz_ai_budget(1,10):
            self.assertIs(await self.execute(AIRequest(('source',))), self.result)
        self.assertEqual(self.calls, [50])

    async def test_lower_configured_timeout_and_explicit_override_are_respected(self):
        self.settings.get_ai_request_timeout.return_value = 30
        async with tb.quiz_ai_budget(1,10):
            await self.execute(AIRequest(('source',)))
            await self.execute(AIRequest(('source',)), timeout=12)
        self.assertEqual(self.calls, [30,12])

    async def test_other_ai_tasks_keep_existing_timeout(self):
        await self.execute(AIRequest(('audio or other task',)))
        self.assertEqual(self.calls,[300])

    async def test_timeout_moves_to_next_model_instead_of_burning_all_keys(self):
        async def generate(provider,key,model,request):
            if model == 'first':
                raise TimeoutError()
            return self.result
        self.adapter.generate.side_effect = generate
        async with tb.quiz_ai_budget(1,10):
            self.assertIs(await self.execute(AIRequest(('source',))), self.result)
        self.assertEqual([c.args[2] for c in self.adapter.generate.await_args_list],
                         ['first','second'])

    async def test_attempt_is_shortened_to_remaining_shared_deadline(self):
        budget = types.SimpleNamespace(attempt_seconds=50, remaining=Mock(side_effect=[2,1]))
        token = tb._current_budget.set(budget)
        try:
            await self.execute(AIRequest(('source',)))
        finally:
            tb._current_budget.reset(token)
        self.assertEqual(self.calls,[1])

    async def test_expired_budget_never_calls_provider(self):
        token = tb._current_budget.set(tb.QuizTimeBudget(50,150,time.monotonic()-1))
        try:
            with self.assertRaises(tb.QuizAITimeout):
                await self.execute(AIRequest(('source',)))
        finally:
            tb._current_budget.reset(token)
        self.adapter.generate.assert_not_awaited()


class TimeoutRefundTests(unittest.IsolatedAsyncioTestCase):
    async def test_deadline_refunds_once_retains_upload_and_restores_review(self):
        fixture = regressions.DebitTests()
        fn = fixture.make()
        fixture.call.bot = object()
        fixture.call.message.chat = types.SimpleNamespace(id=1)
        fixture.state.data['file_paths'] = ['source.pdf']
        fixture.env['_missing_upload_paths'] = lambda data: []
        fixture.env['_render_question_count_screen'] = AsyncMock()
        fixture.env['execute_quiz_generation_workflow'].return_value = (
            None,None,'ai_timeout',None)
        await fn(fixture.call,fixture.state)
        fixture.env['refund_user_on_failure'].assert_awaited_once()
        fixture.env['safe_file_cleanup'].assert_not_called()
        fixture.env['_render_question_count_screen'].assert_awaited_once()
        self.assertEqual(fixture.state.status,'waiting_for_count')

    async def test_expired_generation_never_persists_a_quiz(self):
        async def generate(**kwargs):
            await asyncio.sleep(10)
        env=load_functions('services/quiz_service.py', {'execute_quiz_generation_workflow'},
            **vars(C), asyncio=asyncio,
            _HEAVY_PROCESSING_SEMAPHORE=asyncio.Semaphore(1),
            get_file_quizzes=AsyncMock(return_value=[]),
            resolve_generation_question_type_text=lambda *a:None,
            resolve_quiz_extraction_instruction=lambda *a:None,
            determine_execution_mode=lambda *a,**k:'Standard',
            generate_quiz_smart=generate, save_file_quiz_multiple=AsyncMock(),
            log_warning=Mock(), logger=Mock())
        with patch.object(tb,'quiz_timeout_limits',return_value=(0.01,0.03)):
            quiz,quiz_id,error,meta=await env['execute_quiz_generation_workflow'](
                1,dict(input_type='text',pure_text='source',subject_type=C.SUBJECT_OTHER),10,None)
        self.assertEqual(error,'ai_timeout')
        self.assertIsNone(quiz)
        self.assertIsNone(quiz_id)
        env['save_file_quiz_multiple'].assert_not_awaited()


if __name__ == '__main__':
    unittest.main()
