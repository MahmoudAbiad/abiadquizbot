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
    '_show_settings', '_settings_view', 'export_settings_action', '_start_input', 'export_setting_value',
    '_show_choice',
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
        _EXPORT_SECTIONS=('h', 'l', 'a'), LAYOUT_PRESETS=eo.LAYOUT_PRESETS, parse_bulk_fields=eo.parse_bulk_fields,
        export_section_text=lambda section, notice='': f'section {section}|{notice}',
        get_export_section_keyboard=Mock(side_effect=lambda sec, o, s, f: ('section', sec, o, s, f)),
        export_presets_text=lambda presets: 'presets text', get_export_presets_keyboard=Mock(return_value='presets_kb'),
        export_bulk_prompt_text=lambda: 'bulk prompt',
        get_export_per_page_keyboard=Mock(return_value='per_page_kb'),
        get_export_choice_keyboard=Mock(side_effect=lambda kind, o, s, f: ('choice', kind, o, s, f)),
        export_choice_text=lambda kind: f'choice text {kind}',
        _PAGE_NUMBERS_ARGS={'def': None, 'on': True, 'off': False},
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

    async def test_choice_screens_open_and_apply_values_directly(self):
        env = handler_env()
        state = FakeState({'questions': Q})
        for action, kind in (('pnm', 'pn'), ('clm', 'cl'), ('dsm', 'ds'), ('dtm', 'dt')):
            env['_edit_export_screen'].reset_mock()
            await env['export_settings_action'](call(f'xo|{action}||s|'), state)
            self.assertEqual(env['_edit_export_screen'].await_args.args[1], f'choice text {kind}')
            self.assertEqual(env['_edit_export_screen'].await_args.args[2][:2], ('choice', kind))
        for data in ('xo|pnv|off|s|', 'xo|clv|2|s|', 'xo|dvv|off|s|', 'xo|dsv|large|s|', 'xo|dtv|grid|s|'):
            await env['export_settings_action'](call(data), state)
        saved = ExportOptions.from_state_data(state.data)
        self.assertIs(saved.page_numbers, False)
        self.assertEqual((saved.columns, saved.column_divider), (2, False))
        self.assertEqual((saved.draft_size, saved.draft_style), ('large', 'grid'))
        await env['export_settings_action'](call('xo|pnv|def|s|'), state)
        self.assertIsNone(ExportOptions.from_state_data(state.data).page_numbers)

    async def test_columns_screen_stays_open_but_other_choices_return_to_section(self):
        env = handler_env()
        state = FakeState({'questions': Q, 'export_sec': 'l'})
        env['_edit_export_screen'].reset_mock()
        await env['export_settings_action'](call('xo|clv|2|s|'), state)
        self.assertEqual(env['_edit_export_screen'].await_args.args[1], 'choice text cl')
        env['_edit_export_screen'].reset_mock()
        await env['export_settings_action'](call('xo|dsv|small|s|'), state)
        self.assertTrue(str(env['_edit_export_screen'].await_args.args[1]).startswith('section l'))

    async def test_choice_actions_ignore_invalid_values(self):
        env = handler_env()
        state = FakeState({'questions': Q})
        for data in ('xo|pnv|zzz|s|', 'xo|clv|7|s|', 'xo|dvv|x|s|', 'xo|dsv|huge|s|', 'xo|dtv|nope|s|'):
            await env['export_settings_action'](call(data), state)
        saved = ExportOptions.from_state_data(state.data)
        self.assertEqual((saved.page_numbers, saved.columns, saved.draft_size, saved.draft_style),
                         (None, 1, 'none', 'auto'))

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
            '_xo', '_short_value', '_format_rows', 'get_export_choice_keyboard', 'get_export_settings_keyboard', 'get_export_section_keyboard',
            'get_export_presets_keyboard', 'get_export_per_page_keyboard',
            'get_export_placement_keyboard', 'get_export_input_keyboard', 'get_export_format_keyboard'},
            **{**vars(C), 'types': tg, 'logger': Mock(), 'get_setting': AsyncMock(return_value=2.5),
               'STYLE_CODE_TO_NAME': {'s': 'simple', 'm': 'modern', 'a': 'academic'},
               'STYLE_LABELS_AR': {'simple': 'بسيط', 'modern': 'عصري', 'academic': 'أكاديمي'},
               'ANSWERS_MODE_LABELS_AR': eo.ANSWERS_MODE_LABELS_AR, 'FIELD_CODES': eo.FIELD_CODES,
               'COLUMN_LABELS_AR': eo.COLUMN_LABELS_AR, 'DRAFT_SIZE_LABELS_AR': eo.DRAFT_SIZE_LABELS_AR,
               'DRAFT_STYLE_LABELS_AR': eo.DRAFT_STYLE_LABELS_AR,
               'MAX_PER_PAGE': eo.MAX_PER_PAGE, 'PLACEMENT_LABELS_AR': eo.PLACEMENT_LABELS_AR,
               'PLACEMENTS': eo.PLACEMENTS, 'TEXT_FIELDS': eo.TEXT_FIELDS, 'LAYOUT_PRESETS': eo.LAYOUT_PRESETS})

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
                env['get_export_presets_keyboard']('a', favorite),
                *[env['get_export_section_keyboard'](sec, opts, 'a', favorite)
                  for sec in ('h', 'l', 'a') for opts in ({}, full)],
                env['get_export_per_page_keyboard'](10, 3, 'a', favorite),
                env['get_export_placement_keyboard']('footer', 'a', favorite),
                env['get_export_input_keyboard']('n', True, 'a', favorite),
                env['get_export_format_keyboard']('a', favorite),
                *[env['get_export_choice_keyboard'](kind, opts, 'a', favorite)
                  for kind in ('pn', 'cl', 'ds', 'dt') for opts in ({}, full)],
            ]
            for kb in boards:
                for data in self.all_callbacks(kb):
                    self.assertLessEqual(len(data.encode()), 64, data)
                    if data.startswith('xo|'):
                        self.assertEqual(regex.match(data).group(4), favorite, data)

    def test_columns_and_draft_buttons_open_choice_screens_with_their_sub_options(self):
        env = self.env()
        plain = self.all_callbacks(env['get_export_section_keyboard']('l', {}, 's', ''))
        for action in ('xo|pnm||s|', 'xo|clm||s|', 'xo|dsm||s|'):
            self.assertIn(action, plain)
        self.assertNotIn('xo|dtm||s|', plain)     # الشكل فقط مع مسودة
        # الأزرار الدوّارة القديمة لم تعد تُعرض (لكن معالجها باقٍ للرسائل القديمة)
        for old in ('xo|pn||s|', 'xo|cl||s|', 'xo|ds||s|', 'xo|dt||s|', 'xo|dv||s|'):
            self.assertNotIn(old, plain)
        both = self.all_callbacks(env['get_export_section_keyboard'](
            'l', ExportOptions(columns=2, draft_size='large').to_dict(), 's', ''))
        self.assertIn('xo|dtm||s|', both)

    def test_choice_keyboards_show_every_value_and_mark_current(self):
        env = self.env()
        def view(kind, opts):
            kb = env['get_export_choice_keyboard'](kind, opts, 's', '')
            return [(b.callback_data, b.text) for row in kb.inline_keyboard for b in row]
        pn = view('pn', {})
        self.assertEqual([d for d, _ in pn[:3]], ['xo|pnv|def|s|', 'xo|pnv|on|s|', 'xo|pnv|off|s|'])
        self.assertTrue(pn[0][1].startswith('✅'))
        self.assertTrue(view('pn', {'page_numbers': False})[2][1].startswith('✅'))
        cl = view('cl', {})
        self.assertEqual([d for d, _ in cl[:2]], ['xo|clv|1|s|', 'xo|clv|2|s|'])
        self.assertNotIn('xo|dvv|on|s|', [d for d, _ in cl])               # الفاصل مع عمودين فقط
        cl2 = view('cl', {'columns': 2, 'column_divider': False})
        self.assertIn('xo|dvv|on|s|', [d for d, _ in cl2])
        self.assertTrue(dict(cl2)['xo|dvv|off|s|'].startswith('✅'))
        self.assertEqual(len(view('ds', {})), 5)                            # 4 قيم + رجوع
        self.assertTrue(dict(view('ds', {'draft_size': 'large'}))['xo|dsv|large|s|'].startswith('✅'))
        self.assertTrue(dict(view('dt', {'draft_style': 'grid'}))['xo|dtv|grid|s|'].startswith('✅'))
        for kind in ('pn', 'cl', 'ds', 'dt'):
            self.assertEqual(view(kind, {})[-1][0], 'xo|back||s|')

    def test_settings_screen_has_six_export_buttons_before_done(self):
        env = self.env()
        fav = '12345678-1234-1234-1234-123456789012'
        for favorite in ('', fav):
            data = self.all_callbacks(env['get_export_settings_keyboard']({}, 's', favorite))
            fmt = self.all_callbacks(env['get_export_format_keyboard']('s', favorite))[:6]
            self.assertEqual(len(fmt), 6)
            self.assertEqual([d for d in data if d.startswith(('export_', 'fav_export_'))], fmt)   # نفس callback ونفس الترتيب
            self.assertEqual(data[-1], f'xo|done||s|{favorite}')

    def test_sections_warn_which_formats_and_copies_each_setting_applies_to(self):
        env = self.env()
        texts = lambda sec: [b.text for row in env['get_export_section_keyboard'](sec, {}, 's', '').inline_keyboard for b in row]
        self.assertTrue(any('PDF/Word' in t and 'عدد الأسئلة' in t for t in texts('l')))
        self.assertTrue(any('الأستاذ' in t and 'الإجابة' in t for t in texts('a')))

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


