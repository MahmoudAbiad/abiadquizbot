"""واجهة «إعدادات إضافية»: callback_data، التحقق، حفظ الإعدادات بالجلسة، وتمريرها لمولّد الملف."""
import ast
import asyncio
import re
import types
import unittest
from dataclasses import replace
from html import escape
from pathlib import Path
from unittest.mock import AsyncMock, Mock

from aiogram import types as tg
import constants as C
from reliability_support import load_functions
from services import export_options as eo
from services.export_options import ExportOptions, ExportOptionsError

HANDLER_FUNCS = {
    '_get_options', '_save_options', '_active_settings_note', '_leave_input_state', '_questions_for_settings',
    '_show_settings', 'export_settings_action', '_start_input', 'export_setting_value',
}


class FakeState:
    """بديل بسيط لـ FSMContext: بيانات + حالة، بنفس دلالات update_data/set_state."""
    def __init__(self, data=None, state=None):
        self.data, self.current = dict(data or {}), state

    async def get_data(self):
        return dict(self.data)

    async def update_data(self, **kw):
        self.data.update(kw)

    async def set_state(self, value=None):
        self.current = getattr(value, 'state', value)

    async def get_state(self):
        return self.current


class _St:
    def __init__(self, name): self.state = name


def handler_env(**extra):
    ns = dict(
        asyncio=asyncio, escape=escape, replace=replace, re=re, List=list,
        ExportState=types.SimpleNamespace(waiting_for_value=_St('ExportState:waiting_for_value')),
        ExportOptions=ExportOptions, ExportOptionsError=ExportOptionsError,
        FIELD_CODES=eo.FIELD_CODES, TEXT_FIELDS=eo.TEXT_FIELDS, PLACEMENTS=eo.PLACEMENTS,
        STYLE_CODE_TO_NAME={'s': 'simple', 'm': 'modern', 'a': 'academic'},
        STYLE_LABELS_AR={'simple': 'بسيط', 'modern': 'عصري', 'academic': 'أكاديمي'},
        MSG_NO_QUESTIONS='no questions', _FIELD_PROMPTS={f: f'prompt {f}' for f in eo.TEXT_FIELDS},
        _XO_RE=re.compile(r"^xo\|([a-z]+)\|([A-Za-z0-9_-]*)\|([sma])\|(.*)$"),
        _edit_export_screen=AsyncMock(), _safe_answer=AsyncMock(),
        export_settings_text=lambda lines, notice='': f"{notice}|{';'.join(lines)}",
        export_format_text=lambda label: f'format {label}',
        get_export_settings_keyboard=Mock(side_effect=lambda o, s, f: ('settings', o, s, f)),
        get_export_per_page_keyboard=Mock(return_value='per_page_kb'),
        get_export_placement_keyboard=Mock(return_value='placement_kb'),
        get_export_input_keyboard=Mock(return_value='input_kb'),
        get_export_format_keyboard=Mock(return_value='format_kb'),
        max_questions_per_page=lambda questions, style: 6, max_questions_per_page_docx=lambda questions, style: 4, get_favorite_quiz=AsyncMock(),
    )
    ns.update(extra)
    return load_functions('handlers/export.py', HANDLER_FUNCS, **ns)


def call(data, user=1):
    return types.SimpleNamespace(data=data, from_user=types.SimpleNamespace(id=user), message=types.SimpleNamespace(answer=AsyncMock()))


Q = [{'question': 'q', 'options': ['a', 'b'], 'correct_option_id': 0}]


