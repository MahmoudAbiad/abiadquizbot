"""Vertex opt-in regression tests: no live credentials, Supabase or Telegram needed."""
import sys
import types
import unittest
from unittest.mock import AsyncMock, Mock, patch

from reliability_support import load_functions


class VertexParallelToggleTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.flag_reader = AsyncMock(return_value=False)
        feature_module = types.ModuleType("helpers.supabase_helper.feature_flags")
        feature_module.is_feature_enabled = self.flag_reader
        self.feature_module = feature_module
        self.parent_module = types.ModuleType("helpers.supabase_helper")
        self.parent_module.__path__ = []
        self.runtime = load_functions(
            "services/ai/runtime.py", {"_effective_vertex_exclusion"}, logger=Mock()
        )

    async def _check(self, excluded):
        with patch.dict(sys.modules, {
            "helpers.supabase_helper": self.parent_module,
            "helpers.supabase_helper.feature_flags": self.feature_module,
        }):
            return await self.runtime["_effective_vertex_exclusion"](excluded)

    async def test_default_keeps_vertex_excluded_for_multi_part(self):
        self.assertTrue(await self._check(True))
        self.flag_reader.assert_awaited_once_with(
            "vertex_parallel_enabled", default=False
        )

    async def test_admin_opt_in_allows_vertex_to_join_multi_part_cascade(self):
        self.flag_reader.return_value = True
        self.assertFalse(await self._check(True))

    async def test_regular_requests_do_not_consult_flag_or_get_excluded(self):
        self.assertFalse(await self._check(False))
        self.flag_reader.assert_not_awaited()

    async def test_flag_read_failure_preserves_historical_exclusion(self):
        self.flag_reader.side_effect = ConnectionError("db unavailable")
        self.assertTrue(await self._check(True))

    async def test_admin_toggle_persists_off_to_on(self):
        reader = AsyncMock(return_value=False)
        writer = AsyncMock(return_value=True)
        redraw = AsyncMock()
        env = load_functions(
            "handlers/admin/ai_control.py", {"toggle_vertex_parallel"},
            is_feature_enabled=reader, set_feature_flag=writer,
            _render_ai_menu=redraw,
        )
        call = types.SimpleNamespace(answer=AsyncMock())
        await env["toggle_vertex_parallel"](call)
        reader.assert_awaited_once_with("vertex_parallel_enabled", default=False)
        writer.assert_awaited_once_with("vertex_parallel_enabled", True)
        redraw.assert_awaited_once_with(call)

    async def test_admin_toggle_persists_on_to_off(self):
        reader = AsyncMock(return_value=True)
        writer = AsyncMock(return_value=True)
        redraw = AsyncMock()
        env = load_functions(
            "handlers/admin/ai_control.py", {"toggle_vertex_parallel"},
            is_feature_enabled=reader, set_feature_flag=writer,
            _render_ai_menu=redraw,
        )
        await env["toggle_vertex_parallel"](types.SimpleNamespace(answer=AsyncMock()))
        writer.assert_awaited_once_with("vertex_parallel_enabled", False)

    async def test_failed_admin_save_keeps_menu_unchanged(self):
        reader = AsyncMock(return_value=False)
        writer = AsyncMock(return_value=False)
        redraw = AsyncMock()
        env = load_functions(
            "handlers/admin/ai_control.py", {"toggle_vertex_parallel"},
            is_feature_enabled=reader, set_feature_flag=writer,
            _render_ai_menu=redraw,
        )
        call = types.SimpleNamespace(answer=AsyncMock())
        await env["toggle_vertex_parallel"](call)
        call.answer.assert_awaited_once()
        self.assertTrue(call.answer.call_args.kwargs["show_alert"])
        redraw.assert_not_awaited()

    def test_ai_keyboard_exposes_live_vertex_state(self):
        button = lambda **kwargs: types.SimpleNamespace(**kwargs)
        markup = lambda **kwargs: types.SimpleNamespace(**kwargs)
        fake_types = types.SimpleNamespace(
            InlineKeyboardButton=button, InlineKeyboardMarkup=markup,
        )
        env = load_functions(
            "keyboards.py", {"get_ai_control_keyboard"}, types=fake_types
        )
        def vertex_row(enabled):
            rows = env["get_ai_control_keyboard"](
                vertex_parallel_enabled=enabled
            ).inline_keyboard
            return next(button for row in rows for button in row
                        if button.callback_data == "admin_ai_vertex_parallel_toggle")
        self.assertIn("مستبعد", vertex_row(False).text)
        self.assertIn("مسموح", vertex_row(True).text)


if __name__ == "__main__":
    unittest.main()
