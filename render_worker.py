"""
render_worker.py
==============================================================================
السيرفر الثاني (render worker): يستقبل طلبات رسم/تصدير ثقيلة من السيرفر الأساسي ويرجع
الملف الناتج. لا يعرف شيئاً عن تيليجرام ولا Supabase ولا Redis - مجرد "آلة تحويل":
JSON داخل، bytes خارج. لذلك يحتاج متغيرين فقط على Heroku (APP_ROLE وسرّ مشترك).

التشغيل: نفس المستودع ونفس Procfile. تعيين APP_ROLE=render_worker يجعل main.py يشغّل
هذا الملف بدل البوت (راجع أعلى main.py).

نقاط النهاية:
    GET/HEAD /health                    بلا مصادقة - لأداة الـ ping التي تبقيه مستيقظاً
    POST /v1/render/question-image      صورة PNG لسؤال رياضي
    POST /v1/export/quiz                ملف Word/PDF لكويز
    POST /v1/export/document            ملف Word/PDF لنص حر
المصادقة: ترويسة X-Worker-Secret (مقارنة ثابتة الزمن) - fail-closed لو السر غير مضبوط.
==============================================================================
"""

import asyncio
import hmac
import json
import multiprocessing
import os
from concurrent.futures import ProcessPoolExecutor
from concurrent.futures.process import BrokenProcessPool
from contextlib import asynccontextmanager
from typing import Any, Callable, Dict, Tuple

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, Response

from logger import get_logger
from services import offload_worker_core as core
from services.offload_process import execute

logger = get_logger(__name__)

WORKER_SECRET = (os.getenv("OFFLOAD_WORKER_SECRET") or "").strip()
# A process owns its own Matplotlib parser and lock. Cap at TWO for this 512MB
# deployment; threads sharing one parser would still serialize on _RENDER_LOCK.
CONCURRENCY = max(1, min(2, int(os.getenv("WORKER_CONCURRENCY", "2") or 2)))
MAX_QUEUE = max(0, min(8, int(os.getenv("WORKER_MAX_QUEUE", "8") or 8)))
QUEUE_WAIT_SECONDS = float(os.getenv("WORKER_QUEUE_WAIT_SECONDS", "40") or 40)
MAX_BODY_BYTES = 4 * 1024 * 1024

_slots = asyncio.Semaphore(CONCURRENCY)
_waiting = 0
_admitted = 0
_pool = None


def _release_slot():
    global _admitted
    _admitted -= 1
    _slots.release()


def _new_pool():
    # Avoid implicit numerical-library thread pools inside each child.
    for name in ("OPENBLAS_NUM_THREADS", "OMP_NUM_THREADS", "MKL_NUM_THREADS"):
        os.environ[name] = "1"
    return ProcessPoolExecutor(
        max_workers=CONCURRENCY, mp_context=multiprocessing.get_context("spawn")
    )


async def _submit(work, payload):
    global _pool
    pool = _pool
    if pool is None:
        raise HTTPException(status_code=503, detail="worker not started")
    try:
        return await asyncio.get_running_loop().run_in_executor(pool, execute, work, payload)
    except BrokenProcessPool:
        # A killed child invalidates the whole executor. Recover for the next
        # request rather than leaving the HTTP server alive with a dead pool.
        if _pool is pool:
            _pool = _new_pool()
            pool.shutdown(wait=False, cancel_futures=True)
        logger.exception("render process exited; pool replaced")
        raise HTTPException(status_code=503, detail="render process exited")


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _pool
    _pool = _new_pool()
    if not WORKER_SECRET:
        logger.critical("OFFLOAD_WORKER_SECRET is not set - all work requests will be rejected (503).")
    logger.info(f"render worker ready (concurrency={CONCURRENCY}, max_queue={MAX_QUEUE})")
    try:
        yield
    finally:
        pool, _pool = _pool, None
        if pool is not None:
            await asyncio.to_thread(pool.shutdown, wait=True, cancel_futures=True)


app = FastAPI(title="Quiz Bot Render Worker", version="1.0", lifespan=lifespan)


@app.api_route("/", methods=["GET", "HEAD"])
@app.api_route("/health", methods=["GET", "HEAD"])
async def health():
    return {"status": "ok", "role": "render_worker"}


def _authorize(request: Request) -> None:
    if not WORKER_SECRET:
        raise HTTPException(status_code=503, detail="worker not configured")
    given = request.headers.get("x-worker-secret") or ""
    if not hmac.compare_digest(given.encode("utf-8"), WORKER_SECRET.encode("utf-8")):
        raise HTTPException(status_code=403, detail="forbidden")


async def _read_json(request: Request) -> Dict[str, Any]:
    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > MAX_BODY_BYTES:
        raise HTTPException(status_code=413, detail="payload too large")
    raw = await request.body()
    if len(raw) > MAX_BODY_BYTES:
        raise HTTPException(status_code=413, detail="payload too large")
    try:
        data = json.loads(raw.decode("utf-8"))
    except Exception:
        raise HTTPException(status_code=400, detail="invalid JSON")
    if not isinstance(data, dict):
        raise HTTPException(status_code=400, detail="JSON object expected")
    return data


async def _run(request: Request, work: Callable[[Dict[str, Any]], Tuple[bytes, str]]) -> Response:
    """Bound admission and active process work, including cancelled requests."""
    global _waiting, _admitted
    _authorize(request)
    payload = await _read_json(request)

    if _admitted >= CONCURRENCY + MAX_QUEUE:
        raise HTTPException(status_code=503, detail="busy", headers={"Retry-After": "5"})
    _admitted += 1
    _waiting += 1
    try:
        await asyncio.wait_for(_slots.acquire(), timeout=QUEUE_WAIT_SECONDS)
    except asyncio.TimeoutError:
        _admitted -= 1
        raise HTTPException(status_code=503, detail="busy", headers={"Retry-After": "5"})
    except BaseException:
        _admitted -= 1
        raise
    finally:
        _waiting -= 1

    release_here = True
    try:
        job = asyncio.create_task(_submit(work, payload))
        try:
            status, data, media_type = await asyncio.shield(job)
        except asyncio.CancelledError:
            # Cancellation of an HTTP coroutine does not stop a running process.
            # Keep the slot occupied until that job has ACTUALLY finished.
            release_here = False
            def completed(task):
                _release_slot()
                if not task.cancelled():
                    task.exception()
            job.add_done_callback(completed)
            raise
        except HTTPException:
            raise
        except Exception:
            logger.exception("render worker job failed")
            raise HTTPException(status_code=500, detail="job failed")
        if status == "bad_payload":
            raise HTTPException(status_code=400, detail=data)
        if status == "export_error":
            return JSONResponse(status_code=422, content={"message": data})
        return Response(content=data, media_type=media_type)
    finally:
        if release_here:
            _release_slot()


@app.post("/v1/render/question-image")
async def render_question_image_endpoint(request: Request):
    return await _run(request, core.render_image)


@app.post("/v1/export/quiz")
async def export_quiz_endpoint(request: Request):
    return await _run(request, core.build_quiz)


@app.post("/v1/export/document")
async def export_document_endpoint(request: Request):
    return await _run(request, core.build_document)


def main() -> None:
    import uvicorn
    port = int(os.getenv("PORT", "8080"))
    print("🛠️ تشغيل render worker (السيرفر الثاني) - لا يعمل البوت على هذه العملية.")
    uvicorn.run(app, host="0.0.0.0", port=port, log_level="warning")


if __name__ == "__main__":
    main()
