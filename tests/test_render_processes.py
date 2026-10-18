"""Real spawn/render overlap and HTTP admission/cancellation regressions."""
import asyncio
import importlib
import json
import multiprocessing
import os
from pathlib import Path
import subprocess
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def render_probe(payload):
    # Barrier is reached INSIDE the actual renderer's lock. Both processes must
    # enter it before either may draw, proving that their locks are independent.
    import time
    from services import image_quiz_renderer as renderer
    original = renderer._render_question_image_impl
    times = []

    def measured(*args):
        payload["barrier"].wait(timeout=30)
        times.append(time.monotonic())
        image = original(*args)
        times.append(time.monotonic())
        return image

    renderer._render_question_image_impl = measured
    try:
        image = renderer.render_question_image(
            {"question": "Solve $x^2+2x+1=0$", "options": ["$-1$", "$1$"]}, 0, 1, False
        )
        assert image.startswith(b"\x89PNG")
        result = {"pid": os.getpid(), "start": times[0], "end": times[1],
                  "png_bytes": len(image), "bot_loaded": "config" in sys.modules}
        return json.dumps(result).encode(), "application/json"
    finally:
        renderer._render_question_image_impl = original


class ProcessTests(unittest.IsolatedAsyncioTestCase):
    async def test_real_http_routes_use_spawned_pool_and_keep_parent_light(self):
        code = '''
import asyncio, os, sys, logging
os.environ['OFFLOAD_WORKER_SECRET']='integration-test'
import httpx, render_worker as w
logging.getLogger().setLevel(logging.ERROR)
async def main():
    async with w.lifespan(w.app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=w.app), base_url='http://worker', headers={'X-Worker-Secret':'integration-test'}) as c:
            question = {'question':'Solve $x+1=2$', 'options':['1','2'], 'correct_option_id':0}
            requests = [
                ('/v1/render/question-image', {'question':question,'idx':0,'total':1,'is_ar':False}, b'\\x89PNG'),
                ('/v1/export/quiz', {'fmt':'docx','questions':[question]}, b'PK'),
                ('/v1/export/document', {'fmt':'pdf','text':'Lecture'}, b'%PDF-')]
            responses = await asyncio.gather(*(c.post(path,json=payload) for path,payload,_ in requests))
            for response, (_,_,magic) in zip(responses, requests):
                assert response.status_code == 200, response.text
                assert response.content.startswith(magic)
            from services import offload as client
            client.ENABLED = True
            client._client = c
            assert (await client.remote_render_image(question,0,1,False)).startswith(b'\\x89PNG')
            client.ENABLED = False
            assert await client.remote_render_image(question,0,1,False) is None
            assert (await c.post('/v1/export/quiz',json={'fmt':'pdf','questions':[]})).status_code == 422
            assert (await c.post('/v1/export/quiz',json={'fmt':'exe'})).status_code == 400
            # Losing a child must not leave a permanently broken executor.
            try:
                await w._submit(os._exit, 1)
                raise AssertionError('expected worker failure')
            except w.HTTPException as exc:
                assert exc.status_code == 503
            recovered = await c.post('/v1/export/document',json={'fmt':'pdf','text':'Recovered'})
            assert recovered.status_code == 200
            assert recovered.content.startswith(b'%PDF-')
    assert 'config' not in sys.modules
    assert 'matplotlib' not in sys.modules
    assert 'services.export_service' not in sys.modules
asyncio.run(main())
'''
        completed = await asyncio.to_thread(subprocess.run, [sys.executable, "-c", code],
                                            cwd=ROOT, capture_output=True, text=True, timeout=60)
        self.assertEqual(completed.returncode, 0, completed.stderr)

    async def test_two_real_renderers_overlap_in_separate_processes(self):
        # Other tests may install FastAPI stubs; the process executor itself
        # must still run unmodified here.
        from concurrent.futures import ProcessPoolExecutor
        from services.offload_process import execute
        context = multiprocessing.get_context("spawn")
        with context.Manager() as manager:
            with ProcessPoolExecutor(max_workers=2, mp_context=context) as pool:
                loop = asyncio.get_running_loop()
                barrier = manager.Barrier(2)
                futures = [loop.run_in_executor(pool, execute, render_probe, {"barrier": barrier})
                           for _ in range(2)]
                results = await asyncio.wait_for(asyncio.gather(*futures), timeout=60)
        records = [json.loads(result[1]) for result in results]
        self.assertTrue(all(result[0] == "ok" for result in results))
        self.assertEqual(len({r["pid"] for r in records}), 2)
        self.assertLess(max(r["start"] for r in records), min(r["end"] for r in records))
        self.assertTrue(all(not r["bot_loaded"] for r in records))

    async def test_spawning_entry_point_never_imports_bot(self):
        code = '''
import os, sys, unittest.mock
os.environ['APP_ROLE']='render_worker'
with unittest.mock.patch('os.execv', side_effect=SystemExit(0)) as replace:
    try:
        import runpy
        runpy.run_module('main', run_name='__main__')
    except SystemExit:
        pass
    assert replace.call_count == 1
    assert replace.call_args.args[1] == [sys.executable, '-m', 'render_worker']
    assert 'config' not in sys.modules
'''
        completed = await asyncio.to_thread(subprocess.run, [sys.executable, "-c", code],
                                            cwd=ROOT, capture_output=True, text=True, timeout=15)
        self.assertEqual(completed.returncode, 0, completed.stderr)


class AdmissionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        import render_worker
        self.w = importlib.reload(render_worker)
        self.w.WORKER_SECRET = "test"
        self.started = asyncio.Event()
        self.finish = asyncio.Event()
        self.active = 0
        self.peak = 0
        async def submit(work, payload):
            self.active += 1
            self.peak = max(self.peak, self.active)
            self.started.set()
            try:
                await self.finish.wait()
                return "ok", b"PNG", "image/png"
            finally:
                self.active -= 1
        self.w._submit = submit

    def request(self):
        class Request:
            headers = {"x-worker-secret": "test"}
            async def body(self):
                return b'{}'
        return Request()

    async def test_cancelled_request_keeps_slot_until_job_finishes(self):
        task = asyncio.create_task(self.w._run(self.request(), None))
        await self.started.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(self.w._slots._value, self.w.CONCURRENCY - 1)
        self.assertEqual(self.w._admitted, 1)
        self.finish.set()
        for _ in range(10):
            await asyncio.sleep(0)
        self.assertEqual(self.w._slots._value, self.w.CONCURRENCY)
        self.assertEqual(self.w._admitted, 0)

    async def test_burst_has_bounded_queue_and_execution(self):
        self.w.MAX_QUEUE = 2
        tasks = [asyncio.create_task(self.w._run(self.request(), None)) for _ in range(20)]
        for _ in range(20):
            await asyncio.sleep(0)
        self.assertEqual(self.w._admitted, self.w.CONCURRENCY + 2)
        self.assertEqual(self.peak, self.w.CONCURRENCY)
        self.finish.set()
        results = await asyncio.gather(*tasks, return_exceptions=True)
        rejected = [r for r in results if isinstance(r, Exception)]
        self.assertEqual(len(rejected), 20 - self.w.CONCURRENCY - 2)
        self.assertTrue(all(r.status_code == 503 for r in rejected))
        self.assertEqual(self.w._admitted, 0)


if __name__ == "__main__":
    unittest.main()
