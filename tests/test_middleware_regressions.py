import asyncio
import types
import unittest
from unittest.mock import AsyncMock, Mock, patch
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

    async def test_unhandled_error_records_user_fallback_response(self):
        scheduled = []
        log_error_event = AsyncMock()

        def spawn(coro):
            scheduled.append(coro)

        env=load_functions('middlewares.py', {'ErrorTrackingMiddleware'},
            BaseMiddleware=object, Message=Message, CallbackQuery=type('CallbackQuery',(),{}),
            ADMIN_ID=99, logger=Mock(), spawn=spawn,
            _UNHANDLED_CALLBACK_NOTICE='⚠️ حدث خطأ غير متوقع، الرجاء المحاولة مجدداً.',
            _UNHANDLED_MESSAGE_NOTICE='⚠️ عذراً، حدث خطأ غير متوقع أثناء تنفيذ طلبك. الرجاء المحاولة مرة أخرى.',
            set_error_context=Mock(return_value='token'), reset_error_context=Mock())
        update=types.SimpleNamespace(message=Message(), callback_query=None, poll_answer=None,
            edited_message=None, my_chat_member=None, event_type='message', update_id=3)
        with patch.dict('sys.modules', {
            'helpers.supabase_helper': types.SimpleNamespace(log_error_event=log_error_event),
        }):
            with self.assertRaises(ConnectionError):
                await env['ErrorTrackingMiddleware']()(AsyncMock(side_effect=ConnectionError('boom')), update, {})
            self.assertEqual(len(scheduled), 1)
            await scheduled[0]

        kwargs = log_error_event.await_args.kwargs
        self.assertEqual(kwargs['user_response'], '⚠️ عذراً، حدث خطأ غير متوقع أثناء تنفيذ طلبك. الرجاء المحاولة مرة أخرى.')
        self.assertTrue(kwargs['user_response_delivered'])

    async def test_stale_callback_query_is_ignored_without_unhandled_tracking(self):
        class BadRequest(Exception):
            pass

        class Callback:
            def __init__(self):
                self.from_user = types.SimpleNamespace(id=2)
                self.data = "next_question"
                self.message = None

        env=load_functions('middlewares.py',
            {'ErrorTrackingMiddleware', '_is_stale_callback_query_error'},
            BaseMiddleware=object, Message=Message, CallbackQuery=Callback,
            TelegramBadRequest=BadRequest, ADMIN_ID=1, logger=Mock(),
            _STALE_CALLBACK_QUERY_MARKERS=("query is too old", "query id is invalid"),
            set_error_context=Mock(return_value='token'), reset_error_context=Mock())
        update=types.SimpleNamespace(message=None, callback_query=Callback(), poll_answer=None,
            edited_message=None, my_chat_member=None, event_type='callback_query', update_id=2)
        result = await env['ErrorTrackingMiddleware']()(
            AsyncMock(side_effect=BadRequest('Telegram server says - Bad Request: query is too old')),
            update, {})
        self.assertIsNone(result)
        env['reset_error_context'].assert_called_once_with('token')

    async def test_throttle_ignores_stale_callback_acknowledgement(self):
        class BadRequest(Exception):
            pass

        class Callback:
            def __init__(self):
                self.from_user = types.SimpleNamespace(id=2)
                self.data = "next_question"
                self.answer = AsyncMock(side_effect=BadRequest('query is too old'))

        env=load_functions('middlewares.py',
            {'ThrottlingMiddleware', '_is_stale_callback_query_error'},
            BaseMiddleware=object, TTLCache=lambda **kw: {},
            Message=Message, CallbackQuery=Callback, TelegramBadRequest=BadRequest,
            ADMIN_ID=99, logger=Mock(),
            _STALE_CALLBACK_QUERY_MARKERS=("query is too old", "query id is invalid"))
        middleware=env['ThrottlingMiddleware']()
        event=Callback()
        middleware.cache[event.from_user.id] = True
        handler=AsyncMock()
        result=await middleware(handler, event, {})
        self.assertIsNone(result)
        handler.assert_not_awaited()
