import asyncio
import importlib
import sys
import types
import unittest
from cachetools import TTLCache
from unittest.mock import AsyncMock, Mock, patch

from reliability_support import load_functions


class SettingsTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from services import maintenance
        self.module = maintenance
        maintenance._last_known = None
        maintenance._save_lock = asyncio.Lock()
        self.values = {}
        async def set_value(key, value):
            self.values[key] = value
            return True
        self.redis = types.SimpleNamespace(get=AsyncMock(side_effect=lambda key: self.values.get(key)),
            set=AsyncMock(side_effect=set_value))
        self.config = types.SimpleNamespace(bot=types.SimpleNamespace(id=123), ADMIN_ID=99, redis_client=self.redis)
        self.patcher = patch.dict(sys.modules, {
            'config': self.config,
        })
        self.patcher.start()

    async def asyncTearDown(self):
        self.patcher.stop()

    async def test_default_is_off_and_isolated_by_bot(self):
        self.assertFalse((await self.module.get_maintenance()).enabled)
        self.redis.get.assert_awaited_with('maintenance:123')
        self.config.bot.id = 456
        self.assertEqual(self.module._key(), 'maintenance:456')

    async def test_custom_message_does_not_enable_and_survives_restart(self):
        text = 'نعود الساعة 8 مساءً <قريباً>'
        settings = await self.module.save_maintenance(message=text)
        self.assertFalse(settings.enabled)
        self.assertEqual(settings.message, text)
        self.module._last_known = None
        self.assertEqual((await self.module.get_maintenance(fresh=True)).message, text)
        self.assertIsNone(await self.module.maintenance_notice(1))

    async def test_enable_disable_preserves_message_and_admin_access(self):
        await self.module.save_maintenance(message='نعود غداً')
        await self.module.save_maintenance(enabled=True)
        self.assertEqual(await self.module.maintenance_notice(1), 'نعود غداً')
        self.assertIsNone(await self.module.maintenance_notice(99))
        await self.module.save_maintenance(enabled=False)
        self.assertIsNone(await self.module.maintenance_notice(1))
        self.assertEqual((await self.module.get_maintenance()).message, 'نعود غداً')

    async def test_reset_keeps_enabled(self):
        await self.module.save_maintenance(enabled=True, message='موعد مخصص')
        settings = await self.module.save_maintenance(message=self.module.DEFAULT_MESSAGE)
        self.assertTrue(settings.enabled)
        self.assertEqual(settings.message, self.module.DEFAULT_MESSAGE)

    async def test_storage_failure_keeps_closed_and_save_failure_is_visible(self):
        await self.module.save_maintenance(enabled=True)
        self.redis.get.side_effect = ConnectionError('offline')
        self.assertTrue((await self.module.get_maintenance()).enabled)
        with self.assertRaises(ConnectionError):
            await self.module.save_maintenance(enabled=False)
        self.assertTrue(self.module._last_known.enabled)

    async def test_no_known_state_does_not_silently_bypass_gate(self):
        self.redis.get.side_effect = ConnectionError('offline')
        with self.assertRaises(ConnectionError):
            await self.module.maintenance_notice(1)

    async def test_message_validation(self):
        for text in ('', '   ', 'a' * 2001):
            with self.assertRaises(ValueError):
                self.module.validate_message(text)
        self.assertEqual(self.module.validate_message(' a\nb '), 'a\nb')

    async def test_failed_write_does_not_claim_setting_changed(self):
        await self.module.save_maintenance(enabled=False)
        self.redis.set.side_effect = ConnectionError('offline')
        with self.assertRaises(ConnectionError):
            await self.module.save_maintenance(enabled=True)
        self.assertFalse(self.module._last_known.enabled)

    async def test_setting_has_no_expiry(self):
        await self.module.save_maintenance(message='نعود لاحقاً')
        self.assertEqual(self.redis.set.call_args.kwargs, {})


