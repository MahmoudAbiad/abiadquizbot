import asyncio
import types
import unittest
from unittest.mock import AsyncMock, Mock
from reliability_support import load_functions


class Message:
    def __init__(self):
        self.from_user=types.SimpleNamespace(id=1)
        self.media_group_id=None
        self.text='text';self.caption=None;self.content_type='text';self.answer=AsyncMock()


class MiddlewareTests(unittest.IsolatedAsyncioTestCase):
    async def test_rate_limit_does_not_suppress_failed_processing_retry(self):
        env=load_functions('middlewares.py',{'ThrottlingMiddleware'},BaseMiddleware=object,
            TTLCache=lambda **kw:{},Message=Message,CallbackQuery=type('CallbackQuery',(),{}),ADMIN_ID=99,logger=Mock())
        middleware=env['ThrottlingMiddleware']();event=Message()
        with self.assertRaises(ConnectionError):await middleware(AsyncMock(side_effect=ConnectionError()),event,{})
        retry=AsyncMock();await middleware(retry,event,{});retry.assert_awaited_once()

    async def test_error_tracking_propagates_failure_to_queue(self):
        env=load_functions('middlewares.py',{'ErrorTrackingMiddleware'},BaseMiddleware=object,
            Message=Message,CallbackQuery=type('CallbackQuery',(),{}),ADMIN_ID=1,logger=Mock(),
            set_error_context=Mock(return_value='token'),reset_error_context=Mock())
        update=types.SimpleNamespace(message=Message(),callback_query=None,poll_answer=None,edited_message=None,my_chat_member=None,event_type='message',update_id=1)
        with self.assertRaises(ConnectionError):await env['ErrorTrackingMiddleware']()(AsyncMock(side_effect=ConnectionError()),update,{})
        env['reset_error_context'].assert_called_once_with('token')
