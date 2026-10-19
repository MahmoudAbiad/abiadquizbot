"""Admin Vertex multi-part switch: UI, persisted toggle and error handling."""
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from keyboards import get_ai_control_keyboard
from reliability_support import load_functions


class VertexParallelKeyboardTests(unittest.TestCase):
    def test_label_and_callback_follow_actual_saved_state(self):
        for enabled, icon in [(False, "🚫"), (True, "✅")]:
            with self.subTest(enabled=enabled):
                keyboard = get_ai_control_keyboard(
                    120, vertex_parallel_enabled=enabled,
                )
                buttons = [button for row in keyboard.inline_keyboard for button in row]
                vertex = [button for button in buttons
                          if button.callback_data == "admin_ai_vertex_parallel_toggle"]
                self.assertEqual(len(vertex), 1)
                self.assertIn(icon, vertex[0].text)
                self.assertIn("Vertex", vertex[0].text)


class VertexParallelAdminTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.read_flag = AsyncMock(return_value=False)
        self.set_flag = AsyncMock(return_value=True)
        self.render = AsyncMock()
        self.log = Mock()
        self.env = load_functions(
            "handlers/admin/ai_control.py", {"toggle_vertex_parallel"},
            is_feature_enabled=self.read_flag, set_feature_flag=self.set_flag,
            _render_ai_menu=self.render, logger=self.log,
        )
        self.call = SimpleNamespace(answer=AsyncMock())

    async def test_enables_and_refreshes_menu(self):
        await self.env["toggle_vertex_parallel"](self.call)
        self.read_flag.assert_awaited_once_with("vertex_parallel_enabled", default=False)
        self.set_flag.assert_awaited_once_with("vertex_parallel_enabled", True)
        self.render.assert_awaited_once_with(self.call)

    async def test_disables_when_on(self):
        self.read_flag.return_value = True
        await self.env["toggle_vertex_parallel"](self.call)
        self.set_flag.assert_awaited_once_with("vertex_parallel_enabled", False)
        self.render.assert_awaited_once_with(self.call)

    async def test_failed_save_does_not_change_display(self):
        self.set_flag.return_value = False
        await self.env["toggle_vertex_parallel"](self.call)
        self.render.assert_not_awaited()
        self.call.answer.assert_awaited_once()
        self.assertTrue(self.call.answer.await_args.kwargs["show_alert"])

    async def test_read_failure_does_not_write(self):
        self.read_flag.side_effect = RuntimeError("database unavailable")
        await self.env["toggle_vertex_parallel"](self.call)
        self.set_flag.assert_not_awaited()
        self.render.assert_not_awaited()
        self.call.answer.assert_awaited_once()
        self.assertTrue(self.call.answer.await_args.kwargs["show_alert"])


if __name__ == "__main__":
    unittest.main()
