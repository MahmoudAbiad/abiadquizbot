"""Detection reasoning policy and compatibility boundaries."""
import unittest
from google.genai import types
from services.ai.thinking import google_thinking, chat_thinking


class ThinkingTests(unittest.TestCase):
    def test_medium_on_flash_lite_and_flash_including_vertex_resource_names(self):
        for model in ('gemini-3.5-flash-lite', 'models/gemini-3.1-flash-lite-preview',
                      'gemini-3-flash-preview', 'gemini-3.8-flash',
                      'publishers/google/models/gemini-3.6-flash', 'gemini-4-argon'):
            with self.subTest(model=model):
                config = google_thinking(model, 'medium', types)
                self.assertEqual(config.thinking_level.value, 'MEDIUM')
                self.assertIsNone(config.thinking_budget)

    def test_2_5_uses_numeric_budget_instead_of_unsupported_level(self):
        for model in ('gemini-2.5-flash', 'gemini-2.5-flash-lite', 'gemini-2.5-pro'):
            config = google_thinking(model, 'medium', types)
            self.assertEqual(config.thinking_budget, 4096)
            self.assertIsNone(config.thinking_level)

    def test_unsupported_google_models_and_regular_requests_keep_defaults(self):
        for model in ('gemma-4-31b-it', 'gemini-2.0-flash', 'gemini-3.1-flash-lite-image',
                      'gemini-3.1-pro', 'unknown'):
            self.assertIsNone(google_thinking(model, 'medium', types))
        self.assertIsNone(google_thinking('gemini-3.5-flash-lite', None, types))

    def test_chat_provider_formats_and_unsupported_models(self):
        self.assertEqual(chat_thinking('openrouter', 'google/gemini-3.5-flash-lite', 'medium'),
                         {'reasoning': {'effort': 'medium'}})
        self.assertEqual(chat_thinking('generic', 'gpt-5-mini', 'medium'),
                         {'reasoning_effort': 'medium'})
        self.assertEqual(chat_thinking('generic', 'o3', 'medium'),
                         {'reasoning_effort': 'medium'})
        self.assertEqual(chat_thinking('cloudflare', '@cf/meta/llama', 'medium'), {})
        for model in ('gpt-4o', 'gpt-5-chat-latest', 'o1-mini', 'o1-preview'):
            self.assertEqual(chat_thinking('generic', model, 'medium'), {})
        self.assertEqual(chat_thinking('openrouter', 'model', None), {})
