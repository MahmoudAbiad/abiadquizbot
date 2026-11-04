import asyncio
import os
import tempfile
import types
import unittest
from unittest.mock import AsyncMock, Mock

from reliability_support import FakeRedis, load_functions
from services.redis_reliability import UserLock
from services.upload_validation import InvalidUploadError
from services.wallet_errors import WalletDebitError, WALLET_DEBIT_ERROR_TEXT


class State:
    def __init__(self, data=None):
        self.data = dict(data or {})
        self.status = 'waiting_for_count'
        self.fail_update = False
    async def get_state(self):return self.status
    async def set_state(self, value):self.status=value
    async def get_data(self):return dict(self.data)
    async def set_data(self, value):self.data=dict(value)
    async def update_data(self, **values):
        if self.fail_update:raise ConnectionError('FSM unavailable after debit')
        self.data.update(values)
    async def clear(self):self.data={};self.status=None


class DebitTests(unittest.IsolatedAsyncioTestCase):
    def make(self):
        status=types.SimpleNamespace(edit_text=AsyncMock(),delete=AsyncMock())
        self.call=types.SimpleNamespace(answer=AsyncMock(),from_user=types.SimpleNamespace(id=1),message=types.SimpleNamespace(answer=AsyncMock(return_value=status),delete=AsyncMock()))
        states=types.SimpleNamespace(waiting_for_count='waiting_for_count',processing_file_quiz='processing',answering_quiz='answering')
        def spawn(coro):coro.close()
        async def loaded(call,state,*a,**kw):await state.update_data(quiz_data=['result'],attempt_id='saved')
        loaded.__wrapped__ = loaded
        self.env=load_functions('handlers/files.py', {'handle_count_start','_clear_quiz_charge_markers','_missing_upload_paths'},
            asyncio=asyncio, redis_client=FakeRedis(), UserLock=UserLock, QuizState=states,
            WalletDebitError=WalletDebitError, WALLET_DEBIT_ERROR_TEXT=WALLET_DEBIT_ERROR_TEXT,
            _QUIZ_CHARGE_STATE_KEYS=('debited_cost','debited_free','debited_paid','debit_id','debited_gift'),
            validate_question_count=lambda count:(True,''), SUBJECT_QUIZ_SOLVED='solved',SUBJECT_QUIZ_UNSOLVED='unsolved',
            calculate_quiz_points_cost=AsyncMock(return_value=2),determine_execution_mode=lambda *a,**kw:'Standard',
            _current_user=AsyncMock(return_value={'points':100}),update_user_stats=AsyncMock(return_value={'debited_free':1.5,'debited_paid':0.5,'debited_gift':0,'debit_id':'receipt-1'}),
            log_usage_event=AsyncMock(),spawn=spawn,logger=Mock(),log_error=Mock(),log_warning=Mock(),
            refund_user_on_failure=AsyncMock(), _insufficient_balance=AsyncMock(),MSG_PROCESSING='processing',
            os=os,Dict=dict,List=list,Any=object,InvalidUploadError=InvalidUploadError,safe_title=lambda t:t,
            MSG_UPLOAD_FILE_MISSING='FILE-MISSING',
            execute_quiz_generation_workflow=AsyncMock(return_value=([{'question':'ok'}],'own-id','',None)),
            _run_processing_heartbeat=AsyncMock(), reward_referrer_if_eligible=AsyncMock(),start_loaded_quiz=loaded,safe_file_cleanup=Mock())
        self.state=State({'selected_question_count':5,'items_count':1,'debited_cost':999,'debited_free':999,'debited_paid':999,'source_title':'keep'})
        return self.env['handle_count_start']

    async def test_stale_debit_does_not_refund(self):
        fn=self.make();self.env['_current_user'].side_effect=ValueError('before deduction')
        await fn(self.call,self.state)
        self.env['refund_user_on_failure'].assert_not_awaited()

    async def test_local_debit_survives_fsm_failure(self):
        fn=self.make();self.state.fail_update=True
        await fn(self.call,self.state)
        refund=self.env['refund_user_on_failure'];refund.assert_awaited_once()
        data=refund.call_args.args[1]
        self.assertEqual((data['debited_cost'],data['debited_free'],data['debited_paid']),(2,1.5,0.5))

    async def test_success_preserves_result_state_without_charge_markers(self):
        fn=self.make();await fn(self.call,self.state)
        self.assertEqual(self.state.data['quiz_data'],['result'])
        self.assertEqual(self.state.data['attempt_id'],'saved')
        self.assertEqual(self.state.data['source_title'],'keep')
        self.assertTrue(all(key not in self.state.data for key in ('debited_cost','debited_free','debited_paid','debit_id','debited_gift')))
        self.env['refund_user_on_failure'].assert_not_awaited()

    async def test_failed_ai_refunds_once_even_if_error_message_fails(self):
        fn=self.make();self.env['execute_quiz_generation_workflow'].return_value=(None,None,'ai_failed',None)
        self.call.message.answer.return_value.edit_text.side_effect=ValueError('Telegram failed')
        await fn(self.call,self.state)
        self.env['refund_user_on_failure'].assert_awaited_once()

    async def test_saved_quiz_not_refunded_on_analytics_failure(self):
        fn=self.make();self.env['log_usage_event'].side_effect=ValueError('analytics failure')
        await fn(self.call,self.state)
        self.env['refund_user_on_failure'].assert_not_awaited()

    async def test_distributed_generation_lock_blocks_second_attempt(self):
        fn=self.make();start=asyncio.Event();release=asyncio.Event()
        async def generate(*args):start.set();await release.wait();return ([{}],'id','',None)
        self.env['execute_quiz_generation_workflow'].side_effect=generate
        task=asyncio.create_task(fn(self.call,self.state));await start.wait()
        await fn(self.call,self.state)
        release.set();await task
        self.env['update_user_stats'].assert_awaited_once()


