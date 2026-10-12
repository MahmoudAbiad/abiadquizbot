"""Experimental progressive private-quiz playback; no database schema changes."""
import asyncio
import contextlib
import os
import uuid

from services.redis_reliability import UserLock


class StreamingSessionDetached(Exception):
    """The old producer must never clear or start a newer user's session."""


def streaming_allowed_by_environment():
    return os.getenv("QUIZ_STREAMING_ENABLED", "1").strip().lower() in {"1", "true", "yes", "on"}


async def streaming_enabled():
    """Read the saved admin switch once per generation; env remains a kill switch."""
    if not streaming_allowed_by_environment():
        return False
    from helpers.supabase_helper import is_feature_enabled
    return await is_feature_enabled("quiz_streaming_enabled", default=True)


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
        self.image_queue = asyncio.Queue()
        self.image_worker = None
        self.images_finished = False

    def prepare_image(self, question, index):
        if not question.get("is_math"):
            return
        self.image_queue.put_nowait((question, index))
        if self.image_worker is None:
            from task_utils import spawn
            self.image_worker = spawn(self._prepare_images(), name="stream-quiz-images")

    def _owns_images(self, data):
        return (data.get("stream_id") == self.identity
                or data.get("stream_poll_session") == self.identity)

    async def _prepare_images(self):
        from services.quiz_image_preparation import prepare_math_image, question_image_key
        from logger import get_logger
        logger = get_logger(__name__)
        while True:
            item = await self.image_queue.get()
            try:
                if item is None:
                    return
                question, index = item
                data = await self.state.get_data()
                if not self._owns_images(data):
                    return
                total = data.get("total_count", self.count)
                image_url, _ = await prepare_math_image(question, index, total, data.get("quiz_id"))
                if not image_url:
                    continue
                # Rendering never holds the playback lock. Merge only the URL
                # into fresh state, preserving answers, score and new questions.
                lock = UserLock(self.redis, self.user_id)
                await acquire_wait(lock)
                try:
                    data = await self.state.get_data()
                    if not self._owns_images(data):
                        return
                    questions = list(data.get("questions") or [])
                    if index >= len(questions) or question_image_key(questions[index], index, total) != question_image_key(question, index, total):
                        continue
                    questions[index]["image_url"] = image_url
                    await self.state.update_data(questions=questions)
                    quiz_id = data.get("quiz_id")
                finally:
                    await lock.release()
                if quiz_id:
                    from helpers.supabase_helper import save_question_image_url
                    await save_question_image_url(quiz_id, index, image_url)
            except Exception as exc:
                logger.warning("Streaming image preparation failed for q%s: %s", item[1] if item else "?", exc)
            finally:
                self.image_queue.task_done()

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
                self.prepare_image(question, 0)
                if not data.get("stream_paused"):
                    await send_question_by_ids(self.chat_id, self.user_id, self.state)
            else:
                questions = list(data.get("questions") or [])
                questions.append(question)
                await self.state.update_data(questions=questions)
                self.prepare_image(question, len(questions) - 1)
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
        # Images prepared before the quiz acquired a database ID are now
        # persisted; later worker results persist themselves using fresh state.
        for index, question in enumerate(shown):
            if question.get("image_url"):
                questions[index]["image_url"] = question["image_url"]
                if quiz_id:
                    # Reuse the single worker for sequential URL persistence,
                    # never launch competing JSON read/modify/write updates.
                    self.prepare_image(question, index)
        attempt = start_quiz_attempt(self.user_id, quiz_id or None,
                                     data.get("quiz_origin", "file"), len(questions))
        # Preserve navigation_session_id for polls already sent with this ID.
        await self.state.update_data(
            generation_pending=False, stream_id=None, total_count=len(questions),
            quiz_id=quiz_id, attempt_id=attempt, stream_poll_session=self.identity,
            stream_waiting=False)
        await self.state.set_state(QuizState.answering_quiz)
        self.images_finished = True
        if self.image_worker is not None:
            self.image_queue.put_nowait(None)
        if data.get("stream_paused"):
            await self.state.update_data(current_index=len(questions), stopped_early=True)
            await send_question_by_ids(self.chat_id, self.user_id, self.state)
        elif data.get("stream_waiting") or data.get("current_index", 0) >= len(questions):
            await send_question_by_ids(self.chat_id, self.user_id, self.state)

    async def close(self):
        if self.image_worker is not None and not self.images_finished:
            self.image_worker.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self.image_worker
        if self.lease_acquired:
            await self.lease.release()
            self.lease_acquired = False
        data = await self.state.get_data()
        if data.get("stream_id") == self.identity:
            await self.state.update_data(generation_pending=False, stream_id=None,
                                         stream_waiting=False, stream_paused=False,
                                         questions=[], quiz_completed=True)