class SettingsFlowTests(unittest.IsolatedAsyncioTestCase):
    async def test_toggle_buttons_are_saved_in_session(self):
        env = handler_env()
        state = FakeState({'questions': Q})
        for data in ('xo|sf||s|', 'xo|am||s|', 'xo|pn||s|', 'xo|plv|footer|s|'):
            await env['export_settings_action'](call(data), state)
        saved = ExportOptions.from_state_data(state.data)
        self.assertFalse(saved.student_fields)
        self.assertEqual(saved.answers_mode, 'key')
        self.assertIs(saved.page_numbers, True)
        self.assertEqual(saved.teacher_placement, 'footer')

    async def test_per_page_screen_limits_buttons_and_validates_button_values(self):
        env = handler_env()
        state = FakeState({'questions': Q})
        await env['export_settings_action'](call('xo|pp||s|'), state)
        env['get_export_per_page_keyboard'].assert_called_with(6, None, 's', '')
        self.assertEqual(state.current, 'ExportState:waiting_for_value')
        await env['export_settings_action'](call('xo|ppv|4|s|'), state)
        self.assertEqual(ExportOptions.from_state_data(state.data).per_page, 4)
        self.assertIsNone(state.current)  # خرج من انتظار النص
        # قيمة فوق الحد الأعلى لهذا الكويز تُرفض برسالة (حتى لو وصلت بزر قديم)
        c = call('xo|ppv|9|s|')
        await env['export_settings_action'](c, state)
        self.assertEqual(ExportOptions.from_state_data(state.data).per_page, 4)
        self.assertIn('6', env['_safe_answer'].await_args.args[1])
        await env['export_settings_action'](call('xo|ppv|auto|s|'), state)
        self.assertIsNone(ExportOptions.from_state_data(state.data).per_page)

    async def test_per_page_without_questions_is_refused(self):
        env = handler_env()
        await env['export_settings_action'](call('xo|pp||s|'), FakeState({}))
        env['_safe_answer'].assert_awaited()
        self.assertEqual(env['_safe_answer'].await_args.args[1], 'no questions')

    async def test_text_input_flow_validates_then_saves_and_restores_state(self):
        env = handler_env()
        state = FakeState({'questions': Q}, state='previous')
        await env['export_settings_action'](call('xo|ed|n|s|'), state)
        self.assertEqual(state.current, 'ExportState:waiting_for_value')
        self.assertEqual(state.data['export_input']['prev'], 'previous')
        too_long = types.SimpleNamespace(text='x' * 80, answer=AsyncMock())
        await env['export_setting_value'](too_long, state)
        self.assertIn('60', too_long.answer.await_args.args[0])
        self.assertEqual(state.current, 'ExportState:waiting_for_value')  # يبقى ينتظر قيمة صحيحة
        ok = types.SimpleNamespace(text='  أحمد   العلي ', answer=AsyncMock())
        await env['export_setting_value'](ok, state)
        self.assertEqual(ExportOptions.from_state_data(state.data).teacher_name, 'أحمد العلي')
        self.assertEqual(state.current, 'previous')
        self.assertIsNone(state.data['export_input'])
        self.assertEqual(ok.answer.await_args.kwargs['reply_markup'][0], 'settings')

    async def test_typed_per_page_uses_quiz_specific_maximum(self):
        env = handler_env()
        state = FakeState({'questions': Q})
        await env['export_settings_action'](call('xo|pp||s|'), state)
        bad = types.SimpleNamespace(text='8', answer=AsyncMock())
        await env['export_setting_value'](bad, state)
        self.assertIn('6', bad.answer.await_args.args[0])
        good = types.SimpleNamespace(text='٣', answer=AsyncMock())
        await env['export_setting_value'](good, state)
        self.assertEqual(ExportOptions.from_state_data(state.data).per_page, 3)

    async def test_clear_reset_and_back_leave_input_state(self):
        env = handler_env()
        state = FakeState({'questions': Q, 'export_options': {'subject': 'math', 'per_page': 2}})
        await env['export_settings_action'](call('xo|ed|s|s|'), state)
        await env['export_settings_action'](call('xo|clr|s|s|'), state)
        self.assertEqual(ExportOptions.from_state_data(state.data).subject, '')
        self.assertIsNone(state.current)
        await env['export_settings_action'](call('xo|ed|g|s|'), state)
        await env['export_settings_action'](call('xo|open||s|'), state)
        self.assertIsNone(state.current)
        await env['export_settings_action'](call('xo|rs||s|'), state)
        self.assertTrue(ExportOptions.from_state_data(state.data).is_default)

    async def test_done_returns_to_format_screen_with_active_note(self):
        env = handler_env()
        state = FakeState({'export_options': {'teacher_name': 'علي'}})
        await env['export_settings_action'](call('xo|done||m|FAV1'), state)
        env['get_export_format_keyboard'].assert_called_with('m', 'FAV1')
        text = env['_edit_export_screen'].await_args.args[1]
        self.assertIn('علي', text)

    async def test_favorite_settings_load_quiz_from_database(self):
        env = handler_env()
        env['get_favorite_quiz'].return_value = {'quiz_data': Q}
        await env['export_settings_action'](call('xo|pp||s|FAV1', user=7), FakeState({}))
        env['get_favorite_quiz'].assert_awaited_with(7, 'FAV1')
        env['get_export_per_page_keyboard'].assert_called_with(6, None, 's', 'FAV1')

    async def test_text_in_wrong_state_is_not_stored(self):
        env = handler_env()
        state = FakeState({})
        msg = types.SimpleNamespace(text='hello', answer=AsyncMock())
        await env['export_setting_value'](msg, state)
        self.assertEqual(state.data.get('export_options'), None)


