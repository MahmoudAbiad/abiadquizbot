"""Exercise production streaming without API credentials or external services."""
import asyncio
import copy
import contextlib
import importlib.util
import json
import re
import os
import types
import unittest
from contextvars import ContextVar
from unittest.mock import AsyncMock, Mock, patch

from reliability_support import load_functions, FakeRedis as BaseRedis

class FakeRedis(BaseRedis):
    async def get(self, key): return self.values.get(key)
    async def exists(self, key): return int(key in self.values)

from services.redis_reliability import UserLock
from services.streaming_quiz import StreamingQuiz, streaming_enabled, StreamingSessionDetached

spec = importlib.util.spec_from_file_location('stream_test_schemas', 'helpers/gemini_helper/schemas.py')
schemas = importlib.util.module_from_spec(spec)
spec.loader.exec_module(schemas)
repair = load_functions('helpers/gemini_helper/parsing.py', {'_repair_json_backslashes'},
    _JSON_LEGAL_ALWAYS_ESCAPES=('"', '\\', '/', 'u'),
    _JSON_AMBIGUOUS_ESCAPE_LETTERS=('b', 'f', 'r', 't'))['_repair_json_backslashes']
S = load_functions('helpers/gemini_helper/streaming.py',
    {'StreamingInterrupted', 'QuestionDecoder', 'generate_quiz_response', 'OrderedChunks', 'generate_groq_response'},
    asyncio=asyncio, contextlib=contextlib, json=json, re=re, SimpleNamespace=types.SimpleNamespace,
    QuizQuestion=schemas.QuizQuestion, QuizResponse=schemas.QuizResponse,
    _repair_json_backslashes=repair, question_sink=ContextVar('test_sink', default=None))
Decoder, Interrupted, sink = S['QuestionDecoder'], S['StreamingInterrupted'], S['question_sink']


def q(text='سؤال'):
    return schemas.QuizQuestion(question=text, options=['أ', 'ب', 'ج', 'د'], correct_option_id=1,
                                hint='تلميح').model_dump()


def client_for(stream):
    models = types.SimpleNamespace(generate_content_stream=AsyncMock(return_value=stream),
                                   generate_content=AsyncMock(return_value='legacy'))
    return types.SimpleNamespace(aio=types.SimpleNamespace(models=models))


class DecoderTests(unittest.TestCase):
    def test_every_character_boundary_and_nested_math(self):
        question = q('نص "مقتبس" {x} \\frac{1}{2}')
        question.update(table={'headers':['x'], 'rows':[['[{}]']]},
                        matrices=[{'label':'A', 'rows':[['1', '2']], 'bracket':'square'}])
        raw = json.dumps({'questions':[question, q('التالي')]}, ensure_ascii=False)
        d = Decoder(); emitted = []
        for char in raw:
            emitted.extend(d.feed(char))
        self.assertEqual(emitted, [question, q('التالي')])
        self.assertEqual(d.finish(), emitted)

    def test_incomplete_question_never_emits(self):
        d = Decoder(); self.assertEqual(d.feed('{"questions":[{"question":"x"'), [])
        with self.assertRaises(ValueError): d.finish()

    def test_truncated_envelope_fails_even_with_complete_question(self):
        d = Decoder(); self.assertEqual(len(d.feed('{"questions":[' + json.dumps(q()))), 1)
        with self.assertRaises(ValueError): d.finish()

    def test_invalid_answer_and_empty_option_rejected(self):
        for change in ({'correct_option_id':4}, {'question':' '}, {'options':['a','']}):
            d = Decoder()
            with self.assertRaises(ValueError): d.feed(json.dumps({'questions':[{**q(), **change}]}))

    def test_raw_latex_repair_matches_final(self):
        raw = json.dumps({'questions':[q(r'\frac{1}{2}')]}, ensure_ascii=False).replace('\\\\frac', '\\frac')
        d=Decoder(); emitted=d.feed(raw)
        self.assertEqual(emitted[0]['question'], r'\frac{1}{2}')
        self.assertEqual(d.finish(), emitted)

    def test_response_limit(self):
        with self.assertRaises(ValueError): Decoder().feed(' ' * 4_000_001)


