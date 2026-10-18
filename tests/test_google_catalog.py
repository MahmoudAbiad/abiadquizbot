"""Deterministic Google-model catalog tests: no outbound API calls or charges."""
import asyncio
import contextlib
import json
import types
import sys
import unittest
from typing import Literal
from pydantic import BaseModel, Field
from unittest.mock import AsyncMock, Mock, patch

import httpx

from services.ai.google_catalog import (
    CatalogError, ProbeResult, _synthetic_equation_image,
    list_google_models, list_working_google_models, probe_google_model,
    classification_config, PROBE_TIMEOUT, _choose_current_classifiers,
    _PREFLIGHT_RESULTS, _probe_catalog_candidate,
)


class CatalogTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.calls = []
        original = httpx.AsyncClient
        def factory(**kwargs):
            def handler(request):
                self.calls.append(request)
                if request.url.params.get("pageToken"):
                    return httpx.Response(200, json={"models": [
                        {"name": "models/gemma-4-26b-a4b-it",
                         "supportedGenerationMethods": ["generateContent"]}]})
                return httpx.Response(200, json={"models": [
                    {"name": "models/gemini-3.5-flash-lite",
                     "displayName": "Gemini Flash Lite",
                     "inputTokenLimit": 1000000,
                     "supportedGenerationMethods": ["generateContent", "countTokens"]},
                    {"name": "models/embedding-only", "supportedGenerationMethods": ["embedContent"]},
                    {"name": "models/fake name\ninjection",
                     "supportedGenerationMethods": ["generateContent"]},
                    {"name": "noncanonical", "supportedGenerationMethods": ["generateContent"]},
                ], "nextPageToken": "next"})
            return original(transport=httpx.MockTransport(handler), **kwargs)
        self.client_patch = patch("services.ai.google_catalog.httpx.AsyncClient",
                                  side_effect=factory)
        self.client_patch.start()
        self.addCleanup(self.client_patch.stop)

    async def test_pagination_filters_only_generatecontent_and_never_exposes_key(self):
        models = await list_google_models("SECRET-KEY")
        self.assertEqual([m["name"] for m in models], [
            "gemini-3.5-flash-lite", "gemma-4-26b-a4b-it"])
        self.assertEqual(len(self.calls), 2)
        self.assertEqual(self.calls[0].headers["x-goog-api-key"], "SECRET-KEY")
        self.assertNotIn("SECRET-KEY", str(self.calls[0].url))
        self.assertEqual(self.calls[1].url.params["pageToken"], "next")
        self.assertEqual(models[0]["input_token_limit"], 1000000)

    async def test_empty_credentials_and_status_are_sanitized(self):
        with self.assertRaises(CatalogError):
            await list_google_models("")
        self.assertEqual(len(self.calls), 0)
        self.client_patch.stop()
        original = httpx.AsyncClient
        def factory(**kwargs):
            def handler(request):
                return httpx.Response(429, json={"error": "SECRET-KEY private"})
            return original(transport=httpx.MockTransport(handler), **kwargs)
        with patch("services.ai.google_catalog.httpx.AsyncClient", side_effect=factory):
            with self.assertRaises(CatalogError) as raised:
                await list_google_models("SECRET-KEY")
            self.assertIn("429", str(raised.exception))
            self.assertNotIn("SECRET-KEY", str(raised.exception))

    async def test_catalog_filters_obsolete_media_and_old_flash_generations(self):
        self.client_patch.stop()
        original = httpx.AsyncClient
        listed = [
            "gemini-2.5-flash", "gemini-3.6-flash", "gemini-3.8-flash",
            "gemini-3.8-flash-tts", "gemini-3.8-live",
            "gemini-3.1-flash-lite", "gemini-3.5-flash-lite",
            "gemini-3.1-flash-image", "gemini-nano-banana-2.1",
            "gemini-3-pro-image", "gemini-3.1-pro-preview", "gemini-4-argon",
            "gemma-3-27b-it", "gemma-4-31b-it", "gemma-4-26b-a4b-it",
            "gemini-embedding-001", "veo-3.1-generate-preview",
            "gemini-3.8-flash-preview",
        ]
        def factory(**kwargs):
            return original(transport=httpx.MockTransport(
                lambda _: httpx.Response(200, json={"models": [
                    {"name": "models/" + name,
                     "supportedGenerationMethods": ["generateContent"]}
                    for name in listed]})), **kwargs)
        with patch("services.ai.google_catalog.httpx.AsyncClient", side_effect=factory):
            models = await list_google_models("KEY")
        self.assertEqual([r["name"] for r in models], [
            "gemini-3.5-flash-lite", "gemini-3.8-flash",
            "gemma-4-26b-a4b-it", "gemma-4-31b-it"])
        self.assertTrue(all("image" not in r["name"] for r in models))
        self.assertTrue(all("tts" not in r["name"] for r in models))

    async def test_scan_only_shows_verified_working_models_and_caches(self):
        _PREFLIGHT_RESULTS.clear()
        self.addCleanup(_PREFLIGHT_RESULTS.clear)
        candidate_rows = [
            {"name": "gemini-3.5-flash-lite"},
            {"name": "gemini-3.8-flash"},
            {"name": "gemma-4-31b-it"},
        ]
        async def healthy(key, model):
            return (True, "working") if model == "gemini-3.8-flash" else (
                False, "http_429" if model == "gemma-4-31b-it" else "timeout")
        with (patch("services.ai.google_catalog.list_google_models", AsyncMock(return_value=candidate_rows)),
              patch("services.ai.google_catalog._probe_catalog_candidate", side_effect=healthy) as verify):
            models, stats = await list_working_google_models("SECRET-A")
            second, second_stats = await list_working_google_models("SECRET-A")
            await list_working_google_models("SECRET-B")
        self.assertEqual(models, [{"name": "gemini-3.8-flash"}])
        self.assertEqual(models, second)
        self.assertEqual(stats, {
            "candidates": 3, "available": 1, "quota": 1, "timeout": 1, "other": 0})
        self.assertEqual(second_stats, stats)
        self.assertEqual(verify.call_count, 6)  # Two keys, not three scans.
        self.assertNotIn("SECRET-A", str(_PREFLIGHT_RESULTS))

    async def test_scan_when_no_model_is_available_returns_empty_valid_catalog(self):
        _PREFLIGHT_RESULTS.clear()
        self.addCleanup(_PREFLIGHT_RESULTS.clear)
        with (patch("services.ai.google_catalog.list_google_models",
                    AsyncMock(return_value=[{"name": "gemini-3.8-flash"}])),
              patch("services.ai.google_catalog._probe_catalog_candidate",
                    AsyncMock(return_value=(False, "http_404")))):
            rows, stats = await list_working_google_models("KEY")
        self.assertEqual(rows, [])
        self.assertEqual(stats["other"], 1)

    def test_probe_image_is_genuine_small_png(self):
        from PIL import Image
        from io import BytesIO
        source = _synthetic_equation_image()
        self.assertLess(len(source), 35000)
        with Image.open(BytesIO(source)) as image:
            self.assertEqual(image.size, (384, 176))
            self.assertEqual(image.format, "PNG")


class ProbeTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        # The production classifier imports Supabase/Redis. Test the exact fields
        # and enums without a live DB or any previously stubbed Supabase modules.
        class SubjectClassification(BaseModel):
            subject: Literal["math", "english", "french", "quiz_solved", "quiz_unsolved", "other"]
            suggested_types: list[str] = Field(default_factory=list, max_length=4)
            question_count: int | None = None
            content_subject: Literal["math", "english", "french", "other"] | None = None
        module = types.ModuleType("services.subject_classifier")
        module.SubjectClassification = SubjectClassification
        self.schema_mock = patch.dict(sys.modules, {"services.subject_classifier": module})
        self.schema_mock.start()
        self.addCleanup(self.schema_mock.stop)

    async def test_catalog_preflight_uses_real_image_and_json_without_user_data(self):
        fake = Mock()
        fake.close = Mock()
        fake.aio.aclose = AsyncMock()
        fake.aio.models.generate_content = AsyncMock(
            return_value=types.SimpleNamespace(text='{"subject":"math"}'))
        with patch("google.genai.Client", return_value=fake):
            result = await _probe_catalog_candidate("KEY", "gemini-3.8-flash")
        self.assertEqual(result, (True, "working"))
        self.assertEqual(fake.aio.models.generate_content.await_count, 1)
        kwargs = fake.aio.models.generate_content.await_args.kwargs
        self.assertIsInstance(kwargs["contents"], list)
        self.assertEqual(kwargs["contents"][1].inline_data.mime_type, "image/png")
        self.assertEqual(kwargs["config"].response_mime_type, "application/json")
        fake.aio.aclose.assert_awaited_once()

    async def test_passes_arabic_text_and_image_using_real_classifier_schema(self):
        fake = Mock()
        fake.close = Mock()
        fake.aio.aclose = AsyncMock()
        fake.aio.models.generate_content = AsyncMock(return_value=types.SimpleNamespace(
            text=json.dumps({"subject": "math", "suggested_types": []})))
        with patch("google.genai.Client", return_value=fake):
            result = await probe_google_model("SECRET-KEY", "gemini-test")
        self.assertTrue(result.ready)
        self.assertTrue(result.text_ok)
        self.assertTrue(result.image_ok)
        self.assertEqual(fake.aio.models.generate_content.await_count, 2)
        first = fake.aio.models.generate_content.await_args_list[0].kwargs
        second = fake.aio.models.generate_content.await_args_list[1].kwargs
        self.assertIsInstance(first["contents"], str)
        self.assertIsInstance(second["contents"], list)
        self.assertEqual(first["config"].response_mime_type, "application/json")
        self.assertEqual(second["config"].response_mime_type, "application/json")
        fake.aio.aclose.assert_awaited_once()
        fake.close.assert_called_once()

    async def test_failed_image_cannot_qualify_for_activation(self):
        fake = Mock()
        fake.close = Mock()
        fake.aio.aclose = AsyncMock()
        fake.aio.models.generate_content = AsyncMock(side_effect=[
            types.SimpleNamespace(text='{"subject":"math"}'),
            types.SimpleNamespace(text='{"subject":"other"}')])
        with patch("google.genai.Client", return_value=fake):
            result = await probe_google_model("KEY", "model")
        self.assertTrue(result.text_ok)
        self.assertFalse(result.image_ok)
        self.assertFalse(result.ready)

    async def test_invalid_json_and_timeout_return_failure_without_leaks(self):
        fake = Mock()
        fake.close = Mock()
        fake.aio.aclose = AsyncMock()
        async def call(**kwargs):
            if isinstance(kwargs["contents"], str):
                return types.SimpleNamespace(text="not JSON SECRET-KEY")
            await asyncio.sleep(0.2)
        fake.aio.models.generate_content = AsyncMock(side_effect=call)
        with patch("google.genai.Client", return_value=fake):
            result = await probe_google_model("SECRET-KEY", "model", timeout_s=0.01)
        self.assertFalse(result.ready)
        self.assertEqual(result.text_reason, "JSON غير صالح")
        self.assertEqual(result.image_reason, "انتهت المهلة دون استجابة من النموذج")
        self.assertGreaterEqual(result.image_elapsed_ms, 9)
        self.assertNotIn("SECRET-KEY", str(result))
        fake.aio.aclose.assert_awaited_once()

    async def test_thinking_low_is_3_8_specific_and_consistent_with_adapter(self):
        from google.genai import types as google_types
        from services.ai.adapters import GoogleAdapter
        from services.ai.contracts import AIRequest
        schema = sys.modules["services.subject_classifier"].SubjectClassification
        self.assertEqual(PROBE_TIMEOUT, 24.0)
        fast = classification_config("gemini-3.8-flash", schema, google_types)
        normal = classification_config("gemma-4-31b-it", schema, google_types)
        self.assertEqual(str(fast.thinking_config.thinking_level).split(".")[-1].upper(), "LOW")
        self.assertIsNone(normal.thinking_config)

        fake = Mock()
        fake.close = Mock()
        fake.aio.aclose = AsyncMock()
        fake.aio.models.generate_content = AsyncMock(return_value=types.SimpleNamespace(
            text='{"subject":"math"}', usage_metadata=None, candidates=[]))
        with patch("google.genai.Client", return_value=fake):
            result = await GoogleAdapter().generate(
                {"transport": "gemini"}, "KEY", "gemini-3.8-flash",
                AIRequest(("مسألة رياضيات",), schema))
        self.assertEqual(result.parsed.subject, "math")
        adapter_config = fake.aio.models.generate_content.await_args.kwargs["config"]
        self.assertEqual(str(adapter_config.thinking_config.thinking_level).split(".")[-1].upper(), "LOW")
        fake.aio.aclose.assert_awaited_once()
        fake.close.assert_called_once()

    async def test_each_stage_reports_reason_and_duration(self):
        fake = Mock()
        fake.close = Mock()
        fake.aio.aclose = AsyncMock()
        async def respond(**kwargs):
            if isinstance(kwargs["contents"], str):
                await asyncio.sleep(0.06)
            return types.SimpleNamespace(text='{"subject":"math"}')
        fake.aio.models.generate_content = AsyncMock(side_effect=respond)
        with patch("google.genai.Client", return_value=fake):
            result = await probe_google_model("KEY", "gemini-3.8-flash", timeout_s=0.02)
        self.assertFalse(result.text_ok)
        self.assertTrue(result.image_ok)
        self.assertGreaterEqual(result.text_elapsed_ms, 19)
        self.assertGreaterEqual(result.elapsed_ms, result.text_elapsed_ms)
        self.assertEqual(result.text_reason, "انتهت المهلة دون استجابة من النموذج")
        self.assertGreaterEqual(result.image_elapsed_ms, 0)

    async def test_http_error_is_safe_and_limited_to_status(self):
        fake = Mock()
        fake.close = Mock()
        fake.aio.aclose = AsyncMock()
        failure = RuntimeError("SECRET-KEY request body")
        failure.code = 429
        fake.aio.models.generate_content = AsyncMock(side_effect=failure)
        with patch("google.genai.Client", return_value=fake):
            result = await probe_google_model("SECRET-KEY", "model")
        self.assertEqual(result.text_reason, "حد طلبات أو حصة Google (HTTP 429)")
        self.assertNotIn("SECRET-KEY", str(result))


if __name__ == "__main__":
    unittest.main()
