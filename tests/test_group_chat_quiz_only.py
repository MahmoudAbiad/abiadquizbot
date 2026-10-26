"""Group chats should only accept explicit quiz launches and quiz controls."""
import types
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, Mock

from reliability_support import ROOT, load_functions


class GroupChatQuizOnlyTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        env = load_functions(
            "middlewares.py",
            {"GroupQuizOnlyMiddleware"},
            BaseMiddleware=object,
            logger=Mock(),
        )
        self.guard = env["GroupQuizOnlyMiddleware"]()
        self.handler = AsyncMock(return_value="processed")

    @staticmethod
    def message(chat_type, text=None):
        return types.SimpleNamespace(
            chat=types.SimpleNamespace(type=chat_type), text=text,
        )

    async def dispatch(self, message=None, callback=None, edited_message=None, **other):
        update = types.SimpleNamespace(
            message=message, callback_query=callback,
            edited_message=edited_message, **other,
        )
        return await self.guard(self.handler, update, {})

    async def test_ordinary_group_text_never_enters_generation(self):
        for chat_type in ("group", "supergroup"):
            for value in ("مالقيتهن", "This is a very long message that could be a quiz", "/help", "/start", "42"):
                with self.subTest(chat_type=chat_type, text=value):
                    self.handler.reset_mock()
                    self.assertIsNone(await self.dispatch(message=self.message(chat_type, value)))
                    self.handler.assert_not_awaited()

    async def test_group_files_and_voice_are_silent(self):
        for chat_type in ("group", "supergroup"):
            self.handler.reset_mock()
            self.assertIsNone(await self.dispatch(message=self.message(chat_type)))
            self.handler.assert_not_awaited()

    async def test_only_explicit_group_quiz_commands_pass(self):
        for text in ("/start gq_abc", "/start@AbiadQuizMakerbot gq_abc",
                     "/groupquiz abc", "/groupquiz@AbiadQuizMakerbot gq_abc"):
            with self.subTest(text=text):
                self.handler.reset_mock()
                self.assertEqual("processed", await self.dispatch(message=self.message("supergroup", text)))
                self.handler.assert_awaited_once()
        for text in ("/start share_abc", "/start gq_", "/groupquiz", "/help abc"):
            with self.subTest(text=text):
                self.handler.reset_mock()
                self.assertIsNone(await self.dispatch(message=self.message("group", text)))
                self.handler.assert_not_awaited()

    async def test_private_messages_are_unchanged(self):
        self.assertEqual("processed", await self.dispatch(message=self.message("private", "hi")))
        self.handler.assert_awaited_once()

    async def test_group_non_quiz_callbacks_are_silenced(self):
        for data in ("recharge_info", "student_home", "qcount_pick_10", None):
            with self.subTest(data=data):
                self.handler.reset_mock()
                callback = types.SimpleNamespace(
                    message=self.message("group"), data=data, answer=AsyncMock(),
                )
                self.assertIsNone(await self.dispatch(callback=callback))
                self.handler.assert_not_awaited()
                callback.answer.assert_awaited_once_with()

    async def test_group_quiz_callbacks_still_work(self):
        for prefix in ("gqa:", "gqn:", "gqk:", "gqt:", "gqp:", "gqc:",
                       "gqe:", "gqg:", "gqgr:", "gqchl:", "gqchr:", "gqchp:"):
            with self.subTest(prefix=prefix):
                self.handler.reset_mock()
                callback = types.SimpleNamespace(
                    message=self.message("supergroup"), data=prefix + "abc", answer=AsyncMock(),
                )
                self.assertEqual("processed", await self.dispatch(callback=callback))
                self.handler.assert_awaited_once()
                callback.answer.assert_not_awaited()

    async def test_poll_votes_and_membership_events_pass_through(self):
        for event in ({"poll_answer": types.SimpleNamespace(option_ids=[0])},
                      {"poll": types.SimpleNamespace(id="p1")},
                      {"my_chat_member": types.SimpleNamespace()}):
            self.handler.reset_mock()
            self.assertEqual("processed", await self.dispatch(**event))
            self.handler.assert_awaited_once()

    async def test_edited_group_messages_are_ignored(self):
        self.assertIsNone(await self.dispatch(edited_message=self.message("group", "hi")))
        self.handler.assert_not_awaited()

    def test_guard_is_first_update_middleware(self):
        source = (ROOT / "main.py").read_text(encoding="utf-8")
        guard = source.index("dp.update.outer_middleware(GroupQuizOnlyMiddleware())")
        maintenance = source.index("dp.update.outer_middleware(MaintenanceMiddleware())")
        error = source.index("dp.update.outer_middleware(ErrorTrackingMiddleware())")
        self.assertLess(guard, maintenance)
        self.assertLess(guard, error)

    def test_input_routers_also_require_private_chat(self):
        for path in ("handlers/files.py", "handlers/audio.py"):
            source = (ROOT / path).read_text(encoding="utf-8")
            self.assertIn('router.message.filter(F.chat.type == "private")', source)
