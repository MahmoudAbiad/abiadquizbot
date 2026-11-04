"""Group onboarding requests the permission needed by opt-in quiz locking."""
import html
import unittest
from unittest.mock import AsyncMock, Mock
from urllib.parse import parse_qs, urlsplit
from reliability_support import load_functions


class AddBotPermissionsTests(unittest.IsolatedAsyncioTestCase):
    async def test_group_link_requests_only_restrict_members_and_keeps_payload(self):
        keyboard = Mock()
        env = load_functions('handlers/group_quiz.py', {'_build_group_screen'},
            list_postable_groups=AsyncMock(return_value=[]),
            _filter_channels_user_admins=AsyncMock(return_value=[]),
            _get_bot_username=AsyncMock(return_value='AbiadQuizMakerbot'),
            get_group_channel_picker_keyboard=keyboard, html=html)
        text, _ = await env['_build_group_screen']('quiz-123', 42)
        query = parse_qs(urlsplit(keyboard.call_args.kwargs['add_url']).query)
        self.assertEqual(query, {'startgroup':['gqadd'], 'admin':['restrict_members']})
        self.assertIn('تقييد الأعضاء', text)
        self.assertIn('إضافة مشرفين', text)
        self.assertIn('البوت موجود من قبل', text)

    async def test_channel_link_still_requests_only_post_messages(self):
        keyboard = Mock()
        env = load_functions('handlers/group_quiz.py', {'_build_channel_screen'},
            list_postable_channels=AsyncMock(return_value=[]),
            _filter_channels_user_admins=AsyncMock(return_value=[]),
            _get_bot_username=AsyncMock(return_value='AbiadQuizMakerbot'),
            get_group_channel_picker_keyboard=keyboard)
        await env['_build_channel_screen']('quiz-123', 42)
        self.assertEqual(keyboard.call_args.kwargs['add_url'],
            'https://t.me/AbiadQuizMakerbot?startchannel&admin=post_messages')
