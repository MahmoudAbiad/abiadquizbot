"""Allowlisted latency milestones; never log transport payloads or exception text."""
import asyncio
import contextlib
from contextvars import ContextVar
import os
import time

from logger import get_logger

logger = get_logger(__name__)
_current = ContextVar("ai_attempt_diagnostics", default=None)
HEARTBEAT_SECONDS = 30


class AttemptTrace:
    def __init__(self, request_id, slot, provider, model, credential_index):
        self.identity = (request_id, slot, provider, model, credential_index)
        self.started = self.changed = time.monotonic()
        self.stage = "adapter_start"
        self.last_network = "none"
        self.last_progress = "none"
        self.network_requests = 0
        self.cleanup_started = None

    def emit(self, event, *, status=None):
        now = time.monotonic()
        logger.info(
            "AI_DIAG request_id=%s slot=%s provider=%s model=%s credential_index=%d "
            "event=%s stage=%s elapsed_ms=%d stage_ms=%d last_network=%s "
            "network_requests=%d http_status=%s cleanup_ms=%d last_progress=%s",
            *self.identity, event, self.stage, round((now-self.started)*1000),
            round((now-self.changed)*1000), self.last_network,
            self.network_requests, status if type(status) is int else None,
            round((now-self.cleanup_started)*1000) if self.cleanup_started is not None else 0,
            self.last_progress,
        )

    def mark(self, stage):
        self.emit("stage_done")
        self.stage = stage
        self.changed = time.monotonic()
        self.emit("stage_start")

    def network(self, event, *, status=None):
        if self.cleanup_started is not None:
            # Delete/close requests must not overwrite the failed generation's
            # final network milestone in the eventual timeout/failure summary.
            self.emit("cleanup_network", status=status)
            return
        self.last_network = event
        if not event.endswith("failed"):
            self.last_progress = event
        if event == "request_start":
            self.network_requests += 1
        self.emit("network", status=status)


def mark(stage):
    trace = _current.get()
    if trace is not None:
        trace.mark(stage)


def begin_cleanup():
    trace = _current.get()
    if trace is not None:
        trace.cleanup_started = time.monotonic()
        trace.emit("cleanup_start")


async def _heartbeat(trace):
    while True:
        await asyncio.sleep(HEARTBEAT_SECONDS)
        trace.emit("waiting")


async def run_attempt(coroutine, *, timeout, request_id, slot, provider, model, credential_index):
    trace = AttemptTrace(request_id, slot, provider, model, credential_index)
    token = _current.set(trace)
    # wait_for copies this context into its adapter task. Each concurrent attempt
    # therefore keeps its own milestones, including callbacks from the transport.
    heartbeat = asyncio.create_task(_heartbeat(trace))
    trace.emit("start")
    try:
        result = await asyncio.wait_for(coroutine, timeout=timeout)
        trace.emit("success")
        return result
    except (asyncio.TimeoutError, TimeoutError):
        trace.emit("timeout")
        raise
    except asyncio.CancelledError:
        trace.emit("cancelled")
        raise
    except Exception:
        trace.emit("failed")
        raise
    finally:
        heartbeat.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await heartbeat
        _current.reset(token)


# Only fixed event names leave the transport. In particular, do not stringify
# info: HTTPX includes URLs, request headers, sockets and raw exceptions in it.
_HTTPX_EVENTS = {
    "connection.connect_tcp.started": "connect_start",
    "connection.connect_tcp.complete": "connect_done",
    "connection.connect_tcp.failed": "connect_failed",
    "connection.start_tls.started": "tls_start",
    "connection.start_tls.complete": "tls_done",
    "connection.start_tls.failed": "tls_failed",
}
for _protocol in ("http11", "http2"):
    for _part, _label in (("send_request_headers", "send_headers"),
                          ("send_request_body", "send_body"),
                          ("receive_response_headers", "response_headers"),
                          ("receive_response_body", "response_body")):
        for _suffix in ("started", "complete", "failed"):
            _HTTPX_EVENTS[f"{_protocol}.{_part}.{_suffix}"] = f"{_label}_{_suffix}"


async def httpx_request(request):
    trace = _current.get()
    if trace is None:
        return
    trace.network("request_start")

    async def callback(event_name, info):
        event = _HTTPX_EVENTS.get(event_name)
        if event:
            status = None
            if event == "response_headers_complete":
                value = info.get("return_value")
                if isinstance(value, tuple):
                    index = 0 if event_name.startswith("http2.") else 1
                    if len(value) > index and type(value[index]) is int:
                        status = value[index]
            trace.network(event, status=status)

    request.extensions["trace"] = callback


def aiohttp_trace_config(aiohttp):
    config = aiohttp.TraceConfig()
    events = {
        "on_request_start": "request_start",
        "on_connection_queued_start": "connection_queue_start",
        "on_connection_queued_end": "connection_queue_done",
        "on_dns_resolvehost_start": "dns_start",
        "on_dns_resolvehost_end": "dns_done",
        "on_connection_create_start": "connect_start",
        "on_connection_create_end": "connect_done",
        "on_connection_reuseconn": "connection_reused",
        "on_request_headers_sent": "send_headers_complete",
        "on_request_chunk_sent": "send_body_chunk",
        # aiohttp on_request_end means response HEADERS available, not full body.
        "on_request_end": "response_headers_complete",
        "on_response_chunk_received": "response_body_chunk",
        "on_request_exception": "request_failed",
    }
    for signal, event in events.items():
        async def callback(session, context, params, event=event):
            if event == "request_start":
                context.ai_trace = _current.get()
            trace = getattr(context, "ai_trace", None)
            if trace is not None:
                # Summarize chunks once per HTTP request; never log their bytes.
                if event.endswith("_chunk"):
                    seen = getattr(context, "ai_seen", set())
                    if event in seen:
                        return
                    seen.add(event)
                    context.ai_seen = seen
                response = getattr(params, "response", None)
                trace.network(event, status=getattr(response, "status", None))
        getattr(config, signal).append(callback)
    return config


def google_http_options():
    """Keep the SDK's default backend and its retry/timeout policy.

    The pinned SDK selects aiohttp when installed, otherwise HTTPX. Use its
    supported client injection for tracing; caller owns the injected session.
    mTLS retains the SDK-managed authorized session and coarse SDK milestones.
    """
    trace = _current.get()
    if trace is None:
        return {}, None
    try:
        import aiohttp
    except ImportError:
        trace.network("backend_httpx")
        return {"async_client_args": {"event_hooks": {"request": [httpx_request]}}}, None
    if os.getenv("GOOGLE_API_USE_CLIENT_CERTIFICATE", "").lower() == "true":
        trace.network("backend_sdk_mtls")
        return {}, None
    # Match google-genai 2.10.0's default aiohttp session settings.
    session = aiohttp.ClientSession(
        connector=aiohttp.TCPConnector(limit=0), trust_env=True,
        read_bufsize=2**22, trace_configs=[aiohttp_trace_config(aiohttp)],
    )
    trace.network("backend_aiohttp")
    return {"aiohttp_client": session}, session
