"""Regression: competitive group quizzes without a per-question timer must still
track answers and publish the final ranking (previously: zero tracking, so no top
names at the end and 0 participants in the admin panel)."""
import html
import types
import unittest
from unittest.mock import AsyncMock, Mock

from reliability_support import load_functions

SID, CHAT = "sess-1", -100123


def _env(session, rows):
    bot = types.SimpleNamespace(send_message=AsyncMock(), stop_poll=AsyncMock())
    redis = types.SimpleNamespace(set=AsyncMock())

    async def safe_call(factory, **_kw):
        return await factory()

    env = load_functions(
        "handlers/group_quiz.py", {"finish_group_session"},
        claim_session_finish=AsyncMock(return_value=True), restore_quiz_lock=AsyncMock(),
        clear_session_questions_cache=AsyncMock(), _pop_open_poll=AsyncMock(return_value=None),
        get_leaderboard=AsyncMock(return_value=rows), bot=bot, redis_client=redis, html=html,
        _safe_call=safe_call, GROUP_TOP_N_MIN=1, GROUP_TOP_N_MAX=20,
        _FINISHED_SESSION_PREFIX="gq:finished:", _FINISHED_SESSION_TTL=7200,
        logger=Mock(), log_error=Mock(),
    )
    return env["finish_group_session"], bot, redis


BASE = dict(id=SID, chat_id=CHAT, poll_is_anonymous=False, show_names=True, top_n=10,
            question_timer_seconds=None, pacing_mode="fixed_interval", message_thread_id=None)
ROWS = [dict(user_id=1, first_name="Ali", username=None, score=5),
        dict(user_id=2, first_name="Sara", username=None, score=3)]


class UntimedRankingTests(unittest.IsolatedAsyncioTestCase):
    async def test_untimed_fixed_interval_publishes_ranking_and_marks_finished(self):
        finish, bot, redis = _env(dict(BASE), ROWS)
        await finish(dict(BASE))
        text = bot.send_message.await_args.kwargs["text"]
        self.assertIn("الترتيب النهائي", text)
        self.assertIn("🥇 Ali", text)
        self.assertIn("🥈 Sara", text)
        self.assertIn("ما بتنحسب", text)
        redis.set.assert_awaited_once_with(f"gq:finished:{SID}", "1", ex=7200)

    async def test_untimed_names_hidden_when_show_names_false(self):
        s = dict(BASE, show_names=False)
        finish, bot, _ = _env(s, ROWS)
        await finish(s)
        text = bot.send_message.await_args.kwargs["text"]
        self.assertIn("لاعب 1", text)
        self.assertNotIn("Ali", text)

    async def test_untimed_burst_keeps_old_message_and_keeps_tracking_open(self):
        s = dict(BASE, pacing_mode="burst")
        finish, bot, redis = _env(s, ROWS)
        await finish(s)
        text = bot.send_message.await_args.kwargs["text"]
        self.assertNotIn("الترتيب النهائي", text)
        redis.set.assert_not_awaited()

    async def test_anonymous_session_has_no_ranking(self):
        s = dict(BASE, poll_is_anonymous=True)
        finish, bot, _ = _env(s, ROWS)
        await finish(s)
        self.assertIn("مجهول", bot.send_message.await_args.kwargs["text"])


if __name__ == "__main__":
    unittest.main()
