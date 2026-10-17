import asyncio
import contextlib
import json
import os
import sys
import types
import unittest
from unittest.mock import AsyncMock, Mock, patch

import httpx
from pydantic import BaseModel

from helpers import ai_models_helper as models, ai_providers_helper as providers
from helpers.gemini_helper.generation import _generate_regular
from helpers.gemini_helper.schemas import QuizResponse
from services.ai import runtime
from services.ai.adapters import ChatAdapter, GoogleAdapter, ProviderError, parse_result
from services.ai.contracts import AIRequest, AIResult, Attachment
from services.ai.state import _last_model_used_var, _last_token_usage_var
from reliability_support import load_functions


def provider(identifier="custom", **changes):
    return {"id": identifier, "label": identifier, "transport": "openai_compatible",
            "base_url": "https://example.com/v1", "api_keys_env": "TEST_AI_KEYS",
            "is_enabled": True, **changes}


def model(identifier="custom", name="model", caps=None):
    return {"provider": identifier, "model_name": name, "is_enabled": True,
            "capabilities": caps or ["text"]}


class Answer(BaseModel):
    answer: str


class ProviderValidationTests(unittest.TestCase):
    def test_custom_provider_and_environment_reference(self):
        self.assertEqual(providers.validate_provider(provider())["id"], "custom")
        for changes in ({"api_keys_env": "sk-secret"}, {"base_url": "http://example.com"},
                        {"base_url": "https://user:secret@example.com"},
                        {"base_url": "https://example.com?api_key=secret"},
                        {"is_enabled": "false"}, {"transport": "unknown"}, {"api_key": "secret"}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                providers.validate_provider(provider(**changes))


class RuntimeTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        runtime._blocked.clear()
        runtime._pointers.clear()
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.dict(os.environ, {"TEST_AI_KEYS": "key-a,key-b"}))
        self.provider_rows = {"custom": provider()}
        self.model_rows = [model()]
        self.stack.enter_context(patch.object(runtime, "get_providers", AsyncMock(side_effect=lambda: self.provider_rows)))
        self.stack.enter_context(patch.object(runtime, "get_enabled_models", AsyncMock(side_effect=lambda slot: self.model_rows)))
        self.adapter = types.SimpleNamespace(capabilities={"text", "image"},
            generate=AsyncMock(return_value=AIResult("ok", total_tokens=12)))
        self.stack.enter_context(patch.dict(runtime.ADAPTERS, {"openai_compatible": self.adapter}))

    async def test_custom_provider_runs_without_gemini_and_records_real_identity(self):
        result = await runtime.execute(AIRequest(("hello",)), timeout=1)
        self.assertEqual(result.text, "ok")
        self.assertEqual(_last_model_used_var.get()["provider"], "custom")
        self.assertEqual(_last_token_usage_var.get()["total_tokens"], 12)

    async def test_public_file_generation_works_with_only_a_custom_vision_provider(self):
        parsed = QuizResponse(questions=[{"question": "q", "options": ["a", "b", "c", "d"],
                                         "correct_option_id": 0, "hint": "h"}])
        self.model_rows[0]["capabilities"] = ["text", "image"]
        self.adapter.generate.return_value = AIResult("json", parsed=parsed, total_tokens=7)
        with patch("helpers.settings_helper.get_ai_request_timeout", AsyncMock(return_value=1)):
            result = await _generate_regular(["local.png"], "Create a quiz")
        self.assertEqual(result[0][0]["question"], "q")
        self.assertEqual(result[1], 7)
        self.assertEqual(self.adapter.generate.await_args.args[3].capabilities, {"text", "image"})

    async def test_failover_rotates_all_credentials_then_provider(self):
        self.provider_rows["other"] = provider("other")
        self.model_rows.append(model("other"))
        self.adapter.generate.side_effect = [ProviderError(429), ProviderError(429), AIResult("fallback")]
        self.assertEqual((await runtime.execute(AIRequest(("hi",)), timeout=1)).text, "fallback")
        self.assertEqual([call.args[0]["id"] for call in self.adapter.generate.await_args_list],
                         ["custom", "custom", "other"])
        # Same model name and same key on a DIFFERENT provider isn't blocked.
        self.assertEqual(_last_model_used_var.get()["provider"], "other")
        self.adapter.generate.reset_mock(side_effect=True)
        self.adapter.generate.return_value = AIResult("again")
        await runtime.execute(AIRequest(("hi",)), timeout=1)
        self.assertEqual(self.adapter.generate.await_args.args[0]["id"], "other")

    async def test_disabled_deleted_and_empty_configuration_do_not_fall_back(self):
        self.provider_rows["custom"]["is_enabled"] = False
        self.assertIsNone(await runtime.execute(AIRequest(("hi",)), timeout=1))
        self.provider_rows.clear()
        self.assertIsNone(await runtime.execute(AIRequest(("hi",)), timeout=1))
        self.model_rows.clear()
        self.assertIsNone(await runtime.execute(AIRequest(("hi",)), timeout=1))
        self.adapter.generate.assert_not_awaited()

    async def test_model_and_transport_capabilities_both_required(self):
        image = Attachment("image/png", data=b"image")
        self.assertIsNone(await runtime.execute(AIRequest((image,)), timeout=1))
        self.model_rows[0]["capabilities"] = ["text", "image", "pdf"]
        self.assertIsNotNone(await runtime.execute(AIRequest((image,)), timeout=1))
        self.adapter.generate.reset_mock()
        self.assertIsNone(await runtime.execute(AIRequest((Attachment("application/pdf", data=b"pdf"),)), timeout=1))
        self.adapter.generate.assert_not_awaited()

    async def test_credential_round_robin_and_endpoint_change_clear_cooldown(self):
        await runtime.execute(AIRequest(("a",)), timeout=1)
        await runtime.execute(AIRequest(("b",)), timeout=1)
        self.assertEqual([call.args[1] for call in self.adapter.generate.await_args_list], ["key-a", "key-b"])
        self.adapter.generate.side_effect = ProviderError(429)
        await runtime.execute(AIRequest(("c",)), timeout=1)
        self.provider_rows["custom"]["base_url"] = "https://another.example/v1"
        self.adapter.generate.side_effect = None
        self.assertIsNotNone(await runtime.execute(AIRequest(("d",)), timeout=1))

    async def test_timeout_cancels_attempt_and_next_credential_works(self):
        cancelled = []
        async def generate(p, key, *args):
            if key == "key-a":
                try:
                    await asyncio.sleep(5)
                finally:
                    cancelled.append(key)
            return AIResult("second")
        self.adapter.generate.side_effect = generate
        result = await runtime.execute(AIRequest(("hi",)), timeout=0.01)
        self.assertEqual(result.text, "second")
        self.assertEqual(cancelled, ["key-a"])

    async def test_missing_local_file_does_not_penalize_or_try_other_keys(self):
        self.adapter.generate.side_effect = FileNotFoundError("missing.pdf")
        with self.assertRaises(FileNotFoundError):
            await runtime.execute(AIRequest(("hi",)), timeout=1)
        self.adapter.generate.assert_awaited_once()
        self.assertFalse(runtime._blocked)

    async def test_vertex_exclusion_uses_transport_not_provider_name(self):
        self.provider_rows = {"cloud": provider("cloud", transport="vertex", base_url=None, api_keys_env=None)}
        self.model_rows = [model("cloud")]
        result = await runtime.execute(AIRequest(("hi",)), timeout=1, exclude_vertex=True)
        self.assertIsNone(result)


