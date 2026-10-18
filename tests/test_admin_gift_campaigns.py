"""Admin review boundaries and durable worker behavior, no live messages or balances."""
import ast
import asyncio
from copy import deepcopy
from decimal import Decimal, InvalidOperation
from html import escape
from pathlib import Path
import secrets
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch
import uuid

from aiogram import types
from aiogram.methods import SendMessage
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError, TelegramRetryAfter
from reliability_support import load_functions
from services.gift_campaigns import process_campaign, campaign_notice, campaign_report, campaign_report_buttons


class State:
    def __init__(self): self.data = {}; self.status = None
    async def get_data(self): return deepcopy(self.data)
    async def update_data(self, **kwargs): self.data.update(deepcopy(kwargs))
    async def get_state(self): return self.status
    async def set_state(self, value): self.status = getattr(value, 'state', value)
    async def clear(self): self.data = {}; self.status = None


class Client:
    def __init__(self, responses=None):
        self.responses = responses or {}
        self.calls = []
        self.table_data = []
        self.filters = []

    def rpc(self, name, params):
        self.calls.append((name, deepcopy(params)))
        value = self.responses.get(name)
        if isinstance(value, list) and name == 'admin_gift_campaign_claim':
            value = value.pop(0) if value else []
        async def execute():
            if isinstance(value, BaseException): raise value
            return SimpleNamespace(data=deepcopy(value))
        return SimpleNamespace(execute=execute)

    def table(self, name):
        self.filters.append(('table', name))
        return self

    def select(self, *args): return self
    def update(self, value): self.filters.append(('update', value)); return self
    def eq(self, field, value): self.filters.append((field, value)); return self
    def limit(self, value): return self
    async def execute(self):
        if isinstance(self.table_data, BaseException):
            raise self.table_data
        return SimpleNamespace(data=self.table_data)


def report(**updates):
    return dict(id='campaign', title='<Gift>', points=25, message='<Hello>', status='running',
                recipients=2, granted=2, grant_pending=0, skipped=0, sent=0, blocked=0,
                failed=0, uncertain=0, notice_pending=2, report_done=False,
                report_chat_id=999, report_message_id=12, **updates)


class WorkerTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.recipients = [dict(user_id=i, gift_id=f'g{i}', token=f't{i}', expires_at=None) for i in (1, 2)]
        self.client = Client({'admin_gift_campaign_claim': [self.recipients],
                              'admin_gift_campaign_status': report()})
        self.bot = SimpleNamespace(send_message=AsyncMock(), edit_message_text=AsyncMock())
        self.sleep = patch('services.gift_campaigns.asyncio.sleep', new=AsyncMock())
        self.sleep.start(); self.addCleanup(self.sleep.stop)

    def notices(self):
        return [p for name, p in self.client.calls if name == 'admin_gift_campaign_notice']

    async def test_worker_grants_via_idempotent_rpc_then_sends_escaped_notifications(self):
        await process_campaign(self.client, self.bot, report())
        self.assertEqual(self.client.calls[0][0], 'admin_gift_campaign_grant_step')
        self.assertEqual([p['p_status'] for p in self.notices()], ['sent', 'sent'])
        text = self.bot.send_message.await_args_list[0].args[1]
        self.assertIn('&lt;Gift&gt;', text)
        self.assertIn('&lt;Hello&gt;', text)
        self.assertIn('25 نقطة هدية', text)
        self.assertIn('بدون انتهاء', text)
        buttons = self.bot.send_message.await_args.kwargs['reply_markup']
        self.assertEqual([b.callback_data for row in buttons.inline_keyboard for b in row],
                         ['student_create_quiz', 'gift:wallet'])

    async def test_blocked_and_rejected_messages_do_not_rollback_grants(self):
        method = SendMessage(chat_id=1, text='test')
        self.bot.send_message.side_effect = [TelegramForbiddenError(method=method, message='blocked'),
                                            TelegramBadRequest(method=method, message='invalid chat')]
        await process_campaign(self.client, self.bot, report())
        self.assertEqual([p['p_status'] for p in self.notices()], ['blocked', 'failed'])
        self.assertFalse(any('revoke' in name or 'refund' in name for name, _ in self.client.calls))

    async def test_ambiguous_send_is_not_retried_on_next_step(self):
        self.client.responses['admin_gift_campaign_claim'] = [[self.recipients[0]], []]
        self.bot.send_message.side_effect = TimeoutError('possibly accepted by Telegram')
        await process_campaign(self.client, self.bot, report())
        await process_campaign(self.client, self.bot, report())
        self.bot.send_message.assert_awaited_once()
        self.assertEqual(self.notices()[0]['p_status'], 'uncertain')

    async def test_flood_wait_returns_remaining_claims_without_sending_them(self):
        self.bot.send_message.side_effect = TelegramRetryAfter(
            method=SendMessage(chat_id=1, text='test'), message='flood', retry_after=30)
        await process_campaign(self.client, self.bot, report())
        self.bot.send_message.assert_awaited_once()
        self.assertEqual([p['p_status'] for p in self.notices()], ['pending', 'pending'])
        self.assertTrue(all(p['p_retry'] == 31 for p in self.notices()))

    async def test_failed_ack_does_not_immediately_repeat_a_possibly_sent_message(self):
        self.client.responses['admin_gift_campaign_notice'] = ConnectionError('database unavailable')
        with self.assertRaises(ConnectionError):
            await process_campaign(self.client, self.bot, report())
        self.bot.send_message.assert_awaited_once()
        self.assertEqual(len(self.notices()), 1)

    async def test_cancelled_worker_leaves_claim_for_uncertain_send_recovery(self):
        self.bot.send_message.side_effect = asyncio.CancelledError()
        with self.assertRaises(asyncio.CancelledError):
            await process_campaign(self.client, self.bot, report())
        self.assertEqual(self.notices(), [])

    async def test_completion_notice_is_claimed_and_never_overwrites_admin_menu(self):
        c = report(); c.update(status='completed', notice_pending=0, sent=2)
        self.client.responses['admin_gift_campaign_status'] = c
        self.client.table_data = [c]
        await process_campaign(self.client, self.bot, c)
        self.assertIn(('report_done', False), self.client.filters)
        self.bot.edit_message_text.assert_not_awaited()
        self.assertEqual(self.bot.send_message.await_args.kwargs['chat_id'], 999)
        self.assertIn('اكتملت', self.bot.send_message.await_args.kwargs['text'])
        self.client.table_data = []  # another worker already owns the notice
        await process_campaign(self.client, self.bot, c)
        self.bot.send_message.assert_awaited_once()

    def test_report_distinguishes_grants_from_notice_delivery_and_unconfirmed_drafts(self):
        c = report(); c.update(status='completed', granted=2, sent=1, uncertain=1)
        text = campaign_report(c)
        self.assertIn('تم المنح: 2', text)
        self.assertIn('تسليم غير مؤكد: 1', text)
        c['status'] = 'draft'
        self.assertIn('لم تبدأ', campaign_report(c))


class AdminFlowTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.state = State()
        states = ['campaign_title','campaign_points','campaign_days','campaign_message','campaign_confirm','action_confirm']
        self.states = SimpleNamespace(**{s: SimpleNamespace(state=s) for s in states})
        self.client = Client({'admin_gift_campaign_prepare': dict(recipients=3,total_points=76.5,status='draft'),
                              'admin_gift_campaign_confirm': 'running',
                              'admin_gift_code_summary': dict(affected=2,remaining_points=30.5),
                              'admin_gift_code_action': dict(status='ok',affected=2,remaining_points=30.5)})
        self.clock = SimpleNamespace(time=lambda: 1000)
        self.env = load_functions('handlers/admin/gifts.py', {
            'kb','cancel_kb','campaign_new','campaign_title','campaign_points','campaign_days','campaign_message',
            'campaign_confirm','campaign_cancel','code_action_prompt','code_action_confirm','code_text','_missing_gift_admin_schema','toggle','view'},
            supabase=self.client, CodeState=self.states, types=types, Decimal=Decimal, InvalidOperation=InvalidOperation,
            escape=escape, secrets=secrets, time=self.clock, uuid=uuid, safe_edit_text=AsyncMock(),
            expiry_text=lambda _: 'بدون انتهاء', campaign_report_buttons=campaign_report_buttons)

    def call(self, data):
        return SimpleNamespace(data=data, from_user=SimpleNamespace(id=999), answer=AsyncMock(),
            message=SimpleNamespace(answer=AsyncMock(), chat=SimpleNamespace(id=999), message_id=12))

    def message(self, text):
        return SimpleNamespace(text=text, from_user=SimpleNamespace(id=999), answer=AsyncMock())

    async def preview(self):
        await self.env['campaign_new'](self.call('agc:new'), self.state)
        for name, text in [('campaign_title','<Study gift>'),('campaign_points','25.50'),('campaign_days','10')]:
            await self.env[name](self.message(text), self.state)
        msg = self.message('<Message>')
        await self.env['campaign_message'](msg, self.state)
        return msg

    async def test_preview_has_fixed_audience_totals_escaped_message_and_no_grant(self):
        msg = await self.preview()
        self.assertEqual([n for n, _ in self.client.calls], ['admin_gift_campaign_prepare'])
        text = msg.answer.await_args.args[0]
        self.assertIn('لم يتم المنح بعد', text)
        self.assertIn('المستهدفون الحاليون: 3', text)
        self.assertIn('إجمالي النقاط: 76.5', text)
        self.assertIn('&lt;Message&gt;', text)
        buttons = msg.answer.await_args.kwargs['reply_markup']
        self.assertTrue(all(len(b.callback_data.encode()) <= 64 for row in buttons.inline_keyboard for b in row))
        self.assertEqual(self.state.status, 'campaign_confirm')

    async def test_confirm_is_bound_to_latest_preview_and_second_click_cannot_start_again(self):
        await self.preview()
        wrong = self.call('agc:confirm:' + str(uuid.uuid4()))
        await self.env['campaign_confirm'](wrong, self.state)
        self.assertEqual(len(self.client.calls), 1)
        identity = self.state.data['gift_campaign_draft']['id']
        call = self.call('agc:confirm:' + identity)
        await self.env['campaign_confirm'](call, self.state)
        await self.env['campaign_confirm'](call, self.state)
        self.assertEqual([n for n, _ in self.client.calls].count('admin_gift_campaign_confirm'), 1)
        self.assertEqual(self.state.data, {})

    async def test_invalid_amounts_and_days_do_not_advance_or_call_database(self):
        await self.env['campaign_new'](self.call('agc:new'), self.state)
        await self.env['campaign_title'](self.message('Gift'), self.state)
        for amount in ['NaN','Infinity','-1','0','0.001','100001','invalid']:
            await self.env['campaign_points'](self.message(amount), self.state)
            self.assertEqual(self.state.status, 'campaign_points')
        await self.env['campaign_points'](self.message('0.01'), self.state)
        for days in ['-1','3651','1.5','invalid']:
            await self.env['campaign_days'](self.message(days), self.state)
            self.assertEqual(self.state.status, 'campaign_days')
        self.assertEqual(self.client.calls, [])

    async def test_cancel_only_targets_unconfirmed_owned_draft(self):
        await self.preview()
        identity = self.state.data['gift_campaign_draft']['id']
        await self.env['campaign_cancel'](self.call('agc:cancel:' + identity), self.state)
        self.assertIn(('status','draft'), self.client.filters)
        self.assertIn(('created_by',999), self.client.filters)
        self.assertFalse(any(n.endswith('confirm') for n, _ in self.client.calls))

    async def test_code_revoke_preview_expiry_and_double_confirmation(self):
        identity = str(uuid.uuid4())
        self.client.table_data = [dict(id=identity,title='Code',code='ABIAD-TEST-001',points=100,
                                      gift_valid_days=10,used_count=2,enabled=True,archived_at=None)]
        call = self.call('ag:action:revoke_all:' + identity)
        await self.env['code_action_prompt'](call, self.state)
        text = self.env['safe_edit_text'].await_args.args[1]
        self.assertIn('30.5', text)
        self.assertIn('لا تتأثر أي أرصدة أخرى', text)
        token = self.state.data['gift_code_action']['token']
        confirm = self.call('ag:do:' + token)
        self.clock.time = lambda: 1400
        await self.env['code_action_confirm'](confirm, self.state)
        self.assertFalse(any(n == 'admin_gift_code_action' for n, _ in self.client.calls))
        self.clock.time = lambda: 1001
        await self.env['code_action_confirm'](confirm, self.state)
        await self.env['code_action_confirm'](confirm, self.state)
        self.assertEqual([n for n, _ in self.client.calls].count('admin_gift_code_action'), 1)

    async def test_pre_migration_schema_keeps_old_toggle_but_refuses_new_campaigns(self):
        class MissingSchema(Exception):
            code = 'PGRST202'
        self.client.responses['admin_gift_code_action'] = MissingSchema()
        await self.env['toggle'](self.call('ag:toggle:code:0'))
        self.assertIn(('update', {'enabled': False}), self.client.filters)
        self.client.table_data = MissingSchema()
        self.state.data = {'existing': 'flow'}
        call = self.call('agc:new')
        await self.env['campaign_new'](call, self.state)
        self.assertEqual(self.state.data, {'existing': 'flow'})
        self.assertTrue(call.answer.await_args.kwargs['show_alert'])

    async def test_legacy_code_view_does_not_offer_unavailable_schema_actions(self):
        self.client.table_data = [dict(id='code', title='Old', code='ABIAD-TEST-001',
            points=10, gift_valid_days=0, used_count=1, enabled=True)]
        await self.env['view'](self.call('ag:view:code'))
        keyboard = self.env['safe_edit_text'].await_args.kwargs['reply_markup']
        callbacks = [b.callback_data for row in keyboard.inline_keyboard for b in row]
        self.assertIn('ag:toggle:code:0', callbacks)
        self.assertFalse(any(c.startswith('ag:action:') for c in callbacks))

    def test_all_new_handlers_use_admin_private_router_and_startup_covers_both_modes(self):
        source = Path('handlers/admin/gifts.py').read_text(encoding='utf-8')
        self.assertIn("router.message.filter(IsAdminFilter(), F.chat.type == 'private')", source)
        self.assertIn("router.callback_query.filter(IsAdminFilter(), F.message.chat.type == 'private')", source)
        for file in ['main.py', 'webhook_server.py']:
            self.assertIn('spawn(gift_campaign_loop())', Path(file).read_text(encoding='utf-8'))


if __name__ == '__main__':
    unittest.main()