class ProviderTests(unittest.IsolatedAsyncioTestCase):
    async def test_first_question_before_provider_finishes_and_usage_retained(self):
        first, release = asyncio.Event(), asyncio.Event()
        async def chunks():
            yield types.SimpleNamespace(text='{"questions":['+json.dumps(q()), usage_metadata=None)
            await release.wait()
            yield types.SimpleNamespace(text=','+json.dumps(q('2'))+']}', usage_metadata=types.SimpleNamespace(total_token_count=19))
        seen=[]
        async def emit(question): seen.append(question); first.set()
        token=sink.set(emit)
        stream=chunks(); client=client_for(stream)
        try:
            task=asyncio.create_task(S['generate_quiz_response'](client,model='m',contents=[],config=None,timeout=2))
            await asyncio.wait_for(first.wait(), 1)
            self.assertFalse(task.done()); self.assertEqual(seen,[q()])
            release.set(); response=await task
            self.assertEqual(len(seen),2); self.assertEqual(response.usage_metadata.total_token_count,19)
            self.assertIsNone(stream.ag_frame)
        finally: sink.reset(token)

    async def test_partial_failure_is_not_retryable(self):
        async def chunks():
            yield types.SimpleNamespace(text='{"questions":['+json.dumps(q()))
            raise ConnectionError('dropped')
        token=sink.set(AsyncMock())
        try:
            with self.assertRaises(Interrupted):
                await S['generate_quiz_response'](client_for(chunks()),model='m',contents=[],config=None,timeout=1)
        finally: sink.reset(token)

    async def test_failure_before_any_question_can_fallback(self):
        async def chunks():
            raise ConnectionError('before first question')
            yield
        token=sink.set(AsyncMock())
        try:
            with self.assertRaises(ConnectionError):
                await S['generate_quiz_response'](client_for(chunks()),model='m',contents=[],config=None,timeout=1)
        finally: sink.reset(token)

    async def test_timeout_and_cancellation_close_provider(self):
        for cancel in (False, True):
            emitted=asyncio.Event(); closed=asyncio.Event()
            async def chunks():
                try:
                    yield types.SimpleNamespace(text='{"questions":['+json.dumps(q()))
                    await asyncio.Event().wait()
                finally: closed.set()
            async def emit(question): emitted.set()
            token=sink.set(emit)
            try:
                task=asyncio.create_task(S['generate_quiz_response'](client_for(chunks()),model='m',contents=[],config=None,timeout=.05))
                await emitted.wait()
                if cancel: task.cancel()
                with self.assertRaises(asyncio.CancelledError if cancel else Interrupted): await task
                self.assertTrue(closed.is_set())
            finally: sink.reset(token)

    async def test_disabled_uses_original_nonstream_api(self):
        client=client_for(None)
        self.assertEqual(await S['generate_quiz_response'](client,model='m',contents=[],config=None,timeout=1),'legacy')
        client.aio.models.generate_content_stream.assert_not_called()

    async def test_groq_streams_complete_questions(self):
        class Stream:
            close=AsyncMock()
            def __aiter__(self): return self.parts()
            async def parts(self):
                for part in ['{"questions":[', json.dumps(q()), ']}']:
                    yield types.SimpleNamespace(choices=[types.SimpleNamespace(delta=types.SimpleNamespace(content=part))])
        stream=Stream(); client=types.SimpleNamespace(chat=types.SimpleNamespace(completions=types.SimpleNamespace(create=AsyncMock(return_value=stream))))
        emit=AsyncMock(); token=sink.set(emit)
        try:
            await S['generate_groq_response'](client,model='g')
            emit.assert_awaited_once_with(q()); stream.close.assert_awaited_once()
        finally: sink.reset(token)

    async def test_concurrent_users_have_separate_sinks(self):
        async def run(name):
            emit=AsyncMock(); token=sink.set(emit)
            async def chunks():
                await asyncio.sleep(0)
                yield types.SimpleNamespace(text=json.dumps({'questions':[q(name)]}))
            try: await S['generate_quiz_response'](client_for(chunks()),model='m',contents=[],config=None,timeout=1)
            finally: sink.reset(token)
            return emit.call_args.args[0]['question']
        self.assertEqual(await asyncio.gather(run('a'),run('b')), ['a','b'])


