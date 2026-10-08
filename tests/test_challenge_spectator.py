"""3D integration tests: production functions, real aiogram models, fake transport/RPC.
SQL authorization and non-mutation are executed separately in run_challenge_spectator.mjs.
Redis scripts execute in fakeredis' Lua engine (tests/requirements-spectator.txt).
"""
import asyncio
import copy
import datetime as dt
import json
import logging
import sys
import unittest
from collections import OrderedDict
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock
from uuid import UUID
from weakref import WeakValueDictionary
from aiogram import types
from aiogram.exceptions import TelegramBadRequest
from helpers.challenge_delivery import deliver_screen
from services.challenge_spectator_presence import Presence, TTL, INDEX
from services.challenge_spectator_ui import screen
from services.challenge_live_ui import Btn
from services.challenge_presentation import PhotoText, source_image, image_option_label
sys.path.insert(0,str(Path(__file__).parent))
from reliability_support import load_functions
try:
    from fakeredis.aioredis import FakeRedis
except ImportError:
    FakeRedis = None

MID='11111111-1111-1111-1111-111111111111'
OTHER='22222222-2222-2222-2222-222222222222'
IMAGE='https://example.test/source.png'


def state(**kw):
    return dict(id=MID,status='in_progress',index=0,total=3,
        labels=dict(attacker='A',subject='رياضيات',unit='اشتقاق'),
        question=dict(text='نص السؤال',options=['خيار ب','خيار أ'],image_url=None),
        server_now='2026-10-07T12:00:00+00:00',deadline='2026-10-07T12:00:20+00:00',
        score=0,correct=0,resolved=0,previous=None,result=None,**kw)


@unittest.skipIf(FakeRedis is None,'install tests/requirements-spectator.txt')
class PresenceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.redis=FakeRedis();self.p=Presence(self.redis)
    async def asyncTearDown(self): await self.redis.aclose()
    async def test_join_duplicate_and_count(self):
        await self.p.activity(MID,2,10);await self.p.activity(MID,2,10)
        self.assertEqual(await self.p.count(MID),1)
    async def test_supports_multiple_members(self):
        await self.p.activity(MID,2,10);await self.p.activity(MID,3,11)
        self.assertEqual(await self.p.count(MID),2)
    async def test_refresh_renews_user_activity(self):
        await self.p.activity(MID,2,10)
        await self.redis.zadd(self.p.keys(MID)[0],{'2':1})
        await self.p.activity(MID,2,10)
        self.assertEqual(await self.p.count(MID),1)
    async def test_expiry_prunes_member_and_reference(self):
        await self.p.activity(MID,2,10)
        await self.redis.zadd(self.p.keys(MID)[0],{'2':1})
        self.assertEqual(await self.p.count(MID),0)
        self.assertEqual(await self.redis.hlen(self.p.keys(MID)[1]),0)
    async def test_leave_idempotent_and_isolated(self):
        await self.p.activity(MID,2,10)
        await self.p.leave(MID,3);self.assertEqual(await self.p.count(MID),1)
        await self.p.leave(MID,2);await self.p.leave(MID,2)
        self.assertEqual(await self.p.count(MID),0)
    async def test_delivery_does_not_renew_score_or_ttl(self):
        ref=await self.p.activity(MID,2,10)
        score=await self.redis.zscore(self.p.keys(MID)[0],'2')
        await self.redis.expire(self.p.keys(MID)[0],20)
        await self.p.reference(MID,2,ref,'{"message_id":11}')
        self.assertEqual(await self.redis.zscore(self.p.keys(MID)[0],'2'),score)
        self.assertLessEqual(await self.redis.ttl(self.p.keys(MID)[0]),20)
    async def test_cas_cannot_resurrect_leave(self):
        ref=await self.p.activity(MID,2,10);await self.p.leave(MID,2)
        await self.p.reference(MID,2,ref,'bad')
        self.assertEqual(await self.p.count(MID),0)
    async def test_stale_delivery_cannot_replace_new_session(self):
        old=await self.p.activity(MID,2,10);new=await self.p.activity(MID,2,12)
        await self.p.reference(MID,2,old,'bad')
        self.assertEqual((await self.p.viewers(MID))[2],new)
    async def test_reset_and_rejoin(self):
        await self.p.activity(MID,2,10);await self.redis.flushdb()
        self.assertEqual(await self.p.count(MID),0)
        await self.p.activity(MID,2,11);self.assertEqual(await self.p.count(MID),1)
    async def test_completion_cleanup(self):
        await self.p.activity(MID,2,10);await self.p.clear(MID)
        self.assertEqual(await self.p.batch(),[])
        self.assertEqual(await self.p.count(MID),0)
    async def test_no_unbounded_keys_and_stale_index_pruning(self):
        await self.p.activity(MID,2,10)
        for key in await self.redis.keys('*'): self.assertGreater(await self.redis.ttl(key),0)
        await self.redis.zadd(INDEX,{OTHER:1})
        await self.p.activity(MID,2,10)
        self.assertEqual(await self.p.batch(),[MID])
    async def test_bounded_batches_rotate(self):
        for i in range(101): await self.p.activity(str(UUID(int=i+1)),2,10)
        pages=[await self.p.batch() for _ in range(3)]
        self.assertEqual(list(map(len,pages)),[50,50,1])
        self.assertEqual(len(set(sum(pages,[]))),101)
    async def test_unavailable_and_hung_redis_are_bounded(self):
        bad=NS(eval=AsyncMock(side_effect=ConnectionError))
        self.assertIsNone(await Presence(bad).count(MID))
        self.assertEqual(await Presence(bad).batch(),[])
        async def hang(*a): await asyncio.Event().wait()
        self.assertIsNone(await Presence(NS(eval=hang)).activity(MID,2,10))


