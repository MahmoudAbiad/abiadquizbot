"""Exercise Workers AI native payloads and real classifier PDF/image failover."""
import base64
import contextlib
import io
import json
import tempfile
import types
import typing
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

import fitz
import httpx
from PIL import Image
from pydantic import BaseModel, Field

import constants
from reliability_support import load_functions
from services.ai import runtime
from services.ai.adapters import ChatAdapter, ProviderError
from services.ai.contracts import AIRequest, Attachment
from services.ai.provider_presets import provider_preset
from services.ai.state import _last_model_used_var

VISION = "@cf/meta/llama-3.2-11b-vision-instruct"


def classifier_functions():
    names = {'SubjectClassification', 'ClassificationUnavailable', '_dispatch_classification',
             '_merge_classification_results', '_normalize', '_fallback', 'classify_subject'}
    deps = {key: getattr(constants, key) for key in dir(constants) if key.isupper()}
    deps.update(BaseModel=BaseModel, Field=Field, List=typing.List, Optional=typing.Optional,
                Literal=typing.Literal, logger=Mock(), log_warning=Mock(),
                _VALID_SUBJECTS={'math', 'english', 'french', 'other', 'quiz_solved', 'quiz_unsolved'})
    env = load_functions('services/subject_classifier.py', names, **deps)
    env['SubjectClassification'].model_rebuild(_types_namespace=env)
    return env


class WorkersVisionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        runtime._blocked.clear()
        runtime._pointers.clear()
        self.env = classifier_functions()
        self.schema = self.env['SubjectClassification']
        self.config = provider_preset('cloudflare', account_id='a' * 32)
        self.rows = [{'provider': 'cloudflare', 'model_name': VISION,
                      'capabilities': ['text', 'image'], 'is_enabled': True}]
        self.calls, self.clients = [], []
        self.reply = {'subject': 'math'}
        self.status = 200
        self.error = None
        original = httpx.AsyncClient
        def handler(request):
            self.calls.append(request)
            return httpx.Response(self.status, json=self.error or {'success': True, 'errors': [],
                'result': {'response': json.dumps(self.reply),
                           'usage': {'prompt_tokens': 4, 'completion_tokens': 5, 'total_tokens': 9}}})
        def factory(**kwargs):
            client = original(transport=httpx.MockTransport(handler), **kwargs)
            self.clients.append(client)
            return client
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch('services.ai.adapters.httpx.AsyncClient', side_effect=factory))
        self.stack.enter_context(patch.dict('os.environ', {'CLOUDFLARE_API_TOKEN': 'test-secret'}))
        self.stack.enter_context(patch.object(runtime, 'get_providers', AsyncMock(return_value={'cloudflare': self.config})))
        self.stack.enter_context(patch.object(runtime, 'get_enabled_models', AsyncMock(side_effect=lambda slot: self.rows)))
        self.stack.enter_context(patch.dict(runtime.ADAPTERS, {'openai_compatible': ChatAdapter()}))
        self.stack.enter_context(patch('helpers.settings_helper.get_ai_request_timeout', AsyncMock(return_value=2)))

    def image(self, color='white'):
        output = io.BytesIO()
        Image.new('RGB', (40, 40), color).save(output, format='PNG')
        return Attachment('image/png', data=output.getvalue())

    async def test_native_image_payload_schema_usage_and_cleanup(self):
        image = self.image()
        result = await runtime.execute(AIRequest(('Classify this page', image), self.schema), slot='detection')
        self.assertEqual(result.parsed.subject, 'math')
        self.assertEqual(result.total_tokens, 9)
        wire = self.calls[0]
        self.assertEqual(str(wire.url), 'https://api.cloudflare.com/client/v4/accounts/' + 'a' * 32 + '/ai/run/' + VISION)
        self.assertEqual(wire.headers['Authorization'], 'Bearer test-secret')
        body = json.loads(wire.content)
        self.assertEqual(base64.b64decode(body['image'].split(',', 1)[1]), image.data)
        self.assertIn('math', body['prompt'])
        self.assertIn('Return ONLY valid JSON', body['prompt'])
        self.assertNotIn('response_format', body)
        self.assertNotIn('messages', body)
        self.assertFalse(body['stream'])
        self.assertTrue(self.clients[0].is_closed)
        self.assertEqual(_last_model_used_var.get()['provider'], 'cloudflare')

    async def test_same_vision_model_classifies_plain_text(self):
        result = await self.env['_dispatch_classification'](contents=('English grammar',))
        self.assertEqual(result.subject, 'math')
        self.assertNotIn('image', json.loads(self.calls[0].content))
        self.assertTrue(str(self.calls[0].url).endswith('/ai/run/' + VISION))

    async def test_single_image_endpoint_reads_only_first_image_and_marks_sampling(self):
        first = self.image('red')
        # The unneeded second image must never be opened, even if its path is missing.
        request = AIRequest(('classify', first, Attachment('image/png', path='missing-second.png')), self.schema, allow_image_sampling=True)
        result = await runtime.execute(request, slot='detection')
        self.assertTrue(result.sampled)
        self.assertIsNone(await runtime.execute(AIRequest(request.contents, self.schema), slot='cascade'))
        self.assertEqual(base64.b64decode(json.loads(self.calls[0].content)['image'].split(',', 1)[1]), first.data)
        self.assertEqual(len(self.calls), 1)
        self.assertFalse(runtime._blocked)
        self.rows[0]['model_name'] = '@cf/text-model'
        self.assertIsNone(await runtime.execute(AIRequest((self.image(),), self.schema), slot='detection'))
        self.assertEqual(len(self.calls), 1)

    async def test_pdf_samples_first_page_and_does_not_invent_whole_document_counts(self):
        self.reply = {'subject': 'quiz_unsolved', 'content_subject': 'math', 'question_count': 5}
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'pages.pdf'
            with fitz.open() as document:
                for index in range(4):
                    page = document.new_page()
                    page.insert_text((30, 30), 'Page ' + str(index))
                document.save(str(path))
            from services.ai.media import _page_image
            expected = _page_image(str(path), 0).data
            result = await self.env['_dispatch_classification'](paths=[str(path)])
        self.assertEqual((result.subject, result.content_subject, result.question_count), ('quiz_unsolved', 'math', None))
        self.assertEqual(base64.b64decode(json.loads(self.calls[0].content)['image'].split(',', 1)[1]), expected)
        self.assertEqual(len(self.calls), 1)
        self.assertTrue(all(client.is_closed for client in self.clients))

    async def test_image_album_samples_first_image_only(self):
        with tempfile.TemporaryDirectory() as directory:
            paths = []
            for index, color in enumerate(['red', 'green', 'blue']):
                path = Path(directory) / f'{index}.png'
                path.write_bytes(self.image(color).data)
                paths.append(str(path))
            result = await self.env['_dispatch_classification'](paths=paths)
        self.assertEqual(result.subject, 'math')
        self.assertEqual(len(self.calls), 1)
        sent = base64.b64decode(json.loads(self.calls[0].content)['image'].split(',', 1)[1])
        self.assertEqual(sent, self.image('red').data)

    async def test_native_http_errors_invalid_json_and_license_errors_fail_over(self):
        for status, payload in [(429, {'errors': [{'message': 'test-secret'}]}),
                                (200, {'success': False, 'errors': [{'message': 'test-secret'}]}),
                                (200, {'success': True, 'result': {'response': 'Please accept the license'}}),
                                (200, {'success': True, 'result': {'response': '{"subject":"invalid"}'}})]:
            with self.subTest(status=status, payload=payload):
                runtime._blocked.clear()
                self.status, self.error = status, payload
                self.assertIsNone(await runtime.execute(AIRequest((self.image(),), self.schema), slot='detection'))
                self.assertTrue(self.clients[-1].is_closed)
        # An error on Workers AI moves to the next configured provider.
        self.rows.append({**self.rows[0], 'provider': 'backup', 'model_name': 'vision'})
        backup = {**self.config, 'id': 'backup', 'base_url': 'https://example.com/v1'}
        runtime.get_providers.return_value['backup'] = backup
        self.error = None
        self.status = 200
        runtime._blocked.clear()
        async def fail_cloud_then_backup(adapter, provider, credential, model, request):
            if provider['id'] == 'cloudflare':
                raise ProviderError(429)
            from services.ai.contracts import AIResult
            return AIResult('json', parsed=self.schema(subject='english'))
        with patch.object(ChatAdapter, 'generate', fail_cloud_then_backup):
            result = await runtime.execute(AIRequest((self.image(),), self.schema), slot='detection')
        self.assertEqual(result.parsed.subject, 'english')
        self.assertEqual(_last_model_used_var.get()['provider'], 'backup')

    async def test_partial_document_failure_is_not_cached(self):
        schema = self.schema
        results = [None, types.SimpleNamespace(parsed=schema(subject='math')), None]
        async def batches(paths):
            yield (self.image(),)
            yield (self.image(),)
        store = AsyncMock()
        env = self.env
        async def classify_media(paths):
            return await env['_dispatch_classification'](paths=paths)
        env.update(get_classification_lock=AsyncMock(return_value=None),
                   _get_cached_classification=AsyncMock(return_value=None), _store_cached_classification=store,
                   _classify_media=classify_media)
        with patch.object(runtime, 'execute', AsyncMock(side_effect=results)) as execute, \
             patch('services.ai.media.page_batches', batches):
            result = await env['classify_subject'](['file.pdf'], None, 'file-hash')
        self.assertEqual(result.subject, 'other')
        self.assertEqual(execute.await_count, 3)
        store.assert_not_awaited()
