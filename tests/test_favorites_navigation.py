"""Saved-test navigation: preserve context, protect active flows and confirm removal."""
import ast, __future__, unittest
from pathlib import Path
from html import escape
from types import SimpleNamespace
from unittest.mock import AsyncMock
ROOT = Path(__file__).resolve().parents[1]
sources = {name: (ROOT / name).read_text(encoding='utf-8')
           for name in ('handlers/favorites.py', 'keyboards.py')}
for name, source in sources.items():
    compile(source, name, 'exec')

class Button:
    def __init__(self, text, callback_data=None, **kwargs):
        self.text, self.callback_data = text, callback_data
class Markup:
    def __init__(self, inline_keyboard): self.inline_keyboard = inline_keyboard
class Callback:
    def __init__(self, data=''):
        self.data, self.from_user = data, SimpleNamespace(id=42)
        self.answer = AsyncMock()
        self.message = SimpleNamespace(edit_text=AsyncMock(), answer=AsyncMock())
class BadRequest(Exception): pass
class State:
    def __init__(self, data=None, current=None): self.data, self.current = data or {}, current
    async def get_data(self): return dict(self.data)
    async def update_data(self, **kw): self.data.update(kw)
    async def get_state(self): return self.current
    async def set_state(self, value): self.current = getattr(value, 'state', value)
search = SimpleNamespace(state='search')
env = dict(types=SimpleNamespace(InlineKeyboardButton=Button, InlineKeyboardMarkup=Markup, CallbackQuery=Callback),
    QuizState=SimpleNamespace(searching_favorites=search), escape=escape, TelegramBadRequest=BadRequest,
    DEFAULT_FAVORITE_SECTION_TITLE='عام', MSG_FAVORITES_SEARCH_EMPTY='empty',
    get_delete_quiz_button_row=lambda *args: [], logger=SimpleNamespace(error=lambda *a: None),
    log_error=lambda *a: None)
def load(path, names):
    tree = ast.parse(sources[path])
    funcs = [n for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name in names]
    for n in funcs: n.decorator_list = []
    exec(compile(ast.Module(body=funcs, type_ignores=[]), path, 'exec',
        flags=__future__.annotations.compiler_flag), env)
load('keyboards.py', {'get_favorites_list_keyboard','get_favorite_details_keyboard','get_sections_list_keyboard'})
load('handlers/favorites.py', {'_build_favorites_text','_build_sections_text','_show_favorites_screen',
    '_end_favorites_search','_send_favorites_menu','_send_sections_menu','resume_favorites',
    'view_section_favorites','favorites_search','process_favorites_search','favorites_clear_search',
    'show_favorite_details','manage_favorite','confirm_remove_favorite'})
def actions(kb): return [b.callback_data for row in kb.inline_keyboard for b in row]
def quizzes():
    return [dict(favorite_id=f'00000000-0000-0000-0000-{i:012}', title=f'Test {i}', section_id='s1' if i<7 else 's2',
                 quiz_data=[{},{}]) for i in range(10)]

