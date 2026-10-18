"""Regression: poll_answer must be routed by explicit is_group, not session_id."""
import json
import types
import unittest
from unittest.mock import AsyncMock, Mock

from aiogram.dispatcher.event.bases import SkipHandler
from reliability_support import load_functions


class PollAnswerRoutingTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.poll = types.SimpleNamespace(
            poll_id="testpoll",
            option_ids=[1],
            user=types.SimpleNamespace(id=42, username="tester", first_name="Student"),
        )
        self.db = types.SimpleNamespace(get=AsyncMock())
        self.record = AsyncMock(return_value=True)
        self.bump = AsyncMock()
        self.env = load_functions(
            "handlers/group_quiz.py",
            {"handle_group_poll_answer"},
            redis_client=self.db,
            SkipHandler=SkipHandler,
            json=json,
            time=types.SimpleNamespace(time=lambda: 100),
            record_answer=self.record,
            bump_participant=self.bump,
            logger=Mock(),
            log_error=Mock(),
        )
        self.handler = self.env["handle_group_poll_answer"]

    async def test_private_poll_with_session_id_is_not_consumed_by_group_handler(self):
        # Private polls carry session_id as part of FSM session identity too.
        self.db.get.return_value = json.dumps({
            "session_id": "private-session",
            "user_id": 42,
            "correct_option_id": 0,
            "question_index": 0,
        })
        with self.assertRaises(SkipHandler):
            await self.handler(self.poll)
        self.record.assert_not_awaited()

    async def test_group_poll_with_timer_is_processed(self):
        self.db.get.return_value = json.dumps({
            "session_id": "group-session",
            "is_group": True,
            "has_timer": True,
            "user_id": 99,
            "correct_option_id": 1,
            "question_index": 3,
            "sent_at_ms": 90000,
        })
        await self.handler(self.poll)
        self.record.assert_awaited_once_with(
            session_id="group-session", user_id=42, question_index=3,
            poll_id="testpoll", selected_option=1, is_correct=True,
        )
        self.bump.assert_awaited_once()

    async def test_group_poll_without_timer_keeps_no_tracking_design(self):
        self.db.get.return_value = json.dumps({
            "session_id": "group-session",
            "is_group": True,
            "has_timer": False,
        })
        await self.handler(self.poll)
        self.record.assert_not_awaited()
        self.bump.assert_not_awaited()
