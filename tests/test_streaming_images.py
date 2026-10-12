"""Image lookahead races, bounded work and fallback without live credentials."""
import asyncio
import contextlib
import copy
import types
import unittest
from unittest.mock import AsyncMock, Mock, patch

import test_streaming_quiz as support
from services import quiz_image_preparation as images
from services.redis_reliability import UserLock
from services.streaming_quiz import StreamingQuiz, StreamingSessionDetached
from reliability_support import load_functions


def question(text="math"):
    return {**support.q(text), "is_math": True}


class PreparationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        images._urls.clear()
        images._inflight.clear()
        self.release = asyncio.Event()
        self.started = asyncio.Event()
        async def prepare(*args):
            self.started.set()
            await self.release.wait()
            return "https://images/question.png", None
        self.prepare = AsyncMock(side_effect=prepare)
        self.patch = patch.object(images, "_prepare", self.prepare)
        self.patch.start()
        self.addCleanup(self.patch.stop)

    async def asyncTearDown(self):
        self.release.set()
        await asyncio.gather(*list(images._inflight.values()), return_exceptions=True)
        images._urls.clear()

    async def test_next_joins_prefetch_and_keeps_url_without_database_id(self):
        background, foreground = question(), question()
        prefetch = asyncio.create_task(images.prepare_math_image(background, 1, 40))
        await self.started.wait()
        sending = asyncio.create_task(images.prepare_math_image(foreground, 1, 40))
        await asyncio.sleep(0)
        self.assertEqual(self.prepare.await_count, 1)
        self.release.set()
        self.assertEqual(await prefetch, await sending)
        self.assertEqual(foreground["image_url"], "https://images/question.png")
        fresh = question()
        await images.prepare_math_image(fresh, 1, 40, "saved-quiz")
        self.assertEqual(self.prepare.await_count, 1)
        self.assertEqual(fresh["image_url"], foreground["image_url"])

    async def test_cancelling_prefetch_does_not_cancel_foreground_render(self):
        prefetch = asyncio.create_task(images.prepare_math_image(question(), 1, 40))
        await self.started.wait()
        sending = asyncio.create_task(images.prepare_math_image(question(), 1, 40))
        prefetch.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await prefetch
        self.release.set()
        self.assertEqual((await sending)[0], "https://images/question.png")
        self.assertEqual(self.prepare.await_count, 1)

    async def test_upload_failure_preserves_bytes_and_allows_later_retry(self):
        self.prepare.side_effect = [(None, b"png"), ("https://images/retry.png", None)]
        first = question()
        self.assertEqual(await images.prepare_math_image(first, 0, 40), (None, b"png"))
        self.assertNotIn("image_url", first)
        self.assertEqual((await images.prepare_math_image(first, 0, 40))[0], "https://images/retry.png")

    async def test_changed_choices_or_header_do_not_reuse_stale_image(self):
        self.release.set()
        original = question()
        await images.prepare_math_image(original, 0, 40)
        changed = question()
        changed["options"].reverse()
        await images.prepare_math_image(changed, 0, 40)
        await images.prepare_math_image(question(), 0, 21)
        self.assertEqual(self.prepare.await_count, 3)

    async def test_cache_is_bounded_and_contains_urls_only(self):
        self.release.set()
        with patch.object(images, "_CACHE_LIMIT", 2):
            for index in range(3):
                await images.prepare_math_image(question(str(index)), index, 3)
        self.assertEqual(len(images._urls), 2)
        self.assertTrue(all(isinstance(value[0], str) for value in images._urls.values()))

    async def test_global_slots_bound_render_and_upload_across_users(self):
        self.patch.stop()
        active, peak = 0, 0
        two_active = asyncio.Event()
        release = asyncio.Event()
        async def render(*args):
            nonlocal active, peak
            active += 1
            peak = max(peak, active)
            if active == 2:
                two_active.set()
            await release.wait()
            return b"png"
        async def upload(*args):
            nonlocal active
            active -= 1
            return "https://images/bounded.png"
        modules = {
            "services.image_quiz_renderer": types.SimpleNamespace(render_question_image_async=render, looks_arabic=lambda s:False),
            "services.quiz_engine": types.SimpleNamespace(_question_image_object_path=lambda *a:"path"),
            "helpers.supabase_helper": types.SimpleNamespace(upload_quiz_question_image=upload),
        }
        with patch.dict("sys.modules", modules), patch.object(images, "_PREPARATION_SLOTS", asyncio.Semaphore(2)):
            tasks = [asyncio.create_task(images.prepare_math_image(question(str(i)), i, 5)) for i in range(5)]
            try:
                await asyncio.wait_for(two_active.wait(), 1)
                self.assertEqual(peak, 2)
            finally:
                release.set()
                await asyncio.gather(*tasks)
        self.assertEqual(peak, 2)


class StreamingLookaheadTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.redis = support.FakeRedis()
        self.state = support.State()
        self.lock = UserLock(self.redis, 7)
        await self.lock.acquire()
        self.sent = AsyncMock()
        self.save = AsyncMock()
        self.modules = patch.dict("sys.modules", {
            "config": types.SimpleNamespace(QuizState=types.SimpleNamespace(answering_quiz="answering")),
            "handlers.quiz_runner": types.SimpleNamespace(send_question_by_ids=self.sent),
            "helpers.supabase_helper": types.SimpleNamespace(start_quiz_attempt=Mock(return_value="attempt"), save_question_image_url=self.save),
        })
        self.modules.start()
        self.addCleanup(self.modules.stop)
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        async def prepare(q, index, total, quiz_id=None):
            self.started.set()
            await self.release.wait()
            return f"https://images/{index}.png", None
        self.prepare = AsyncMock(side_effect=prepare)
        p = patch.object(images, "prepare_math_image", self.prepare)
        p.start()
        self.addCleanup(p.stop)
        self.session = StreamingQuiz(self.state, 7, 7, {}, 3, self.lock, self.redis)
        await self.session.begin()

    async def asyncTearDown(self):
        self.release.set()
        with contextlib.suppress(StreamingSessionDetached):
            await self.session.reacquire()
        await self.session.close()
        await self.lock.release()
        if self.session.image_worker:
            with contextlib.suppress(asyncio.CancelledError):
                await asyncio.wait_for(self.session.image_worker, 1)

    async def drain(self):
        await asyncio.wait_for(self.session.image_queue.join(), 1)

    async def test_prepares_future_question_before_next_and_generation_finish(self):
        await self.session.publish(question("first"))
        await self.started.wait()
        await self.session.publish(question("second"))
        await self.state.update_data(score=1, answered_questions={"0":1}, current_index=1)
        self.release.set()
        await self.drain()
        self.assertEqual(self.prepare.await_count, 2)
        self.sent.assert_awaited_once()
        data = self.state.data
        self.assertTrue(data["generation_pending"])
        self.assertEqual(data["questions"][1]["image_url"], "https://images/1.png")
        self.assertEqual(data["score"], 1)
        self.assertEqual(data["answered_questions"], {"0":1})
        self.assertEqual(data["current_index"], 1)

    async def test_replaced_session_is_not_overwritten_by_late_image(self):
        await self.session.publish(question())
        await self.started.wait()
        await self.state.update_data(stream_id="new", stream_poll_session="new", questions=[question("new")])
        self.release.set()
        await asyncio.wait_for(self.session.image_worker, 1)
        self.assertNotIn("image_url", self.state.data["questions"][0])
        self.save.assert_not_awaited()

    async def test_failure_of_one_image_does_not_stop_remaining_preparation(self):
        self.prepare.side_effect = [RuntimeError("render failed"), ("https://images/1.png", None)]
        await self.session.publish(question("first"))
        await self.session.publish(question("second"))
        await self.drain()
        self.assertNotIn("image_url", self.state.data["questions"][0])
        self.assertEqual(self.state.data["questions"][1]["image_url"], "https://images/1.png")

    async def test_finished_generation_keeps_pending_images_and_saves_urls(self):
        await self.session.publish(question("first"))
        await self.started.wait()
        await self.session.publish(question("second"))
        await self.session.reacquire()
        await self.session.finish([question("first"), question("second")], "saved")
        await self.session.close()
        self.assertFalse(self.session.image_worker.cancelled())
        await self.lock.release()
        self.release.set()
        await asyncio.wait_for(self.session.image_worker, 1)
        self.assertEqual(self.state.data["questions"][1]["image_url"], "https://images/1.png")
        self.save.assert_any_await("saved", 1, "https://images/1.png")

    async def test_plain_questions_do_not_start_image_worker(self):
        await self.session.publish(support.q())
        self.assertIsNone(self.session.image_worker)
        self.prepare.assert_not_awaited()

    async def test_failed_generation_cancels_queued_image_worker(self):
        await self.session.publish(question())
        await self.started.wait()
        await self.session.reacquire()
        await self.session.close()
        self.assertTrue(self.session.image_worker.cancelled())
        self.assertEqual(self.state.data["questions"], [])
        self.save.assert_not_awaited()

    async def test_urls_ready_before_finish_are_persisted_after_database_id_exists(self):
        await self.session.publish(question())
        self.release.set()
        await self.drain()
        self.save.assert_not_awaited()
        await self.session.reacquire()
        await self.session.finish([question()], "saved")
        await self.session.close()
        await self.lock.release()
        await asyncio.wait_for(self.session.image_worker, 1)
        self.save.assert_awaited_once_with("saved", 0, "https://images/0.png")


class ForegroundTests(unittest.IsolatedAsyncioTestCase):
    async def test_sending_streamed_math_keeps_url_and_raw_upload_fallback(self):
        for uploaded in (True, False):
            q = question()
            async def prepare(q, *args):
                if uploaded:
                    q["image_url"] = "https://images/ready.png"
                    return q["image_url"], None
                return None, b"ready-png"
            poll = types.SimpleNamespace(poll=types.SimpleNamespace(id="poll"))
            bot = types.SimpleNamespace(send_photo=AsyncMock(), send_poll=AsyncMock(return_value=poll))
            render = AsyncMock()
            env = load_functions("services/quiz_engine.py", {"_send_math_image_question"},
                logger=Mock(), prepare_math_image=prepare, render_question_image_async=render,
                looks_arabic=lambda s:True, letters_for=lambda *a:["أ","ب","ج","د"],
                _is_valid_uuid=lambda s:False, save_question_image_url=AsyncMock(),
                bot=bot, redis_client=support.FakeRedis(), json=__import__("json"),
                latex_to_plain=lambda s:s, _sanitize_open_period=lambda s:s,
                types=types.SimpleNamespace(BufferedInputFile=lambda data, **kw: data))
            await env["_send_math_image_question"](7, 7, q, 0, 40, None, None)
            render.assert_not_awaited()
            expected = "https://images/ready.png" if uploaded else b"ready-png"
            self.assertEqual(bot.send_photo.call_args.kwargs["photo"], expected)
            if uploaded:
                self.assertEqual(q["image_url"], expected)
            bot.send_poll.assert_awaited_once()
