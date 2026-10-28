"""Grant/retry flow preserves setup and checks the intended group's rights live."""
import datetime
import types
import unittest
from unittest.mock import AsyncMock, Mock
from urllib.parse import urlsplit, parse_qs
from aiogram import types as telegram_types
from reliability_support import load_functions

SID='11111111-1111-1111-1111-111111111111'
CHAT=-100123


class PermissionHelpTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.session=dict(id=SID, chat_id=CHAT, status='waiting', question_timer_seconds=30, lock_chat=False)
        self.member=types.SimpleNamespace(status='administrator',can_restrict_members=False)
        self.bot=Mock(id=123, get_chat_member=AsyncMock(return_value=self.member))
        self.update=AsyncMock(return_value=self.session)
        self.guard=AsyncMock(return_value=self.session)
        self.call=Mock(data=f'gql:{SID}:1',answer=AsyncMock(),message=Mock(edit_text=AsyncMock()))
        async def safe(factory, **kwargs):
            try: return await factory()
            except Exception: return None
        self.env=load_functions('handlers/group_quiz.py',
            {'_ensure_group_lock_permission','_member_status','choose_group_lock','choose_group_pacing'},
            bot=self.bot, types=telegram_types, _get_bot_username=AsyncMock(return_value='AbiadQuizMakerbot'),
            _safe_call=safe, _guard_config_callback=self.guard, update_session=self.update,
            quiz_lock_schema_ready=AsyncMock(return_value=True), _is_channel_chat=AsyncMock(return_value=False),
            get_group_pacing_keyboard=Mock(return_value='pacing'), start_waiting_session=AsyncMock(),
            datetime=datetime, MIN_QUESTION_INTERVAL_SECONDS=12, _utc_iso=Mock(),
            logger=Mock(), log_error=Mock())

    async def choose(self): await self.env['choose_group_lock'](self.call)

    async def test_missing_right_shows_action_retry_and_manual_steps_without_writes(self):
        await self.choose()
        self.update.assert_not_awaited()
        self.env['start_waiting_session'].assert_not_awaited()
        text=self.call.message.edit_text.await_args.args[0]
        self.assertIn('المشرفون',text)
        self.assertIn('إعدادات الاختبار محفوظة',text)
        rows=self.call.message.edit_text.await_args.kwargs['reply_markup'].inline_keyboard
        self.assertEqual(parse_qs(urlsplit(rows[0][0].url).query,keep_blank_values=True),
                         {'startgroup':[''],'admin':['restrict_members']})
        self.assertEqual(rows[1][0].callback_data,f'gql:{SID}:1')
        self.assertEqual(rows[2][0].callback_data,f'gql:{SID}:0')
        self.assertEqual(rows[3][0].callback_data,f'gqc:{SID}')
        self.bot.get_chat_member.assert_awaited_once_with(CHAT,123,request_timeout=15)

    async def test_retry_after_grant_proceeds_with_same_session(self):
        await self.choose()
        self.member.can_restrict_members=True
        await self.choose()
        self.update.assert_awaited_once_with(SID,{'lock_chat':True})
        self.assertEqual(self.call.message.edit_text.await_args.kwargs['reply_markup'],'pacing')
        self.assertEqual(self.bot.get_chat_member.await_count,2)

    async def test_member_without_admin_role_still_needs_grant(self):
        self.member.status='member'
        self.member.can_restrict_members=True
        await self.choose()
        self.update.assert_not_awaited()

    async def test_network_failure_keeps_setup_and_offers_retry(self):
        self.bot.get_chat_member.side_effect=TimeoutError()
        await self.choose()
        self.update.assert_not_awaited()
        self.assertIn('تعذر التحقق',self.call.message.edit_text.await_args.args[0])

    async def test_repeated_missing_permission_click_is_safe_when_message_unchanged(self):
        self.call.message.edit_text.side_effect=RuntimeError('message is not modified')
        await self.choose()
        self.call.answer.assert_awaited_once()
        self.update.assert_not_awaited()

    async def test_continue_without_lock_needs_no_bot_admin_rights(self):
        self.call.data=f'gql:{SID}:0'
        await self.choose()
        self.bot.get_chat_member.assert_not_awaited()
        self.update.assert_awaited_once_with(SID,{'lock_chat':False})

    async def test_unauthorized_or_expired_setup_never_offers_grant(self):
        self.guard.return_value=None
        await self.choose()
        self.bot.get_chat_member.assert_not_awaited()
        self.call.message.edit_text.assert_not_awaited()

    async def test_channel_callback_does_not_change_channel_flow(self):
        self.env['_is_channel_chat'].return_value=True
        await self.choose()
        self.bot.get_chat_member.assert_not_awaited()
        self.update.assert_not_awaited()

    async def test_rights_revoked_before_start_keeps_session_waiting(self):
        self.session['lock_chat']=True
        self.call.data=f'gqp:{SID}:c:0'
        await self.env['choose_group_pacing'](self.call)
        self.env['start_waiting_session'].assert_not_awaited()
        self.update.assert_not_awaited()
        self.assertEqual(self.session['status'],'waiting')
