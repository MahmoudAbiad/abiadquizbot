"""واجهة «إعدادات إضافية»: callback_data، التحقق، حفظ الإعدادات بالجلسة، وتمريرها لمولّد الملف."""
import ast
import asyncio
import hashlib
import json
import re
import types
import unittest
from dataclasses import replace
from html import escape
from pathlib import Path
from typing import Optional
from unittest.mock import AsyncMock, Mock

from aiogram import types as tg
import constants as C
from reliability_support import load_functions
from services import export_options as eo
from services.export_options import ExportOptions, ExportOptionsError

HANDLER_FUNCS = {
    '_quiz_scope', '_sync_options', '_save_options', '_active_settings_note', '_leave_input_state',
    '_questions_for_settings', '_comfortable_per_page', '_per_page_hint', '_join_notes',
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
        asyncio=asyncio, escape=escape, replace=replace, re=re, List=list, Optional=Optional,
        hashlib=hashlib, json=json, MAX_PER_PAGE=eo.MAX_PER_PAGE, per_page_hint_ar=eo.per_page_hint_ar,
        logger=Mock(), log_error=Mock(),
        get_export_profile=AsyncMock(return_value=None), save_export_profile=AsyncMock(return_value=True),
        ExportState=types.SimpleNamespace(waiting_for_value=_St('ExportState:waiting_for_value')),
        ExportOptions=ExportOptions, ExportOptionsError=ExportOptionsError,
        FIELD_CODES=eo.FIELD_CODES, TEXT_FIELDS=eo.TEXT_FIELDS, PLACEMENTS=eo.PLACEMENTS,
        DRAFT_SIZES=eo.DRAFT_SIZES, DRAFT_STYLES=eo.DRAFT_STYLES,
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
        max_questions_per_page=lambda questions, style, options=None: 6, max_questions_per_page_docx=lambda questions, style, options=None: 4, get_favorite_quiz=AsyncMock(),
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

    async def test_columns_and_draft_actions_cycle_and_persist(self):
        env = handler_env()
        state = FakeState({'questions': Q})
        for data in ('xo|cl||s|', 'xo|dv||s|', 'xo|ds||s|', 'xo|ds||s|', 'xo|dt||s|'):
            await env['export_settings_action'](call(data), state)
        saved = ExportOptions.from_state_data(state.data)
        self.assertEqual((saved.columns, saved.column_divider), (2, False))
        self.assertEqual((saved.draft_size, saved.draft_style), ('medium', 'lines'))
        for data in ('xo|cl||s|', 'xo|ds||s|', 'xo|ds||s|'):
            await env['export_settings_action'](call(data), state)
        saved = ExportOptions.from_state_data(state.data)
        self.assertEqual((saved.columns, saved.draft_size), (1, 'none'))      # الدورة تعود إلى «بدون»

    async def test_per_page_screen_offers_full_range_and_validates_button_values(self):
        env = handler_env()
        state = FakeState({'questions': Q})
        await env['export_settings_action'](call('xo|pp||s|'), state)
        # كل القيم حتى 16 متاحة، و6 = الحد المريح لهذا الكويز (تُعلَّم فوقه ⚠️)
        env['get_export_per_page_keyboard'].assert_called_with(16, None, 's', '', 6)
        self.assertEqual(state.current, 'ExportState:waiting_for_value')
        await env['export_settings_action'](call('xo|ppv|4|s|'), state)
        self.assertEqual(ExportOptions.from_state_data(state.data).per_page, 4)
        self.assertIsNone(state.current)  # خرج من انتظار النص
        # فوق المريح (6) لكن ضمن 16: مقبول لأنه سقف يخفّضه المخطط
        await env['export_settings_action'](call('xo|ppv|12|s|'), state)
        self.assertEqual(ExportOptions.from_state_data(state.data).per_page, 12)
        # فوق 16 يُرفض برسالة (حتى لو وصل بزر قديم)
        await env['export_settings_action'](call('xo|ppv|17|s|'), state)
        self.assertEqual(ExportOptions.from_state_data(state.data).per_page, 12)
        self.assertIn('16', env['_safe_answer'].await_args.args[1])
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
        too_long = types.SimpleNamespace(text='x' * 80, answer=AsyncMock(), from_user=types.SimpleNamespace(id=1))
        await env['export_setting_value'](too_long, state)
        self.assertIn('60', too_long.answer.await_args.args[0])
        self.assertEqual(state.current, 'ExportState:waiting_for_value')  # يبقى ينتظر قيمة صحيحة
        ok = types.SimpleNamespace(text='  أحمد   العلي ', answer=AsyncMock(), from_user=types.SimpleNamespace(id=1))
        await env['export_setting_value'](ok, state)
        self.assertEqual(ExportOptions.from_state_data(state.data).teacher_name, 'أحمد العلي')
        self.assertEqual(state.current, 'previous')
        self.assertIsNone(state.data['export_input'])
        self.assertEqual(ok.answer.await_args.kwargs['reply_markup'][0], 'settings')

    async def test_typed_per_page_accepts_up_to_sixteen_and_rejects_above(self):
        env = handler_env()
        state = FakeState({'questions': Q})
        await env['export_settings_action'](call('xo|pp||s|'), state)
        bad = types.SimpleNamespace(text='17', answer=AsyncMock(), from_user=types.SimpleNamespace(id=1))
        await env['export_setting_value'](bad, state)
        self.assertIn('16', bad.answer.await_args.args[0])
        good = types.SimpleNamespace(text='١٢', answer=AsyncMock(), from_user=types.SimpleNamespace(id=1))
        await env['export_setting_value'](good, state)
        self.assertEqual(ExportOptions.from_state_data(state.data).per_page, 12)
        # 12 > المريح (6): يظهر تنبيه بدل الرفض
        self.assertIn('6', good.answer.await_args.args[0])
        self.assertIn('12', good.answer.await_args.args[0])

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
        env['get_export_per_page_keyboard'].assert_called_with(16, None, 's', 'FAV1', 6)

    async def test_text_in_wrong_state_is_not_stored(self):
        env = handler_env()
        state = FakeState({})
        msg = types.SimpleNamespace(text='hello', answer=AsyncMock())
        await env['export_setting_value'](msg, state)
        self.assertEqual(state.data.get('export_options'), None)


class QuizScopeAndProfileTests(unittest.IsolatedAsyncioTestCase):
    """النقطتان 1 و3: عزل إعدادات كل كويز + إعدادات المعلم الدائمة."""

    async def test_switching_quiz_clears_quiz_fields_but_keeps_teacher_settings(self):
        env = handler_env()
        state = FakeState({'questions': Q})
        for data in ('xo|ed|t|s|', 'xo|cl||s|'):
            await env['export_settings_action'](call(data), state)
        await state.update_data(export_options={'exam_title': 'Final A', 'subject': 'math', 'per_page': 4,
                                                'teacher_name': 'علي', 'columns': 2})
        other = [{'question': 'غيره', 'options': ['a', 'b'], 'correct_option_id': 1}]
        await state.update_data(questions=other)
        options, note = await env['_sync_options'](state, 1, '')
        self.assertEqual((options.exam_title, options.subject, options.per_page), ('', '', None))
        self.assertEqual((options.teacher_name, options.columns), ('علي', 2))
        self.assertIn('مختلف', note)
        # الحالة المحفوظة بالجلسة تغيّرت أيضاً (لا يتسرّب العنوان عند التصدير المباشر)
        self.assertEqual(ExportOptions.from_state_data(state.data).exam_title, '')

    async def test_same_quiz_keeps_everything_and_favorite_scope_is_separate(self):
        env = handler_env()
        state = FakeState({'questions': Q})
        await env['_sync_options'](state, 1, '')
        await state.update_data(export_options={'exam_title': 'T', 'teacher_name': 'x'})
        options, note = await env['_sync_options'](state, 1, '')
        self.assertEqual((options.exam_title, note), ('T', ''))
        options, note = await env['_sync_options'](state, 1, 'FAV1')   # كويز مفضلة = نطاق مختلف
        self.assertEqual((options.exam_title, options.teacher_name), ('', 'x'))

    async def test_first_use_loads_saved_profile_once(self):
        saved = {'teacher_name': 'سامر', 'columns': 2, 'exam_title': 'must be ignored', 'draft_size': 'bogus'}
        env = handler_env(get_export_profile=AsyncMock(return_value=saved))
        state = FakeState({'questions': Q})
        options, note = await env['_sync_options'](state, 9, '')
        env['get_export_profile'].assert_awaited_with(9)
        self.assertEqual((options.teacher_name, options.columns), ('سامر', 2))
        self.assertEqual(options.exam_title, '')            # حقول الكويز لا تُحمَّل من الملف المحفوظ
        self.assertEqual(options.draft_size, 'none')        # قيمة فاسدة تُهمل وحدها
        self.assertIn('المحفوظة', note)
        await env['_sync_options'](state, 9, '')
        self.assertEqual(env['get_export_profile'].await_count, 1)   # مرة واحدة بالجلسة

    async def test_profile_not_reloaded_over_explicit_reset_or_existing_session(self):
        env = handler_env(get_export_profile=AsyncMock(return_value={'teacher_name': 'سامر'}))
        state = FakeState({'questions': Q})
        await env['_sync_options'](state, 1, '')
        await env['export_settings_action'](call('xo|rs||s|'), state)
        options, _ = await env['_sync_options'](state, 1, '')
        self.assertTrue(options.is_default)
        legacy = FakeState({'questions': Q, 'export_options': {'subject': 'x'}})   # جلسة قبل هذا التحديث
        options, _ = await env['_sync_options'](legacy, 1, '')
        self.assertEqual(options.subject, 'x')
        env['get_export_profile'].assert_awaited_once()

    async def test_save_profile_stores_only_teacher_settings(self):
        env = handler_env()
        state = FakeState({'questions': Q})
        await env['_sync_options'](state, 5, '')
        await state.update_data(export_options={'exam_title': 'T', 'per_page': 3, 'teacher_name': 'علي',
                                                'grade': 'التاسع', 'columns': 2})
        await env['export_settings_action'](call('xo|sv||s|', user=5), state)
        env['save_export_profile'].assert_awaited_with(5, {'teacher_name': 'علي', 'grade': 'التاسع', 'columns': 2})
        self.assertIn('حُفظت', env['_edit_export_screen'].await_args.args[1])

    async def test_save_profile_failure_alerts_and_empty_profile_clears(self):
        env = handler_env(save_export_profile=AsyncMock(return_value=False))
        state = FakeState({'questions': Q, 'export_options': {'teacher_name': 'a'}})
        await env['export_settings_action'](call('xo|sv||s|'), state)
        self.assertTrue(env['_safe_answer'].await_args.kwargs.get('show_alert'))
        env = handler_env()
        await env['export_settings_action'](call('xo|sv||s|', user=2), FakeState({'questions': Q}))
        env['save_export_profile'].assert_awaited_with(2, {})
        self.assertIn('أُزيلت', env['_edit_export_screen'].await_args.args[1])

    async def test_per_page_hint_is_shown_cached_and_never_breaks_the_screen(self):
        calls = []
        env = handler_env(max_questions_per_page=lambda q, st, o=None: calls.append(1) or 5)
        state = FakeState({'questions': Q, 'export_options': {'per_page': 12}})
        await env['export_settings_action'](call('xo|open||s|'), state)
        text = env['_edit_export_screen'].await_args.args[1]
        self.assertIn('12', text)
        self.assertIn('5', text)
        await env['export_settings_action'](call('xo|open||s|'), state)
        self.assertEqual(len(calls), 1)                        # القياس الثقيل مخزَّن بالجلسة
        broken = handler_env(max_questions_per_page=lambda q, st, o=None: 1 / 0)
        await broken['export_settings_action'](call('xo|open||s|'), FakeState({'questions': Q, 'export_options': {'per_page': 3}}))
        broken['_edit_export_screen'].assert_awaited()          # الفشل لا يسقط الشاشة
        quiet = handler_env(max_questions_per_page=lambda q, st, o=None: 8)
        await quiet['export_settings_action'](call('xo|open||s|'), FakeState({'questions': Q, 'export_options': {'per_page': 8}}))
        self.assertNotIn('⚠️', quiet['_edit_export_screen'].await_args.args[1])


class KeyboardTests(unittest.TestCase):
    def env(self):
        return load_functions('keyboards.py', {
            '_xo', 'get_export_settings_keyboard', 'get_export_per_page_keyboard',
            'get_export_placement_keyboard', 'get_export_input_keyboard', 'get_export_format_keyboard'},
            **{**vars(C), 'types': tg, 'logger': Mock(), 'get_setting': AsyncMock(return_value=2.5),
               'STYLE_CODE_TO_NAME': {'s': 'simple', 'm': 'modern', 'a': 'academic'},
               'STYLE_LABELS_AR': {'simple': 'بسيط', 'modern': 'عصري', 'academic': 'أكاديمي'},
               'ANSWERS_MODE_LABELS_AR': eo.ANSWERS_MODE_LABELS_AR, 'FIELD_CODES': eo.FIELD_CODES,
               'COLUMN_LABELS_AR': eo.COLUMN_LABELS_AR, 'DRAFT_SIZE_LABELS_AR': eo.DRAFT_SIZE_LABELS_AR,
               'DRAFT_STYLE_LABELS_AR': eo.DRAFT_STYLE_LABELS_AR,
               'MAX_PER_PAGE': eo.MAX_PER_PAGE, 'PLACEMENT_LABELS_AR': eo.PLACEMENT_LABELS_AR,
               'PLACEMENTS': eo.PLACEMENTS, 'TEXT_FIELDS': eo.TEXT_FIELDS})

    def all_callbacks(self, kb):
        return [b.callback_data for row in kb.inline_keyboard for b in row]

    def test_every_callback_fits_telegram_limit_and_matches_handler_regex(self):
        env = self.env()
        regex = re.compile(r"^xo\|([a-z]+)\|([A-Za-z0-9_-]*)\|([sma])\|(.*)$")
        fav = '12345678-1234-1234-1234-123456789012'
        full = ExportOptions(per_page=3, subject='a', teacher_name='t', total_marks='5', columns=2,
                             draft_size='small', draft_style='grid').to_dict()
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

    def test_columns_and_draft_buttons_appear_with_their_sub_options(self):
        env = self.env()
        plain = self.all_callbacks(env['get_export_settings_keyboard']({}, 's', ''))
        self.assertIn('xo|cl||s|', plain)
        self.assertIn('xo|ds||s|', plain)
        self.assertNotIn('xo|dv||s|', plain)      # خط الفصل فقط مع عمودين
        self.assertNotIn('xo|dt||s|', plain)      # الشكل فقط مع مسودة
        both = self.all_callbacks(env['get_export_settings_keyboard'](
            ExportOptions(columns=2, draft_size='large').to_dict(), 's', ''))
        self.assertIn('xo|dv||s|', both)
        self.assertIn('xo|dt||s|', both)

    def test_format_keyboard_has_extra_settings_button_before_back(self):
        env = self.env()
        data = self.all_callbacks(env['get_export_format_keyboard']('s', ''))
        self.assertEqual(data[-2], 'xo|open||s|')
        self.assertEqual(data[-1], 'export_back')
        texts = [b.text for row in env['get_export_format_keyboard']('s', '').inline_keyboard for b in row]
        self.assertIn('⚙️ إعدادات إضافية', texts)

    def test_settings_keyboard_has_save_profile_button(self):
        env = self.env()
        data = self.all_callbacks(env['get_export_settings_keyboard']({}, 's', 'FAV'))
        self.assertIn('xo|sv||s|FAV', data)
        self.assertEqual(data[-1], 'xo|done||s|FAV')

    def test_per_page_keyboard_marks_numbers_above_comfortable(self):
        env = self.env()
        kb = env['get_export_per_page_keyboard'](16, 3, 's', '', 6)
        labels = {b.callback_data.split('|')[2]: b.text for row in kb.inline_keyboard for b in row
                  if b.callback_data.startswith('xo|ppv|') and b.callback_data.split('|')[2] != 'auto'}
        self.assertEqual(len(labels), 16)
        self.assertTrue(labels['3'].startswith('✅'))
        self.assertFalse(labels['6'].startswith('⚠️'))
        self.assertTrue(all(labels[str(n)].startswith('⚠️') for n in range(7, 17)))

    def test_per_page_keyboard_offers_only_valid_numbers(self):
        env = self.env()
        kb = env['get_export_per_page_keyboard'](4, 2, 's', '')
        values = [d.split('|')[2] for d in self.all_callbacks(kb) if d.startswith('xo|ppv|')]
        self.assertEqual(values, ['1', '2', '3', '4', 'auto'])
        self.assertEqual(len(env['get_export_per_page_keyboard'](99, None, 's', '').inline_keyboard[0]), 5)
        self.assertEqual([d.split('|')[2] for d in self.all_callbacks(
            env['get_export_per_page_keyboard'](99, None, 's', '')) if d.startswith('xo|ppv|')][-2], '16')


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
