"""Behavior tests for the actual navigation functions, without external services."""
import asyncio
import ast
import json
import logging
import time
import types
import unittest
import uuid
from pathlib import Path
from unittest.mock import AsyncMock, patch

from reliability_support import load_functions, FakeRedis, HTTPError
from services.redis_reliability import UserLock


class Redis(FakeRedis):
    async def get(self, key):
        return self.values.get(key)
    async def delete(self, key):
        self.values.pop(key, None)
    async def zadd(self, key, values):
        self.values.setdefault(key, {}).update(values)
    async def zrem(self, key, item):
        self.values.get(key, {}).pop(item, None)
    async def zrangebyscore(self, key, low, high, **kw):
        return [k for k, v in self.values.get(key, {}).items() if low <= v <= high]


class State:
    def __init__(self):
        self.status = 'answering_quiz'
        self.data = dict(questions=[{'question': 'Q'}] * 3, current_index=1,
                         score=1, attempt_id='attempt-A', source_title='Test')
    async def get_state(self): return self.status
    async def get_data(self): return dict(self.data)
    async def update_data(self, **kwargs): self.data.update(kwargs)
    async def set_state(self, value): self.status = getattr(value, 'state', value)
    async def clear(self): self.data = {}; self.status = None


class NavigationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.redis = Redis()
        self.state = State()
        names = {n.name for n in ast.parse(Path('handlers/session_navigation.py').read_text()).body
                 if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
        states = ['answering_quiz','waiting_for_custom_name','waiting_for_new_section_title',
                  'waiting_for_question_edit_choice','waiting_for_question_edit_text',
                  'waiting_for_answer_edit_choice','waiting_for_answer_edit_text','searching_favorites']
        self.quiz_states = types.SimpleNamespace(**{x: types.SimpleNamespace(state=x) for x in states})
        self.bot = types.SimpleNamespace(send_message=AsyncMock())
        self.env = load_functions('handlers/session_navigation.py', names,
            redis_client=self.redis, bot=self.bot, QuizState=self.quiz_states, time=time, uuid=uuid,
            json=json, asyncio=asyncio, UserLock=UserLock, PENDING_SECONDS=1800,
            EXPIRY_KEY='expiry', logger=logging.getLogger('navigation-test'),
            types=types.SimpleNamespace(InlineKeyboardButton=lambda **k:k, InlineKeyboardMarkup=lambda **k:k))
        self.stopped = AsyncMock()
        self.send_question = AsyncMock()
        self.cleanup = types.SimpleNamespace(delete_audio_temp=AsyncMock(), delete_file_temp=AsyncMock(), delete_file_temp_batch=AsyncMock())
        self.modules = patch.dict('sys.modules', {
            'handlers.session_navigation': types.SimpleNamespace(**self.env),
            'handlers.quiz_runner': types.SimpleNamespace(send_question_by_ids=self.send_question),
            'helpers.supabase_helper': types.SimpleNamespace(mark_quiz_attempt_stopped=self.stopped),
            'helpers.r2_helper': self.cleanup})
        self.modules.start()
        self.addCleanup(self.modules.stop)
        self.dispatch = AsyncMock()
        self.env['dispatch_action'] = self.dispatch

    async def offer(self, action=None):
        return await self.env['offer_transition'](self.state, 7, 7, action or {'kind':'upload','type':'all'})

    async def call(self, choice, token=None):
        pending = await self.env['read_pending'](7)
        event = types.SimpleNamespace(data=f"sn:{choice}:{token or pending['token']}",
            from_user=types.SimpleNamespace(id=7), answer=AsyncMock(),
            message=types.SimpleNamespace(chat=types.SimpleNamespace(id=7),edit_reply_markup=AsyncMock()))
        await self.env['transition_callback'](event, self.state)
        return event

    async def test_no_quiz_no_warning(self):
        self.state.status=None
        self.state.data["quiz_completed"]=True
        self.assertFalse(await self.offer())
        self.bot.send_message.assert_not_awaited()

    async def test_favorites_search_menu_still_protects_unfinished_quiz(self):
        self.state.status=None
        self.assertTrue(await self.offer())
        self.assertEqual(self.state.data['score'],1)

    async def test_pending_upload_cleanup_after_processing_failure(self):
        original = load_functions('handlers/session_navigation.py', {'dispatch_action'}, types=types,
                                  cleanup_action=self.env['cleanup_action'])['dispatch_action']
        failed = types.SimpleNamespace(__wrapped__=AsyncMock(side_effect=ValueError('download failed')))
        with patch.dict('sys.modules', {
                'handlers.audio': types.SimpleNamespace(process_web_uploaded_audio=failed),
                'handlers.files': types.SimpleNamespace(process_web_uploaded_file=failed,process_web_uploaded_images=failed)}):
            with self.assertRaises(ValueError):
                await original({'kind':'web_file','user_id':7,'chat_id':7,'object_path':'7/file'},None,self.state)
        self.cleanup.delete_file_temp.assert_awaited_once_with('7/file')

    async def test_saved_message_dispatch_preserves_original_sender(self):
        from aiogram import types as telegram_types, Bot
        fake_bot=Bot('123456:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghi')
        original = load_functions('handlers/session_navigation.py', {'dispatch_action'},
            types=telegram_types,bot=fake_bot)['dispatch_action']
        handler=types.SimpleNamespace(__wrapped__=AsyncMock())
        message=telegram_types.Message.model_validate({'message_id':10,'date':1,
            'chat':{'id':7,'type':'private'},'from':{'id':7,'is_bot':False,'first_name':'Student'},
            'text':'A long study text that has more than thirty letters'})
        with patch.dict('sys.modules', {
                'handlers.audio':types.SimpleNamespace(handle_audio_message=handler),
                'handlers.files':types.SimpleNamespace(handle_media=handler,handle_pure_text=handler)}):
            await original({'kind':'text','messages':[message.model_dump(mode='json')]},None,self.state)
        forwarded=handler.__wrapped__.call_args.args[0]
        self.assertEqual(forwarded.from_user.id,7)
        self.assertIs(forwarded.bot,fake_bot)
        await fake_bot.session.close()

    async def test_warning_preserves_progress_and_has_three_choices(self):
        self.assertTrue(await self.offer())
        self.assertEqual(self.state.data['score'],1)
        self.assertEqual(self.state.data['current_index'],1)
        rows=self.bot.send_message.call_args.kwargs['reply_markup']['inline_keyboard']
        self.assertEqual(len(rows),3)

    async def test_resume_reopens_current_question_without_reset(self):
        await self.offer()
        await self.call('resume')
        self.send_question.assert_awaited_once_with(7,7,self.state)
        self.assertEqual(self.state.data['score'],1)
        self.assertIsNone(await self.env['read_pending'](7))
        self.stopped.assert_not_awaited()

    async def test_confirm_stops_then_dispatches_once(self):
        await self.offer()
        token=(await self.env['read_pending'](7))['token']
        await self.call('replace',token)
        await self.call('replace',token)
        self.stopped.assert_awaited_once_with('attempt-A')
        self.dispatch.assert_awaited_once()

    async def test_old_prompt_cannot_replace_newer_quiz(self):
        await self.offer()
        self.state.data['attempt_id']='attempt-B'
        await self.call('replace')
        self.assertEqual(self.state.data['attempt_id'],'attempt-B')
        self.stopped.assert_not_awaited()
        self.dispatch.assert_not_awaited()

    async def test_expired_request_cannot_stop_quiz(self):
        await self.offer()
        p=await self.env['read_pending'](7);p['expires']=0
        await self.redis.set(self.env['pending_key'](7),json.dumps(p))
        await self.call('replace')
        self.stopped.assert_not_awaited()
        self.dispatch.assert_not_awaited()

    async def test_new_request_invalidates_old_button(self):
        await self.offer()
        token=(await self.env['read_pending'](7))['token']
        await self.offer({'kind':'stop'})
        await self.call('replace',token)
        self.dispatch.assert_not_awaited()
        self.assertEqual((await self.env['read_pending'](7))['action']['kind'],'stop')

    async def test_album_collects_all_messages_without_duplicate_prompt(self):
        for i in [1,2,2,3]:
            await self.offer({'kind':'media','album':'group','messages':[{'message_id':i}]})
        p=await self.env['read_pending'](7)
        self.assertEqual(len(p['action']['messages']),3)
        self.bot.send_message.assert_awaited_once()

    async def test_late_web_conflict_keeps_upload_until_cancel(self):
        await self.offer({'kind':'web_file','object_path':'7/file'})
        self.cleanup.delete_file_temp.assert_not_awaited()
        await self.call('cancel')
        self.cleanup.delete_file_temp.assert_awaited_once_with('7/file')

    async def test_web_confirmation_does_not_delete_file_before_processing(self):
        await self.offer({'kind':'web_audio','object_path':'7/audio'})
        await self.call('replace')
        self.cleanup.delete_audio_temp.assert_not_awaited()
        self.dispatch.assert_awaited_once()

    async def test_new_processing_flow_not_cleared_by_old_confirmation(self):
        await self.offer()
        self.state.status='processing_file_quiz'
        await self.call('replace')
        self.assertEqual(self.state.status,'processing_file_quiz')
        self.dispatch.assert_not_awaited()

    async def test_finished_quiz_can_proceed_without_stopping_again(self):
        await self.offer()
        self.state.status=None;self.state.data['quiz_completed']=True
        await self.call('replace')
        self.stopped.assert_not_awaited()
        self.dispatch.assert_awaited_once()

    async def test_preflight_blocks_before_presigning(self):
        env=load_functions('webhook_server.py',{'_ensure_upload_session_available'},
            redis_client=self.redis,HTTPException=HTTPError)
        with patch.dict('sys.modules',{'services.flow_state':types.SimpleNamespace(build_state_for_chat=lambda *a:self.state)}):
            with self.assertRaises(HTTPError) as cm:
                await env['_ensure_upload_session_available'](7,'document')
        self.assertEqual(cm.exception.status_code,409)
        self.assertEqual((await self.env['read_pending'](7))['action']['type'],'document')

    async def test_all_init_endpoints_check_before_upload_target(self):
        module=ast.parse(Path('webhook_server.py').read_text())
        for name in ('audio_upload_init','file_upload_init','image_upload_init'):
            fn=next(n for n in module.body if isinstance(n,ast.AsyncFunctionDef) and n.name==name)
            calls=[n for n in ast.walk(fn) if isinstance(n,ast.Call) and isinstance(n.func,ast.Name)]
            check=next(n.lineno for n in calls if n.func.id=='_ensure_upload_session_available')
            target=next(n.lineno for n in calls if n.func.id.startswith('create_') and 'upload' in n.func.id)
            self.assertLess(check,target)

    async def test_duplicate_and_old_poll_answers_do_not_change_score(self):
        env=load_functions('handlers/quiz_runner.py',{'handle_poll_answer'},
            redis_client=self.redis,json=json,log_error=lambda *a,**kw:None,logger=logging.getLogger('test'))
        poll=types.SimpleNamespace(poll_id='p',user=types.SimpleNamespace(id=7),option_ids=[0])
        info=dict(user_id=7,correct_option_id=0,session_id='attempt-A',question_index=1)
        await self.redis.set('poll:p',json.dumps(info))
        await env['handle_poll_answer'](poll,self.state)
        await env['handle_poll_answer'](poll,self.state)
        self.assertEqual(self.state.data['score'],2)
        self.state.data['attempt_id']='attempt-B'
        await env['handle_poll_answer'](poll,self.state)
        self.assertEqual(self.state.data['score'],2)

    async def test_expiry_sweeper_cleans_abandoned_web_upload(self):
        await self.offer({'kind':'web_images','object_paths':['7/a','7/b']})
        p=await self.env['read_pending'](7);p['expires']=0
        await self.redis.set(self.env['pending_key'](7),json.dumps(p))
        await self.redis.zadd('expiry',{'7':0})
        self.env['asyncio']=types.SimpleNamespace(sleep=AsyncMock(side_effect=asyncio.CancelledError))
        with self.assertRaises(asyncio.CancelledError):
            await self.env['pending_cleanup_loop']()
        self.cleanup.delete_file_temp_batch.assert_awaited_once_with(['7/a','7/b'])
        self.assertIsNone(await self.env['read_pending'](7))


if __name__=='__main__': unittest.main()
