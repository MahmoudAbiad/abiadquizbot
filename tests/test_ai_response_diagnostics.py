"""Safe and actionable diagnostics for malformed AI responses.

These tests deliberately put secrets in *raw* provider/SDK error messages.
Neither the exception surface nor the logging path may disclose that content.
"""
import contextlib
import os
import types
import unittest
from unittest.mock import AsyncMock, Mock, patch

from pydantic import BaseModel

from services.ai.adapters import (
    AIResponseDiagnosticError, GoogleAdapter, parse_result, safe_finish_reason,
)
from services.ai.contracts import AIRequest, AIResult
from services.ai import runtime


class ExampleSchema(BaseModel):
    answer: str


class QuestionsSchema(BaseModel):
    questions: list


class ParseDiagnosticTests(unittest.TestCase):
    def test_json_error_has_safe_detail_and_no_original_content(self):
        result = AIResult('{"answer":"private student content', output_tokens=500,
                          truncated=True, finish_reason="MAX_TOKENS")
        with self.assertRaises(AIResponseDiagnosticError) as caught:
            parse_result(result, ExampleSchema)
        exc = caught.exception
        self.assertEqual(exc.detail, "truncated_json")
        self.assertEqual(exc.finish_reason, "MAX_TOKENS")
        self.assertEqual(exc.response_chars, len(result.text))
        self.assertEqual(exc.output_tokens, 500)
        self.assertNotIn("private student content", str(exc))

    def test_schema_empty_nontext_and_thinking_only_are_distinct(self):
        scenarios = [
            (AIResult('{"other":"private"}'), ExampleSchema, "schema_validation"),
            (AIResult('{"questions":[]}'), QuestionsSchema, "empty_questions"),
            (AIResult("", thoughts_tokens=88, finish_reason="MAX_TOKENS"),
             ExampleSchema, "empty_after_thinking"),
            (AIResult("", output_tokens=0), ExampleSchema, "empty_response"),
            (AIResult(None), ExampleSchema, "non_text_response"),
        ]
        for result, schema, detail in scenarios:
            with self.subTest(detail=detail):
                with self.assertRaises(AIResponseDiagnosticError) as caught:
                    parse_result(result, schema)
                self.assertEqual(caught.exception.detail, detail)

    def test_empty_questions_only_valid_for_explicit_nontruncated_extraction_eof(self):
        result = parse_result(AIResult('{"questions":[]}'), QuestionsSchema,
                              allow_empty_questions=True)
        self.assertEqual(result.parsed.questions, [])
        for response in (AIResult(''), AIResult('{"questions":[]}', truncated=True)):
            with self.assertRaises(AIResponseDiagnosticError):
                parse_result(response, QuestionsSchema, allow_empty_questions=True)

    def test_finish_reason_is_allowlisted(self):
        self.assertEqual(safe_finish_reason("length"), "MAX_TOKENS")
        self.assertEqual(safe_finish_reason("FinishReason.STOP"), "STOP")
        self.assertEqual(safe_finish_reason("private student secret"), "UNKNOWN")


class RuntimeDiagnosticTests(unittest.IsolatedAsyncioTestCase):
    async def test_extraction_eof_reaches_adapter_without_cascade_retries(self):
        from services.ai.adapters import ChatAdapter
        from helpers.gemini_helper import generation
        calls = []
        def transport(request):
            calls.append(request)
            return __import__('httpx').Response(200, json={
                'choices': [{'message': {'content': '{"questions":[]}'}, 'finish_reason': 'stop'}]})
        import httpx
        client_type = httpx.AsyncClient
        async def execute(request, **kwargs):
            return await ChatAdapter().generate(
                {'base_url': 'https://example.com/v1'}, 'fixture', 'model', request)
        request = AIRequest(('exam section without original questions',), QuestionsSchema)
        with (patch('services.ai.adapters.httpx.AsyncClient',
                    side_effect=lambda **kw: client_type(transport=httpx.MockTransport(transport), **kw)),
              patch.object(generation, '_execute_request', new=execute)):
            result = await generation._execute_cascade(request, allow_empty=True)
            self.assertEqual(result[0], [])
            self.assertEqual(len(calls), 1)
            self.assertFalse(request.allow_empty_questions)
            with self.assertRaises(AIResponseDiagnosticError):
                await generation._execute_cascade(request)

    async def test_log_contains_safe_detail_and_breakdown_but_no_raw_payload(self):
        runtime._blocked.clear()
        runtime._pointers.clear()
        runtime._shared_blocked.clear()
        self.addCleanup(runtime._blocked.clear)
        self.addCleanup(runtime._pointers.clear)
        self.addCleanup(runtime._shared_blocked.clear)
        provider = {"id": "mock", "transport": "openai_compatible",
                    "base_url": "https://example.com/v1", "is_enabled": True,
                    "api_keys_env": "TEST_DIAG_KEYS"}
        model = {"provider": "mock", "model_name": "mock-model",
                 "capabilities": ["text"], "is_enabled": True}
        invalid_text = '{"answer":"private-class-note"}'
        issues = [
            AIResponseDiagnosticError("malformed_json",
                AIResult(invalid_text, output_tokens=13, finish_reason="STOP")),
            AIResponseDiagnosticError("schema_validation",
                AIResult(invalid_text, output_tokens=17, finish_reason="STOP")),
        ]
        adapter = types.SimpleNamespace(
            capabilities={"text"},
            generate=AsyncMock(side_effect=issues),
        )
        log = Mock()
        with (patch.dict(os.environ, {"TEST_DIAG_KEYS": "secret-key-1,secret-key-2"}),
              patch.object(runtime, "get_providers", AsyncMock(return_value={"mock": provider})),
              patch.object(runtime, "get_enabled_models", AsyncMock(return_value=[model])),
              patch.dict(runtime.ADAPTERS, {"openai_compatible": adapter}),
              patch.object(runtime.logger, "warning", log)):
            self.assertIsNone(await runtime.execute(AIRequest(("hello",)), timeout=1))
        lines = "\n".join(call.args[0] % call.args[1:] for call in log.call_args_list)
        self.assertIn("response_detail=malformed_json", lines)
        self.assertIn("response_detail=schema_validation", lines)
        self.assertIn("response_chars=", lines)
        self.assertIn("diagnostics=", lines)
        self.assertIn("'count': 1", lines)
        self.assertNotIn("private-class-note", lines)
        self.assertNotIn("secret-key", lines)


class SDKDiagnosticTests(unittest.IsolatedAsyncioTestCase):
    async def test_google_response_access_failure_is_classified_without_text(self):
        class BadResponse:
            usage_metadata = None
            candidates = []
            @property
            def text(self):
                raise ValueError("private-student-secret")

        files = types.SimpleNamespace(upload=AsyncMock(), delete=AsyncMock())
        client = types.SimpleNamespace(
            aio=types.SimpleNamespace(
                files=files, aclose=AsyncMock(),
                models=types.SimpleNamespace(
                    generate_content=AsyncMock(return_value=BadResponse()))),
            close=Mock(),
        )
        provider = {"id":"google", "transport":"gemini", "base_url":None}
        with patch("google.genai.Client", return_value=client):
            with self.assertRaises(AIResponseDiagnosticError) as caught:
                await GoogleAdapter().generate(
                    provider, "private-api-key", "model", AIRequest(("test",), ExampleSchema))
        self.assertEqual(caught.exception.detail, "sdk_response_access_error")
        self.assertNotIn("private-student-secret", str(caught.exception))
        client.aio.aclose.assert_awaited_once()
        client.close.assert_called_once()


if __name__ == "__main__":
    unittest.main()
