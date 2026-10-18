"""Provider success/failure logs, with safe upstream error detail."""
import contextlib
import os
import time
import unittest
from unittest.mock import AsyncMock, patch

import httpx

from services.ai import runtime
from services.ai.adapters import ChatAdapter, ProviderError
from services.ai.contracts import AIRequest, AIResult
from services.ai.provider_presets import provider_preset


class LoggingTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        runtime._blocked.clear()
        runtime._pointers.clear()
        runtime._shared_blocked.clear()
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.dict(os.environ, {"OPENROUTER_API_KEY": "SECRET-KEY"}))
        self.providers = {"openrouter": provider_preset("openrouter")}
        self.entries = [{"provider": "openrouter", "model_name": "gemma",
                         "capabilities": ["text"], "is_enabled": True}]
        self.stack.enter_context(patch.object(runtime, "get_providers", AsyncMock(side_effect=lambda: self.providers)))
        self.stack.enter_context(patch.object(runtime, "get_enabled_models", AsyncMock(side_effect=lambda slot: self.entries)))

    async def test_success_and_correlation_do_not_leak_content(self):
        fake = type("Fake", (), {"capabilities": {"text"}, "generate":
            AsyncMock(return_value=AIResult("PRIVATE_REPLY", total_tokens=12))})()
        self.stack.enter_context(patch.dict(runtime.ADAPTERS, {"openai_compatible": fake}))
        with self.assertLogs("services.ai.runtime", level="INFO") as capture:
            result = await runtime.execute(AIRequest(("PRIVATE_STUDENT_TEXT",)), slot="detection", timeout=2)
        self.assertEqual(result.total_tokens, 12)
        data = "\n".join(capture.output)
        for event in ("AI_REQUEST_START", "AI_ATTEMPT_START", "AI_ATTEMPT_SUCCESS", "AI_REQUEST_SUCCESS"):
            self.assertIn(event, data)
        self.assertIn("total_tokens=12", data)
        ids = [line.split("request_id=")[1].split()[0] for line in capture.output]
        self.assertEqual(len(set(ids)), 1)
        for secret in ("SECRET-KEY", "PRIVATE_REPLY", "PRIVATE_STUDENT_TEXT"):
            self.assertNotIn(secret, data)

    async def test_429_reason_retry_and_fallback_are_visible_without_raw_message(self):
        original = httpx.AsyncClient
        def client_factory(**kwargs):
            def handler(request):
                return httpx.Response(429, headers={"Retry-After": "17"},
                    json={"error": {"message": "free-models-per-day SECRET-KEY"}})
            return original(transport=httpx.MockTransport(handler), **kwargs)
        with patch("services.ai.adapters.httpx.AsyncClient", side_effect=client_factory):
            with self.assertRaises(ProviderError) as caught:
                await ChatAdapter().generate(self.providers["openrouter"], "SECRET-KEY", "gemma", AIRequest(("private",)))
        error = caught.exception
        self.assertEqual((error.status, error.reason, error.retry_after_s), (429, "daily_quota", 17))
        self.assertNotIn("SECRET-KEY", str(error))
        self.providers["backup"] = {**self.providers["openrouter"], "id": "backup"}
        self.entries.append({"provider": "backup", "model_name": "other",
                             "capabilities": ["text"], "is_enabled": True})
        async def generate(provider, credential, model, request):
            if provider["id"] == "openrouter":
                raise error
            return AIResult("PRIVATE_REPLY", total_tokens=3)
        fake = type("Fake", (), {"capabilities": {"text"}, "generate": AsyncMock(side_effect=generate)})()
        self.stack.enter_context(patch.dict(runtime.ADAPTERS, {"openai_compatible": fake}))
        with self.assertLogs("services.ai.runtime", level="INFO") as capture:
            result = await runtime.execute(AIRequest(("PRIVATE_STUDENT_TEXT",)), slot="detection", timeout=2)
        self.assertEqual(result.text, "PRIVATE_REPLY")
        data = "\n".join(capture.output)
        for marker in ("AI_ATTEMPT_FAILED", "status=429", "reason=daily_quota",
                       "retry_after_s=17", "AI_ATTEMPT_SUCCESS", "provider=backup",
                       "fallback=True", "AI_REQUEST_SUCCESS"):
            self.assertIn(marker, data)
        for secret in ("SECRET-KEY", "PRIVATE_REPLY", "PRIVATE_STUDENT_TEXT"):
            self.assertNotIn(secret, data)

    async def test_exhaustion_and_cooldown_have_distinct_events(self):
        fake = type("Fake", (), {"capabilities": {"text"}, "generate":
            AsyncMock(side_effect=ProviderError(429, reason="rate_limited"))})()
        self.stack.enter_context(patch.dict(runtime.ADAPTERS, {"openai_compatible": fake}))
        with self.assertLogs("services.ai.runtime", level="INFO") as capture:
            self.assertIsNone(await runtime.execute(AIRequest(("hi",)), timeout=2))
            self.assertIsNone(await runtime.execute(AIRequest(("hi",)), timeout=2))
        data = "\n".join(capture.output)
        for event in ("AI_ATTEMPT_FAILED", "AI_ATTEMPT_SKIPPED", "AI_REQUEST_FAILED"):
            self.assertIn(event, data)


    async def test_platform_429_blocks_all_free_models_for_same_account(self):
        self.providers["backup"] = {**provider_preset("openrouter"),
                                    "id": "backup", "base_url": "https://example.net/v1"}
        self.entries[:] = [
            {"provider": "openrouter", "model_name": "vendor/first:free",
             "capabilities": ["text"], "is_enabled": True},
            {"provider": "openrouter", "model_name": "vendor/second:free",
             "capabilities": ["text"], "is_enabled": True},
            {"provider": "backup", "model_name": "backup-model",
             "capabilities": ["text"], "is_enabled": True},
        ]
        async def generate(provider, credential, model, request):
            if provider["id"] == "openrouter":
                raise ProviderError(429, reason="minute_rate_limit", retry_after_s=40)
            return AIResult("backup result")
        adapter = type("Fake", (), {"capabilities": {"text"}, "generate": AsyncMock(side_effect=generate)})()
        self.stack.enter_context(patch.dict(runtime.ADAPTERS, {"openai_compatible": adapter}))
        with self.assertLogs("services.ai.runtime", level="INFO") as capture:
            self.assertEqual((await runtime.execute(AIRequest(("hello",)), timeout=2)).text, "backup result")
            self.assertEqual((await runtime.execute(AIRequest(("hello",)), timeout=2)).text, "backup result")
        self.assertEqual([v.args[2] for v in adapter.generate.await_args_list],
                         ["vendor/first:free", "backup-model", "backup-model"])
        self.assertIn("reason=shared_rate_limit", "\\n".join(capture.output))
        for key, (_, why) in list(runtime._shared_blocked.items()):
            runtime._shared_blocked[key] = (time.monotonic() - 1, why)
        await runtime.execute(AIRequest(("hello",)), timeout=2)
        self.assertEqual(adapter.generate.await_args_list[-2].args[2], "vendor/first:free")

    async def test_upstream_429_keeps_other_free_model_eligible(self):
        self.entries[:] = [
            {"provider": "openrouter", "model_name": "vendor/first:free",
             "capabilities": ["text"], "is_enabled": True},
            {"provider": "openrouter", "model_name": "vendor/second:free",
             "capabilities": ["text"], "is_enabled": True},
        ]
        async def generate(provider, credential, model, request):
            if model == "vendor/first:free":
                raise ProviderError(429, reason="upstream_rate_limit")
            return AIResult("next model works")
        adapter = type("Fake", (), {"capabilities": {"text"}, "generate": AsyncMock(side_effect=generate)})()
        self.stack.enter_context(patch.dict(runtime.ADAPTERS, {"openai_compatible": adapter}))
        self.assertEqual((await runtime.execute(AIRequest(("hello",)), timeout=2)).text, "next model works")
        self.assertEqual([v.args[2] for v in adapter.generate.await_args_list],
                         ["vendor/first:free", "vendor/second:free"])
        self.assertFalse(runtime._shared_blocked)


if __name__ == "__main__":
    unittest.main()
