"""Analytics contracts: paging beyond API limits, safe button intent and wallet outcomes."""
import ast
import asyncio
import datetime
import html
import re
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

from aiogram import types
from cachetools import TTLCache
from reliability_support import load_functions
from services.usage_events import EVENT_LABELS, CALLBACK_EVENTS, CALLBACK_PATTERNS, callback_event, event_page


class TaxonomyTests(unittest.TestCase):
    def test_changed_and_legacy_buttons_have_explicit_meaning(self):
        token = 'a' * 16
        mid = 'b' * 32
        cases = {
            'student_help': 'help_clicked',
            f'qrev:{token}:0': 'error_review_clicked',
            f'qrevstart:{token}': 'error_review_clicked',
            f'qrevexp:{token}:1': 'review_explanation_clicked',
            f'qrevback:{token}:1': 'review_explanation_hide_clicked',
            f'qpractice:{token}': 'mistake_practice_clicked',
            'gift:list:0': 'gifts_clicked',
            'quiz_extract_ai_solve': 'extract_solve_clicked',
            'upload_open:document': 'upload_page_clicked',
            'cg:h:a:0': 'land_history_clicked',
            'cg:h:d:1': 'land_history_clicked',
            f'cg:b:42:{mid}': 'land_attack_clicked',
            f'lv:j:{mid}': 'live_search_clicked',
            f'sp:o:{mid}': 'spectator_open_clicked',
        }
        for data, event in cases.items():
            self.assertEqual(callback_event(data), event, data)
            self.assertIn(event, EVENT_LABELS)
        for data in [None, 'admin_main_menu', 'how_to_use', 'tut_finish', 'recharge_info',
                     f'lv:a:{mid}:1:2', 'sp:readonly', 'gift:redeem:SECRET', 'qpractice:old']:
            self.assertIsNone(callback_event(data), data)
        self.assertIn('قديم', EVENT_LABELS['quiz_shared'])
        self.assertNotIn('قديم', EVENT_LABELS['tutorial_opened'])

    def test_every_literal_emitted_event_has_a_label(self):
        for folder in ['handlers', 'helpers', 'services']:
            for path in Path(folder).rglob('*.py'):
                for node in ast.walk(ast.parse(path.read_text())):
                    if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                        and node.func.id in ('log_usage_event', 'track_usage') and len(node.args) > 1
                        and isinstance(node.args[1], ast.Constant)):
                        self.assertIn(node.args[1].value, EVENT_LABELS, str(path))

    def test_pages_include_rare_unknown_and_legacy_events_without_loss(self):
        counts = {f'event_{i:02}': i+1 for i in range(19)}
        counts['quiz_shared'] = 1
        pages = [event_page(counts, i) for i in range(1, 4)]
        self.assertEqual([len(p[0]) for p in pages], [8, 8, 4])
        self.assertEqual(dict(item for p in pages for item in p[0]), counts)
        self.assertEqual(event_page(counts, 999)[1:3], (3, 3))
        self.assertEqual(event_page({}, -1), ([], 1, 1, 0))
        self.assertEqual(event_page({'b': 1, 'a': 1}, 1)[0], [('a', 1), ('b', 1)])


class Query:
    def __init__(self, db):
        self.db, self.filters = db, []
        self.start, self.end = 0, 999
    def select(self, *a): return self
    def gte(self, field, value):
        self.filters.append(lambda r: r[field] >= value); return self
    def lte(self, field, value):
        self.filters.append(lambda r: r[field] <= value); return self
    def neq(self, field, value):
        self.filters.append(lambda r: r[field] != value); return self
    def order(self, column):
        self.column = column; return self
    def range(self, start, end):
        self.start, self.end = start, end; return self
    async def execute(self):
        self.db.calls += 1
        if self.db.fail:
            raise RuntimeError('database offline')
        rows = sorted([r for r in self.db.rows if all(f(r) for f in self.filters)], key=lambda r: r[self.column])
        # Server may be configured BELOW our requested page size.
        return SimpleNamespace(data=rows[self.start:min(self.end+1, self.start+137)])