class GateTests(unittest.IsolatedAsyncioTestCase):
    async def test_updates_blocked_before_handler_and_plain_text_notice_sent(self):
        class Message:
            def __init__(self):
                self.from_user = types.SimpleNamespace(id=1)
                self.answer = AsyncMock()
        class CallbackQuery:
            def __init__(self):
                self.from_user = types.SimpleNamespace(id=1)
                self.message = Message()
                self.answer = AsyncMock()
        env = load_functions('middlewares.py', {'MaintenanceMiddleware'},
            BaseMiddleware=object, Message=Message, CallbackQuery=CallbackQuery, logger=Mock(), TTLCache=TTLCache)
        bot = types.SimpleNamespace(send_message=AsyncMock())
        for kind, tg in [('message', Message()), ('callback_query', CallbackQuery()),
                         ('poll_answer', types.SimpleNamespace(user=types.SimpleNamespace(id=1)))]:
            with self.subTest(kind=kind), patch('services.maintenance.maintenance_notice', AsyncMock(return_value='<صيانة>')):
                gate = env['MaintenanceMiddleware']()
                handler = AsyncMock()
                await gate(handler, types.SimpleNamespace(event=tg, event_type=kind), {'bot': bot})
                handler.assert_not_awaited()
                if kind == 'message':
                    tg.answer.assert_awaited_with('<صيانة>', parse_mode=None)
                elif kind == 'callback_query':
                    tg.answer.assert_awaited_once()
                    tg.message.answer.assert_awaited_with('<صيانة>', parse_mode=None)
                else:
                    bot.send_message.assert_awaited_with(1, '<صيانة>', parse_mode=None)

    async def test_off_or_admin_passes_through(self):
        gate = load_functions('middlewares.py', {'MaintenanceMiddleware'}, BaseMiddleware=object,
            Message=object, CallbackQuery=object, logger=Mock(), TTLCache=TTLCache)['MaintenanceMiddleware']()
        handler = AsyncMock(return_value='passed')
        with patch('services.maintenance.maintenance_notice', AsyncMock(return_value=None)):
            self.assertEqual(await gate(handler, types.SimpleNamespace(event=types.SimpleNamespace(), event_type='message'), {}), 'passed')

    async def test_notice_cooldown_shared_by_messages_buttons_and_polls(self):
        class Message:
            def __init__(self, user_id=1):
                self.from_user = types.SimpleNamespace(id=user_id)
                self.answer = AsyncMock()
        class CallbackQuery:
            def __init__(self):
                self.from_user = types.SimpleNamespace(id=1)
                self.message = Message()
                self.answer = AsyncMock()
        clock = [0.0]
        env = load_functions('middlewares.py', {'MaintenanceMiddleware'}, BaseMiddleware=object,
            Message=Message, CallbackQuery=CallbackQuery, logger=Mock(),
            TTLCache=lambda **kw: TTLCache(**kw, timer=lambda: clock[0]))
        gate = env['MaintenanceMiddleware']()
        handler = AsyncMock()
        bot = types.SimpleNamespace(send_message=AsyncMock())
        async def run(kind, event):
            await gate(handler, types.SimpleNamespace(event=event, event_type=kind), {'bot': bot})
        with patch('services.maintenance.maintenance_notice', AsyncMock(return_value='صيانة')) as notice:
            first = Message()
            await asyncio.gather(*(run('message', first) for _ in range(10)))
            first.answer.assert_awaited_once()
            button = CallbackQuery()
            await run('callback_query', button)
            button.answer.assert_awaited_once_with()
            button.message.answer.assert_not_awaited()
            await run('poll_answer', types.SimpleNamespace(user=types.SimpleNamespace(id=1)))
            bot.send_message.assert_not_awaited()
            other = Message(2)
            await run('message', other)
            other.answer.assert_awaited_once()
            clock[0] = 9.99
            await run('message', first)
            self.assertEqual(first.answer.await_count, 1)
            clock[0] = 10
            await run('message', first)
            self.assertEqual(first.answer.await_count, 2)
            handler.assert_not_awaited()
            # Disabling maintenance is effective even inside the notice cooldown.
            notice.return_value = None
            await run('message', first)
            handler.assert_awaited_once()

    async def test_miniapp_rejected_before_consuming_rate_limit(self):
        env = load_functions('webhook_server.py', {'_enforce_upload_rate_limit'},
            HTTPException=type('HTTPError', (Exception,), {'__init__': lambda self, **kw: Exception.__init__(self, kw)}),
            redis_client=Mock(), logger=Mock())
        with patch('services.maintenance.maintenance_notice', AsyncMock(return_value='الصيانة حتى 8')):
            with self.assertRaises(env['HTTPException']) as caught:
                await env['_enforce_upload_rate_limit'](1, 'file_init', 5, 60)
        self.assertEqual(caught.exception.args[0]['status_code'], 503)
        env['redis_client'].incr.assert_not_called()

    async def test_admin_edit_preview_requires_explicit_save(self):
        from aiogram import types as tg
        states = types.SimpleNamespace(confirming_message='confirm')
        env = load_functions('handlers/admin/maintenance.py', {'receive_message'}, types=tg,
            MaintenanceState=states)
        state = types.SimpleNamespace(update_data=AsyncMock(), set_state=AsyncMock())
        msg = types.SimpleNamespace(text='نعود غداً', answer=AsyncMock())
        with patch('services.maintenance.save_maintenance', AsyncMock()) as save:
            await env['receive_message'](msg, state)
            save.assert_not_awaited()
        state.update_data.assert_awaited_with(maintenance_draft='نعود غداً')
        self.assertEqual(msg.answer.await_args.args[0], 'نعود غداً')

    async def test_admin_filter_rejects_other_users(self):
        env = load_functions('handlers/admin/dashboard.py', {'IsAdminFilter'}, BaseFilter=object,
            ADMIN_ID=99)
        guard = env['IsAdminFilter']()
        self.assertFalse(await guard(types.SimpleNamespace(from_user=types.SimpleNamespace(id=1))))
        self.assertTrue(await guard(types.SimpleNamespace(from_user=types.SimpleNamespace(id=99))))


if __name__ == '__main__':
    unittest.main()