class AdapterTests(unittest.IsolatedAsyncioTestCase):
    async def test_chat_payload_schema_images_usage_and_client_cleanup(self):
        calls, clients = [], []
        def handler(request):
            calls.append(request)
            return httpx.Response(200, json={"choices": [{"message": {"content": '{"answer":"ok"}'},
                "finish_reason": "length"}], "usage": {"prompt_tokens": 7, "completion_tokens": 8,
                "total_tokens": 15, "completion_tokens_details": {"reasoning_tokens": 3}}})
        original = httpx.AsyncClient
        def factory(**kwargs):
            client = original(transport=httpx.MockTransport(handler), **kwargs)
            clients.append(client)
            return client
        with patch("services.ai.adapters.httpx.AsyncClient", side_effect=factory):
            result = await ChatAdapter().generate(provider(), "secret", "my-model",
                AIRequest(("hello", Attachment("image/png", data=b"abc")), Answer))
        self.assertEqual(result.parsed.answer, "ok")
        self.assertTrue(result.truncated)
        self.assertEqual((result.input_tokens, result.output_tokens, result.thoughts_tokens, result.total_tokens), (7, 8, 0, 15))
        self.assertTrue(clients[0].is_closed)
        self.assertEqual(str(calls[0].url), "https://example.com/v1/chat/completions")
        body = json.loads(calls[0].content)
        self.assertEqual(body["model"], "my-model")
        self.assertEqual(body["response_format"]["type"], "json_object")
        self.assertIn("data:image/png;base64,YWJj", body["messages"][0]["content"][1]["image_url"]["url"])

    async def test_upstream_error_does_not_include_secrets_and_does_not_follow_redirects(self):
        original = httpx.AsyncClient
        client = original(transport=httpx.MockTransport(lambda request: httpx.Response(302,
            headers={"location": "https://untrusted.example/"}, text="secret API key")), follow_redirects=False)
        with patch("services.ai.adapters.httpx.AsyncClient", return_value=client):
            with self.assertRaises(ProviderError) as caught:
                await ChatAdapter().generate(provider(), "secret", "model", AIRequest(("hi",)))
        self.assertNotIn("secret", str(caught.exception))
        self.assertTrue(client.is_closed)

    async def test_google_uploads_are_per_attempt_and_cleaned_on_failure(self):
        import tempfile
        from pathlib import Path
        response = types.SimpleNamespace(text='{"answer":"yes"}', candidates=[], usage_metadata=None)
        files = types.SimpleNamespace(upload=AsyncMock(return_value=types.SimpleNamespace(name="files/test")),
                                      delete=AsyncMock())
        client = types.SimpleNamespace(aio=types.SimpleNamespace(files=files, aclose=AsyncMock(),
            models=types.SimpleNamespace(generate_content=AsyncMock(return_value=response))), close=Mock())
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "audio.wav"
            with path.open("wb") as file:
                file.truncate(16 * 1024 * 1024)
            with patch("google.genai.Client", return_value=client):
                result = await GoogleAdapter().generate(provider(transport="gemini", base_url=None), "key", "g-model",
                    AIRequest((Attachment("audio/wav", path=str(path)),), Answer))
                self.assertEqual(result.parsed.answer, "yes")
                client.aio.models.generate_content.side_effect = ValueError("bad")
                with self.assertRaises(ValueError):
                    await GoogleAdapter().generate(provider(transport="gemini", base_url=None), "key", "g-model",
                        AIRequest((Attachment("audio/wav", path=str(path)),), Answer))
        self.assertEqual(files.delete.await_count, 2)
        self.assertEqual(client.aio.aclose.await_count, 2)
        self.assertEqual(client.close.call_count, 2)


