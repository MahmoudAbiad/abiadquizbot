import html
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

try:
    from aiogram import types
except ModuleNotFoundError:
    class _InlineKeyboardButton:
        def __init__(self, text, callback_data=None, url=None, copy_text=None):
            self.text = text
            self.callback_data = callback_data
            self.url = url
            self.copy_text = copy_text

    class _InlineKeyboardMarkup:
        def __init__(self, inline_keyboard):
            self.inline_keyboard = inline_keyboard

    types = SimpleNamespace(
        InlineKeyboardButton=_InlineKeyboardButton,
        InlineKeyboardMarkup=_InlineKeyboardMarkup,
        Message=object,
        CallbackQuery=object,
    )

from reliability_support import load_functions


kb_env = load_functions(
    "keyboards.py",
    {
        "build_contact_button",
        "get_admin_user_actions_keyboard",
        "get_admin_user_delete_menu_keyboard",
        "get_admin_user_delete_confirm_keyboard",
        "get_admin_users_menu_keyboard",
    },
    types=types,
    Optional=object,
)


class AdminUserDeletionAndRedisTests(unittest.IsolatedAsyncioTestCase):
    def test_user_actions_keyboard_includes_redis_and_delete_buttons(self):
        kb = kb_env["get_admin_user_actions_keyboard"](12345, username="student1")
        callbacks = [btn.callback_data for row in kb.inline_keyboard for btn in row if btn.callback_data]
        self.assertIn("admin_user_redis_reset_12345", callbacks)
        self.assertIn("admin_user_delete_menu_12345", callbacks)

    def test_delete_menu_and_confirm_keyboards(self):
        menu_kb = kb_env["get_admin_user_delete_menu_keyboard"](12345, owned_quizzes_count=4)
        callbacks = [btn.callback_data for row in menu_kb.inline_keyboard for btn in row if btn.callback_data]
        self.assertIn("admin_del_preview_q_12345_1", callbacks)
        self.assertIn("admin_del_ask_full_12345", callbacks)
        self.assertIn("admin_del_ask_keepq_12345", callbacks)
        self.assertIn("admin_del_ask_consent_12345", callbacks)
        self.assertIn("admin_del_ask_quizzes_12345", callbacks)
        self.assertIn("admin_user_redis_reset_12345", callbacks)

        confirm_kb = kb_env["get_admin_user_delete_confirm_keyboard"](12345, "keepq", owned_quizzes_count=4)
        confirm_cbs = [btn.callback_data for row in confirm_kb.inline_keyboard for btn in row if btn.callback_data]
        self.assertIn("admin_del_preview_q_12345_1", confirm_cbs)
        self.assertIn("admin_del_exec_keepq_12345", confirm_cbs)
        self.assertIn("admin_user_delete_menu_12345", confirm_cbs)

        consent_kb = kb_env["get_admin_user_delete_confirm_keyboard"](12345, "consent", owned_quizzes_count=4)
        consent_cbs = [btn.callback_data for row in consent_kb.inline_keyboard for btn in row if btn.callback_data]
        self.assertNotIn("admin_del_preview_q_12345_1", consent_cbs)
        self.assertIn("admin_del_exec_consent_12345", consent_cbs)

    async def test_purge_user_consent_cache_clears_memory_and_redis(self):
        uid = 987654
        ok_cache = {(uid, 1, 1): True, (111, 1, 1): True}
        denied_cache = {(uid, 1, 1): "declined"}
        mw = SimpleNamespace(_notices={uid: True})

        fake_redis = SimpleNamespace(
            delete=AsyncMock(return_value=3),
            scan_iter=lambda match=None, count=50: self._empty_async_gen(),
        )
        env = load_functions(
            "services/consent.py",
            {"purge_user_consent_cache", "_ok_redis_key", "_resume_key", "_pending_key", "_pending_cb_key"},
            _ok_cache=ok_cache,
            _denied_cache=denied_cache,
            _active_middlewares=[mw],
            _redis=lambda: fake_redis,
            current_versions=AsyncMock(return_value=(1, 1)),
            logger=SimpleNamespace(warning=lambda *a, **k: None),
        )
        deleted = await env["purge_user_consent_cache"](uid)
        self.assertEqual(deleted, 3)
        self.assertNotIn((uid, 1, 1), ok_cache)
        self.assertIn((111, 1, 1), ok_cache)
        self.assertNotIn((uid, 1, 1), denied_cache)
        self.assertNotIn(uid, mw._notices)

    async def test_execute_user_delete_action_modes_and_admin_guard(self):
        edits = []

        async def fake_edit(msg, text, reply_markup=None):
            edits.append((text, reply_markup))

        delete_rpc = AsyncMock(return_value={
            "status": "ok",
            "deleted_quizzes_count": 2,
            "kept_quizzes_count": 3,
        })
        redis_reset = AsyncMock(return_value={"deleted_keys": 5})
        purge_consent = AsyncMock(return_value=2)
        render_menu = AsyncMock(return_value=True)

        env = load_functions(
            "handlers/admin/users.py",
            {"execute_user_delete_action"},
            ADMIN_ID=999,
            admin_delete_user_action=delete_rpc,
            reset_user_redis_operations=redis_reset,
            purge_user_consent_cache=purge_consent,
            _render_user_delete_menu=render_menu,
            safe_edit_text=fake_edit,
            get_admin_users_menu_keyboard=kb_env["get_admin_users_menu_keyboard"],
            TelegramBadRequest=Exception,
            logger=SimpleNamespace(error=lambda *a, **k: None),
            types=types,
            html=html,
        )

        # 1. Prevent deleting ADMIN_ID
        admin_call = SimpleNamespace(
            data="admin_del_exec_full_999",
            message=SimpleNamespace(),
            answer=AsyncMock(),
        )
        await env["execute_user_delete_action"](admin_call)
        delete_rpc.assert_not_awaited()
        admin_call.answer.assert_awaited_once()

        # 2. Full wipe on regular user
        user_call = SimpleNamespace(
            data="admin_del_exec_full_12345",
            message=SimpleNamespace(),
            answer=AsyncMock(),
        )
        await env["execute_user_delete_action"](user_call)
        delete_rpc.assert_awaited_with(12345, "full_wipe")
        redis_reset.assert_awaited_with(12345, include_consent_cache=True)
        self.assertIn("تم حذف المستخدم وكامل أثره نهائياً", edits[-1][0])

        # 3. Keep quizzes mode
        keep_call = SimpleNamespace(
            data="admin_del_exec_keepq_12345",
            message=SimpleNamespace(),
            answer=AsyncMock(),
        )
        await env["execute_user_delete_action"](keep_call)
        delete_rpc.assert_awaited_with(12345, "keep_quizzes")
        self.assertIn("مع الإبقاء على كويزاته", edits[-1][0])

        # 4. Consent only mode
        consent_call = SimpleNamespace(
            data="admin_del_exec_consent_12345",
            message=SimpleNamespace(),
            answer=AsyncMock(),
        )
        await env["execute_user_delete_action"](consent_call)
        delete_rpc.assert_awaited_with(12345, "consent_only")
        purge_consent.assert_awaited_with(12345)
        render_menu.assert_awaited()

    @staticmethod
    async def _empty_async_gen():
        if False:
            yield None


if __name__ == "__main__":
    unittest.main()
