"""Regression tests execute production functions without production credentials."""
import asyncio
import datetime
import html
import json
import secrets
import types
import unittest
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, Mock
from aiogram import types as telegram_types
from aiogram.types import ChatPermissions
from reliability_support import load_functions

SID = '11111111-1111-1111-1111-111111111111'
CHAT = -100123


class Query:
    def __init__(self, db, table):
        self.db, self.table = db, table
        self.filters, self.operation, self.payload = [], 'select', None
    def select(self, *a, **kw): return self
    def eq(self, k, v): self.filters.append((k, v)); return self
    def limit(self, *a): return self
    def order(self, *a): return self
    def range(self, *a): return self
    def insert(self, payload): self.operation, self.payload = 'insert', payload; return self
    def update(self, payload): self.operation, self.payload = 'update', payload; return self
    def delete(self): self.operation = 'delete'; return self
    async def execute(self):
        rows = self.db.rows[self.table]
        matched = [r for r in rows if all(r.get(k) == v for k, v in self.filters)]
        if self.operation == 'insert':
            if self.db.fail_insert: raise ConnectionError('DB unavailable')
            row = dict(self.payload, created_at='2020-01-01T00:00:00+00:00')
            rows.append(row); matched = [row]
        elif self.operation == 'update':
            for row in matched: row.update(self.payload)
        elif self.operation == 'delete':
            if self.db.fail_delete: raise ConnectionError('DB unavailable')
            self.db.rows[self.table] = [r for r in rows if r not in matched]
        return types.SimpleNamespace(data=[dict(r) for r in matched])


class DB:
    def __init__(self, session):
        self.rows = {'group_quiz_sessions': [session], 'group_quiz_chat_locks': []}
        self.fail_insert = self.fail_delete = False
    def table(self, name): return Query(self, name)


class Redis:
    def __init__(self): self.values = {}; self.busy = False
    async def set(self, key, value, nx=False, ex=None):
        if self.busy or nx and key in self.values: return False
        self.values[key] = value; return True
    async def get(self, key): return self.values.get(key)
    async def getdel(self, key): return self.values.pop(key, None)
    async def eval(self, script, count, key, value):
        if self.values.get(key) == value: self.values.pop(key); return 1
        return 0


class LockTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.session = dict(id=SID, chat_id=CHAT, status='active', lock_chat=True,
                            question_timer_seconds=30, pacing_mode='chain_to_timer')
        self.db = DB(self.session)
        self.redis = Redis()
        self.original = ChatPermissions(can_send_messages=True, can_send_photos=False,
                                        can_send_polls=True, can_invite_users=True,
                                        can_send_voice_notes=True, can_pin_messages=False)
        self.bot = Mock(id=1234, get_chat=AsyncMock(return_value=types.SimpleNamespace(
            type='supergroup', permissions=self.original)),
            get_chat_member=AsyncMock(return_value=types.SimpleNamespace(
                status='administrator', can_restrict_members=True)),
            set_chat_permissions=AsyncMock(return_value=True))
        names = {'chat_permission_mutex', '_load_lock', '_live_session', '_restore_locked',
                 'acquire_quiz_lock', 'restore_quiz_lock', 'recover_quiz_locks'}
        self.env = load_functions('services/group_quiz_lock.py', names,
            bot=self.bot, redis_client=self.redis, supabase=self.db, secrets=secrets,
            datetime=datetime, ChatPermissions=ChatPermissions, logger=Mock(),
            _TABLE='group_quiz_chat_locks', _RELEASE='release_owned', quiz_lock_schema_ready=AsyncMock(return_value=True))
        self.env['chat_permission_mutex'] = asynccontextmanager(self.env['chat_permission_mutex'])

    async def lock(self): await self.env['acquire_quiz_lock'](self.session)
    async def restore(self): return await self.env['restore_quiz_lock'](SID, CHAT)

    async def test_snapshot_before_apply_and_exact_restore(self):
        async def assert_saved(**kwargs):
            self.assertEqual(self.db.rows['group_quiz_chat_locks'][0]['original_permissions'],
                             self.original.model_dump(exclude_none=True))
            return True
        self.bot.set_chat_permissions.side_effect = assert_saved
        await self.lock()
        permissions = self.bot.set_chat_permissions.await_args.kwargs['permissions']
        for name in ChatPermissions.model_fields:
            if name.startswith('can_send_'): self.assertFalse(getattr(permissions, name))
        self.assertTrue(permissions.can_invite_users)
        self.assertTrue(await self.restore())
        self.assertEqual(self.bot.set_chat_permissions.await_args.kwargs['permissions'], self.original)
        self.assertEqual(self.db.rows['group_quiz_chat_locks'], [])

    async def test_preexisting_closed_group_is_untouched(self):
        self.bot.get_chat.return_value.permissions = ChatPermissions(can_send_messages=False)
        await self.lock()
        self.bot.set_chat_permissions.assert_not_awaited()
        self.assertEqual(self.db.rows['group_quiz_chat_locks'], [])

    async def test_missing_permission_or_original_snapshot_rejected(self):
        self.bot.get_chat_member.return_value.can_restrict_members = False
        with self.assertRaises(RuntimeError): await self.lock()
        self.bot.set_chat_permissions.assert_not_awaited()

    async def test_database_failure_never_locks_group(self):
        self.db.fail_insert = True
        with self.assertRaises(ConnectionError): await self.lock()
        self.bot.set_chat_permissions.assert_not_awaited()

    async def test_apply_timeout_retains_snapshot_and_can_restore(self):
        self.bot.set_chat_permissions.side_effect = TimeoutError()
        with self.assertRaises(TimeoutError): await self.lock()
        self.assertEqual(self.db.rows['group_quiz_chat_locks'][0]['state'], 'prepared')
        self.bot.set_chat_permissions.side_effect = None
        self.session['status'] = 'cancelled'
        await self.env['recover_quiz_locks']()
        self.assertEqual(self.db.rows['group_quiz_chat_locks'], [])

    async def test_failed_unlock_retried_after_restart(self):
        await self.lock()
        self.session['status'] = 'finished'
        self.bot.set_chat_permissions.side_effect = TimeoutError()
        self.assertFalse(await self.restore())
        self.assertEqual(len(self.db.rows['group_quiz_chat_locks']), 1)
        self.bot.set_chat_permissions.side_effect = None
        # Recovery reads only durable state, with no in-process task/session cache.
        await self.env['recover_quiz_locks']()
        self.assertEqual(self.db.rows['group_quiz_chat_locks'], [])
        self.assertEqual(self.bot.set_chat_permissions.await_args.kwargs['permissions'], self.original)

    async def test_restore_db_ack_failure_is_idempotent(self):
        await self.lock()
        self.db.fail_delete = True
        self.assertFalse(await self.restore())
        self.db.fail_delete = False
        self.assertTrue(await self.restore())
        self.assertEqual(self.bot.set_chat_permissions.await_args.kwargs['permissions'], self.original)

    async def test_active_locked_session_recovery_does_not_unlock(self):
        await self.lock()
        self.bot.set_chat_permissions.reset_mock()
        await self.env['recover_quiz_locks']()
        await self.lock()
        self.bot.set_chat_permissions.assert_not_awaited()

    async def test_prepared_session_recovery_completes_apply(self):
        await self.lock()
        self.db.rows['group_quiz_chat_locks'][0]['state'] = 'prepared'
        self.bot.set_chat_permissions.reset_mock()
        await self.env['recover_quiz_locks']()
        self.bot.set_chat_permissions.assert_awaited_once()

    async def test_concurrent_finish_during_lock_restores(self):
        async def finish_during_apply(**kw):
            self.session['status'] = 'finished'
            return True
        self.bot.set_chat_permissions.side_effect = finish_during_apply
        with self.assertRaises(RuntimeError): await self.lock()
        self.assertEqual(self.db.rows['group_quiz_chat_locks'], [])
        self.assertEqual(self.bot.set_chat_permissions.await_count, 2)

    async def test_other_session_cannot_restore_or_overwrite_snapshot(self):
        await self.lock()
        self.bot.set_chat_permissions.reset_mock()
        await self.env['restore_quiz_lock']('other', CHAT)
        self.bot.set_chat_permissions.assert_not_awaited()
        self.db.rows['group_quiz_chat_locks'][0]['session_id'] = 'other'
        with self.assertRaises(RuntimeError): await self.lock()
        self.bot.set_chat_permissions.assert_not_awaited()

    async def test_channels_private_and_untimed_burst_never_lock(self):
        for chat_type in ('channel', 'private'):
            self.bot.get_chat.return_value.type = chat_type
            with self.assertRaises(RuntimeError): await self.lock()
        self.bot.get_chat.return_value.type = 'supergroup'
        self.session.update(question_timer_seconds=None, pacing_mode='burst')
        with self.assertRaises(RuntimeError): await self.lock()
        self.bot.set_chat_permissions.assert_not_awaited()

    async def test_default_off_uses_no_external_services(self):
        self.session['lock_chat'] = False
        await self.lock()
        self.bot.get_chat.assert_not_awaited()
        self.bot.set_chat_permissions.assert_not_awaited()

    async def test_cross_worker_mutex_contention_fails_closed(self):
        self.redis.busy = True
        with self.assertRaises(RuntimeError): await self.lock()
        self.bot.set_chat_permissions.assert_not_awaited()


class SetupLinkTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.redis = Redis()
        self.token = 'a' * 32
        self.key = 'gq:setup:' + self.token
        self.redis.values[self.key] = json.dumps(dict(user_id=42, chat_id=CHAT,
            share_id='quiz-token', message_thread_id=77, title='My Group'))
        self.msg = Mock(chat=types.SimpleNamespace(type='private', id=42),
                        from_user=types.SimpleNamespace(id=42), answer=AsyncMock())
        self.setup = AsyncMock()
        self.admin = AsyncMock(return_value=True)
        self.env = load_functions('handlers/group_quiz.py', {'resume_group_setup'},
            json=json, redis_client=self.redis, is_group_admin=self.admin,
            _start_session_setup=self.setup)
    async def resume(self, payload=None):
        await self.env['resume_group_setup'](self.msg, payload or 'gqsetup_' + self.token)
    async def test_first_private_start_preserves_quiz_group_and_topic(self):
        await self.resume()
        self.setup.assert_awaited_once_with(ui_msg=self.msg, chat_id=CHAT,
            user=self.msg.from_user, share_id='quiz-token', message_thread_id=77,
            dest_title='My Group')
        self.admin.assert_awaited_once_with(CHAT, 42, fresh=True)
    async def test_replay_and_simultaneous_clicks_create_only_one_setup(self):
        await asyncio.gather(self.resume(), self.resume())
        self.setup.assert_awaited_once()
    async def test_wrong_user_does_not_consume_link(self):
        self.msg.from_user.id = 43
        await self.resume()
        self.setup.assert_not_awaited()
        self.assertIn(self.key, self.redis.values)
    async def test_revoked_admin_cannot_consume_link(self):
        self.admin.return_value = False
        await self.resume()
        self.setup.assert_not_awaited()
        self.assertIn(self.key, self.redis.values)
    async def test_expired_and_forged_links(self):
        self.redis.values.clear()
        for payload in ('gqsetup_' + self.token, 'gqsetup_x', 'gqsetup_' + '/' * 32):
            await self.resume(payload)
        self.setup.assert_not_awaited()


class EndSessionTests(unittest.IsolatedAsyncioTestCase):
    async def test_any_live_admin_can_end_without_private_registration(self):
        session = dict(id=SID, chat_id=CHAT, status='active')
        finish = AsyncMock()
        admin = AsyncMock(return_value=True)
        env = load_functions('handlers/group_quiz.py', {'end_group_session_now'},
            is_group_admin=admin, get_session=AsyncMock(return_value=session),
            finish_group_session=finish, log_info=Mock(), log_error=Mock(), logger=Mock())
        call = Mock(data='gqe:' + SID, from_user=types.SimpleNamespace(id=99),
                    message=types.SimpleNamespace(chat=types.SimpleNamespace(id=CHAT)), answer=AsyncMock())
        await env['end_group_session_now'](call)
        admin.assert_awaited_once_with(CHAT, 99, fresh=True)
        finish.assert_awaited_once_with(session, manual=True)
        admin.return_value = False
        finish.reset_mock()
        await env['end_group_session_now'](call)
        finish.assert_not_awaited()

    async def test_finish_restores_before_result_processing_can_fail(self):
        restore = AsyncMock()
        clear = AsyncMock(side_effect=RuntimeError('later result processing failed'))
        env = load_functions('handlers/group_quiz.py', {'finish_group_session'},
            claim_session_finish=AsyncMock(return_value=True), restore_quiz_lock=restore,
            clear_session_questions_cache=clear)
        with self.assertRaises(RuntimeError):
            await env['finish_group_session'](dict(id=SID, chat_id=CHAT))
        restore.assert_awaited_once_with(SID, CHAT)


