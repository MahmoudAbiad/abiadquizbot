"""
services/offload.py
==============================================================================
ميزة اختيارية: ترحيل العمليات الثقيلة (رسم صور الأسئلة الرياضية + تصدير Word/PDF)
إلى سيرفر ثانٍ ("render worker")، وإبقاء السيرفر الأساسي (512MB) للطلبات الخفيفة.

التفعيل بالكامل عبر متغيرات البيئة (لا شيء بقاعدة البيانات):
    OFFLOAD_ENABLED=1                  مفتاح التشغيل (غيابه/0 = السلوك القديم تماماً)
    OFFLOAD_WORKER_URL=https://...     عنوان سيرفر العمل (بدون / بالنهاية)
    OFFLOAD_WORKER_SECRET=...          سر مشترك (نفسه على السيرفرين)

اختيارية:
    OFFLOAD_FALLBACK_LOCAL=1           لو فشل السيرفر الثاني: أكمل محلياً (الافتراضي 1)
    OFFLOAD_REMOTE_CONCURRENCY=8       أقصى طلبات رسم/تصدير معلّقة نحو السيرفر الثاني
    OFFLOAD_UPDATE_CONCURRENCY=24      سقف معالجة تحديثات تيليجرام المتزامنة عند التفعيل

مبدأ السلامة: فشل الإعداد (مفتاح مفعّل لكن URL/Secret ناقص) = الميزة تُعطَّل ويعمل
البوت محلياً كما كان - لا يتوقف البوت بسبب خطأ إعداد في ميزة اختيارية.

⚠️ هالملف عمداً ما بيستورد إلا logger (لا استيراد لـ image_quiz_renderer/export_service)
حتى لا تنشأ دورة استيراد، ولأن httpx بيُستورد كسولاً عند أول استخدام فعلي فقط.
==============================================================================
"""

import asyncio
import json
import os
import time
from typing import Any, Callable, Dict, List, Optional, Tuple

from logger import get_logger, log_warning

logger = get_logger(__name__)


# ==================== قراءة الإعدادات ====================

def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def _env_int(name: str, default: int, lo: int, hi: int) -> int:
    try:
        value = int(os.getenv(name, "") or default)
    except ValueError:
        log_warning(logger, f"offload: invalid integer for {name}, using {default}")
        return default
    return max(lo, min(hi, value))


# الحدود الأصلية (قبل الميزة) - هي نفسها التي يعود لها البوت عند إغلاق الميزة.
BASE_LOCAL_RENDER_LIMIT = 3       # كان RENDER_SEMAPHORE بـ image_quiz_renderer.py
BASE_UPDATE_CONCURRENCY = 15      # كان UPDATE_CONCURRENCY_LIMIT بـ webhook_server.py

_REQUESTED = _env_bool("OFFLOAD_ENABLED", False)
WORKER_URL = (os.getenv("OFFLOAD_WORKER_URL") or "").strip().rstrip("/")
WORKER_SECRET = (os.getenv("OFFLOAD_WORKER_SECRET") or "").strip()

ENABLED = _REQUESTED and bool(WORKER_URL) and bool(WORKER_SECRET)
if _REQUESTED and not ENABLED:
    log_warning(
        logger,
        "offload: OFFLOAD_ENABLED is on but OFFLOAD_WORKER_URL / OFFLOAD_WORKER_SECRET "
        "is missing - feature DISABLED, running everything locally.",
    )

FALLBACK_LOCAL = _env_bool("OFFLOAD_FALLBACK_LOCAL", True)

# عند التفعيل فقط تتوسّع الحدود؛ عند الإغلاق تبقى القيم الأصلية حرفياً.
REMOTE_CONCURRENCY = _env_int("OFFLOAD_REMOTE_CONCURRENCY", 8, 1, 64)
UPDATE_CONCURRENCY_LIMIT = (
    _env_int("OFFLOAD_UPDATE_CONCURRENCY", 24, BASE_UPDATE_CONCURRENCY, 100)
    if ENABLED else BASE_UPDATE_CONCURRENCY
)

# مهلات القراءة (ثوانٍ). الاتصال نفسه 6 ثوانٍ دائماً.
CONNECT_TIMEOUT = 6.0
RENDER_READ_TIMEOUT = float(_env_int("OFFLOAD_RENDER_READ_TIMEOUT", 45, 5, 180))
EXPORT_READ_TIMEOUT = float(_env_int("OFFLOAD_EXPORT_READ_TIMEOUT", 150, 15, 600))
MAX_REQUEST_BYTES = 3 * 1024 * 1024   # أكبر من هيك: نطبّق سياسة FALLBACK_LOCAL

if ENABLED:
    logger.info("offload: enabled (remote_concurrency=%s, update_concurrency=%s)",
                REMOTE_CONCURRENCY, UPDATE_CONCURRENCY_LIMIT)


