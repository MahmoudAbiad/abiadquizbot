"""UX navigation invariants: no lost state, escaped text and valid back actions."""
import ast
import importlib.util
import re
import sys
import types
import unittest
from html import escape
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

from aiogram import types as tg
import constants as C
from reliability_support import load_functions

# Some legacy payment tests replace the helpers package globally during discovery.
_spec = importlib.util.spec_from_file_location('student_ui_copy', Path(__file__).resolve().parents[1] / 'helpers/student_ui.py')
ui = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ui)


def keyboard_env(*names):
    return load_functions('keyboards.py', set(names), **{
        **vars(C), 'types': tg, 'logger': Mock(), 'get_setting': AsyncMock(return_value=2.5),
        'STYLE_CODE_TO_NAME': {'s': 'simple', 'm': 'modern', 'a': 'academic'},
        'STYLE_LABELS_AR': {'simple': 'بسيط', 'modern': 'عصري', 'academic': 'أكاديمي'},
        'get_delete_quiz_button_row': lambda *a, **kw: [],
    })


def callbacks(keyboard):
    return [b.callback_data for row in keyboard.inline_keyboard for b in row if b.callback_data]


class StudentNavigationTests(unittest.IsolatedAsyncioTestCase):
    async def test_main_menu_primary_action_and_optional_features(self):
        env = keyboard_env('get_main_menu_keyboard')
        flag = AsyncMock(return_value=False)
        with patch.dict(sys.modules, {'helpers.supabase_helper.feature_flags': types.SimpleNamespace(is_feature_enabled=flag)}):
            for web in ('', 'https://example.com'):
                env['WEBAPP_PUBLIC_BASE_URL'] = web
                menu = await env['get_main_menu_keyboard']('bot', 42)
                data = callbacks(menu)
                self.assertEqual(data[0], 'student_create_quiz')
                self.assertEqual(data[1], 'favorites_menu')
                self.assertIn('student_help', data)
                self.assertEqual('upload_open:all' in data, bool(web))
                self.assertNotIn('cg:home:0', data)
            flag.return_value = True
            self.assertIn('cg:home:0', callbacks(await env['get_main_menu_keyboard']('bot', 42)))

    async def test_help_create_and_home_do_not_touch_active_state(self):
        env = load_functions('handlers/start.py', {'student_navigation'},
            bot=types.SimpleNamespace(get_me=AsyncMock(return_value=types.SimpleNamespace(username='bot'))),
            get_main_menu_keyboard=AsyncMock(return_value='main-menu'), _student_info_keyboard=lambda: 'info-menu',
            HOME_TEXT=ui.HOME_TEXT, HELP_TEXT=ui.HELP_TEXT, UPLOAD_TEXT=ui.UPLOAD_TEXT)
        for action in ('student_create_quiz', 'student_help', 'student_home'):
            call = types.SimpleNamespace(data=action, from_user=types.SimpleNamespace(id=42),
                answer=AsyncMock(), message=types.SimpleNamespace(answer=AsyncMock()))
            await env['student_navigation'](call)
            call.answer.assert_awaited_once()
            call.message.answer.assert_awaited_once()
        # The old home button must also preserve a newer quiz and its export data.
        state = types.SimpleNamespace(clear=AsyncMock())
        env = load_functions('handlers/quiz_runner.py', {'quiz_home'}, send_main_menu=AsyncMock())
        await env['quiz_home'](call, state)
        state.clear.assert_not_awaited()

    async def test_back_to_difficulty_restores_selected_count(self):
        state = types.SimpleNamespace(get_data=AsyncMock(return_value={
            'selected_question_count': 17, 'subject_type': 'other', 'question_type': 'general'}),
            update_data=AsyncMock(), set_state=AsyncMock())
        env = load_functions('handlers/quiz_options.py', {'back_to_difficulty'}, **{
            **vars(C), 'QuizState': types.SimpleNamespace(waiting_for_quiz_options='options'),
            '_render_difficulty_screen': AsyncMock(),
        })
        call = types.SimpleNamespace(answer=AsyncMock(), message=object())
        await env['back_to_difficulty'](call, state)
        state.update_data.assert_awaited_with(review_count_to_restore=17)
        env = load_functions('handlers/files.py', {'show_question_count_screen'}, **{
            **vars(C), 'QuizState': types.SimpleNamespace(waiting_for_count='count'),
            '_render_question_count_screen': AsyncMock(),
        })
        state.get_data.return_value = {'subject_type': 'other', 'review_count_to_restore': 17}
        msg = types.SimpleNamespace(bot=object(), chat=types.SimpleNamespace(id=1), message_id=2)
        await env['show_question_count_screen'](msg, state, edit=True)
        self.assertEqual(state.update_data.await_args.kwargs['selected_question_count'], 17)

    async def test_export_style_changes_same_message_and_preserves_favorite(self):
        env = load_functions('handlers/export.py', {'export_style_chosen', 'export_favorite_style_chosen'},
            _STYLE_RE=re.compile(r'^export_style_([sma])$'),
            _FAV_STYLE_RE=re.compile(r'^fav_export_style_([sma])_(.+)$'),
            STYLE_CODE_TO_NAME={'a': 'academic'}, STYLE_LABELS_AR={'academic': 'أكاديمي'},
            _edit_export_screen=AsyncMock(), export_format_text=ui.export_format_text,
            get_export_format_keyboard=Mock(return_value='keyboard'), _safe_answer=AsyncMock())
        call = types.SimpleNamespace(data='export_style_a', message=types.SimpleNamespace(answer=AsyncMock()))
        await env['export_style_chosen'](call)
        env['_edit_export_screen'].assert_awaited_once()
        call.message.answer.assert_not_awaited()
        call.data = 'fav_export_style_a_1234'
        await env['export_favorite_style_chosen'](call)
        env['get_export_format_keyboard'].assert_called_with('a', '1234')

    async def test_return_from_custom_count_reuses_current_message(self):
        render = AsyncMock()
        env = load_functions('handlers/files.py', {'back_to_count_review'}, _render_question_count_screen=render)
        state = object()
        call = types.SimpleNamespace(bot=object(), message=types.SimpleNamespace(chat=types.SimpleNamespace(id=1), message_id=20), answer=AsyncMock())
        await env['back_to_count_review'](call, state)
        render.assert_awaited_with(call.bot, 1, 20, state, target_for_send=call.message)

    async def test_export_edit_recovers_deleted_message_but_not_duplicate(self):
        class BadRequest(Exception):
            pass
        env = load_functions('handlers/export.py', {'_edit_export_screen'}, TelegramBadRequest=BadRequest)
        msg = types.SimpleNamespace(edit_text=AsyncMock(side_effect=BadRequest('message is not modified')), answer=AsyncMock())
        call = types.SimpleNamespace(message=msg)
        await env['_edit_export_screen'](call, 'text', None)
        msg.answer.assert_not_awaited()
        msg.edit_text.side_effect = BadRequest('message to edit not found')
        await env['_edit_export_screen'](call, 'text', 'keyboard')
        msg.answer.assert_awaited_with('text', parse_mode='HTML', reply_markup='keyboard')

    def test_export_download_buttons_and_back_targets_fit_telegram_limit(self):
        env = keyboard_env('get_export_format_keyboard', 'get_export_style_keyboard')
        favorite_id = '12345678-1234-1234-1234-123456789012'
        for favorite in ('', favorite_id):
            formats = env['get_export_format_keyboard']('a', favorite)
            actions = callbacks(formats)
            downloads = [a for a in actions if re.match(r'^(fav_)?export_(pdf|docx)', a)]
            self.assertEqual(len(downloads), 4)
            self.assertEqual(actions[-1], f'fav_export_back_{favorite}' if favorite else 'export_back')
            styles = env['get_export_style_keyboard'](favorite)
            self.assertEqual(callbacks(styles)[-1], f'fav_details_{favorite}' if favorite else 'export_close')
            for action in actions + callbacks(styles):
                self.assertLessEqual(len(action.encode()), 64)

    def test_custom_question_type_is_html_escaped(self):
        env = load_functions('services/quiz_service.py', {'build_transparency_text'}, DIFFICULTY_LABELS_AR=C.DIFFICULTY_LABELS_AR)
        text = env['build_transparency_text'](5, 12, 'Standard', 7.25, 'medium', '<الفصل & الثالث>')
        self.assertIn('&lt;الفصل &amp; الثالث&gt;', text)
        self.assertIn('7.25', text)
        self.assertNotIn('Standard', text)

    def test_favorite_titles_and_search_do_not_break_telegram_html(self):
        env = load_functions('handlers/favorites.py', {'_build_favorites_text', '_build_sections_text'},
            escape=escape, DEFAULT_FAVORITE_SECTION_TITLE='عام', MSG_FAVORITES_SEARCH_EMPTY=C.MSG_FAVORITES_SEARCH_EMPTY)
        self.assertIn('&lt;x&gt;', env['_build_favorites_text']([], 'latest', '<x>'))
        self.assertIn('&lt;x&gt;', env['_build_sections_text']([{'title': '<x>', 'section_id': 'a'}], {'a': 2}))

    def test_empty_favorites_have_recovery_action(self):
        env = keyboard_env('get_favorites_list_keyboard')
        menu = env['get_favorites_list_keyboard']([])
        self.assertEqual(callbacks(menu)[0], 'student_create_quiz')


if __name__ == '__main__':
    unittest.main()
