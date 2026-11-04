"""No Gemini SDK needed: verify file reuse and per-credential isolation."""
import asyncio
import hashlib
import os
import sys
import types
import unittest
from unittest.mock import AsyncMock, Mock, patch

from reliability_support import load_functions


class SessionTests(unittest.IsolatedAsyncioTestCase):
    async def test_upload_is_reused_per_key_and_cleaned_up(self):
        upload = AsyncMock(side_effect=[
            types.SimpleNamespace(name='files/first', uri='gs://one', mime_type='application/pdf'),
            types.SimpleNamespace(name='files/second', uri='gs://two', mime_type='application/pdf'),
        ])
        client = types.SimpleNamespace(aio=types.SimpleNamespace(
            files=types.SimpleNamespace(upload=upload, delete=AsyncMock())))
        deleted = []

        def new_client(api_key):
            deletion = AsyncMock(side_effect=lambda name: deleted.append((api_key, name)))
            return types.SimpleNamespace(
                aio=types.SimpleNamespace(
                    files=types.SimpleNamespace(delete=deletion), aclose=AsyncMock()),
                close=Mock())

        env = load_functions(
            'services/ai/file_session.py', {'GeminiFileSession'},
            hashlib=hashlib, os=os, asyncio=asyncio, logger=Mock())
        session = env['GeminiFileSession']()
        one = await session.get(client, 'gemini', 'secretA', 'x.pdf')
        self.assertIs(one, await session.get(client, 'gemini', 'secretA', 'x.pdf'))
        two = await session.get(client, 'gemini', 'secretB', 'x.pdf')
        self.assertIsNot(one, two)
        self.assertEqual(upload.await_count, 2)
        with patch.dict(sys.modules, {
            'google': types.SimpleNamespace(
                genai=types.SimpleNamespace(Client=new_client)),
        }):
            await session.aclose()
        self.assertEqual(set(deleted), {
            ('secretA', 'files/first'), ('secretB', 'files/second'),
        })
        self.assertFalse(session._uploaded)


if __name__ == '__main__':
    unittest.main()