class UploadRetentionTests(DebitTests):
    """الملف المرفوع لا يُحذف عند رصيد غير كافٍ/عدد غير صالح، والرسائل تطابق السبب الفعلي."""
    def make_with_file(self, directory, **extra):
        fn=self.make()
        path=os.path.join(directory,'u.pdf')
        with open(path,'wb') as handle:handle.write(b'%PDF')
        self.env['safe_file_cleanup']=Mock(side_effect=lambda p:os.remove(p) if os.path.exists(p) else False)
        self.state.data.update({'input_type':'media','file_paths':[path],**extra})
        return fn,path

    async def test_insufficient_balance_keeps_file_and_waiting_state(self):
        with tempfile.TemporaryDirectory() as d:
            fn,path=self.make_with_file(d)
            self.env['_current_user'].return_value={'points':0}
            await fn(self.call,self.state)
            self.assertTrue(os.path.exists(path))
            self.assertEqual(self.state.status,'waiting_for_count')
            self.env['_insufficient_balance'].assert_awaited_once()
            self.env['update_user_stats'].assert_not_awaited()
            # شحن الرصيد ثم إعادة المحاولة بنفس الملف تنجح (هذا بالضبط ما كان يفشل سابقاً)
            self.env['_current_user'].return_value={'points':100}
            await fn(self.call,self.state)
            self.env['execute_quiz_generation_workflow'].assert_awaited_once()
            self.assertFalse(os.path.exists(path))  # بعد انتهاء التوليد فقط

    async def test_invalid_question_count_keeps_file(self):
        with tempfile.TemporaryDirectory() as d:
            fn,path=self.make_with_file(d)
            self.env['validate_question_count']=lambda c:(False,'bad count')
            await fn(self.call,self.state)
            self.assertTrue(os.path.exists(path))
            self.assertEqual(self.state.status,'waiting_for_count')
            self.call.message.answer.assert_awaited_with('❌ bad count')

    async def test_wallet_rpc_failure_keeps_upload_without_low_balance_or_refund(self):
        with tempfile.TemporaryDirectory() as d:
            fn,path=self.make_with_file(d)
            self.env['update_user_stats'].side_effect=WalletDebitError('Database constraint')
            await fn(self.call,self.state)
            self.assertTrue(os.path.exists(path))
            self.assertEqual(self.state.status,'waiting_for_count')
            self.call.message.answer.assert_awaited_once_with(WALLET_DEBIT_ERROR_TEXT)
            self.env['_insufficient_balance'].assert_not_awaited()
            self.env['execute_quiz_generation_workflow'].assert_not_awaited()
            self.env['refund_user_on_failure'].assert_not_awaited()

    async def test_balance_race_uses_fresh_balance_after_confirmed_rejection(self):
        with tempfile.TemporaryDirectory() as d:
            fn,path=self.make_with_file(d)
            self.env['_current_user'].side_effect=[{'points':100},{'points':0}]
            self.env['update_user_stats'].return_value=None
            await fn(self.call,self.state)
            self.assertTrue(os.path.exists(path))
            self.env['_insufficient_balance'].assert_awaited_once_with(
                self.call.message,{'points':0},2,action='quiz_generation')
            self.env['execute_quiz_generation_workflow'].assert_not_awaited()
            self.env['refund_user_on_failure'].assert_not_awaited()

    async def test_missing_file_checked_before_any_debit(self):
        with tempfile.TemporaryDirectory() as d:
            fn,path=self.make_with_file(d);os.remove(path)
            await fn(self.call,self.state)
            self.env['update_user_stats'].assert_not_awaited()
            self.env['execute_quiz_generation_workflow'].assert_not_awaited()
            self.assertIsNone(self.state.status)
            text=self.call.message.answer.call_args.args[0]
            self.assertIn('FILE-MISSING',text);self.assertIn('لم يتم خصم',text)

    async def test_file_vanishing_mid_generation_refunds_with_specific_message(self):
        with tempfile.TemporaryDirectory() as d:
            fn,path=self.make_with_file(d)
            self.env['execute_quiz_generation_workflow'].side_effect=FileNotFoundError(path)
            await fn(self.call,self.state)
            self.env['refund_user_on_failure'].assert_awaited_once()
            text=self.call.message.answer.call_args.args[0]
            self.assertIn('FILE-MISSING',text);self.assertIn('تم إرجاع نقاطك',text)
            self.assertNotIn('تعذر معالجة الملف',text)

    async def test_protected_pdf_shows_its_own_reason(self):
        with tempfile.TemporaryDirectory() as d:
            fn,path=self.make_with_file(d)
            self.env['execute_quiz_generation_workflow'].side_effect=InvalidUploadError('PDF protected msg')
            await fn(self.call,self.state)
            self.env['refund_user_on_failure'].assert_awaited_once()
            text=self.call.message.answer.call_args.args[0]
            self.assertIn('PDF protected msg',text);self.assertNotIn('FILE-MISSING',text)

    async def test_generic_error_before_charge_says_no_points_deducted(self):
        fn=self.make();self.env['_current_user'].side_effect=ValueError('db down')
        await fn(self.call,self.state)
        self.assertIn('لم يتم خصم',self.call.message.answer.call_args.args[0])

    async def test_generic_error_after_charge_says_refunded(self):
        fn=self.make();self.env['execute_quiz_generation_workflow'].side_effect=RuntimeError('boom')
        await fn(self.call,self.state)
        self.assertIn('تم إرجاع نقاطك',self.call.message.answer.call_args.args[0])

    async def test_error_after_successful_generation_not_reported_as_refund(self):
        fn=self.make()
        async def broken(*a,**k):raise RuntimeError('telegram send failed')
        broken.__wrapped__ = broken
        self.env['start_loaded_quiz']=broken
        await fn(self.call,self.state)
        self.env['refund_user_on_failure'].assert_not_awaited()
        self.assertIn('تم توليد الكويز',self.call.message.answer.call_args.args[0])