class AnalyticsTests(unittest.IsolatedAsyncioTestCase):
    def env(self):
        now = datetime.datetime.now(datetime.timezone.utc)
        rows = [dict(id=i, user_id=i % 20, event_type='help_clicked', created_at=now.isoformat()) for i in range(1207)]
        rows += [dict(id=1208, user_id=99, event_type='error_occurred', created_at=now.isoformat()),
                 dict(id=1209, user_id=1, event_type='quiz_shared', created_at=(now-datetime.timedelta(days=100)).isoformat())]
        db = SimpleNamespace(rows=rows, calls=0, fail=False)
        db.table = lambda name: Query(db)
        env = load_functions('helpers/supabase_helper/analytics.py',
            {'_analytics_rows', '_get_usage_summary', 'admin_get_event_counts'},
            supabase=db, ADMIN_ID=99, datetime=datetime,
            _usage_summary_cache=TTLCache(maxsize=3, ttl=30), _usage_summary_lock=asyncio.Lock())
        return env, db

    async def test_counts_beyond_1000_exclude_admin_and_old_rows_and_cache_pages(self):
        env, db = self.env()
        counts = await env['admin_get_event_counts'](7)
        self.assertEqual(counts, {'help_clicked': 1207})
        self.assertGreater(db.calls, 8)
        calls = db.calls
        counts['help_clicked'] = 0
        self.assertEqual(await env['admin_get_event_counts'](7), {'help_clicked': 1207})
        self.assertEqual(db.calls, calls)
        self.assertEqual((await env['_get_usage_summary'](7))['active_users'], 20)

    async def test_failure_is_not_cached_as_an_empty_success(self):
        env, db = self.env()
        db.fail = True
        with self.assertRaises(RuntimeError): await env['admin_get_event_counts'](7)
        self.assertFalse(env['_usage_summary_cache'])
        db.fail = False
        self.assertEqual((await env['admin_get_event_counts'](7))['help_clicked'], 1207)

    async def test_paged_ui_keeps_period_and_escapes_unknown_names(self):
        counts = {f'event{i}': 20-i for i in range(17)}
        counts['<unknown>'] = 1
        env = load_functions('handlers/admin/analytics.py', {'show_all_usage_events'},
            admin_get_event_counts=AsyncMock(return_value=counts), event_page=event_page,
            html=html, EVENT_LABELS=EVENT_LABELS, types=types, safe_edit_text=AsyncMock(), logger=Mock())
        call = SimpleNamespace(data='admin_events_30_3', answer=AsyncMock(), message=SimpleNamespace(answer=AsyncMock()))
        await env['show_all_usage_events'](call)
        args = env['safe_edit_text'].await_args
        self.assertIn('3/3', args.args[1])
        self.assertIn('&lt;unknown&gt;', args.args[1])
        buttons = [b.callback_data for row in args.kwargs['reply_markup'].inline_keyboard for b in row]
        self.assertEqual(buttons, ['admin_events_30_2', 'admin_analytics_30'])

    async def test_tracking_failure_never_blocks_handler_or_records_payload(self):
        log = AsyncMock()
        def spawn(coro): coro.close()
        env = load_functions('middlewares.py', {'UsageCallbackMiddleware'},
            BaseMiddleware=object, ADMIN_ID=99, spawn=spawn, logger=Mock())
        middleware = env['UsageCallbackMiddleware']()
        call = SimpleNamespace(data='gift:redeem', from_user=SimpleNamespace(id=1),
                               message=SimpleNamespace(chat=SimpleNamespace(type='private')))
        handler = AsyncMock(return_value='ok')
        with patch.dict('sys.modules', {'helpers.supabase_helper': SimpleNamespace(log_usage_event=log)}):
            self.assertEqual(await middleware(handler, call, {}), 'ok')
            log.assert_called_once_with(1, 'gift_code_clicked', {'kind': 'button_click'})
            for uid, chat in [(99, 'private'), (1, 'group')]:
                log.reset_mock(); call.from_user.id=uid; call.message.chat.type=chat
                await middleware(handler, call, {}); log.assert_not_called()
            call.from_user.id=1; call.message.chat.type='private'
            env['spawn'] = Mock(side_effect=RuntimeError('scheduler unavailable'))
            self.assertEqual(await middleware(handler, call, {}), 'ok')
        self.assertEqual(handler.await_count, 4)

    async def test_gift_success_and_rejection_have_no_raw_code(self):
        track = Mock()
        state = SimpleNamespace(set_state=AsyncMock())
        msg = SimpleNamespace(text='ABCD-SECRET-1234', from_user=SimpleNamespace(id=42), answer=AsyncMock())
        env = load_functions('handlers/gifts.py', {'redeem_message'}, re=re, types=types,
            ensure_user=AsyncMock(return_value={}), redeem=AsyncMock(return_value={'status':'ok','points':50}),
            remaining_expiry_text=lambda v:'expiry', expiry_text=lambda v:'expiry')
        with patch('services.usage_tracking.track_usage', track):
            await env['redeem_message'](msg, state)
            track.assert_called_once_with(42, 'gift_code_redeemed', {'points':50})
            track.reset_mock()
            env['redeem'].return_value={'status':'expired'}
            await env['redeem_message'](msg, state)
            track.assert_called_once_with(42, 'gift_code_rejected', {'reason':'expired'})
            self.assertNotIn(msg.text, str(track.call_args))

if __name__ == '__main__': unittest.main()
