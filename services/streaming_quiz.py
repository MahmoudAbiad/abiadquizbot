"""Experimental progressive private-quiz playback; no database schema changes."""
import asyncio
import os
import uuid

from services.redis_reliability import UserLock


class StreamingSessionDetached(Exception):
    """The old producer must never clear or start a newer user's session."""


def streaming_enabled():
    return os.getenv("QUIZ_STREAMING_ENABLED", "1").strip().lower() in {"1", "true", "yes", "on"}


def producer_key(user_id):
    return f"quiz:stream:producer:{user_id}"


async def acquire_wait(lock):
    deadline = asyncio.get_running_loop().time() + 30
    while not await lock.acquire():
        if asyncio.get_running_loop().time() >= deadline:
            raise RuntimeError("Timed out waiting for quiz session lock")
        await asyncio.sleep(0.05)


class StreamingQuiz:
    def __init__(self, state, user_id, chat_id, data, count, main_lock, redis):
        self.state, self.user_id, self.chat_id = state, user_id, chat_id
        self.data, self.count = data, count
        self.main_lock, self.redis = main_lock, redis
        self.identity = uuid.uuid4().hex
        self.lease = UserLock(redis, user_id)
        self.lease.key = producer_key(user_id)
        self.started = False
        self.released = False
        self.lease_acquired = False

    async def begin(self):
        if not await self.lease.acquire():
            raise RuntimeError("Another stream already owns this user")
        self.lease_acquired = True
        await self.state.update_data(stream_id=self.identity, generation_pending=True,
                                     stream_waiting=False, stream_paused=False)
        await self.main_lock.release()
        self.released = True

    async def publish(self, question):
        from config import QuizState
        from handlers.quiz_runner import send_question_by_ids
        lock = UserLock(self.redis, self.user_id)
        await acquire_wait(lock)
        try:
            data = await self.state.get_data()
            if data.get("stream_id") != self.identity:
                raise RuntimeError("Streaming session was replaced")
            if not self.started:
                await self.state.update_data(
                    questions=[question], current_index=0, score=0, answered_questions={},
                    navigation_session_id=self.identity, stream_poll_session=self.identity, total_count=self.count,
                    source_title=self.data.get("source_title", "كويز"),
                    quiz_origin="file" if self.data.get("input_type") == "media" else "text",
                    quiz_completed=False, quiz_id=None, attempt_id=None,
                    is_saved_in_session=False, is_switching_question=False,
                    stopped_early=False, share_id=None)
                await self.state.set_state(QuizState.answering_quiz)
                self.started = True
                if not data.get("stream_paused"):
                    await send_question_by_ids(self.chat_id, self.user_id, self.state)
            else:
                questions = list(data.get("questions") or [])
                questions.append(question)
                await self.state.update_data(questions=questions)
                if data.get("stream_waiting") and not data.get("stream_paused"):
                    await self.state.update_data(stream_waiting=False)
                    await send_question_by_ids(self.chat_id, self.user_id, self.state)
        finally:
            await lock.release()

    async def reacquire(self):
        # The caller resumes its original error/refund and finalization flow
        # only while owning the same lock as poll answers and Next.
        if self.released:
            await acquire_wait(self.main_lock)
            self.released = False
        if self.lease_acquired and (await self.state.get_data()).get("stream_id") != self.identity:
            raise StreamingSessionDetached()

    async def finish(self, questions, quiz_id):
        from config import QuizState
        from handlers.quiz_runner import send_question_by_ids
        from helpers.supabase_helper import start_quiz_attempt
        data = await self.state.get_data()
        if data.get("stream_id") != self.identity:
            return
        # Keep rendered image URLs and every answer accumulated while streaming.
        shown = data.get("questions") or []
        if len(shown) != len(questions):
            raise RuntimeError("Playback differs from saved streaming quiz")
        attempt = start_quiz_attempt(self.user_id, quiz_id or None,
                                     data.get("quiz_origin", "file"), len(questions))
        # Preserve navigation_session_id for polls already sent with this ID.
        await self.state.update_data(
            generation_pending=False, stream_id=None, total_count=len(questions),
            quiz_id=quiz_id, attempt_id=attempt, stream_poll_session=self.identity,
            stream_waiting=False)
        await self.state.set_state(QuizState.answering_quiz)
        if data.get("stream_paused"):
            await self.state.update_data(current_index=len(questions), stopped_early=True)
            await send_question_by_ids(self.chat_id, self.user_id, self.state)
        elif data.get("stream_waiting") or data.get("current_index", 0) >= len(questions):
            await send_question_by_ids(self.chat_id, self.user_id, self.state)

    async def close(self):
        if self.lease_acquired:
            await self.lease.release()
            self.lease_acquired = False
        data = await self.state.get_data()
        if data.get("stream_id") == self.identity:
            await self.state.update_data(generation_pending=False, stream_id=None,
                                         stream_waiting=False, stream_paused=False,
                                         questions=[], quiz_completed=True)
