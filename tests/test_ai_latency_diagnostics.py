"""Local-only latency probes: real SDK/HTTP requests, no credentials or API usage."""
import asyncio
import contextlib
import json
import os
import types
import unittest
from unittest.mock import AsyncMock, patch

import httpx

from services.ai import diagnostics, runtime
from services.ai.adapters import GoogleAdapter
from services.ai.contracts import AIRequest, AIResult, Attachment


IDENTITY = dict(slot="cascade", provider="gemini", model="test-model", credential_index=0)


class LatencyDiagnosticTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.release = asyncio.Event()
        self.received = asyncio.Event()
        self.delay_headers = False
        self.response_status = 200
        self.connections = set()
        self.server = await asyncio.start_server(self.serve, "127.0.0.1", 0)
        self.base_url = f"http://127.0.0.1:{self.server.sockets[0].getsockname()[1]}"

    async def asyncTearDown(self):
        self.release.set()
        self.server.close()
        await self.server.wait_closed()
        if self.connections:
            await asyncio.gather(*list(self.connections), return_exceptions=True)

    async def serve(self, reader, writer):
        task = asyncio.current_task()
        self.connections.add(task)
        try:
            headers = await reader.readuntil(b"\r\n\r\n")
            length = 0
            for line in headers.split(b"\r\n"):
                if line.lower().startswith(b"content-length:"):
                    length = int(line.split(b":", 1)[1])
            await reader.readexactly(length)
            self.received.set()
            if self.delay_headers:
                await self.release.wait()
            if self.response_status == 200:
                payload = {"candidates": [{"content": {"role": "model", "parts": [{"text": "private answer"}]},
                                            "finishReason": "STOP"}],
                           "usageMetadata": {"promptTokenCount": 2, "candidatesTokenCount": 2, "totalTokenCount": 4}}
            else:
                payload = {"error": {"code": 503, "status": "UNAVAILABLE", "message": "PRIVATE_UPSTREAM_ERROR"}}
            body = json.dumps(payload).encode()
            writer.write(f"HTTP/1.1 {self.response_status} Test\r\nContent-Type: application/json\r\nContent-Length: {len(body)}\r\nConnection: close\r\n\r\n".encode() + body)
            await writer.drain()
        except (ConnectionError, asyncio.IncompleteReadError):
            pass
        finally:
            writer.close()
            await writer.wait_closed()
            self.connections.discard(task)

    async def google_attempt(self, *, timeout=3, request_id="google-local", force_httpx=False):
        from google.genai import _api_client
        original = diagnostics.google_http_options
        sessions = []
        def options():
            if force_httpx:
                values, session = {"async_client_args": {"event_hooks": {"request": [diagnostics.httpx_request]}}}, None
            else:
                values, session = original()
            if session is not None:
                sessions.append(session)
            return {**values, "base_url": self.base_url}, session
        try:
            with patch.object(diagnostics, "google_http_options", options), \
                 patch.object(_api_client, "has_aiohttp", False if force_httpx else _api_client.has_aiohttp):
                return await diagnostics.run_attempt(
                    GoogleAdapter().generate({"id": "gemini", "transport": "gemini"},
                                             "PRIVATE_API_KEY", "test-model",
                                             AIRequest(("PRIVATE_PROMPT", Attachment("image/png", data=b"PRIVATE_IMAGE")))),
                    timeout=timeout, request_id=request_id, **IDENTITY)
        finally:
            self.assertTrue(all(session.closed for session in sessions))

    async def test_real_google_sdk_with_httpx_backend(self):
        with self.assertLogs("services.ai.diagnostics", level="INFO") as capture:
            result = await self.google_attempt(force_httpx=True)
        self.assertEqual(result.text, "private answer")
        data = "\n".join(capture.output)
        self.assertIn("send_body_complete", data)
        self.assertIn("http_status=200", data)
        self.assertIn("event=cleanup_start", data)
        self.assertNotIn("PRIVATE_", data)

    async def test_real_google_sdk_records_phases_without_payloads(self):
        with self.assertLogs("services.ai.diagnostics", level="INFO") as capture:
            result = await self.google_attempt()
        self.assertEqual(result.text, "private answer")
        data = "\n".join(capture.output)
        for value in ("stage=attachment_read", "stage=generate_wait", "request_start",
                      "connect_done", "response_headers_complete", "http_status=200",
                      "stage=response_parse", "event=success"):
            self.assertIn(value, data)
        for secret in ("PRIVATE_API_KEY", "PRIVATE_PROMPT", "PRIVATE_IMAGE", "private answer", self.base_url):
            self.assertNotIn(secret, data)
        self.assertIsNone(diagnostics._current.get())

    async def test_real_sdk_timeout_shows_sent_body_without_response_headers(self):
        self.delay_headers = True
        with self.assertLogs("services.ai.diagnostics", level="INFO") as capture:
            with self.assertRaises(TimeoutError):
                await self.google_attempt(timeout=1)
        self.assertTrue(self.received.is_set())
        data = "\n".join(capture.output)
        self.assertIn("stage=generate_wait", data)
        self.assertIn("event=timeout", data)
        self.assertNotIn("response_headers_complete", data)
        self.assertIn("send_body", data)
        self.assertIsNone(diagnostics._current.get())

    async def test_httpx_real_transport_traces_and_ignores_secret_info(self):
        async def generate():
            async with httpx.AsyncClient(event_hooks={"request": [diagnostics.httpx_request]}) as client:
                await client.post(self.base_url, content=b"PRIVATE_BODY", headers={"secret": "PRIVATE_HEADER"})
        with self.assertLogs("services.ai.diagnostics", level="INFO") as capture:
            await diagnostics.run_attempt(generate(), timeout=3, request_id="httpx-local", **IDENTITY)
        data = "\n".join(capture.output)
        self.assertIn("send_body_complete", data)
        self.assertIn("response_headers_complete", data)
        self.assertIn("http_status=200", data)
        for secret in ("PRIVATE_BODY", "PRIVATE_HEADER", self.base_url):
            self.assertNotIn(secret, data)

    async def test_http2_header_status_and_unknown_events_are_safe(self):
        async def generate():
            request = httpx.Request("POST", "https://private.invalid/PRIVATE_URL")
            await diagnostics.httpx_request(request)
            callback = request.extensions["trace"]
            await callback("http2.receive_response_headers.complete", {"return_value": (503, [(b"secret", b"PRIVATE_HEADER")])})
            await callback("PRIVATE_EVENT", {"exception": RuntimeError("PRIVATE_ERROR")})
        with self.assertLogs("services.ai.diagnostics", level="INFO") as capture:
            await diagnostics.run_attempt(generate(), timeout=3, request_id="http2-local", **IDENTITY)
        data = "\n".join(capture.output)
        self.assertIn("http_status=503", data)
        self.assertNotIn("PRIVATE_", data)

    async def test_concurrent_attempts_heartbeat_cancellation_and_context_reset(self):
        async def generate(stage):
            diagnostics.mark(stage)
            await asyncio.sleep(0.06)
        with patch.object(diagnostics, "HEARTBEAT_SECONDS", 0.01), self.assertLogs("services.ai.diagnostics", level="INFO") as capture:
            await asyncio.gather(*[
                diagnostics.run_attempt(generate(stage), timeout=1, request_id=identity, **IDENTITY)
                for identity, stage in (("first", "attachment_prepare"), ("second", "generate_wait"))])
        for line in capture.output:
            if "event=waiting" in line:
                expected = "attachment_prepare" if "request_id=first " in line else "generate_wait"
                self.assertIn("stage="+expected, line)
        self.assertTrue(any("event=waiting" in line for line in capture.output))
        self.assertIsNone(diagnostics._current.get())
        task = asyncio.create_task(diagnostics.run_attempt(asyncio.sleep(30), timeout=60, request_id="cancel", **IDENTITY))
        await asyncio.sleep(0)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        await asyncio.sleep(0)
        self.assertFalse(any(t.get_coro().__name__ == "_heartbeat" for t in asyncio.all_tasks()))

    async def test_real_google_503_code_is_preserved_and_not_retried_by_sdk(self):
        from google.genai.errors import ServerError
        self.response_status = 503
        with self.assertLogs("services.ai.diagnostics", level="INFO") as capture:
            with self.assertRaises(ServerError) as caught:
                await self.google_attempt()
        self.assertEqual(caught.exception.code, 503)
        data = "\n".join(capture.output)
        self.assertEqual(data.count("last_network=request_start "), 1)
        self.assertIn("http_status=503", data)
        self.assertNotIn("PRIVATE_UPSTREAM_ERROR", data)
        runtime._blocked.clear()
        runtime._pointers.clear()
        provider = dict(id="gemini", transport="gemini", api_keys_env="TEST_DIAG_KEYS", is_enabled=True, base_url="")
        adapter = types.SimpleNamespace(capabilities={"text"}, generate=AsyncMock(side_effect=caught.exception))
        with patch.dict(os.environ, {"TEST_DIAG_KEYS": "PRIVATE_KEY"}), \
             patch.object(runtime, "get_providers", AsyncMock(return_value={"gemini": provider})), \
             patch.object(runtime, "get_enabled_models", AsyncMock(return_value=[dict(provider="gemini", model_name="test-model", capabilities=["text"])])), \
             patch.dict(runtime.ADAPTERS, {"gemini": adapter}), \
             patch("helpers.settings_helper.get_ai_thinking_level", AsyncMock(return_value=None)), \
             self.assertLogs("services.ai.runtime", level="WARNING") as logged:
            await runtime.execute(AIRequest(("PRIVATE_PROMPT",)), timeout=1)
        self.assertIn("status=503", "\n".join(logged.output))
        self.assertNotIn("PRIVATE_", "\n".join(logged.output))
        runtime._blocked.clear()
        runtime._pointers.clear()