class ScreenTests(unittest.TestCase):
    def test_text_order_no_answer_callbacks_or_secret_fields(self):
        s=state();s['question'].update(correct_option_id='SECRET',explanation='SECRET')
        s['snapshot']='SECRET';s['option_id']='SECRET'
        text,rows=screen(s,1)
        self.assertNotIn('SECRET',text)
        self.assertIn('نص السؤال',text)
        self.assertIn('خيار ب',text)
        self.assertEqual([b.text for b in rows[0]],['ب','أ'])
        self.assertTrue(all(b.data.startswith('sp:') for r in rows for b in r))
        self.assertTrue(all(len(b.data.encode())<=64 for r in rows for b in r))
    def test_math_source_image_and_mixed_text(self):
        s=state();s['question'].update(image_url=IMAGE,is_math=True,table={'rows':[[1]]},matrices=[])
        s['question']['options']=['أول','ثاني','ثالث','رابع']
        text,rows=screen(s,1)
        self.assertIsInstance(text,PhotoText);self.assertEqual(text.image_url,IMAGE)
        self.assertNotIn('نص السؤال',text)
        self.assertEqual([len(row) for row in rows[:2]],[2,2])
        self.assertEqual([b.text for row in rows[:2] for b in row],
                         ['ب','أ','د','ج'])
        self.assertTrue(all(b.data=='sp:readonly' for row in rows[:2] for b in row))
    def test_long_media_stays_compact_without_duplicate_pages(self):
        s=state();s['question'].update(text='ن'*3500,image_url=IMAGE,options=['أ'*1000,'ب'*1000])
        for page in range(20):
            text,rows=screen(s,1,page);self.assertLessEqual(len(text),1024)
            self.assertEqual(text.image_url,IMAGE)
            self.assertFalse(any(b.data.startswith('sp:p:') for r in rows for b in r))
    def test_previous_is_distinguished_from_current(self):
        s=state();s.update(index=1,previous=dict(index=0,option_id=1,correct_option_id=2,is_correct=False,timed_out=False,score=0))
        text,_=screen(s,1);self.assertIn('السؤال السابق 1',text);self.assertIn('السؤال 2 / 3',text)
        self.assertIn('اختار المهاجم: ب — الإجابة الصحيحة: ج',text)
        s['previous'].update(timed_out=True,option_id=None)
        self.assertIn('اختار المهاجم: لم يجب — الإجابة الصحيحة: ج',screen(s,1)[0])
    def test_terminal_statuses_rewards_and_defense(self):
        for status in ['completed','expired','abandoned']:
            s=state();s.update(status=status,result=dict(correct=2,score=190,stars=2,attacker_won=True,attacker_xp=20,attacker_trophies=10))
            text,rows=screen(s);self.assertIn('خسر دفاع أرضك',text);self.assertIn('20 XP',text)
            self.assertFalse(any(b.data.startswith('sp:r') for r in rows for b in r))
    def test_presence_unavailable_fallback(self):
        self.assertIn('غير متاح',screen(state(),None)[0])
    def test_attacker_count_preserves_image_and_answer_order(self):
        f=load_functions('handlers/challenge_game.py',['button','attack_screen'],UUID=UUID,dt=dt,types=types,
                         PhotoText=PhotoText,source_image=source_image,image_option_label=image_option_label)
        s=state();s['question']['image_url']=IMAGE;s['viewer_count']=1
        text,rows=f['attack_screen'](s)
        self.assertIn('👁',text);self.assertEqual(text.image_url,IMAGE)
        self.assertTrue(rows[0][0].callback_data.endswith(':0:0'))


