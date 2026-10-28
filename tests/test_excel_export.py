"""Check the actual XLSX archive and reopened cell values without app services."""
import ast
import io
import json
import re
import unittest
import zipfile
from pathlib import Path

from openpyxl import load_workbook
from reliability_support import load_functions


def exporter():
    path = 'services/export_service.py'
    tree = ast.parse(Path(path).read_text(encoding='utf-8'))
    env = dict(io=io, re=re)
    # Load production constants and pure helpers without image/Telegram imports.
    for node in tree.body:
        if isinstance(node, ast.Assign) and all(isinstance(t, ast.Name) for t in node.targets):
            name = node.targets[0].id
            if name.startswith('STYLE_') or name in {
                'DEFAULT_STYLE', 'VALID_STYLES', '_ARABIC_RE', '_LETTER_RE', '_PARENS_RE'
            }:
                exec(compile(ast.Module(body=[node], type_ignores=[]), path, 'exec'), env)
    return load_functions(path, {
        'build_quiz_xlsx', 'ExportError', 'normalize_style', 'detect_language', '_strip_parens'
    }, **env)


class ExcelExportTests(unittest.TestCase):
    def setUp(self):
        self.env = exporter()
        self.build = self.env['build_quiz_xlsx']
        self.questions = [
            {'question': 'ما ناتج جمع العددين؟', 'options': ['واحد', 'اثنان', 'ثلاثة'],
             'correct_option_id': 1, 'explanation': 'شرح سري للأستاذ فقط'},
            {'question': 'اختر الإجابة التالية', 'options': ['نعم', 'لا'],
             'correct_option_id': '0', 'points': 2},
        ]

    def test_teacher_arabic_and_variable_options(self):
        wb = load_workbook(io.BytesIO(self.build('اختبار', self.questions)))
        self.assertEqual(wb.sheetnames, ['الأسئلة', 'معلومات الاختبار'])
        sheet = wb.worksheets[0]
        self.assertTrue(sheet.sheet_view.rightToLeft)
        self.assertEqual(sheet.freeze_panes, 'A2')
        self.assertEqual(sheet.auto_filter.ref, 'A1:J3')
        self.assertEqual(sheet['H2'].value, 2)  # zero-based source -> one-based Excel
        self.assertEqual(sheet['I2'].value, 'اثنان')
        self.assertEqual(sheet['J2'].value, 'شرح سري للأستاذ فقط')
        self.assertIsNone(sheet['F3'].value)  # padding for a shorter options list
        self.assertEqual(sheet['G3'].value, 2)
        self.assertEqual(wb.worksheets[1]['B2'].value, 'اختبار')

    def test_student_archive_contains_no_answer_key_or_explanation(self):
        data = self.build('اختبار', self.questions, include_answers=False)
        wb = load_workbook(io.BytesIO(data))
        self.assertEqual(wb.worksheets[0].max_column, 7)
        self.assertEqual(len(wb.sheetnames), 2)
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            xml = ''.join(archive.read(n).decode() for n in archive.namelist() if n.endswith('.xml'))
        self.assertNotIn('شرح سري للأستاذ فقط', xml)
        self.assertNotIn('الإجابة الصحيحة', xml)
        self.assertNotIn('state="hidden"', xml)

    def test_formula_and_link_text_remain_literal(self):
        q = {'question': '=HYPERLINK("https://example.com","x")',
             'options': ['+1', '-2', '@SUM(1,2)', 'https://example.com'],
             'correct_option_id': 0, 'explanation': '=1+1'}
        wb = load_workbook(io.BytesIO(self.build('=1+1', [q])))
        for sheet in wb:
            for row in sheet:
                for cell in row:
                    self.assertNotEqual(cell.data_type, 'f')
                    self.assertIsNone(cell.hyperlink)
        self.assertEqual(wb.worksheets[0]['B2'].value, q['question'])
        self.assertEqual(wb.worksheets[1]['B2'].value, '=1+1')

    def test_math_tables_and_matrices_preserve_source(self):
        q = self.questions[0] | {
            'question': r'احسب $\frac{1}{2}$',
            'table': {'headers': ['س'], 'rows': [['1']]},
            'matrices': [{'rows': [['1', '2']]}],
        }
        wb = load_workbook(io.BytesIO(self.build('رياضيات', [q])))
        sheet = wb.worksheets[0]
        self.assertEqual(sheet['B2'].value, q['question'])
        self.assertEqual(json.loads(sheet['H2'].value), q['table'])
        self.assertEqual(json.loads(sheet['I2'].value), q['matrices'])

    def test_english_and_invalid_answer_index(self):
        for index in (None, -1, 100, 'invalid'):
            q = {'question': 'Select the answer', 'options': ['yes', 'no'], 'correct_option_id': index}
            sheet = load_workbook(io.BytesIO(self.build('Quiz', [q]))).worksheets[0]
            self.assertEqual(sheet.title, 'Questions')
            self.assertFalse(sheet.sheet_view.rightToLeft)
            self.assertIsNone(sheet['G2'].value)
            self.assertIsNone(sheet['H2'].value)

    def test_empty_and_invalid_or_oversized_text_are_explicit_errors(self):
        with self.assertRaises(self.env['ExportError']):
            self.build('Quiz', [])
        for text in ('x' * 32768, 'bad\x00text'):
            with self.assertRaises(self.env['ExportError']):
                self.build('Quiz', [{'question': text, 'options': ['a', 'b']}])


if __name__ == '__main__':
    unittest.main()
