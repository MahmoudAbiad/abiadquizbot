"""Gift deep link guards and admin channel-link UX without live balances."""
import asyncio
import re
from html import escape
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock

from aiogram import types
from reliability_support import load_functions


class GiftLinks(unittest.TestCase):
    def setUp(self):
        self.fn = load_functions(
            'services/gift_links.py', {'gift_code_from_start', 'build_gift_link'},
            re=re, GIFT_PREFIX='gift_',
            _CODE=re.compile(r'[A-Za-z0-9-]{12,59}\Z'),
            _USERNAME=re.compile(r'[A-Za-z0-9_]{5,32}\Z'),
        )

    def test_link_round_trip_with_existing_code_format(self):
        url = self.fn['build_gift_link']('AbiadQuizMakerbot', 'ABIAD-1234567890ABCDEF1234')
        self.assertEqual(url,
            'https://t.me/AbiadQuizMakerbot?start=gift_ABIAD-1234567890ABCDEF1234')
        self.assertEqual(self.fn['gift_code_from_start'](url.split('start=')[1]),
                         'ABIAD-1234567890ABCDEF1234')

    def test_invalid_payloads_rejected_before_database(self):
        parse = self.fn['gift_code_from_start']
        for value in ('gift_', 'gift_123', 'gift_ABC/1234567890', 'gift_ABC%2D123456789',
                      'gift_' + 'X'*60, 'share_ABC-1234567890', None):
            with self.subTest(payload=value):
                self.assertIsNone(parse(value))
        self.assertEqual(parse('gift_abiad-1234567890'), 'ABIAD-1234567890')

    def test_non_telegram_username_and_overlong_code_are_rejected(self):
        build = self.fn['build_gift_link']
        for username in ('bad/name', 'bad name', '', None):
            with self.assertRaises(ValueError):
                build(username, 'ABIAD-1234567890')
        with self.assertRaises(ValueError):
            build('AbiadQuizMakerbot', 'A'*60)


class GiftStart(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.db = AsyncMock(return_value={'status': 'existing'})
        self.redeem = AsyncMock(return_value={'status':'ok','points':25,'expires_at':None})
        self.message = SimpleNamespace(
            from_user=SimpleNamespace(id=43,username=None,first_name='Test',last_name=None),
            answer=AsyncMock(),
        )
        self.events = []
        def spawn(coro):
            coro.close()
            self.events.append(True)
        self.env = load_functions('handlers/start.py', {'redeem_linked_gift'},
            gift_code_from_start=lambda payload: 'ABIAD-1234567890' if payload=='gift_ABIAD-1234567890' else None,
            check_or_add_user=self.db, redeem_gift_code=self.redeem,
            log_usage_event=AsyncMock(), spawn=spawn, types=types,
            remaining_expiry_text=lambda _: 'بدون انتهاء', expiry_text=lambda _: 'بدون انتهاء')

    async def test_new_or_existing_student_redeems_without_state_changes(self):
        await self.env['redeem_linked_gift'](self.message, 'gift_ABIAD-1234567890')
        self.db.assert_awaited_once()
        self.redeem.assert_awaited_once_with(43, 'ABIAD-1234567890')
        text = self.message.answer.await_args.args[0]
        self.assertIn('25 نقطة هدية', text)
        self.assertIn('تلقائياً', text)
        keyboard = self.message.answer.await_args.kwargs['reply_markup']
        self.assertEqual([b.callback_data for row in keyboard.inline_keyboard for b in row],
                         ['student_create_quiz','gift:wallet'])

    async def test_bad_link_never_queries_or_redeems(self):
        await self.env['redeem_linked_gift'](self.message, 'gift_bad')
        self.db.assert_not_awaited()
        self.redeem.assert_not_awaited()

    async def test_second_click_is_rejected_by_same_atomic_redemption_rpc(self):
        self.redeem.return_value = {'status': 'already_used'}
        await self.env['redeem_linked_gift'](self.message, 'gift_ABIAD-1234567890')
        self.assertIn('سبق لك', self.message.answer.await_args.args[0])

    async def test_exhaustion_expiration_and_account_failure(self):
        for status in ('exhausted','expired','disabled'):
            self.redeem.return_value={'status':status}
            await self.env['redeem_linked_gift'](self.message,'gift_ABIAD-1234567890')
            self.assertIn('❌', self.message.answer.await_args.args[0])
        self.db.return_value={'status':'error'}
        self.redeem.reset_mock()
        await self.env['redeem_linked_gift'](self.message,'gift_ABIAD-1234567890')
        self.redeem.assert_not_awaited()

    async def test_start_dispatch_skips_referral_quiz_and_welcome_for_gift(self):
        target=AsyncMock()
        bot=SimpleNamespace(get_me=AsyncMock())
        env=load_functions('handlers/start.py',{'start'},
            bot=bot,redeem_linked_gift=target,check_or_add_user=AsyncMock(),
            launch_deep_linked_quiz=AsyncMock(),logger=SimpleNamespace(error=lambda *_:None))
        msg=self.message
        await env['start'](msg,SimpleNamespace(args='gift_ABIAD-1234567890'),SimpleNamespace())
        target.assert_awaited_once()
        env['check_or_add_user'].assert_not_awaited()
        env['launch_deep_linked_quiz'].assert_not_awaited()


class GiftAdminLink(unittest.IsolatedAsyncioTestCase):
    async def test_copy_button_uses_current_bot_username_and_does_not_redeem(self):
        row={'id':'id','title':'هدية','code':'ABIAD-1234567890','points':10,
             'used_count':0,'max_uses':1,'redeem_expires_at':None,'gift_valid_days':0,'enabled':True}
        query=SimpleNamespace()
        query.select=lambda *_:query
        query.eq=lambda *_:query
        query.limit=lambda *_:query
        query.execute=AsyncMock(return_value=SimpleNamespace(data=[row]))
        db=SimpleNamespace(table=lambda name: query if name=='gift_codes' else None)
        edit=AsyncMock()
        env=load_functions('handlers/admin/gifts.py',{'gift_link','code_text'},
            supabase=db,types=types,escape=escape,expiry_text=lambda _:'بدون انتهاء',
            safe_edit_text=edit,build_gift_link=lambda bot,code:f'https://t.me/{bot}?start=gift_{code}')
        call=SimpleNamespace(data='ag:link:id',message=SimpleNamespace(),answer=AsyncMock(),
                             bot=SimpleNamespace(get_me=AsyncMock(return_value=SimpleNamespace(username='AbiadQuizMakerbot'))))
        await env['gift_link'](call)
        text=edit.await_args.args[1]
        self.assertIn('https://t.me/AbiadQuizMakerbot?start=gift_ABIAD-1234567890',text)
        kb=edit.await_args.kwargs['reply_markup']
        self.assertEqual(kb.inline_keyboard[0][0].copy_text.text,
                         'https://t.me/AbiadQuizMakerbot?start=gift_ABIAD-1234567890')
        self.assertTrue(all(b.url is None for row in kb.inline_keyboard for b in row))
        call.answer.assert_awaited_once()


if __name__ == '__main__':
    unittest.main()