@unittest.skipIf(FakeRedis is None,'install tests/requirements-spectator.txt')
class HandlerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.redis=FakeRedis();self.p=Presence(self.redis);self.s=state()
        async def get(mid,uid):
            if mid!=MID or uid!=2: raise ValueError('spectator_denied')
            return copy.deepcopy(self.s)
        self.repo=NS(state=AsyncMock(side_effect=get),incoming=AsyncMock(return_value=[]))
        self.bot=NS(edit_message_text=AsyncMock(),edit_message_media=AsyncMock(),
                    send_message=AsyncMock(return_value=NS(message_id=55)),send_photo=AsyncMock(return_value=NS(message_id=56)),
                    edit_message_reply_markup=AsyncMock())
        names=['markup','send','notification_markup','incoming','present','notify','beat','callback']
        self.e=load_functions('handlers/challenge_spectator.py',names,asyncio=asyncio,json=json,UUID=UUID,
            types=types,TelegramBadRequest=TelegramBadRequest,repo=self.repo,presence=self.p,screen=screen,Btn=Btn,
            deliver_screen=deliver_screen,is_feature_enabled=AsyncMock(return_value=True),
            _locks=WeakValueDictionary(),_cache=OrderedDict(),_slots=asyncio.Semaphore(4),_pending=OrderedDict(),
            logger=logging.getLogger('spectator-test'),update_attacker=AsyncMock())
    async def asyncTearDown(self): await self.redis.aclose()
    def cb(self,action='o',uid=2,extra=''):
        return NS(data=f'sp:{action}:{UUID(MID).hex}{extra}',from_user=NS(id=uid),bot=self.bot,
                  message=NS(message_id=10),answer=AsyncMock())
    async def open(self): await self.e['callback'](self.cb())
    async def test_option_buttons_only_show_notice_for_any_actor(self):
        before=copy.deepcopy(self.s)
        for uid in [1,2,3]:
            c=self.cb(uid=uid);c.data='sp:readonly'
            await self.e['callback'](c)
            c.answer.assert_awaited_once_with('لا تستطيع المشاركة في وضعية المشاهدة',show_alert=True)
        self.repo.state.assert_not_awaited()
        self.repo.incoming.assert_not_awaited()
        self.e['update_attacker'].assert_not_awaited()
        self.bot.send_message.assert_not_awaited()
        self.bot.edit_message_text.assert_not_awaited()
        self.assertEqual(await self.redis.keys('*'),[])
        self.assertEqual(self.s,before)
    async def test_authorized_open_and_refresh(self):
        await self.open();self.repo.state.assert_awaited_with(MID,2)
        self.assertEqual(await self.p.count(MID),1)
        await self.e['callback'](self.cb('r'))
        self.assertEqual(await self.p.count(MID),1)
        self.e['update_attacker'].assert_awaited_with(self.bot,MID)
    async def test_stranger_and_attacker_forgery_rejected(self):
        for uid in [1,3]: await self.e['callback'](self.cb(uid=uid))
        self.assertEqual(await self.p.count(MID),0)
        self.assertTrue(all('لا يمكنك' in c.args[0] for c in self.bot.edit_message_text.await_args_list))
    async def test_callback_cannot_override_actor(self):
        await self.e['callback'](self.cb(uid=3,extra=':2'))
        self.repo.state.assert_not_awaited();self.assertEqual(await self.p.count(MID),0)
    async def test_flag_off_no_session(self):
        self.repo.state.side_effect=ValueError('spectator_disabled')
        await self.open();self.assertEqual(await self.p.count(MID),0)
    async def test_completed_open_does_not_join(self):
        self.s.update(status='completed',result={})
        await self.open();self.assertEqual(await self.p.count(MID),0)
    async def test_stop_idempotent_and_no_db_write(self):
        await self.open();self.repo.state.reset_mock()
        await self.e['callback'](self.cb('x'));await self.e['callback'](self.cb('x'))
        self.assertEqual(await self.p.count(MID),0);self.repo.state.assert_awaited_with(MID,2)
    async def test_event_push_and_no_per_second_telegram_edit(self):
        await self.open();self.bot.edit_message_text.reset_mock()
        await self.e['beat'](self.bot);await self.e['beat'](self.bot)
        self.bot.edit_message_text.assert_not_awaited()
        self.s.update(index=1,score=98,correct=1,previous=dict(index=0,is_correct=True,timed_out=False,score=98))
        await self.e['notify'](self.bot,MID)
        self.bot.edit_message_text.assert_awaited_once()
    async def test_worker_never_renews_presence(self):
        await self.open();key=self.p.keys(MID)[0];stamp=await self.redis.zscore(key,'2')
        self.s['index']=1;await self.e['notify'](self.bot,MID)
        self.assertEqual(await self.redis.zscore(key,'2'),stamp)
    async def test_completion_cleanup(self):
        await self.open();self.s.update(status='completed',result={})
        await self.e['notify'](self.bot,MID)
        self.assertEqual(await self.p.count(MID),0);self.assertEqual(await self.p.batch(),[])
    async def test_restart_uses_message_reference_and_reset_can_reopen(self):
        await self.open();self.e['_cache'].clear()
        await self.e['beat'](self.bot)
        self.assertEqual(self.bot.edit_message_text.await_args.kwargs['message_id'],55)
        await self.redis.flushdb();await self.open();self.assertEqual(await self.p.count(MID),1)
    async def test_deleted_message_falls_back_and_saves_reference(self):
        await self.open()
        self.bot.edit_message_text.side_effect=TelegramBadRequest(method=None,message='message to edit not found')
        await self.e['callback'](self.cb('r'))
        self.assertEqual(json.loads((await self.p.viewers(MID))[2])['message_id'],55)
        self.assertEqual(self.bot.send_message.await_count,2)
    async def test_image_and_mixed_transition_delivery(self):
        self.s['question']['image_url']=IMAGE;await self.open()
        self.bot.send_photo.assert_awaited_once()
        self.s['question']['image_url']=None;self.s['index']=1
        self.bot.edit_message_text.side_effect=TelegramBadRequest(method=None,message='there is no text')
        await self.e['notify'](self.bot,MID);self.bot.send_message.assert_awaited_once()
    async def test_redis_failure_still_displays_safe_state(self):
        self.e['presence']=Presence(NS(eval=AsyncMock(side_effect=ConnectionError)))
        await self.open();self.bot.send_message.assert_awaited_once()
        self.assertIn('غير متاح',self.bot.send_message.await_args.args[1])
    async def test_notification_button_only_in_progress_and_enabled(self):
        notice={'kind':'attack_started','match_id':MID}
        self.assertIsNotNone(await self.e['notification_markup'](notice,{'status':'in_progress'}))
        self.assertIsNone(await self.e['notification_markup'](notice,{'status':'completed'}))
        self.e['is_feature_enabled'].return_value=False
        self.assertIsNone(await self.e['notification_markup'](notice,{'status':'in_progress'}))
        notice['kind']='attack_completed';self.e['is_feature_enabled'].return_value=True
        self.assertIsNone(await self.e['notification_markup'](notice,{'status':'in_progress'}))
    async def test_incoming_multiple_and_pagination(self):
        self.repo.incoming.return_value=[dict(id=str(UUID(int=i+1)),attacker='A',subject='S') for i in range(6)]
        await self.e['incoming'](self.bot,2,10)
        rows=self.bot.edit_message_text.await_args.kwargs['reply_markup'].inline_keyboard
        self.assertEqual(sum(b.callback_data.startswith('sp:o:') for r in rows for b in r),5)
        self.assertTrue(any(b.callback_data=='sp:l:1' for r in rows for b in r))
    async def test_expired_session_clears_count_without_new_heartbeat(self):
        await self.open();await self.redis.zadd(self.p.keys(MID)[0],{'2':1})
        await self.e['beat'](self.bot)
        self.assertEqual(await self.p.count(MID),0)
        self.e['update_attacker'].assert_awaited_with(self.bot,MID)

    async def test_forged_stop_rejected_and_defender_retained(self):
        await self.open();await self.e['callback'](self.cb('x',uid=3))
        self.assertEqual(await self.p.count(MID),1)
        self.assertIn('لا يمكنك',self.bot.edit_message_text.await_args.args[0])
    async def test_stop_allowed_when_flag_off(self):
        await self.open();self.repo.state.side_effect=ValueError('spectator_disabled')
        await self.e['callback'](self.cb('x'))
        self.assertEqual(await self.p.count(MID),0)
    async def test_final_delivery_failure_retried_then_cleaned(self):
        await self.open();self.s.update(status='completed',result={})
        self.bot.edit_message_text.side_effect=ConnectionError()
        await self.e['notify'](self.bot,MID);self.assertEqual(await self.p.count(MID),1)
        self.bot.edit_message_text.side_effect=None
        await self.e['beat'](self.bot);self.assertEqual(await self.p.count(MID),0)
    async def test_cleanup_cannot_delete_newly_joined_viewer(self):
        await self.open();await self.p.clear_empty(MID)
        self.assertEqual(await self.p.count(MID),1)
    async def test_projection_refetched_under_lock_and_stop_wins_after_delivery(self):
        await self.open()
        started=asyncio.Event();release=asyncio.Event()
        async def slow(*a,**kw): started.set();await release.wait()
        self.bot.edit_message_text.side_effect=slow
        self.s['index']=1
        task=asyncio.create_task(self.e['notify'](self.bot,MID))
        await started.wait()
        stop=asyncio.create_task(self.e['callback'](self.cb('x')))
        release.set();await task;await stop
        self.assertEqual(await self.p.count(MID),0)
        self.assertIn('تم إيقاف',self.bot.edit_message_text.await_args.args[0])


class AttackIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_redis_down_does_not_prevent_attack_delivery_or_change_state(self):
        from unittest.mock import patch
        s=state();s['attacker_id']=1;s['message_id']=10
        original=copy.deepcopy(s)
        presence=Presence(NS(eval=AsyncMock(side_effect=ConnectionError)))
        repo=NS(attach_message=AsyncMock())
        deliver=AsyncMock(return_value=10)
        env=load_functions('handlers/challenge_game.py',['button','keyboard','attack_screen','present_attack'],
            UUID=UUID,dt=dt,types=types,PhotoText=PhotoText,source_image=source_image,
            image_option_label=image_option_label,asyncio=asyncio,_attack_locks=WeakValueDictionary(),
            _attack_views=OrderedDict(),repo=repo,deliver_screen=deliver)
        with patch.dict(sys.modules,{'handlers.challenge_spectator':NS(presence=presence)}):
            await env['present_attack'](None,s,10)
        deliver.assert_awaited_once();repo.attach_message.assert_awaited_with(MID,1,10)
        self.assertEqual(s,original)
        self.assertNotIn('👁',deliver.await_args.args[3])

    async def test_attacker_stale_render_rejected_and_presence_preserves_option_page(self):
        from unittest.mock import patch
        s=state();s['attacker_id']=1;s['message_id']=10;s['question']['options']=['a','b','c','d']
        presence=NS(count=AsyncMock(return_value=1))
        deliver=AsyncMock(return_value=10)
        env=load_functions('handlers/challenge_game.py',['button','keyboard','attack_screen','present_attack'],
            UUID=UUID,dt=dt,types=types,PhotoText=PhotoText,source_image=source_image,
            image_option_label=image_option_label,asyncio=asyncio,_attack_locks=WeakValueDictionary(),
            _attack_views=OrderedDict(),repo=NS(attach_message=AsyncMock()),deliver_screen=deliver)
        with patch.dict(sys.modules,{'handlers.challenge_spectator':NS(presence=presence)}):
            await env['present_attack'](None,s,10,1)
            old=dict(s,server_now='2026-10-07T11:59:59+00:00')
            await env['present_attack'](None,old,10,0)
            self.assertEqual(deliver.await_count,1)
            presence.count.return_value=0
            await env['present_attack'](None,s,10,None)
        self.assertIn('3) c',deliver.await_args.args[3]);self.assertNotIn('1) a',deliver.await_args.args[3])

    async def test_land_recovers_incoming_without_notification_or_memory(self):
        from unittest.mock import patch
        p=dict(branch_id='branch',xp=0,trophies=0,notifications_enabled=True)
        incoming=AsyncMock(return_value=[{'id':MID},{'id':OTHER}])
        render=AsyncMock()
        env=load_functions('handlers/challenge_game.py',['button','home'],
            types=types,UUID=UUID,identity=AsyncMock(return_value=p),repo=NS(towers=AsyncMock(return_value=[]),active_attack=AsyncMock(return_value=None)),
            live_repo=NS(queue_state=AsyncMock(return_value={})),load_rules=lambda:{},EMPTY='empty',
            is_feature_enabled=AsyncMock(return_value=False),render=render)
        with patch.dict(sys.modules,{'handlers.challenge_spectator':NS(repo=NS(incoming=incoming))}):
            await env['home'](None,NS(id=2))
            rows=render.await_args.args[2]
            self.assertTrue(any(b.callback_data=='sp:l:0' for row in rows for b in row))
            incoming.return_value=[]
            await env['home'](None,NS(id=2))
            rows=render.await_args.args[2]
            self.assertFalse(any(b.callback_data=='sp:l:0' for row in rows for b in row))

    async def test_attack_only_wakes_worker_without_awaiting_spectators(self):
        from unittest.mock import patch,Mock
        wake=Mock();present=AsyncMock()
        env=load_functions('handlers/challenge_game.py',['show_attack'],present_attack=present)
        with patch.dict(sys.modules,{'handlers.challenge_spectator':NS(wake_spectators=wake)}):
            await env['show_attack'](NS(bot=None,message_id=10),state())
        present.assert_awaited_once();wake.assert_called_once_with(MID)

    async def test_worker_wired_in_both_entrypoints(self):
        for file in ['main.py','webhook_server.py']:
            self.assertIn('spawn(challenge_spectator_loop(bot))',Path(file).read_text())
