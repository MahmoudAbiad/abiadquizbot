import datetime
import html
import time
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock
from aiogram import types
from reliability_support import load_functions


class Query:
    def __init__(self, db, table):
        self.db, self.table, self.filters, self.orders = db, table, [], []
    def select(self, columns, count=None):
        self.count = count
        return self
    def eq(self, key, value):
        self.filters.append(lambda r: r[key] == value); return self
    def lte(self, key, value):
        self.filters.append(lambda r: r[key] <= value); return self
    def order(self, key, desc=False):
        self.orders.append((key, desc)); return self
    def range(self, start, end):
        self.start, self.end = start, end; return self
    async def execute(self):
        rows = [r for r in self.db.rows[self.table] if all(f(r) for f in self.filters)]
        for key, desc in reversed(self.orders):
            rows.sort(key=lambda r: r[key], reverse=desc)
        self.db.calls.append((self.table, self.start, self.end, self.count))
        return SimpleNamespace(data=rows[self.start:self.end+1], count=len(rows))


class ActivityPagesTests(unittest.IsolatedAsyncioTestCase):
    def setup_env(self):
        stamp = '2026-10-18T00:00:00+00:00'
        rows = [dict(id=i, user_id=42, created_at=stamp, event_type='bot_start', metadata={}) for i in range(1,1208)]
        rows += [dict(id=9999, user_id=7, created_at=stamp),
                 dict(id=10000, user_id=42, created_at='2026-10-19T00:00:00+00:00')]
        attempts = [dict(id=i, user_id=42, started_at=stamp, score=i, total_questions=10) for i in range(1,12)]
        db = SimpleNamespace(rows={'usage_events':rows, 'quiz_attempts':attempts}, calls=[])
        db.table = lambda name: Query(db,name)
        fn = load_functions('helpers/supabase_helper/analytics.py', {'admin_get_user_activity_page'},
                            supabase=db, datetime=datetime)['admin_get_user_activity_page']
        cutoff = int(datetime.datetime(2026,10,18,12,tzinfo=datetime.timezone.utc).timestamp()*1000)
        return fn, db, cutoff

    async def test_all_history_beyond_1000_with_bounded_fetch_and_user_isolation(self):
        fn, db, cutoff = self.setup_env()
        result = await fn(42,'e',151,cutoff)
        self.assertEqual(result['total'],1207)
        self.assertEqual((result['page'],result['pages']),(151,151))
        self.assertEqual([r['id'] for r in result['rows']],list(range(7,0,-1)))
        self.assertEqual(db.calls,[('usage_events',1200,1207,'exact')])

    async def test_stable_ties_and_new_activity_does_not_shift_old_pages(self):
        fn, db, cutoff = self.setup_env()
        first = await fn(42,'e',1,cutoff)
        db.rows['usage_events'].append(dict(id=10001,user_id=42,created_at='2026-10-18T13:00:00+00:00'))
        second = await fn(42,'e',2,cutoff)
        self.assertEqual([r['id'] for r in first['rows']],list(range(1207,1199,-1)))
        self.assertEqual([r['id'] for r in second['rows']],list(range(1199,1191,-1)))
        self.assertEqual(second['total'],1207)

    async def test_attempt_pages_empty_user_and_stale_last_page(self):
        fn, db, cutoff = self.setup_env()
        result = await fn(42,'a',2,cutoff)
        self.assertEqual([r['id'] for r in result['rows']],[3,2,1])
        self.assertEqual(result['total'],11)
        result = await fn(42,'a',99,cutoff)
        self.assertEqual((result['page'],result['pages']),(2,2))
        result = await fn(500,'e',1,cutoff)
        self.assertEqual((result['rows'],result['total'],result['pages']),([],0,1))
        with self.assertRaises(ValueError): await fn(42,'other',1,cutoff)

    def ui_env(self, result):
        return load_functions('handlers/admin/analytics.py',
            {'show_user_activity_page','_activity_event_detail','_format_event_label','_format_seconds'},
            html=html, time=time, types=types, EVENT_LABELS={}, SOURCE_LABELS={},
            format_syria_time=lambda value:value, admin_get_user_activity_page=AsyncMock(return_value=result),
            safe_edit_text=AsyncMock(), logger=Mock())

    async def test_ui_pages_keep_student_tab_snapshot_and_return_links(self):
        result={'page':2,'pages':3,'total':20,'rows':[dict(event_type='<old>',metadata={'points':5,'code':'SECRET'},created_at='date')]}
        env=self.ui_env(result)
        call=SimpleNamespace(data='aua:42:e:2:1792324800000',answer=AsyncMock(),message=SimpleNamespace(answer=AsyncMock()))
        await env['show_user_activity_page'](call)
        args=env['safe_edit_text'].await_args
        self.assertIn('&lt;old&gt;',args.args[1]);self.assertNotIn('SECRET',args.args[1])
        self.assertIn('الصفحة 2/3',args.args[1])
        buttons=[b.callback_data for row in args.kwargs['reply_markup'].inline_keyboard for b in row]
        for value in ['aua:42:e:1:1792324800000','aua:42:e:3:1792324800000',
                      'aua:42:a:1:1792324800000','admin_user_activity_42','admin_user_profile_42']:
            self.assertIn(value,buttons)
        self.assertTrue(all(len(b.encode())<=64 for b in buttons))

    async def test_attempt_status_does_not_label_unfinished_as_stopped(self):
        result={'page':1,'pages':1,'total':3,'rows':[
            dict(source_type='file',score=1,total_questions=5,is_completed=True,started_at='date',completed_at='date',duration_seconds=30),
            dict(source_type='file',score=1,total_questions=5,is_completed=False,started_at='date',completed_at='date'),
            dict(source_type='file',score=None,total_questions=5,is_completed=False,started_at='date',completed_at=None)]}
        env=self.ui_env(result)
        call=SimpleNamespace(data='aua:42:a:1:1792324800000',answer=AsyncMock(),message=SimpleNamespace(answer=AsyncMock()))
        await env['show_user_activity_page'](call)
        text=env['safe_edit_text'].await_args.args[1]
        for label in ['✅ مكتملة','⏹ متوقفة','⏳ غير مكتملة','المدة: 30 ث']:
            self.assertIn(label,text)

    async def test_database_error_is_not_reported_as_empty_history(self):
        env=self.ui_env({});env['admin_get_user_activity_page'].side_effect=RuntimeError('offline')
        call=SimpleNamespace(data='aua:42:e:1:1792324800000',answer=AsyncMock(),message=SimpleNamespace(answer=AsyncMock()))
        await env['show_user_activity_page'](call)
        env['safe_edit_text'].assert_not_awaited();call.message.answer.assert_awaited_once()

if __name__=='__main__': unittest.main()