class ConfigTests(unittest.IsolatedAsyncioTestCase):
    async def test_channels_skip_lock_option_and_groups_default_off(self):
        for channel in (False, True):
            update = AsyncMock(return_value={'id': SID})
            env = load_functions('handlers/group_quiz.py', {'choose_group_timer'},
                _guard_config_callback=AsyncMock(return_value=dict(id=SID, chat_id=CHAT)),
                update_session=update, _is_channel_chat=AsyncMock(return_value=channel),
                types=telegram_types, quiz_lock_schema_ready=AsyncMock(return_value=True), get_group_pacing_keyboard=Mock(return_value='pacing'),
                log_error=Mock(), logger=Mock())
            call = Mock(data=f'gqt:{SID}:30', answer=AsyncMock(),
                message=Mock(edit_text=AsyncMock()))
            await env['choose_group_timer'](call)
            self.assertIn(unittest.mock.call(SID, {'question_timer_seconds': 30, 'lock_chat': False}), update.await_args_list)
            kb = call.message.edit_text.await_args.kwargs['reply_markup']
            if channel: self.assertEqual(kb, 'pacing')
            else: self.assertIn('gql:', kb.inline_keyboard[0][0].callback_data)

    async def test_burst_cannot_start_with_lock_enabled(self):
        start = AsyncMock()
        env = load_functions('handlers/group_quiz.py', {'choose_group_pacing'},
            _guard_config_callback=AsyncMock(return_value={'lock_chat': True}),
            start_waiting_session=start, logger=Mock(), log_error=Mock())
        call = Mock(data=f'gqp:{SID}:b:0', answer=AsyncMock())
        await env['choose_group_pacing'](call)
        start.assert_not_awaited()
        self.assertTrue(any(c.kwargs.get('show_alert') for c in call.answer.await_args_list))


class SessionStoreTests(unittest.IsolatedAsyncioTestCase):
    async def test_setup_writes_cannot_change_running_or_finished_sessions(self):
        for status in ('active', 'finished', 'cancelled'):
            session = dict(id=SID, status=status, lock_chat=True)
            db = DB(session)
            env = load_functions('services/group_quiz_store.py', {'update_session'},
                supabase=db, log_error=Mock(), logger=Mock())
            result = await env['update_session'](SID, {'lock_chat': False})
            self.assertIsNone(result)
            self.assertTrue(session['lock_chat'])

    async def test_start_and_heartbeat_arm_cannot_resurrect_finished_session(self):
        session = dict(id=SID, status='waiting')
        env = load_functions('services/group_quiz_store.py', {'start_waiting_session', 'activate_session_due'},
            supabase=DB(session), datetime=datetime)
        self.assertIsNotNone(await env['start_waiting_session'](SID, {'status': 'active'}))
        self.assertIsNone(await env['start_waiting_session'](SID, {'status': 'active'}))
        session['status'] = 'finished'
        self.assertIsNone(await env['activate_session_due'](SID))
        self.assertNotIn('next_question_due_at', session)