class OffloadUnavailable(RuntimeError):
    """السيرفر الثاني تعذّر والرجوع للمعالجة المحلية معطّل (OFFLOAD_FALLBACK_LOCAL=0)."""


class _TransportError(Exception):
    """فشل شبكي (انقطاع/مهلة). retryable=True فقط لفشل الاتصال الأولي."""

    def __init__(self, message: str, retryable: bool = False):
        super().__init__(message)
        self.retryable = retryable


# ==================== قاطع الدائرة (Circuit Breaker) ====================

class _Breaker:
    """بعد N فشل متتالي نتوقف عن المحاولة لفترة قصيرة، بدل أن يدفع كل طلب مستخدم
    مهلة الانتظار كاملة أثناء تعطّل السيرفر الثاني. بعد انتهاء الفترة يُسمح بمحاولة واحدة."""

    def __init__(self, threshold: int = 3, cooldown: float = 30.0, clock: Callable[[], float] = time.monotonic):
        self.threshold = threshold
        self.cooldown = cooldown
        self.clock = clock
        self.failures = 0
        self.opened_at: Optional[float] = None
        self.generation = 0
        self.probe_in_flight = False

    def ready(self) -> bool:
        """Read-only check; only allow() reserves the recovery probe."""
        return self.opened_at is None or (
            not self.probe_in_flight and self.clock() - self.opened_at >= self.cooldown
        )

    def allow(self) -> bool:
        if self.opened_at is None:
            return True
        if self.ready():
            # One recovery request, even if its duration exceeds the cooldown.
            self.probe_in_flight = True
            return True
        return False

    def record_success(self, generation: Optional[int] = None) -> None:
        if generation is not None and generation != self.generation:
            return  # An older in-flight request cannot close a newly opened circuit.
        if self.opened_at is not None:
            logger.info("offload: worker recovered, circuit closed.")
            self.generation += 1
        self.failures = 0
        self.opened_at = None
        self.probe_in_flight = False

    def record_failure(self, generation: Optional[int] = None) -> None:
        if generation is not None and generation != self.generation:
            return
        self.failures += 1
        if self.failures >= self.threshold and self.opened_at is None:
            self.opened_at = self.clock()
            self.generation += 1
            log_warning(logger, f"offload: {self.failures} consecutive failures - circuit OPEN for {self.cooldown:.0f}s")
        elif self.opened_at is not None:
            self.opened_at = self.clock()
            self.generation += 1
        self.probe_in_flight = False


_breaker = _Breaker()
_remote_sem = asyncio.Semaphore(REMOTE_CONCURRENCY)

stats: Dict[str, int] = {"remote_ok": 0, "remote_fail": 0, "fallback_local": 0, "skipped_circuit_open": 0}


def get_stats() -> Dict[str, Any]:
    return {"enabled": ENABLED, "circuit_open": _breaker.opened_at is not None, **stats}


def is_active() -> bool:
    """Read-only status; recovery probes are reserved at actual dispatch."""
    return ENABLED and _breaker.ready()


def _fallback(reason: str, allow_local_fallback: bool = True) -> Optional[bytes]:
    """Apply the same policy to every failure while offload is enabled."""
    if FALLBACK_LOCAL and allow_local_fallback:
        stats["fallback_local"] += 1
        return None
    raise OffloadUnavailable(f"render worker unavailable: {reason}")


def _skip_open_circuit(allow_local_fallback=True) -> Optional[bytes]:
    stats["skipped_circuit_open"] += 1
    return _fallback("circuit open", allow_local_fallback)


# ==================== النقل (قابل للاستبدال بالاختبارات) ====================

_client = None


def _get_client():
    global _client
    if _client is None:
        import httpx  # كسول: لا يُستورد أبداً لو الميزة مغلقة
        _client = httpx.AsyncClient(
            base_url=WORKER_URL,
            headers={"X-Worker-Secret": WORKER_SECRET},
            limits=httpx.Limits(max_connections=REMOTE_CONCURRENCY, max_keepalive_connections=4),
            timeout=httpx.Timeout(10.0, connect=CONNECT_TIMEOUT),
        )
    return _client


async def _post(path: str, body: bytes, read_timeout: float) -> Tuple[int, bytes]:
    """يرسل JSON خاماً ويرجع (status, content). أي فشل شبكي يُحوَّل إلى _TransportError."""
    import httpx
    try:
        resp = await _get_client().post(
            path,
            content=body,
            headers={"Content-Type": "application/json"},
            timeout=httpx.Timeout(read_timeout, connect=CONNECT_TIMEOUT),
        )
        return resp.status_code, resp.content
    except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
        raise _TransportError(f"connect failed: {exc!r}", retryable=True) from exc
    except httpx.TransportError as exc:
        raise _TransportError(f"transport failed: {exc!r}") from exc