# الاختبارات الموروثة من DebitTests لا تُعاد تشغيلها ضمن UploadRetentionTests
for _name in [n for n in dir(DebitTests) if n.startswith('test_')]:
    setattr(UploadRetentionTests,_name,None)


class QuizIDTests(unittest.IsolatedAsyncioTestCase):
    async def test_concurrent_generations_use_their_insert_return_id(self):
        def spawn(coro):coro.close()
        arrived=0;ready=asyncio.Event()
        async def save(**kwargs):
            nonlocal arrived
            arrived+=1
            if arrived==2:ready.set()
            await ready.wait()
            return 'quiz-'+str(kwargs['creator_id'])
        env=load_functions('services/quiz_service.py',{'execute_quiz_generation_workflow','normalize_quiz_option_count'},
            asyncio=asyncio,os=os,OPTION_COUNT=4,_HEAVY_PROCESSING_SEMAPHORE=asyncio.Semaphore(2),
            get_file_quizzes=AsyncMock(return_value=[]), SUBJECT_OTHER='other',SUBJECT_QUIZ_SOLVED='solved',SUBJECT_QUIZ_UNSOLVED='unsolved',
            SUBJECT_MATH='math',SUBJECT_ENGLISH='english',SUBJECT_FRENCH='french',QUESTION_TYPE_GENERAL='general',QUESTION_TYPE_SIMILAR_MATH='similar_math',DIFFICULTY_MEDIUM='medium',
            resolve_generation_question_type_text=lambda *a:None,resolve_quiz_extraction_instruction=lambda *a:None,
            generate_quiz_smart=AsyncMock(return_value=[{'question':'ok','options':['a','b','c','d','e'],'correct_option_id':4}]),get_last_generation_metadata=lambda:None,
            determine_execution_mode=lambda *args, **kw: 'Standard',
            log_ai_generation=AsyncMock(),spawn=spawn,shuffle_quiz_options=lambda x:x,save_file_quiz_multiple=save,build_question_type_label=lambda *a,**k:'label')
        results=await asyncio.gather(*(env['execute_quiz_generation_workflow'](user,{'input_type':'text','file_hash':'same','pure_text':'text','subject_type':'solved'},5,None) for user in [1,2]))
        self.assertEqual([r[1] for r in results],['quiz-1','quiz-2'])
        self.assertEqual(results[0][0][0]['options'], ['a','b','c','d','e'])
        self.assertEqual(results[0][0][0]['correct_option_id'], 4)
        self.assertEqual(env['get_file_quizzes'].await_count,2)  # No post-insert ID guessing.



    async def test_ready_made_fallback_applies_to_unsolved_but_not_other_quizzes(self):
        def spawn(coro): coro.close()
        for subject_type, expected_options in (('unsolved', ['a', 'b', 'c', 'd', 'e']),
                                               ('other', ['a', 'b', 'c', 'd', 'e'])):
            with self.subTest(subject_type=subject_type):
                generated = [{'question': 'Q', 'options': ['a', 'b', 'c', 'd', 'e'],
                              'correct_option_id': 4}]
                env = load_functions(
                    'services/quiz_service.py',
                    {'execute_quiz_generation_workflow', 'normalize_quiz_option_count'},
                    asyncio=asyncio, os=os, OPTION_COUNT=4,
                    _HEAVY_PROCESSING_SEMAPHORE=asyncio.Semaphore(1),
                    SUBJECT_OTHER='other', SUBJECT_QUIZ_SOLVED='solved',
                    SUBJECT_QUIZ_UNSOLVED='unsolved', SUBJECT_MATH='math',
                    SUBJECT_ENGLISH='english', SUBJECT_FRENCH='french',
                    QUESTION_TYPE_GENERAL='general',QUESTION_TYPE_SIMILAR_MATH='similar_math', DIFFICULTY_MEDIUM='medium',
                    resolve_generation_question_type_text=lambda *args: None,
                    resolve_quiz_extraction_instruction=lambda subject, mode: 'convert' if subject == 'unsolved' else None,
                    MAX_QUESTIONS_TO_GENERATE=120,
                    generate_quiz_smart=AsyncMock(return_value=generated),
                    determine_execution_mode=lambda *args, **kw: 'Standard',
                    get_last_generation_metadata=lambda: None, spawn=spawn,
                    log_ai_generation=AsyncMock(), shuffle_quiz_options=lambda data: data,
                )
                quiz, _, error, _ = await env['execute_quiz_generation_workflow'](
                    1, {'input_type': 'text', 'pure_text': 'source',
                        'subject_type': subject_type}, 1, None
                )
                self.assertEqual(error, '')
                self.assertEqual(quiz[0]['options'], expected_options)
                self.assertEqual(env['generate_quiz_smart'].await_args.kwargs['count'],
                                 1)
                self.assertEqual(quiz[0]['options'][quiz[0]['correct_option_id']], 'e')


