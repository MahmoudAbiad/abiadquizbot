"""Expiry countdowns and real wallet/query functions against isolated gift fixtures."""
from datetime import datetime, timedelta, timezone
import re
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock

from aiogram import types
from reliability_support import load_functions
from services.gift_presentation import expiry_text, gift_text, remaining_expiry_text


NOW = datetime(2026, 10, 18, 12, tzinfo=timezone.utc)


class CountdownTests(unittest.TestCase):
    def test_days_are_remaining_time_not_initial_gift_duration(self):
        end = NOW + timedelta(days=10)
        self.assertEqual(remaining_expiry_text(end.isoformat(), NOW),
                         'باقي على انتهاء صلاحية نقاط هذه الهدية: 10 أيام.')
        self.assertIn('9 أيام و 23 ساعة', remaining_expiry_text(end.isoformat(), NOW + timedelta(hours=1)))
        self.assertIn('11 يوماً', remaining_expiry_text((NOW + timedelta(days=11)).isoformat(), NOW))

    def test_less_than_a_day_uses_hours_and_minutes_without_false_expiry(self):
        self.assertIn('5 ساعات و 30 دقيقة', remaining_expiry_text((NOW + timedelta(hours=5, minutes=30)).isoformat(), NOW))
        self.assertIn('ساعتان', remaining_expiry_text((NOW + timedelta(hours=2)).isoformat(), NOW))
        self.assertIn('دقيقتان', remaining_expiry_text((NOW + timedelta(minutes=2)).isoformat(), NOW))
        for seconds in (45, 0.5):
            self.assertIn('أقل من دقيقة', remaining_expiry_text((NOW + timedelta(seconds=seconds)).isoformat(), NOW))

    def test_expired_forever_and_invalid_dates_are_distinct(self):
        for end in (NOW, NOW - timedelta(days=1)):
            self.assertEqual(remaining_expiry_text(end.isoformat(), NOW), 'انتهت صلاحية نقاط هذه الهدية.')
        self.assertIn('بدون انتهاء صلاحية', remaining_expiry_text(None, NOW))
        self.assertIn('تعذر تحديد', remaining_expiry_text('invalid', NOW))

    def test_equivalent_damascus_and_utc_timestamps_have_same_countdown(self):
        self.assertEqual(remaining_expiry_text('2026-10-19T15:00:00+03:00', NOW),
                         remaining_expiry_text('2026-10-19T12:00:00Z', NOW))
        self.assertIn('15:00', expiry_text('2026-10-19T12:00:00Z'))

    def test_compact_student_summary_preserves_admin_details_and_inactive_status(self):
        gift = dict(title='<هدية>', remaining_amount=5, original_amount=10,
                    granted_at='2026-10-01T00:00:00Z', expires_at='2099-10-11T00:00:00Z')
        student = gift_text(gift, compact=True)
        self.assertIn('&lt;هدية&gt;', student)
        self.assertIn('5</code> من 10 نقطة', student)
        self.assertIn('باقي على انتهاء صلاحية نقاط هذه الهدية', student)
        self.assertIn('انتهاء الهدية:', student)
        self.assertNotIn('بداية الهدية:', student)
        self.assertNotIn('المدة التقديرية:', student)
        self.assertIn('بداية الهدية:', gift_text(gift))
        self.assertIn('المدة التقديرية:', gift_text(gift))
        for updates, status in [({'revoked_at': '2026-10-01'}, 'ملغاة'),
                                ({'remaining_amount': 0}, 'مستهلكة'),
                                ({'expires_at': '2000-01-01T00:00:00Z'}, 'منتهية')]:
            text = gift_text({**gift, **updates}, compact=True)
            self.assertIn(status, text)
            self.assertNotIn('باقي على انتهاء', text)


class GiftQuery:
    """In-memory SELECT double: retain actual owner/order/inclusive-range arguments."""
    def __init__(self, fixture, requests):
        self.fixture, self.requests = fixture, requests
        self.filters, self.ordering = [], []
        self.bounds = None

    def select(self, fields):
        assert fields == '*'
        return self

    def eq(self, field, value):
        self.filters.append((field, value))
        return self

    def order(self, field, desc=False):
        self.ordering.append((field, desc))
        return self

    def range(self, start, end):
        self.bounds = (start, end)
        return self

    async def execute(self):
        self.requests.append((self.filters, self.ordering, self.bounds))
        rows = [g for g in self.fixture if all(g[field] == value for field, value in self.filters)]
        for field, desc in reversed(self.ordering):
            rows.sort(key=lambda g: g[field], reverse=desc)
        start, end = self.bounds
        return SimpleNamespace(data=rows[start:end + 1])


class Callback(SimpleNamespace):
    pass


class GiftNavigationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.gifts = [dict(id=i, user_id=42, title=f'Gift {i}', remaining_amount=10,
                           original_amount=10, granted_at=f'2026-10-{i+1:02d}T00:00:00Z',
                           expires_at=None) for i in range(7)]
        self.gifts.append({**self.gifts[-1], 'id': 99, 'user_id': 99, 'title': 'Other user'})
        self.requests = []

        def table(name):
            self.assertEqual(name, 'point_gifts')
            return GiftQuery(self.gifts, self.requests)

        self.list_gifts = load_functions('helpers/supabase_helper/gifts.py', {'list_gifts'},
            supabase=SimpleNamespace(table=table))['list_gifts']
        self.env = load_functions('handlers/gifts.py',
            {'show_gifts', '_show_code_screen', 'wallet', 'wallet_buttons', 'redeem_message'},
            re=re, list_gifts=self.list_gifts, gift_text=gift_text,
            remaining_expiry_text=lambda value: remaining_expiry_text(value, NOW), expiry_text=expiry_text,
            types=SimpleNamespace(CallbackQuery=Callback, InlineKeyboardButton=types.InlineKeyboardButton,
                                  InlineKeyboardMarkup=types.InlineKeyboardMarkup),
            ensure_user=AsyncMock(return_value=dict(points=50, free_points=10, gift_points=30, paid_points=10)),
            redeem=AsyncMock(return_value=dict(status='ok', points=50, expires_at=(NOW + timedelta(days=10)).isoformat())),
            TelegramBadRequest=type('BadRequest', (Exception,), {}))

    def call(self, page=0):
        return Callback(data=f'gift:list:{page}', from_user=SimpleNamespace(id=42), answer=AsyncMock(),
                        message=SimpleNamespace(edit_text=AsyncMock(), answer=AsyncMock()))

    async def page(self, page):
        call = self.call(page)
        await self.env['show_gifts'](call)
        text = call.message.edit_text.await_args.args[0]
        keyboard = call.message.edit_text.await_args.kwargs['reply_markup']
        buttons = [b.callback_data for row in keyboard.inline_keyboard for b in row]
        call.message.answer.assert_not_awaited()
        call.answer.assert_awaited_once_with()
        return text, buttons

    async def test_three_pages_are_newest_first_disjoint_and_user_scoped(self):
        expected = [[6, 5, 4], [3, 2, 1], [0]]
        for page, indices in enumerate(expected):
            text, buttons = await self.page(page)
            self.assertEqual(re.findall(r'<b>Gift (\d+)</b>', text), [str(i) for i in indices])
            self.assertIn(f'صفحة {page+1}', text)
            self.assertEqual('gift:list:' + str(page-1) in buttons, page > 0)
            self.assertEqual('gift:list:' + str(page+1) in buttons, page < 2)
            self.assertEqual(buttons[-1], 'gift:wallet')
            self.assertNotIn('Other user', text)
        self.assertEqual([request[2] for request in self.requests], [(0, 3), (3, 6), (6, 9)])
        self.assertEqual(self.requests[0][0], [('user_id', 42)])
        self.assertEqual(self.requests[0][1], [('granted_at', True), ('id', False)])

    async def test_exactly_three_gifts_have_no_next_page_and_empty_list_has_back(self):
        self.gifts[:] = self.gifts[:3]
        _, buttons = await self.page(0)
        self.assertEqual(buttons, ['gift:wallet'])
        self.gifts.clear()
        text, buttons = await self.page(0)
        self.assertIn('لا توجد هدايا', text)
        self.assertEqual(buttons, ['gift:wallet'])

    async def test_stale_page_returns_to_first_and_invalid_page_does_not_query(self):
        text, buttons = await self.page(30)
        self.assertIn('صفحة 1', text)
        self.assertNotIn('gift:list:29', buttons)
        count = len(self.requests)
        for bad in ('invalid', '-1', '123456789'):
            call = self.call(bad)
            await self.env['show_gifts'](call)
            call.message.edit_text.assert_not_awaited()
            self.assertTrue(call.answer.await_args.kwargs['show_alert'])
        self.assertEqual(len(self.requests), count)

    async def test_admin_default_keeps_five_rows_plus_lookahead(self):
        rows = await self.list_gifts(42)
        self.assertEqual(len(rows), 6)
        self.assertEqual(self.requests[-1][2], (0, 5))

    async def test_success_message_has_remaining_days_then_create_and_wallet_actions(self):
        message = SimpleNamespace(text='ABIAD-1234567890', from_user=SimpleNamespace(id=42), answer=AsyncMock())
        state = SimpleNamespace(set_state=AsyncMock())
        await self.env['redeem_message'](message, state)
        state.set_state.assert_awaited_once_with(None)
        self.env['redeem'].assert_awaited_once_with(42, message.text)
        text = message.answer.await_args.args[0]
        self.assertIn('50 نقطة هدية', text)
        self.assertIn('نقاط هذه الهدية: 10 أيام', text)
        self.assertIn('بتوقيت دمشق', text)
        buttons = message.answer.await_args.kwargs['reply_markup'].inline_keyboard
        self.assertEqual([[b.callback_data for b in row] for row in buttons],
                         [['student_create_quiz'], ['gift:wallet']])

    async def test_return_to_wallet_edits_same_message_with_wallet_main_menu(self):
        call = self.call()
        call.data = 'gift:wallet'
        await self.env['wallet'](call)
        call.message.answer.assert_not_awaited()
        text = call.message.edit_text.await_args.args[0]
        self.assertIn('رصيدك: 50.00', text)
        self.assertEqual(call.message.edit_text.await_args.kwargs['parse_mode'], 'HTML')
        keyboard = call.message.edit_text.await_args.kwargs['reply_markup']
        self.assertEqual([b.callback_data for row in keyboard.inline_keyboard for b in row],
                         ['recharge_info', 'gift:list:0', 'gift:redeem', 'student_home'])

    async def test_duplicate_page_edit_is_acknowledged_without_extra_message(self):
        call = self.call()
        call.message.edit_text.side_effect = self.env['TelegramBadRequest']('message is not modified')
        await self.env['show_gifts'](call)
        call.message.answer.assert_not_awaited()
        call.answer.assert_awaited_once_with()