class Navigation(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        env['list_favorite_quizzes'] = AsyncMock(return_value=quizzes())
        env['list_favorite_sections'] = AsyncMock(return_value=[dict(section_id='s1',title='<Medicine>'),dict(section_id='s2',title='Other')])
    def test_result_buttons_before_tools_and_empty_recovery(self):
        kb = env['get_favorites_list_keyboard'](quizzes())
        self.assertTrue(actions(kb)[0].startswith('fav_details_'))
        self.assertEqual(sum(x.startswith('fav_details_') for x in actions(kb)), 5)
        self.assertIn('fav_page_2', actions(kb))
        self.assertEqual(actions(env['get_favorites_list_keyboard']([]))[0], 'student_create_quiz')
        self.assertNotIn('student_create_quiz', actions(env['get_favorites_list_keyboard']([],section_filter='s1')))
    def test_clamps_pages_and_keeps_callback_lengths(self):
        for page in (-10, 1, 2, 1000):
            kb = env['get_favorites_list_keyboard'](quizzes(),current_page=page)
            self.assertEqual(sum(x.startswith('fav_details_') for x in actions(kb)),5)
            for x in actions(kb): self.assertLessEqual(len(x.encode()),64)
        kb = env['get_favorite_details_keyboard'](quizzes()[0]['favorite_id'])
        self.assertNotIn('fav_del_'+quizzes()[0]['favorite_id'],actions(kb))
        self.assertIn('favorites_resume',actions(kb))
        for x in actions(kb): self.assertLessEqual(len(x.encode()),64)
    async def test_section_filter_persists_on_next_page_and_return(self):
        state = State(dict(favorites_section_filter='s1',favorites_section_title='Medicine'),current='answering_quiz')
        call = Callback()
        await env['_send_favorites_menu'](call,state,page=2)
        kb = call.message.edit_text.await_args.kwargs['reply_markup']
        self.assertEqual(sum(x.startswith('fav_details_') for x in actions(kb)),2)
        self.assertEqual(state.current,'answering_quiz')
        self.assertEqual(state.data['favorites_page'],2)
        await env['resume_favorites'](call,state)
        self.assertEqual(state.data['favorites_page'],2)
        self.assertIn('صفحة 2 من 2',call.message.edit_text.await_args.args[0])
    async def test_deletion_of_last_item_clamps_to_remaining_page(self):
        env['list_favorite_quizzes'].return_value = quizzes()[:5]
        state = State(dict(favorites_page=2))
        call = Callback()
        await env['resume_favorites'](call,state)
        self.assertEqual(state.data['favorites_page'],1)
    async def test_unsectioned_tests_have_reachable_general_section(self):
        env['list_favorite_quizzes'].return_value = [dict(favorite_id='a', section_id=None,quiz_data=[])]
        state,call = State(),Callback()
        await env['_send_sections_menu'](call,state)
        kb = call.message.edit_text.await_args.kwargs['reply_markup']
        self.assertIn('fav_sec_view_general',actions(kb))
        self.assertIn('1 اختبار',kb.inline_keyboard[0][0].text)
        state.data.update(favorites_section_filter='general',favorites_section_title='عام')
        await env['_send_favorites_menu'](call,state)
        self.assertIn('fav_details_a',actions(call.message.edit_text.await_args.kwargs['reply_markup']))
    async def test_section_entry_clears_stale_search(self):
        state,call = State(dict(favorites_search_query='old')),Callback('fav_sec_view_s1')
        await env['view_section_favorites'](call,state)
        self.assertEqual(state.data['favorites_search_query'],'')
        self.assertEqual(state.data['favorites_section_title'],'<Medicine>')
        self.assertIn('&lt;Medicine&gt;',call.message.edit_text.await_args.args[0])
    async def test_search_does_not_replace_active_flow(self):
        state,call = State(current='answering_quiz'),Callback()
        await env['favorites_search'](call,state)
        self.assertEqual(state.current,'answering_quiz')
        call.message.edit_text.assert_not_awaited()
        self.assertTrue(call.answer.await_args.kwargs['show_alert'])
    async def test_cancel_search_returns_to_same_section_page(self):
        state = State(dict(favorites_section_filter='s1',favorites_section_title='Medicine',favorites_page=2),current='search')
        call = Callback()
        await env['resume_favorites'](call,state)
        self.assertIsNone(state.current)
        self.assertEqual(state.data['favorites_page'],2)
    async def test_noop_does_not_duplicate_messages_and_deleted_message_falls_back(self):
        call = Callback()
        call.message.edit_text.side_effect = BadRequest('message is not modified')
        await env['_show_favorites_screen'](call,'text',None)
        call.message.answer.assert_not_awaited()
        call.message.edit_text.side_effect = BadRequest('message to edit not found')
        await env['_show_favorites_screen'](call,'text',None)
        call.message.answer.assert_awaited_once()
    def test_text_escapes_user_content_and_paginates_long_sections(self):
        self.assertIn('&lt;x&gt;',env['_build_favorites_text']([], 'latest','<x>'))
        sections = [dict(section_id=str(i),title='<x>') for i in range(40)]
        text = env['_build_sections_text'](sections,{},page=2)
        self.assertEqual(text.count('&lt;x&gt;'),6)
        kb = env['get_sections_list_keyboard'](sections,{},2)
        self.assertEqual(sum(x.startswith('fav_sec_view_') for x in actions(kb)),6)
        self.assertIn('fav_sections_1',actions(kb))
        self.assertIn('fav_sections_3',actions(kb))
    async def test_details_show_real_section_and_manage_requires_confirmation(self):
        fid = quizzes()[0]['favorite_id']
        env['get_favorite_quiz'] = AsyncMock(return_value=dict(title='<Test>',section_id='s1',quiz_data=[{}],quiz_id='q',creator_id=42))
        call = Callback('fav_details_'+fid)
        await env['show_favorite_details'](call,State())
        text = call.message.edit_text.await_args.args[0]
        self.assertIn('&lt;Test&gt;',text)
        self.assertIn('&lt;Medicine&gt;',text)
        call.data='fav_manage_'+fid
        await env['manage_favorite'](call)
        kb=call.message.edit_text.await_args.kwargs['reply_markup']
        self.assertIn('fav_remove_'+fid,actions(kb))
        self.assertNotIn('fav_del_'+fid,actions(kb))
        call.data='fav_remove_'+fid
        await env['confirm_remove_favorite'](call)
        kb=call.message.edit_text.await_args.kwargs['reply_markup']
        self.assertIn('fav_del_'+fid,actions(kb))
        self.assertIn('fav_details_'+fid,actions(kb))
        env['get_favorite_quiz'].assert_awaited_with(42,fid)

if __name__ == "__main__":
    unittest.main()
