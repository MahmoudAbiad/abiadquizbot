import html
import types
import unittest
from unittest.mock import AsyncMock, Mock

from reliability_support import load_functions


class Query:
    def __init__(self, database):
        self.database = database
        self.offset = 0
        self.size = 1000  # Reproduce the API's default row cap.
        self.filters = []
        self.orders = []

    def select(self, *args, **kwargs):
        return self

    def order(self, column, desc=False):
        self.orders.append((column, desc))
        return self

    def range(self, start, end):
        self.offset, self.size = start, end - start + 1
        return self

    def limit(self, size):
        self.size = size
        return self

    def gt(self, column, value):
        self.filters.append(lambda row: row[column] > value)
        return self

    def eq(self, column, value):
        self.filters.append(lambda row: row[column] == value)
        return self

    def ilike(self, column, pattern):
        self.database.pattern = pattern
        needle = pattern[1:-1].replace('\\_', '_').replace('\\%', '%').replace('\\\\', '\\')
        self.filters.append(lambda row: needle.lower() in row[column].lower())
        return self

    async def execute(self):
        rows = [row for row in self.database.rows if all(f(row) for f in self.filters)]
        for column, desc in reversed(self.orders):
            rows.sort(key=lambda row: row[column], reverse=desc)
        self.database.calls.append((self.offset, self.size))
        return types.SimpleNamespace(data=rows[self.offset:self.offset+self.size], count=len(rows))


class Database:
    def __init__(self, count=1207):
        self.rows = [dict(user_id=i, username=f'mahmoud_{i}', joined_at='2026-10-02', first_name='<Name>', last_name='') for i in range(1, count+1)]
        self.calls = []

    def table(self, name):
        return Query(self)


class AdminUsersTests(unittest.IsolatedAsyncioTestCase):
    def helpers(self, database):
        return load_functions('helpers/supabase_helper/admin_ops.py', {'_admin_users_query', 'admin_get_users_page'}, supabase=database)

    async def test_registry_can_reach_users_beyond_1000(self):
        db = Database()
        rows, count, page, pages = await self.helpers(db)['admin_get_users_page'](242)
        self.assertEqual([r['user_id'] for r in rows], [2, 1])
        self.assertEqual((count, page, pages), (1207, 242, 242))
        self.assertEqual(db.calls, [(1205, 5)])

    async def test_search_all_matches_on_pages_of_five(self):
        db = Database(12)
        helper = self.helpers(db)['admin_get_users_page']
        ids = []
        for page in range(1, 4):
            rows, count, actual, pages = await helper(page, '@MAHMOUD')
            self.assertEqual((count, actual, pages), (12, page, 3))
            self.assertLessEqual(len(rows), 5)
            ids.extend(r['user_id'] for r in rows)
        self.assertEqual(ids, list(range(12, 0, -1)))

    async def test_search_underscore_is_literal_and_id_is_exact(self):
        db = Database(12)
        db.rows.append(dict(user_id=13, username='mahmoudX1', joined_at='2026-10-02'))
        helper = self.helpers(db)['admin_get_users_page']
        rows, count, *_ = await helper(1, 'mahmoud_1')
        self.assertEqual(count, 4)
        self.assertEqual(db.pattern, '%mahmoud\\_1%')
        rows, count, *_ = await helper(1, '1')
        self.assertEqual([r['user_id'] for r in rows], [1])

    async def test_outdated_page_clamped_after_user_deletion(self):
        db = Database(6)
        rows, total, page, pages = await self.helpers(db)['admin_get_users_page'](99)
        self.assertEqual((total, page, pages), (6, 2, 2))
        self.assertEqual(len(rows), 1)

    async def test_export_contains_every_user_after_api_cap(self):
        db = Database()
        env = load_functions('handlers/admin/users.py', {'fetch_users_async'}, supabase=db)
        rows = await env['fetch_users_async']()
        self.assertEqual([r['user_id'] for r in rows], list(range(1, 1208)))
        self.assertTrue(all(size == 500 for _, size in db.calls))

    def renderer(self, db):
        class Message:
            def __init__(self): self.answer = AsyncMock()
        class Callback:
            def __init__(self): self.message = Message(); self.answer = AsyncMock()
        widget = lambda **kwargs: types.SimpleNamespace(**kwargs)
        ui = types.SimpleNamespace(Message=Message, CallbackQuery=Callback, InlineKeyboardButton=widget, InlineKeyboardMarkup=widget)
        env = load_functions('handlers/admin/users.py', {'render_user_search_page', 'callback_user_search_page'},
            types=ui, html=html, logger=Mock(), admin_get_users_page=self.helpers(db)['admin_get_users_page'], safe_edit_text=AsyncMock())
        return env, Message, Callback

    async def test_search_buttons_forward_backward_and_select_user(self):
        env, Message, _ = self.renderer(Database(12))
        message = Message()
        await env['render_user_search_page'](message, 'mahmoud', 'abc', 2)
        args, kwargs = message.answer.call_args
        self.assertIn('&lt;Name&gt;', args[0])
        callbacks = [button.callback_data for row in kwargs['reply_markup'].inline_keyboard for button in row]
        self.assertIn('admin_search_page_abc_1', callbacks)
        self.assertIn('admin_search_page_abc_3', callbacks)
        self.assertEqual(sum(c.startswith('admin_user_profile_') for c in callbacks), 5)

    async def test_expired_search_buttons_do_not_run_new_query(self):
        env, _, Callback = self.renderer(Database())
        env['render_user_search_page'] = AsyncMock()
        call = Callback(); call.data = 'admin_search_page_old_2'
        state = types.SimpleNamespace(get_data=AsyncMock(return_value={'admin_user_search_token':'new', 'admin_user_search':'mahmoud'}))
        await env['callback_user_search_page'](call, state)
        env['render_user_search_page'].assert_not_awaited()
        self.assertTrue(call.answer.call_args.kwargs['show_alert'])

    async def test_active_search_callback_preserves_query(self):
        env, _, Callback = self.renderer(Database())
        env['render_user_search_page'] = AsyncMock()
        call = Callback(); call.data = 'admin_search_page_current_3'
        state = types.SimpleNamespace(get_data=AsyncMock(return_value={'admin_user_search_token':'current', 'admin_user_search':'mahmoud'}))
        await env['callback_user_search_page'](call, state)
        env['render_user_search_page'].assert_awaited_once_with(call, 'mahmoud', 'current', 3)