class KeyboardTests(unittest.TestCase):
    def env(self):
        return load_functions('keyboards.py', {
            '_xo', 'get_export_settings_keyboard', 'get_export_per_page_keyboard',
            'get_export_placement_keyboard', 'get_export_input_keyboard', 'get_export_format_keyboard'},
            **{**vars(C), 'types': tg, 'logger': Mock(), 'get_setting': AsyncMock(return_value=2.5),
               'STYLE_CODE_TO_NAME': {'s': 'simple', 'm': 'modern', 'a': 'academic'},
               'STYLE_LABELS_AR': {'simple': 'بسيط', 'modern': 'عصري', 'academic': 'أكاديمي'},
               'ANSWERS_MODE_LABELS_AR': eo.ANSWERS_MODE_LABELS_AR, 'FIELD_CODES': eo.FIELD_CODES,
               'MAX_PER_PAGE': eo.MAX_PER_PAGE, 'PLACEMENT_LABELS_AR': eo.PLACEMENT_LABELS_AR,
               'PLACEMENTS': eo.PLACEMENTS, 'TEXT_FIELDS': eo.TEXT_FIELDS})

    def all_callbacks(self, kb):
        return [b.callback_data for row in kb.inline_keyboard for b in row]

    def test_every_callback_fits_telegram_limit_and_matches_handler_regex(self):
        env = self.env()
        regex = re.compile(r"^xo\|([a-z]+)\|([A-Za-z0-9_-]*)\|([sma])\|(.*)$")
        fav = '12345678-1234-1234-1234-123456789012'
        full = ExportOptions(per_page=3, subject='a', teacher_name='t', total_marks='5').to_dict()
        for favorite in ('', fav):
            boards = [
                env['get_export_settings_keyboard']({}, 'a', favorite),
                env['get_export_settings_keyboard'](full, 'a', favorite),
                env['get_export_per_page_keyboard'](10, 3, 'a', favorite),
                env['get_export_placement_keyboard']('footer', 'a', favorite),
                env['get_export_input_keyboard']('n', True, 'a', favorite),
                env['get_export_format_keyboard']('a', favorite),
            ]
            for kb in boards:
                for data in self.all_callbacks(kb):
                    self.assertLessEqual(len(data.encode()), 64, data)
                    if data.startswith('xo|'):
                        self.assertEqual(regex.match(data).group(4), favorite, data)

    def test_format_keyboard_has_extra_settings_button_before_back(self):
        env = self.env()
        data = self.all_callbacks(env['get_export_format_keyboard']('s', ''))
        self.assertEqual(data[-2], 'xo|open||s|')
        self.assertEqual(data[-1], 'export_back')
        texts = [b.text for row in env['get_export_format_keyboard']('s', '').inline_keyboard for b in row]
        self.assertIn('⚙️ إعدادات إضافية', texts)

    def test_per_page_keyboard_offers_only_valid_numbers(self):
        env = self.env()
        kb = env['get_export_per_page_keyboard'](4, 2, 's', '')
        values = [d.split('|')[2] for d in self.all_callbacks(kb) if d.startswith('xo|ppv|')]
        self.assertEqual(values, ['1', '2', '3', '4', 'auto'])
        self.assertEqual(len(env['get_export_per_page_keyboard'](99, None, 's', '').inline_keyboard[0]), 5)
        self.assertEqual([d.split('|')[2] for d in self.all_callbacks(
            env['get_export_per_page_keyboard'](99, None, 's', '')) if d.startswith('xo|ppv|')][-2], '10')