class OrderingTests(unittest.IsolatedAsyncioTestCase):
    async def test_later_chunk_waits_for_earlier_chunk_and_deduplicates(self):
        emitted=[]; later_ready=asyncio.Event(); finish_first=asyncio.Event()
        async def emit(question): emitted.append(question['question'])
        ordered=S['OrderedChunks'](emit,10)
        async def first():
            await sink.get()(q('1')); await finish_first.wait(); await sink.get()(q('boundary'))
            return ([q('1'),q('boundary')],)
        async def second():
            await sink.get()(q('boundary')); await sink.get()(q('3')); later_ready.set()
            return ([q('boundary'),q('3')],)
        tasks=[asyncio.create_task(ordered.run(0,first())),asyncio.create_task(ordered.run(1,second()))]
        await later_ready.wait(); self.assertEqual(emitted,['1'])
        finish_first.set(); await asyncio.gather(*tasks)
        self.assertEqual(emitted,['1','boundary','3'])

    async def test_quotas_total_and_same_chunk_duplicates_match_existing_merge(self):
        merge=load_functions('helpers/gemini_helper/generation.py', {'_merge_pdf_chunk_questions'})['_merge_pdf_chunk_questions']
        results=[([q('1'),q('1'),q('extra')],),([q('1'),q('2'),q('extra2')],),([q('3')],)]
        for quotas in (None,[2,2,1]):
            for limit in (2,9):
                received=[]
                async def emit(question):received.append(question)
                ordered=S['OrderedChunks'](emit,limit,quotas)
                async def run(index):
                    for question in results[index][0]:await sink.get()(question)
                    return results[index]
                await asyncio.gather(*(ordered.run(i,run(i)) for i in (2,1,0)))
                self.assertEqual(received,merge(results,limit,quotas))


class State:
    def __init__(self): self.data={};self.status='processing'
    async def get_data(self):return copy.deepcopy(self.data)
    async def update_data(self,**kwargs):self.data.update(copy.deepcopy(kwargs))
    async def set_state(self,value):self.status=value
    async def get_state(self):return self.status
    async def clear(self):self.data={};self.status=None


class PlaybackTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.redis=FakeRedis();self.state=State();self.lock=UserLock(self.redis,7)
        await self.lock.acquire()
        self.sent=[]
        async def send(chat,user,state):
            d=await state.get_data();self.sent.append(d['current_index'])
            if d['current_index']>=len(d['questions']) and d.get('generation_pending'):
                await state.update_data(stream_waiting=True)
        self.send=AsyncMock(side_effect=send)
        self.modules=patch.dict('sys.modules',{'config':types.SimpleNamespace(QuizState=types.SimpleNamespace(answering_quiz='answering')),
            'handlers.quiz_runner':types.SimpleNamespace(send_question_by_ids=self.send),
            'helpers.supabase_helper':types.SimpleNamespace(start_quiz_attempt=Mock(return_value='attempt'))})
        self.modules.start();self.addCleanup(self.modules.stop)
        self.session=StreamingQuiz(self.state,7,7,{'source_title':'Title'},3,self.lock,self.redis)
        await self.session.begin()

    async def asyncTearDown(self):
        with contextlib.suppress(StreamingSessionDetached): await self.session.reacquire()
        await self.session.close();await self.lock.release()

    async def test_answer_lock_available_during_generation(self):
        other=UserLock(self.redis,7);self.assertTrue(await other.acquire());await other.release()
        await self.session.publish(q())
        self.assertEqual(self.sent,[0]);self.assertTrue(self.state.data['generation_pending'])

    async def test_waiting_autoresumes_without_resetting_score(self):
        await self.session.publish(q())
        await self.state.update_data(current_index=1,score=1,answered_questions={'0':1},stream_waiting=True)
        await self.session.publish(q('2'))
        self.assertEqual(self.sent,[0,1]);self.assertEqual(self.state.data['score'],1)
        self.assertEqual(self.state.data['answered_questions'],{'0':1})
        self.assertFalse(self.state.data['stream_waiting'])

    async def test_finish_preserves_current_poll_identity_answers_and_index(self):
        await self.session.publish(q());await self.session.publish(q('2'))
        await self.state.update_data(score=1,answered_questions={'0':1})
        await self.session.reacquire();await self.session.finish([q(),q('2')],'saved')
        self.assertEqual(self.state.data['total_count'],2)
        self.assertEqual(self.state.data['stream_poll_session'],self.session.identity)
        self.assertEqual(self.state.data['score'],1);self.assertEqual(self.sent,[0])
        self.assertFalse(self.state.data['generation_pending'])

    async def test_finish_waiting_emits_completion_only_after_all_generated(self):
        await self.session.publish(q());await self.state.update_data(current_index=1,stream_waiting=True)
        await self.session.reacquire();await self.session.finish([q()],'saved')
        self.assertEqual(self.sent,[0,1]);self.assertFalse(self.state.data['generation_pending'])

    async def test_pause_keeps_generation_but_does_not_send_more_questions(self):
        await self.state.update_data(stream_paused=True)
        await self.session.publish(q());await self.session.publish(q('2'))
        self.assertEqual(self.sent,[])
        await self.session.reacquire();await self.session.finish([q(),q('2')],'saved')
        self.assertEqual(self.sent,[2]);self.assertTrue(self.state.data['stopped_early'])

    async def test_failed_stream_cannot_remain_active(self):
        await self.session.publish(q());await self.session.reacquire();await self.session.close()
        self.assertFalse(self.state.data['generation_pending']);self.assertEqual(self.state.data['questions'],[])
        self.assertTrue(self.state.data['quiz_completed'])

    async def test_old_stream_does_not_overwrite_new_session(self):
        await self.state.update_data(stream_id='new',questions=[q('new')])
        with self.assertRaises(RuntimeError):await self.session.publish(q('old'))
        with self.assertRaises(StreamingSessionDetached):await self.session.reacquire()
        await self.session.close()
        self.assertEqual(self.state.data['questions'],[q('new')])

    async def test_disable_switch(self):
        saved = AsyncMock(return_value=True)
        with patch.dict('sys.modules', {'helpers.supabase_helper': types.SimpleNamespace(is_feature_enabled=saved)}):
            with patch.dict(os.environ, {'QUIZ_STREAMING_ENABLED': '0'}):
                self.assertFalse(await streaming_enabled())
                saved.assert_not_awaited()
            with patch.dict(os.environ, {'QUIZ_STREAMING_ENABLED': '1'}):
                self.assertTrue(await streaming_enabled())
                saved.assert_awaited_once_with('quiz_streaming_enabled', default=True)
                saved.return_value = False
                self.assertFalse(await streaming_enabled())

class AdminSwitchTests(unittest.IsolatedAsyncioTestCase):
    async def test_admin_toggle_updates_generation_and_survives_cache_loss(self):
        import datetime
        from constants import FEATURE_FLAGS_REGISTRY
        key = 'quiz_streaming_enabled'
        self.assertIn(key, FEATURE_FLAGS_REGISTRY)
        rows = {}
        table = Mock()
        table.select.return_value = table
        table.eq.return_value = table
        table.limit.return_value = table
        async def read():
            return types.SimpleNamespace(data=[{'enabled': rows[key]}] if key in rows else [])
        table.execute = AsyncMock(side_effect=read)
        def upsert(row):
            async def save():
                rows[row['key']] = row['enabled']
                return types.SimpleNamespace(data=[row])
            return types.SimpleNamespace(execute=save)
        table.upsert.side_effect = upsert
        redis = FakeRedis()
        db = load_functions('helpers/supabase_helper/feature_flags.py',
            {'is_feature_enabled', 'set_feature_flag', '_flag_redis_key', '_decode_flag_value'},
            redis_client=redis, supabase=types.SimpleNamespace(table=Mock(return_value=table)),
            datetime=datetime, _GAME_FLAGS=set(), _FEATURE_FLAG_REDIS_PREFIX='feature_flag:',
            FEATURE_FLAG_CACHE_TTL_SECONDS=30, logger=Mock(), log_error=Mock())
        async def all_flags(registry):
            return {key: await db['is_feature_enabled'](key)}
        render = AsyncMock()
        admin = load_functions('handlers/admin/feature_flags.py', {'toggle_flag'},
            FEATURE_FLAGS_REGISTRY=FEATURE_FLAGS_REGISTRY, FEATURE_SUBFLAGS_REGISTRY={},
            get_all_feature_flags=all_flags, set_feature_flag=db['set_feature_flag'],
            render_feature_detail=render, logger=Mock())
        call = types.SimpleNamespace(data='admin_flag_toggle_' + key, answer=AsyncMock())
        with patch.dict(os.environ, {'QUIZ_STREAMING_ENABLED': '1'}), patch.dict('sys.modules',
                {'helpers.supabase_helper': types.SimpleNamespace(is_feature_enabled=db['is_feature_enabled'])}):
            self.assertTrue(await streaming_enabled())
            await admin['toggle_flag'](call)
            self.assertFalse(await streaming_enabled())
            redis.values.clear()
            self.assertFalse(await streaming_enabled())
            await admin['toggle_flag'](call)
            self.assertTrue(await streaming_enabled())
            render.assert_awaited_with(call, key)
        table.upsert.assert_called()