class QuizOptionNormalizationTests(unittest.TestCase):
    """Ready-made exams preserve every original choice and reject malformed answers."""

    def setUp(self):
        self.normalize = load_functions(
            'services/quiz_service.py', {'normalize_quiz_option_count'}, OPTION_COUNT=4
        )['normalize_quiz_option_count']

    def test_fifth_wrong_option_is_preserved(self):
        question = {'question': 'Q', 'options': ['A', 'B', 'C', 'D', 'E'],
                    'correct_option_id': 1, 'hint': 'original hint'}
        result = self.normalize([question])
        self.assertEqual(result[0]['options'], ['A', 'B', 'C', 'D', 'E'])
        self.assertEqual(result[0]['correct_option_id'], 1)
        self.assertEqual(result[0]['hint'], 'original hint')

    def test_fifth_correct_option_is_preserved(self):
        result = self.normalize([{'options': ['A', 'B', 'C', 'D', 'E'],
                                  'correct_option_id': 4}])[0]
        self.assertEqual(result['options'], ['A', 'B', 'C', 'D', 'E'])
        self.assertEqual(result['correct_option_id'], 4)

    def test_correct_answer_survives_subsequent_shuffle(self):
        shuffle = load_functions(
            'services/quiz_service.py', {'_shuffle_question_options'},
            random=types.SimpleNamespace(shuffle=lambda indices: indices.reverse())
        )['_shuffle_question_options']
        question = {'options': ['A', 'B', 'C', 'D', 'E'], 'correct_option_id': 4}
        self.normalize([question])
        shuffle(question)
        self.assertEqual(len(question['options']), 5)
        self.assertEqual(question['options'][question['correct_option_id']], 'E')

    def test_more_than_five_options_still_keep_correct_answer(self):
        result = self.normalize([{'options': ['A', 'B', 'C', 'D', 'E', 'F'],
                                  'correct_option_id': 5}])[0]
        self.assertEqual(result['options'], ['A', 'B', 'C', 'D', 'E', 'F'])
        self.assertEqual(result['correct_option_id'], 5)

    def test_four_options_remain_unchanged_and_are_idempotent(self):
        question = {'options': ['A', 'B', 'C', 'D'], 'correct_option_id': 3}
        self.assertIs(self.normalize([question])[0], question)
        self.assertEqual(self.normalize([question])[0], question)

    def test_reject_too_few_choices_or_unreliable_answer_index(self):
        invalid_questions = [
            {'options': ['A'], 'correct_option_id': 0},
            {'options': ['A', 'B', 'C', 'D', 'E'], 'correct_option_id': 5},
            {'options': ['A', '', 'C', 'D'], 'correct_option_id': 1},
            {'options': ['A', 'B', 'C', 'D'], 'correct_option_id': None},
        ]
        for question in invalid_questions:
            with self.subTest(question=question):
                with self.assertRaises(ValueError):
                    self.normalize([question])