class ConfigurationTests(unittest.IsolatedAsyncioTestCase):
    async def test_empty_provider_table_never_recreates_builtins_and_crud_invalidates(self):
        query = Mock()
        for name in ["select", "order", "insert", "update", "eq", "delete"]:
            getattr(query, name).return_value = query
        query.execute = AsyncMock(return_value=types.SimpleNamespace(data=[]))
        fake = types.SimpleNamespace(supabase=types.SimpleNamespace(table=Mock(return_value=query)))
        providers.invalidate_provider_cache()
        self.addCleanup(providers.invalidate_provider_cache)
        with patch.dict(sys.modules, {"helpers.supabase_helper": fake}):
            self.assertEqual(await providers.get_providers(), {})
            query.execute.return_value.data = [provider()]
            await providers.save_provider(provider(), create=True)
            query.insert.assert_called_once()
            self.assertEqual((await providers.get_providers())["custom"]["id"], "custom")
            self.assertTrue(await providers.delete_provider("custom"))
            query.execute.return_value.data = []
            self.assertEqual(await providers.get_providers(), {})

    async def test_disabling_all_models_is_respected(self):
        with patch.object(models, "get_all_models", AsyncMock(return_value=[{**model(), "is_enabled": False}])):
            self.assertEqual(await models.get_enabled_models("cascade"), [])


class ProviderUITests(unittest.IsolatedAsyncioTestCase):
    async def test_router_registration_and_admin_private_filters(self):
        import importlib.util
        from pathlib import Path
        from aiogram.filters import BaseFilter
        class AdminFilter(BaseFilter):
            async def __call__(self, event):
                return True
        dashboard = types.SimpleNamespace(IsAdminFilter=AdminFilter, safe_edit_text=AsyncMock())
        with patch.dict(sys.modules, {"handlers.admin.dashboard": dashboard}):
            spec = importlib.util.spec_from_file_location("handlers.admin.ai_providers",
                Path(__file__).resolve().parents[1] / "handlers/admin/ai_providers.py")
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
        self.assertGreaterEqual(len(module.router.callback_query.handlers), 9)
        # Global guards apply to every wizard state, including secret-reference input.
        self.assertEqual(len(module.router.message._handler.filters), 2)
        self.assertEqual(len(module.router.callback_query._handler.filters), 2)

    async def test_failed_save_stays_in_preview_and_reports_failure(self):
        call = types.SimpleNamespace(answer=AsyncMock())
        state = types.SimpleNamespace(get_data=AsyncMock(return_value={"draft": provider(), "create": True}))
        env = load_functions('handlers/admin/ai_providers.py', {"commit_provider"},
            save_provider=AsyncMock(side_effect=ConnectionError()), list_providers=AsyncMock())
        await env['commit_provider'](call, state)
        env['list_providers'].assert_not_awaited()
        self.assertTrue(call.answer.await_args.kwargs['show_alert'])

    async def test_model_edit_is_applied_to_correct_slot(self):
        state = types.SimpleNamespace(get_data=AsyncMock(return_value={"model_id": 42, "slot": "cascade"}), clear=AsyncMock())
        message = types.SimpleNamespace(text='new-name | text,image', answer=AsyncMock())
        render = AsyncMock()
        with patch.dict(sys.modules, {"handlers.admin.ai_control": types.SimpleNamespace(_render_slot_screen=render)}):
            env = load_functions('handlers/admin/ai_providers.py', {"save_model"}, update_model=AsyncMock())
            env['__package__'] = 'handlers.admin'
            await env['save_model'](message, state)
        env['update_model'].assert_awaited_once_with(42, 'cascade', 'new-name', ['text', 'image'])
        state.clear.assert_awaited_once()
        render.assert_awaited_once_with(message, 'cascade')