class CascadeTests(unittest.IsolatedAsyncioTestCase):
    async def test_no_provider_fallback_after_visible_prefix(self):
        mark=Mock()
        env=load_functions('helpers/gemini_helper/cascade.py',{'_execute_cascade'},
            API_KEYS=['key'],_VERTEX_CLIENT=None,_GEMINI_CLIENTS=[object()],
            _round_robin_key_order=lambda:[0],
            _get_models_cascade=AsyncMock(return_value=[{'provider':'gemini','model_name':'a'},{'provider':'gemini','model_name':'b'}]),
            _is_model_key_blocked=lambda *a:False,_mark_model_key_failure=mark,
            _last_model_used_var=ContextVar('winner',default=None),log_info=Mock(),log_error=Mock(),logger=Mock())
        with patch.dict('sys.modules',{'helpers.gemini_helper.streaming':types.SimpleNamespace(StreamingInterrupted=Interrupted)}):
            attempt=AsyncMock(side_effect=Interrupted('partial'))
            with self.assertRaises(Interrupted):await env['_execute_cascade'](attempt)
            self.assertEqual(attempt.await_count,1);mark.assert_not_called()
            attempt=AsyncMock(side_effect=[ConnectionError('before output'),('success',)])
            self.assertEqual(await env['_execute_cascade'](attempt),('success',))
            self.assertEqual(attempt.await_count,2)


class WorkflowTests(unittest.IsolatedAsyncioTestCase):
    async def test_streamed_options_are_exactly_the_saved_options(self):
        import random
        original={**q(), 'options':['a','b','c','d','correct'], 'correct_option_id':4}
        async def generate(**kwargs):
            await sink.get()(copy.deepcopy(original))
            return [copy.deepcopy(original)]
        env=load_functions('services/quiz_service.py',
            {'execute_quiz_generation_workflow','normalize_quiz_option_count','_shuffle_question_options','shuffle_quiz_options'},
            asyncio=asyncio,os=os,random=random,OPTION_COUNT=4,question_sink=sink,
            _HEAVY_PROCESSING_SEMAPHORE=asyncio.Semaphore(2),get_file_quizzes=AsyncMock(return_value=[]),
            SUBJECT_OTHER='other',SUBJECT_QUIZ_SOLVED='solved',SUBJECT_QUIZ_UNSOLVED='unsolved',
            SUBJECT_MATH='math',SUBJECT_ENGLISH='english',SUBJECT_FRENCH='french',
            QUESTION_TYPE_GENERAL='general',DIFFICULTY_MEDIUM='medium',
            resolve_generation_question_type_text=lambda *a:None,resolve_quiz_extraction_instruction=lambda *a:None,
            generate_quiz_smart=generate,get_last_generation_metadata=lambda:None,
            log_ai_generation=AsyncMock(),spawn=lambda coro:coro.close(),
            save_file_quiz_multiple=AsyncMock(return_value='saved'),build_question_type_label=lambda *a,**k:'label')
        emitted=AsyncMock()
        result=await env['execute_quiz_generation_workflow'](7,{'pure_text':'x','file_hash':'h','subject_type':'solved'},1,None,on_question=emitted)
        prepared=result[0][0]
        self.assertEqual(prepared,emitted.call_args.args[0])
        self.assertEqual(prepared['options'][prepared['correct_option_id']],'correct')
        self.assertEqual(env['save_file_quiz_multiple'].call_args.kwargs['quiz_data'],result[0])
        self.assertIsNone(sink.get())
        async def inconsistent(**kwargs):
            await sink.get()(copy.deepcopy(original))
            return [q('replacement')]
        env['generate_quiz_smart']=inconsistent
        with self.assertRaises(ValueError):
            await env['execute_quiz_generation_workflow'](7,{'pure_text':'x','subject_type':'solved'},1,None,on_question=emitted)
        self.assertIsNone(sink.get())