class WebAudioTests(unittest.IsolatedAsyncioTestCase):
    async def run_audio(self, seconds):
        import uuid
        state=State();state.status=None
        bot=types.SimpleNamespace(send_message=AsyncMock(return_value=types.SimpleNamespace(edit_text=AsyncMock())))
        with tempfile.TemporaryDirectory() as directory:
            async def download(obj,dest,**kw):
                with open(dest,'wb') as target:target.write(b'test audio')
                return True
            env=load_functions('handlers/audio.py',{'process_web_uploaded_audio'}, offer_transition=AsyncMock(return_value=False),
                asyncio=asyncio,os=os,uuid=uuid,build_state_for_chat=lambda *a:state,
                QuizState=types.SimpleNamespace(answering_quiz='answering',processing_audio='audio',processing_file_quiz='quiz',processing_web_file='file',waiting_for_audio_confirm='confirm'),
                bot=bot,ensure_directory_exists=lambda *a:None,DOWNLOADS_DIR=directory,MAX_AUDIO_WEB_UPLOAD_SIZE=250,
                download_audio_temp_to_file=download,evaluate_audio_cost=AsyncMock(return_value=types.SimpleNamespace(probed_seconds=seconds,duration_minutes=1,exceeds_cap=False,cost=1)),
                safe_file_cleanup=lambda p:os.remove(p) if os.path.exists(p) else None,delete_audio_temp=AsyncMock(),
                logger=Mock(),log_error=Mock(),log_info=Mock(),get_or_create_audio_user=AsyncMock(return_value={'points':10}),
                compute_balance_and_deficit=lambda *a:(10,0),get_audio_confirm_keyboard=lambda:None,
                get_safe_mime_type=lambda p:'audio/mpeg',log_audio_uploaded_event=AsyncMock(),
                spawn=lambda coro:coro.close(),
                MSG_AUDIO_CONFIRM_TEMPLATE='{duration_minutes} {cost} {balance}')
            await env['process_web_uploaded_audio'](1,1,'1/audio.mp3','audio.mp3')
            if seconds is None:
                self.assertEqual(os.listdir(directory),[])
                env['delete_audio_temp'].assert_awaited()
                self.assertNotIn('pending_audio_cost',state.data)
            else:
                self.assertEqual(state.status,'confirm')
                self.assertEqual(state.data['pending_audio_cost'],1)

    async def test_unprobeable_web_audio_rejected_and_cleaned(self):await self.run_audio(None)
    async def test_normal_web_audio_reaches_confirmation(self):await self.run_audio(60)


