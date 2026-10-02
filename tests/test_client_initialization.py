import asyncio
import unittest
from services.async_client_init import resolve_client


class ClientInitTests(unittest.TestCase):
    def test_sync_client_unchanged(self):
        client=object();self.assertIs(resolve_client(client),client)
    def test_async_constructor_without_running_loop(self):
        async def create():return 'client'
        self.assertEqual(resolve_client(create()),'client')
    def test_import_style_init_inside_running_loop(self):
        async def create():await asyncio.sleep(0);return 'client'
        async def running():return resolve_client(create())
        self.assertEqual(asyncio.run(running()),'client')