class SectionsPresetsAndBulkFlowTests(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def typed(text):
        return types.SimpleNamespace(text=text, answer=AsyncMock(), from_user=types.SimpleNamespace(id=1))

    async def test_sections_open_stay_after_changes_and_reset_on_open_or_done(self):
        env = handler_env()
        state = FakeState({'questions': Q})
        await env['export_settings_action'](call('xo|sec|a|s|'), state)
        self.assertEqual(state.data['export_sec'], 'a')
        self.assertEqual(env['_edit_export_screen'].await_args.args[2][:2], ('section', 'a'))
        await env['export_settings_action'](call('xo|sf||s|'), state)   # تعديل داخل القسم يبقي المستخدم فيه
        self.assertEqual(env['_edit_export_screen'].await_args.args[2][:2], ('section', 'a'))
        self.assertFalse(ExportOptions.from_state_data(state.data).student_fields)
        await env['export_settings_action'](call('xo|back||s|'), state)
        self.assertEqual(env['_edit_export_screen'].await_args.args[2][:2], ('section', 'a'))
        await env['export_settings_action'](call('xo|open||s|'), state)
        self.assertEqual(state.data['export_sec'], '')
        self.assertEqual(env['_edit_export_screen'].await_args.args[2][0], 'settings')
        await env['export_settings_action'](call('xo|sec|l|s|'), state)
        await env['export_settings_action'](call('xo|done||s|'), state)
        self.assertEqual(state.data['export_sec'], '')

    async def test_unknown_section_is_ignored(self):
        env = handler_env()
        state = FakeState({'questions': Q})
        await env['export_settings_action'](call('xo|sec|z|s|'), state)
        self.assertNotIn('export_sec', state.data)
        env['_edit_export_screen'].assert_not_awaited()

    async def test_per_page_hint_only_on_main_and_layout_screens(self):
        env = handler_env(max_questions_per_page=lambda q, st, o=None: 2)
        state = FakeState({'questions': Q, 'export_options': {'per_page': 9}})
        await env['export_settings_action'](call('xo|sec|h|s|'), state)
        self.assertNotIn('⚠️', env['_edit_export_screen'].await_args.args[1])
        await env['export_settings_action'](call('xo|sec|l|s|'), state)
        self.assertIn('⚠️', env['_edit_export_screen'].await_args.args[1])

    async def test_preset_changes_only_layout_fields(self):
        env = handler_env()
        before = ExportOptions(subject='math', teacher_name='Ali', student_fields=False, answers_mode='key',
                               per_page=5, columns=2, draft_size='large')
        state = FakeState({'questions': Q, 'export_options': before.to_dict(), 'export_sec': 'l'})
        await env['export_settings_action'](call('xo|prv|exam|s|'), state)
        after = ExportOptions.from_state_data(state.data)
        self.assertEqual((after.columns, after.draft_size, after.page_numbers), (1, 'medium', True))
        self.assertEqual((after.subject, after.teacher_name, after.student_fields, after.answers_mode, after.per_page),
                         ('math', 'Ali', False, 'key', 5))
        self.assertEqual(state.data['export_sec'], '')                 # يعود للرئيسية
        self.assertIn('ورقة امتحان', env['_edit_export_screen'].await_args.args[1])
        await env['export_settings_action'](call('xo|prv|plain|s|'), state)
        plain = ExportOptions.from_state_data(state.data)
        self.assertEqual((plain.columns, plain.draft_size, plain.page_numbers, plain.per_page), (1, 'none', None, None))
        self.assertEqual((plain.subject, plain.teacher_name), ('math', 'Ali'))

    async def test_presets_screen_and_unknown_preset(self):
        env = handler_env()
        state = FakeState({'questions': Q})
        await env['export_settings_action'](call('xo|pre||s|'), state)
        self.assertEqual(env['_edit_export_screen'].await_args.args[1:], ('presets text', 'presets_kb'))
        await env['export_settings_action'](call('xo|prv|nope|s|'), state)
        self.assertIn('قالب', env['_safe_answer'].await_args.args[1])
        self.assertEqual(ExportOptions.from_state_data(state.data), ExportOptions())

    async def test_bulk_fill_sets_many_fields_and_returns_to_sheet_section(self):
        env = handler_env()
        state = FakeState({'questions': Q, 'export_sec': 'h'}, state='previous')
        await env['export_settings_action'](call('xo|bulk||s|'), state)
        self.assertEqual(state.current, 'ExportState:waiting_for_value')
        self.assertEqual(state.data['export_input']['field'], 'bulk')
        self.assertEqual(env['_edit_export_screen'].await_args.args[1], 'bulk prompt')
        garbage = self.typed('مرحبا بدون حقول')
        await env['export_setting_value'](garbage, state)
        self.assertEqual(state.current, 'ExportState:waiting_for_value')   # يبقى ينتظر
        self.assertIn('الاسم: القيمة', garbage.answer.await_args.args[0])
        msg = self.typed('المادة: رياضيات\nالصف: التاسع\nالعلامة: abc\nالأستاذ: أحمد')
        await env['export_setting_value'](msg, state)
        saved = ExportOptions.from_state_data(state.data)
        self.assertEqual((saved.subject, saved.grade, saved.teacher_name, saved.total_marks),
                         ('رياضيات', 'التاسع', 'أحمد', ''))               # الحقل الخاطئ لا يُسقط الباقي
        self.assertEqual(state.current, 'previous')
        text = msg.answer.await_args.args[0]
        self.assertIn('3', text)
        self.assertIn('⚠️', text)
        self.assertEqual(msg.answer.await_args.kwargs['reply_markup'][:2], ('section', 'h'))


class BulkParserTests(unittest.TestCase):
    def test_line_per_field_and_synonyms(self):
        values, problems = eo.parse_bulk_fields(
            'المادة: رياضيات\nالصف الدراسي: التاسع\nتاريخ الاختبار: 2026/11/07\nالزمن: 45 دقيقة\n'
            'الدرجة الكلية: ١٠٠\nعنوان الاختبار: الوحدة الثانية\nالمعلمة: سارة')
        self.assertEqual(values, {'subject': 'رياضيات', 'grade': 'التاسع', 'exam_date': '2026/11/07',
                                  'duration': '45 دقيقة', 'total_marks': '100',
                                  'exam_title': 'الوحدة الثانية', 'teacher_name': 'سارة'})
        self.assertEqual(problems, [])

    def test_many_fields_on_one_line_keep_commas_inside_values(self):
        values, _ = eo.parse_bulk_fields('المادة: رياضيات، الصف: التاسع، التاريخ: 7 نوفمبر، 2026')
        self.assertEqual(values, {'subject': 'رياضيات', 'grade': 'التاسع', 'exam_date': '7 نوفمبر، 2026'})

    def test_english_labels_bullets_and_last_value_wins(self):
        values, _ = eo.parse_bulk_fields('- Subject: Math\n* Teacher = Ali\nمادة: فيزياء')
        self.assertEqual(values, {'subject': 'فيزياء', 'teacher_name': 'Ali'})

    def test_invalid_fields_are_reported_but_do_not_drop_the_rest(self):
        values, problems = eo.parse_bulk_fields('العلامة: abc\nالمادة: فيزياء\n' + 'المدة: ' + 'x' * 40)
        self.assertEqual(values, {'subject': 'فيزياء'})
        self.assertEqual(len(problems), 2)

    def test_empty_values_are_skipped_and_unknown_text_is_reported(self):
        values, problems = eo.parse_bulk_fields('ملاحظة عشوائية\nالمادة: كيمياء\nالصف:\nسطر زائد')
        self.assertEqual(values, {'subject': 'كيمياء'})
        self.assertTrue(problems)

    def test_nothing_understood_raises_with_help(self):
        for text in ('', 'مرحبا', 'المادة:', None):
            with self.assertRaises(ExportOptionsError) as caught:
                eo.parse_bulk_fields(text)
            self.assertIn('الاسم: القيمة', str(caught.exception))


class LayoutPresetTests(unittest.TestCase):
    def test_every_preset_is_valid_and_touches_only_layout_fields(self):
        layout = {'per_page', 'columns', 'column_divider', 'draft_size', 'draft_style', 'page_numbers'}
        for name, preset in eo.LAYOUT_PRESETS.items():
            self.assertLessEqual(set(preset['values']), layout, name)
            applied = ExportOptions().with_preset(name)
            ExportOptions.from_dict(applied.to_dict())               # قابل للتحقق الصارم
            self.assertTrue(preset['label'] and preset['desc'] and preset['icon'], name)
        with self.assertRaises(ExportOptionsError):
            ExportOptions().with_preset('missing')

    def test_exam_and_save_presets_differ_from_default(self):
        self.assertEqual(ExportOptions().with_preset('plain'), ExportOptions())
        self.assertEqual(ExportOptions().with_preset('save').columns, 2)
        self.assertEqual(ExportOptions().with_preset('practice').draft_size, 'large')


class SectionKeyboardTests(unittest.TestCase):
    env = KeyboardTests.env
    all_callbacks = KeyboardTests.all_callbacks

    def test_sheet_section_shows_values_bulk_button_and_placement(self):
        env = self.env()
        opts = ExportOptions(subject='رياضيات', teacher_name='أحمد').to_dict()
        kb = env['get_export_section_keyboard']('h', opts, 's', '')
        texts = [b.text for row in kb.inline_keyboard for b in row]
        self.assertIn('✅ 📚 المادة: رياضيات', texts)
        self.assertIn('🎓 الصف', texts)                                # الفارغ بلا قيمة
        data = self.all_callbacks(kb)
        self.assertEqual(data[0], 'xo|bulk||s|')
        self.assertIn('xo|pl||s|', data)
        self.assertEqual(data[-1], 'xo|open||s|')
        self.assertNotIn('xo|pl||s|', self.all_callbacks(env['get_export_section_keyboard']('h', {}, 's', '')))

    def test_long_value_is_shortened_inside_the_button(self):
        env = self.env()
        kb = env['get_export_section_keyboard']('h', {'exam_title': 'ع' * 90}, 's', '')
        title = next(b.text for row in kb.inline_keyboard for b in row if b.callback_data.startswith('xo|ed|t|'))
        self.assertLess(len(title), 40)
        self.assertTrue(title.endswith('…'))

    def test_answers_section_and_main_counts(self):
        env = self.env()
        data = self.all_callbacks(env['get_export_section_keyboard']('a', {}, 's', ''))
        self.assertEqual(data, ['xo|sf||s|', 'xo|am||s|', 'xo|open||s|'])
        main = env['get_export_settings_keyboard'](
            ExportOptions(subject='a', teacher_name='b', columns=2, student_fields=False).to_dict(), 's', '')
        texts = [row[0].text for row in main.inline_keyboard[:3]]
        self.assertEqual(texts, ['📝 بيانات الورقة · 2', '📐 التخطيط والمظهر · 1', '🔑 الإجابات وسطر الطالب · 1'])
        plain = [row[0].text for row in env['get_export_settings_keyboard']({}, 's', '').inline_keyboard[:3]]
        self.assertTrue(all('·' not in t for t in plain))

    def test_layout_section_has_distinct_icons_and_back(self):
        env = self.env()
        kb = env['get_export_section_keyboard']('l', {}, 's', '')
        texts = [b.text for row in kb.inline_keyboard for b in row]
        self.assertEqual(sum(t.startswith('🔢') for t in texts), 1)    # 🔢 لم تعد مكررة
        self.assertEqual(self.all_callbacks(kb)[-1], 'xo|open||s|')

    def test_input_pickers_go_back_to_the_open_section(self):
        env = self.env()
        for kb in (env['get_export_per_page_keyboard'](10, 3, 's', ''), env['get_export_placement_keyboard']('first', 's', ''),
                   env['get_export_input_keyboard']('n', True, 's', '')):
            self.assertEqual(self.all_callbacks(kb)[-1], 'xo|back||s|')

    def test_presets_keyboard_lists_every_preset_then_back(self):
        env = self.env()
        data = self.all_callbacks(env['get_export_presets_keyboard']('m', 'FAV'))
        self.assertEqual(data, [f'xo|prv|{name}|m|FAV' for name in eo.LAYOUT_PRESETS] + ['xo|open||m|FAV'])

    def test_main_screen_still_has_save_reset_and_done_last(self):
        env = self.env()
        data = self.all_callbacks(env['get_export_settings_keyboard']({}, 's', 'FAV'))
        self.assertEqual(data[-1], 'xo|done||s|FAV')
        self.assertEqual(data[-9:-7], ['xo|sv||s|FAV', 'xo|rs||s|FAV'])      # ثم صفوف التصدير الستة ثم «رجوع»
        self.assertIn('xo|pre||s|FAV', data)


if __name__ == '__main__':
    unittest.main()