class FreshAdminTests(unittest.IsolatedAsyncioTestCase):
    async def test_fresh_check_bypasses_stale_positive_cache(self):
        redis = Mock(get=AsyncMock(return_value=b'1'), set=AsyncMock())
        bot = Mock(get_chat_member=AsyncMock(return_value=types.SimpleNamespace(status='member')))
        class APIError(Exception): pass
        env = load_functions('services/group_permissions.py', {'is_group_admin', '_cache_key', '_decode_cached'},
            redis_client=redis, bot=bot, _CACHE_PREFIX='chat_admin:', CACHE_TTL=300,
            GROUP_ANONYMOUS_BOT_ID=1087968824, ADMIN_STATUSES={'administrator','creator'},
            TelegramAPIError=APIError, TelegramBadRequest=APIError, TelegramForbiddenError=APIError,
            log_error=Mock(), logger=Mock())
        self.assertFalse(await env['is_group_admin'](CHAT, 42, fresh=True))
        redis.get.assert_not_awaited()
        bot.get_chat_member.assert_awaited_once_with(CHAT, 42)


class GroupLaunchTests(unittest.IsolatedAsyncioTestCase):
    async def test_group_launch_always_offers_bound_deeplink_without_sending_dm(self):
        redis = Redis()
        bot = Mock(send_message=AsyncMock())
        async def safe(factory, **kw): return await factory()
        user = types.SimpleNamespace(id=42)
        msg = Mock(from_user=user, chat=types.SimpleNamespace(id=CHAT,type='supergroup',title='Group'),
                   message_thread_id=77, reply=AsyncMock())
        env = load_functions('handlers/group_quiz.py', {'_begin_group_session'},
            is_group_admin=AsyncMock(return_value=True), redis_client=redis,
            get_chat_membership=AsyncMock(return_value={'status':'administrator'}),
            upsert_chat_membership=AsyncMock(), secrets=secrets, json=json,
            _get_bot_username=AsyncMock(return_value='AbiadQuizMakerbot'),
            _safe_call=safe, types=telegram_types, bot=bot)
        await env['_begin_group_session'](msg, 'quiz-token')
        button = msg.reply.await_args.kwargs['reply_markup'].inline_keyboard[0][0]
        self.assertEqual(button.text, '📩 إكمال إعداد الاختبار في الخاص')
        payload = button.url.split('start=')[1]
        self.assertLessEqual(len(payload), 64)
        saved = json.loads(redis.values['gq:setup:' + payload.removeprefix('gqsetup_')])
        self.assertEqual((saved['user_id'],saved['chat_id'],saved['share_id'],saved['message_thread_id']),
                         (42,CHAT,'quiz-token',77))
        bot.send_message.assert_not_awaited()


class RolloutGateTests(unittest.IsolatedAsyncioTestCase):
    async def test_legacy_schema_skips_lock_ui_and_keeps_timer_setup(self):
        update = AsyncMock(return_value={'id': SID})
        env = load_functions('handlers/group_quiz.py', {'choose_group_timer'},
            _guard_config_callback=AsyncMock(return_value=dict(id=SID, chat_id=CHAT)),
            update_session=update, quiz_lock_schema_ready=AsyncMock(return_value=False),
            _is_channel_chat=AsyncMock(return_value=False), types=telegram_types,
            get_group_pacing_keyboard=Mock(return_value='pacing'), log_error=Mock(), logger=Mock())
        call = Mock(data=f'gqt:{SID}:30', answer=AsyncMock(), message=Mock(edit_text=AsyncMock()))
        await env['choose_group_timer'](call)
        update.assert_awaited_once_with(SID, {'question_timer_seconds': 30})
        self.assertEqual(call.message.edit_text.await_args.kwargs['reply_markup'], 'pacing')

    async def test_missing_schema_gate_recovers_after_migration(self):
        import time
        query = Mock()
        query.select.return_value = query
        query.limit.return_value = query
        query.execute = AsyncMock(side_effect=RuntimeError('column missing'))
        supabase = Mock(table=Mock(return_value=query))
        cache = {'ready':False, 'until':0}
        env = load_functions('services/group_quiz_lock.py', {'quiz_lock_schema_ready'},
            time=time, supabase=supabase, _TABLE='group_quiz_chat_locks', _SCHEMA_CACHE=cache)
        self.assertFalse(await env['quiz_lock_schema_ready']())
        cache['until'] = 0
        query.execute.side_effect = None
        self.assertTrue(await env['quiz_lock_schema_ready']())