class RunnerTests(unittest.IsolatedAsyncioTestCase):
    async def test_next_at_buffer_end_does_not_skip_future_question(self):
        state=State();await state.update_data(questions=[q()],current_index=1,generation_pending=True)
        event=types.SimpleNamespace(answer=AsyncMock(),message=types.SimpleNamespace(delete=AsyncMock()))
        send=AsyncMock()
        env=load_functions('handlers/quiz_runner.py',{'handle_next'},send_question=send,log_error=Mock(),logger=Mock())
        await env['handle_next'](event,state)
        self.assertEqual(state.data['current_index'],1);send.assert_not_called()

    async def test_buffer_end_waits_and_only_complete_stream_finishes_quiz(self):
        state=State();await state.update_data(questions=[q()],current_index=1,generation_pending=True)
        bot=types.SimpleNamespace(send_message=AsyncMock());complete=AsyncMock()
        env=load_functions('handlers/quiz_runner.py',{'send_question_by_ids'},bot=bot,_handle_quiz_completion=complete,log_error=Mock(),logger=Mock())
        await env['send_question_by_ids'](7,7,state)
        await env['send_question_by_ids'](7,7,state)
        self.assertTrue(state.data['stream_waiting']);self.assertEqual(bot.send_message.await_count,1)
        complete.assert_not_called()
        await state.update_data(generation_pending=False)
        await env['send_question_by_ids'](7,7,state)
        complete.assert_awaited_once()

    async def test_poll_created_before_finalization_still_scores_once(self):
        state=State();await state.update_data(questions=[q()],current_index=0,score=0,
            stream_poll_session='stream',attempt_id='new-attempt',answered_questions={})
        redis=FakeRedis();await redis.set('poll:p',json.dumps({'user_id':7,'question_index':0,'correct_option_id':1,'session_id':'stream'}))
        identity=load_functions('handlers/session_navigation.py',{'session_id'},uuid=__import__('uuid'))['session_id']
        navigation=types.SimpleNamespace(active_quiz=AsyncMock(return_value=True),session_id=identity)
        env=load_functions('handlers/quiz_runner.py',{'handle_poll_answer'},redis_client=redis,json=json,log_error=Mock(),logger=Mock())
        event=types.SimpleNamespace(poll_id='p',option_ids=[1],user=types.SimpleNamespace(id=7))
        with patch.dict('sys.modules',{'handlers.session_navigation':navigation}):
            await env['handle_poll_answer'](event,state);await env['handle_poll_answer'](event,state)
        self.assertEqual(state.data['score'],1)
        self.assertEqual(state.data['answered_questions'],{'0':1})

class HandlerIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def run_handler(self, fail=False):
        from test_generation_regressions import DebitTests
        harness=DebitTests();fn=harness.make();self.addCleanup(harness.doCleanups)
        harness.call.message.chat=types.SimpleNamespace(id=1)
        config=types.SimpleNamespace(QuizState=harness.env['QuizState'])
        send=AsyncMock()
        runner=types.SimpleNamespace(send_question_by_ids=send)
        db=types.SimpleNamespace(start_quiz_attempt=Mock(return_value='stream-attempt'),
                                 is_feature_enabled=AsyncMock(return_value=True))
        async def generate(user,data,count,status,on_question):
            await on_question(q())
            await harness.state.update_data(score=1,current_index=1,answered_questions={'0':1},stream_waiting=True)
            if fail:raise Interrupted('lost connection after first question')
            await on_question(q('second'))
            return [q(),q('second')],'saved-quiz','',None
        harness.env['execute_quiz_generation_workflow'].side_effect=generate
        with patch.dict(os.environ,{'QUIZ_STREAMING_ENABLED':'1'}), patch.dict('sys.modules',
                {'config':config,'handlers.quiz_runner':runner,'helpers.supabase_helper':db}):
            await fn(harness.call,harness.state)
        return harness,send

    async def test_end_to_end_handler_preserves_partial_answers(self):
        h,send=await self.run_handler()
        self.assertEqual(h.state.data['score'],1)
        self.assertEqual(h.state.data['current_index'],1)
        self.assertEqual(h.state.data['quiz_id'],'saved-quiz')
        self.assertEqual(h.state.data['total_count'],2)
        self.assertEqual(h.state.data['attempt_id'],'stream-attempt')
        self.assertFalse(h.state.data['generation_pending'])
        self.assertEqual(send.await_count,2)
        h.env['refund_user_on_failure'].assert_not_awaited()
        self.assertFalse(h.env['redis_client'].values)

    async def test_end_to_end_partial_failure_refunds_once_and_releases_locks(self):
        h,send=await self.run_handler(fail=True)
        h.env['refund_user_on_failure'].assert_awaited_once()
        self.assertEqual(h.state.data['questions'],[])
        self.assertFalse(h.state.data['generation_pending'])
        self.assertIsNone(h.state.status)
        self.assertFalse(h.env['redis_client'].values)

class StreamingMiddlewareTests(unittest.IsolatedAsyncioTestCase):
    async def test_blocks_new_requests_but_allows_next_and_pause_resume(self):
        class Message:
            def __init__(self):
                self.from_user=types.SimpleNamespace(id=7);self.answer=AsyncMock();self.chat=types.SimpleNamespace(id=7)
        class Callback:
            def __init__(self,value):
                self.from_user=types.SimpleNamespace(id=7);self.data=value;self.answer=AsyncMock();self.message=Message()
        state=State();await state.update_data(generation_pending=True,questions=[q()])
        redis=FakeRedis();await redis.set('quiz:stream:producer:7','owner')
        env=load_functions('middlewares.py',{'StreamingQuizMiddleware'},BaseMiddleware=object,Message=Message,CallbackQuery=Callback)
        middleware=env['StreamingQuizMiddleware']();handler=AsyncMock();send=AsyncMock()
        with patch.dict('sys.modules',{'config':types.SimpleNamespace(redis_client=redis),
                'handlers.quiz_runner':types.SimpleNamespace(send_question_by_ids=send)}):
            for event in (Message(),Callback('save_quiz'),Callback('sn:replace:old')):
                await middleware(handler,event,{'state':state})
            handler.assert_not_called()
            await middleware(handler,Callback('next_question'),{'state':state});handler.assert_awaited_once()
            await middleware(handler,Callback('quiz_stop'),{'state':state})
            self.assertTrue(state.data['stream_paused'])
            await middleware(handler,Callback('stream_resume'),{'state':state})
            self.assertFalse(state.data['stream_paused']);send.assert_awaited_once()

    async def test_dead_producer_refunds_before_clearing_session(self):
        class Message:
            def __init__(self):self.from_user=types.SimpleNamespace(id=7);self.answer=AsyncMock()
        state=State();await state.update_data(generation_pending=True,debit_id='old-receipt')
        refund=AsyncMock();redis=FakeRedis()
        env=load_functions('middlewares.py',{'StreamingQuizMiddleware'},BaseMiddleware=object,Message=Message,CallbackQuery=type('Callback',(),{}))
        with patch.dict('sys.modules',{'config':types.SimpleNamespace(redis_client=redis),
                'services.quiz_service':types.SimpleNamespace(refund_user_on_failure=refund)}):
            await env['StreamingQuizMiddleware']()(AsyncMock(),Message(),{'state':state})
        refund.assert_awaited_once();self.assertEqual(state.data,{})