class GenerateWithOptionsTests(unittest.IsolatedAsyncioTestCase):
    def ns(self):
        from tests.test_export_queue import load_functions as load_queue
        return load_queue()

    @staticmethod
    def make_call(user=1):
        status = types.SimpleNamespace(edit_text=AsyncMock(), delete=AsyncMock())
        return types.SimpleNamespace(from_user=types.SimpleNamespace(id=user), message=types.SimpleNamespace(
            answer=AsyncMock(return_value=status), answer_document=AsyncMock()))

    async def test_options_reach_local_builder_filename_and_caption(self):
        ns = self.ns()
        builder = ns['build_quiz_pdf'] = Mock(return_value=b'pdf')
        ns['build_export_filename'] = lambda title, fmt: f'{title}.{fmt}'
        opts = ExportOptions(per_page=3, exam_title='Final', teacher_name='Ali')
        c = self.make_call()
        await ns['_generate_and_send'](c, [{'question': 'q'}], 'Quiz', 'pdf', 's', True, opts)
        builder.assert_called_once_with('Quiz', [{'question': 'q'}], 'simple', True, opts)
        kwargs = c.message.answer_document.call_args.kwargs
        self.assertIn('Final', kwargs['caption'])
        self.assertIn('Ali', kwargs['caption'])

    async def test_options_are_sent_to_remote_worker_as_plain_dict(self):
        ns = self.ns()
        ns['offload'].ENABLED = True
        ns['offload'].remote_build_quiz = AsyncMock(return_value=b'remote')
        opts = ExportOptions(per_page=2)
        await ns['_generate_and_send'](self.make_call(), [{'question': 'q'}], 'Quiz', 'docx', 's', False, opts)
        ns['offload'].remote_build_quiz.assert_awaited_once_with(
            'docx', 'Quiz', [{'question': 'q'}], 'simple', False, {'per_page': 2})

    async def test_default_options_keep_legacy_call_signature(self):
        ns = self.ns()
        builder = ns['build_quiz_xlsx'] = Mock(return_value=b'x')
        await ns['_generate_and_send'](self.make_call(), [{'question': 'q'}], 'T', 'xlsx', 's', False, ExportOptions())
        builder.assert_called_once_with('T', [{'question': 'q'}], 'simple', False)


class WorkerPayloadTests(unittest.TestCase):
    def test_worker_validates_and_applies_options(self):
        import logging
        logging.disable(logging.CRITICAL)
        self.addCleanup(logging.disable, logging.NOTSET)
        from services import offload_worker_core as core
        qs = [{'question': 'سؤال', 'options': ['أ', 'ب'], 'correct_option_id': 0} for _ in range(5)]
        base = {'fmt': 'pdf', 'title': 't', 'questions': qs, 'style': 'simple', 'include_answers': False}
        legacy, _ = core.build_quiz(dict(base))
        with_opts, _ = core.build_quiz({**base, 'options': {'per_page': 1}})
        self.assertNotEqual(legacy, with_opts)
        with self.assertRaises(core.BadPayload):
            core.build_quiz({**base, 'options': {'per_page': 0}})
        with self.assertRaises(core.BadPayload):
            core.build_quiz({**base, 'options': 'x'})


if __name__ == '__main__':
    unittest.main()