_MAGIC = {"png": b"\x89PNG\r\n\x1a\n", "pdf": b"%PDF-", "docx": b"PK"}


async def _call(path: str, payload: Dict[str, Any], read_timeout: float, kind: str, *, allow_local_fallback=True) -> Optional[bytes]:
    """ينفّذ الاستدعاء كاملاً. يرجع bytes عند النجاح، None عند فشل مسموح معه الرجوع محلياً،
    ويرمي OffloadUnavailable لو الرجوع المحلي معطّل. رسالة 422 (خطأ مستخدم) تُرفع كما هي."""
    if not is_active():
        return _skip_open_circuit(allow_local_fallback)
    try:
        body = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
    except (TypeError, ValueError) as exc:
        log_warning(logger, f"offload: payload not serializable: {exc}")
        return _fallback("payload not serializable", allow_local_fallback)
    if len(body) > MAX_REQUEST_BYTES:
        log_warning(logger, f"offload: payload {len(body)} bytes exceeds limit")
        return _fallback("payload exceeds limit", allow_local_fallback)

    failure = ""
    async with _remote_sem:
        # Queued callers may have observed a healthy circuit before waiting.
        # Reserve a recovery probe only after a connection slot is available.
        if not _breaker.allow():
            return _skip_open_circuit(allow_local_fallback)
        generation = _breaker.generation
        for attempt in (1, 2):
            try:
                status, content = await _post(path, body, read_timeout)
            except asyncio.CancelledError:
                if _breaker.probe_in_flight:
                    _breaker.record_failure(generation)
                raise
            except _TransportError as exc:
                failure = str(exc)
                if exc.retryable and attempt == 1:
                    try:
                        await asyncio.sleep(1.0)   # غالباً السيرفر الثاني يستيقظ من النوم
                    except asyncio.CancelledError:
                        if _breaker.probe_in_flight:
                            _breaker.record_failure(generation)
                        raise
                    continue
                break

            if status == 200 and content.startswith(_MAGIC[kind]):
                _breaker.record_success(generation)
                stats["remote_ok"] += 1
                return content
            if status == 422:
                # خطأ بمحتوى المستخدم نفسه (مثلاً لا أسئلة): ليس عطلاً بالسيرفر، لا fallback.
                _breaker.record_success(generation)
                try:
                    message = json.loads(content.decode("utf-8")).get("message") or "تعذّر إنشاء الملف."
                except Exception:
                    message = "تعذّر إنشاء الملف."
                from services.export_service import ExportError
                raise ExportError(message)
            failure = f"HTTP {status}" + ("" if status != 200 else " (unexpected body)")
            if status in (401, 403):
                logger.error("offload: worker rejected the secret (OFFLOAD_WORKER_SECRET mismatch?)")
            break

    _breaker.record_failure(generation)
    stats["remote_fail"] += 1
    log_warning(logger, f"offload: remote call {path} failed ({failure})")
    return _fallback(failure, allow_local_fallback)


# ==================== الواجهة العامة ====================

async def remote_render_image(question: Dict[str, Any], idx: int, total: int, is_ar: bool, *, allow_local_fallback=True) -> Optional[bytes]:
    """PNG لسؤال رياضي من السيرفر الثاني، أو None => نفّذ محلياً."""
    if not ENABLED:
        return None
    return await _call(
        "/v1/render/question-image",
        {"question": question, "idx": idx, "total": total, "is_ar": bool(is_ar)},
        RENDER_READ_TIMEOUT, "png", allow_local_fallback=allow_local_fallback,
    )


async def remote_build_quiz(fmt: str, title: str, questions: List[Dict[str, Any]], style: str, include_answers: bool = True) -> Optional[bytes]:
    """ملف Word/PDF لكويز كامل، أو None => نفّذ محلياً. fmt: 'docx' | 'pdf'."""
    if fmt not in ("docx", "pdf") or not ENABLED:
        return None
    return await _call(
        "/v1/export/quiz",
        {"fmt": fmt, "title": title, "questions": questions, "style": style, "include_answers": bool(include_answers)},
        EXPORT_READ_TIMEOUT, fmt,
    )


async def remote_build_document(fmt: str, title: str, text: str, style: Optional[str] = None) -> Optional[bytes]:
    """ملف Word/PDF لنص حر (محاضرة/ملخص)، أو None => نفّذ محلياً."""
    if fmt not in ("docx", "pdf") or not ENABLED:
        return None
    payload: Dict[str, Any] = {"fmt": fmt, "title": title, "text": text}
    if style:
        payload["style"] = style
    return await _call("/v1/export/document", payload, EXPORT_READ_TIMEOUT, fmt)
