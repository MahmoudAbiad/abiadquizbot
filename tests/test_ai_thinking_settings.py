"""Saved admin thinking levels, request isolation and picker validation."""
import sys
import types
import unittest
from unittest.mock import AsyncMock, Mock, patch

from aiogram import types as telegram_types
from helpers import settings_helper as sh
from reliability_support import load_functions


class ThinkingSettingsTests(unittest.IsolatedAsyncioTestCase):
    async def test_defaults_and_invalid_stored_codes(self):
        for settings in ({}, {'ai_detection_thinking': float('nan'), 'ai_generation_thinking': 2.5}):
            with patch.object(sh, 'get_app_settings', AsyncMock(return_value=settings)):
                self.assertEqual(await sh.get_ai_thinking_level('detection'), 'medium')
                self.assertIsNone(await sh.get_ai_thinking_level('generation'))

    async def test_tasks_have_independent_values(self):
        with patch.object(sh, 'get_app_settings', AsyncMock(return_value={
                'ai_detection_thinking': 1, 'ai_generation_thinking': 3})):
            self.assertEqual(await sh.get_ai_thinking_level('detection'), 'low')
            self.assertEqual(await sh.get_ai_thinking_level('generation'), 'high')

    async def test_saves_create_missing_rows_and_refresh_cache_including_auto_zero(self):
        saved = {}
        db = Mock()
        def upsert(row, **kwargs):
            self.assertEqual(kwargs, {'on_conflict': 'key'})
            saved[row['key']] = row['value']
            return types.SimpleNamespace(execute=AsyncMock(return_value=types.SimpleNamespace(data=[row])))
        db.table.return_value.upsert.side_effect = upsert
        with patch.dict(sys.modules, {'helpers.supabase_helper': types.SimpleNamespace(supabase=db)}), \
             patch.object(sh, '_cache', {}), patch.object(sh, '_cache_timestamp', 0):
            self.assertTrue(await sh.update_ai_thinking_level('generation', 'high'))
            self.assertEqual(await sh.get_ai_thinking_level('generation'), 'high')
            self.assertEqual(await sh.get_ai_thinking_level('detection'), 'medium')
            self.assertTrue(await sh.update_ai_thinking_level('generation', 'auto'))
            self.assertIsNone(await sh.get_ai_thinking_level('generation'))
            self.assertEqual(saved, {'ai_generation_thinking': 0.0})
            db.rpc.assert_not_called()

    async def test_invalid_input_never_writes(self):
        with patch.object(sh, 'update_app_setting', AsyncMock()) as save:
            for task, level in [('unknown', 'medium'), ('generation', 'max'), ('detection', 'none')]:
                self.assertFalse(await sh.update_ai_thinking_level(task, level))
            save.assert_not_awaited()
        for value in (float('nan'), float('inf'), -1, 0.5, 4):
            self.assertIsNone(await sh.update_app_setting('ai_generation_thinking', value))

    async def test_failed_save_retains_last_known_setting(self):
        db = Mock()
        db.table.return_value.upsert.return_value.execute = AsyncMock(side_effect=RuntimeError('unavailable'))
        with patch.dict(sys.modules, {'helpers.supabase_helper': types.SimpleNamespace(supabase=db)}), \
             patch.object(sh, '_cache', {'ai_generation_thinking': 1.0}):
            self.assertFalse(await sh.update_ai_thinking_level('generation', 'high'))
            self.assertEqual(sh._cache['ai_generation_thinking'], 1.0)


class ThinkingAdminTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.render = AsyncMock()
        self.env = load_functions('handlers/admin/ai_control.py', {'set_thinking_level', 'open_thinking_settings'},
                                  _render_thinking_screen=self.render, logger=Mock())
        self.call = types.SimpleNamespace(data='ait:set:generation:high', answer=AsyncMock())

    async def test_success_saves_selected_task_and_refreshes(self):
        with patch.object(sh, 'update_ai_thinking_level', AsyncMock(return_value=True)) as save:
            await self.env['set_thinking_level'](self.call)
            save.assert_awaited_once_with('generation', 'high')
        self.render.assert_awaited_once_with(self.call, 'generation')

    async def test_failed_save_shows_error_without_successful_refresh(self):
        with patch.object(sh, 'update_ai_thinking_level', AsyncMock(return_value=False)):
            await self.env['set_thinking_level'](self.call)
        self.render.assert_not_awaited()
        self.assertTrue(self.call.answer.await_args.kwargs['show_alert'])

    async def test_invalid_callbacks_cannot_write(self):
        with patch.object(sh, 'update_ai_thinking_level', AsyncMock()) as save:
            for data in ('ait:set:unknown:high', 'ait:set:generation:max', 'ait:set:generation', 'ait:set:generation:low:extra'):
                self.call.data = data
                await self.env['set_thinking_level'](self.call)
            save.assert_not_awaited()
        self.render.assert_not_awaited()

    async def test_open_clears_stale_admin_edit_state(self):
        self.call.data = 'ait:open:detection'
        state = types.SimpleNamespace(clear=AsyncMock())
        await self.env['open_thinking_settings'](self.call, state)
        state.clear.assert_awaited_once()
        self.render.assert_awaited_once_with(self.call, 'detection')

    def test_picker_marks_only_selected_level_and_shows_main_menu_values(self):
        env = load_functions('keyboards.py', {'get_ai_thinking_keyboard', 'get_ai_control_keyboard'}, types=telegram_types)
        for task in ('detection', 'generation'):
            kb = env['get_ai_thinking_keyboard'](task, 'low')
            buttons = [b for row in kb.inline_keyboard for b in row]
            selected = [b for b in buttons if b.text.startswith('✅')]
            self.assertEqual([b.callback_data for b in selected], [f'ait:set:{task}:low'])
            self.assertEqual(len([b for b in buttons if b.callback_data.startswith('ait:set:')]), 4)
        kb = env['get_ai_control_keyboard'](120, detection_thinking='low', generation_thinking='high')
        labels = {b.callback_data: b.text for row in kb.inline_keyboard for b in row}
        self.assertIn('منخفض', labels['ait:open:detection'])
        self.assertIn('مرتفع', labels['ait:open:generation'])