class AudioRefundTests(unittest.IsolatedAsyncioTestCase):
    async def test_audio_refund_not_repeated_after_failure_message_error(self):
        import time
        with tempfile.TemporaryDirectory() as d:
            destination=d+'/audio.mp3'
            with open(destination,'wb') as f:f.write(b'audio')
            state=State({'pending_audio_path':destination,'pending_audio_cost':2})
            edits=AsyncMock(side_effect=[None,ValueError('Telegram UI failed'),None])
            call=types.SimpleNamespace(from_user=types.SimpleNamespace(id=1),answer=AsyncMock(),message=types.SimpleNamespace(edit_text=edits,answer=AsyncMock()))
            env=load_functions('handlers/audio.py',{'handle_audio_confirm_start'},asyncio=asyncio,os=os,time=time,
                get_or_create_audio_user=AsyncMock(return_value={'points':10}),compute_balance_and_deficit=lambda *a:(10,0),
                update_user_stats=AsyncMock(return_value={'debited_free':1,'debited_paid':1,'debited_gift':0,'debit_id':'audio-1'}),
                spawn=lambda coro:coro.close(),log_usage_event=AsyncMock(),transcribe_audio_lecture=AsyncMock(return_value=None),
                refund_wallet=AsyncMock(return_value=True),refund_user_points_split=AsyncMock(),safe_file_cleanup=lambda p:os.remove(p) if os.path.exists(p) else None,
                logger=Mock(),log_info=Mock(),log_error=Mock())
            await env['handle_audio_confirm_start'](call,state)
            env['refund_wallet'].assert_awaited_once_with(1,'audio-1')

