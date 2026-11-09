import io
import unittest
import fitz
from docx import Document
from services.export_service import build_quiz_pdf, build_quiz_docx, ExportError
from services.latex_text import latex_to_plain


def sample():
    return {'is_math': True, 'question': r'أوجد ناتج المجموع التالي: $S_n=1+2+3+\ldots+n$',
            'options': [r'$n^2+n$', r'$n(n+1)$', r'$\frac{n(n+2)}{2}$', r'$\frac{n^2+n}{2}$'],
            'correct_option_id': 3, 'explanation': r'نطبق قانون مجموع المتتالية الحسابية: $\frac{n(n+1)}{2}$.'}


class PrintExportTests(unittest.TestCase):
    def test_student_and_teacher_pdf(self):
        questions = [sample() for _ in range(13)]
        student = build_quiz_pdf('اختبار الرياضيات', questions, include_answers=False)
        teacher = build_quiz_pdf('اختبار الرياضيات', questions, include_answers=True)
        a, b = fitz.open(stream=student, filetype='pdf'), fitz.open(stream=teacher, filetype='pdf')
        self.assertLessEqual(len(a), 4)
        self.assertGreater(len(b), len(a))
        for page in b:
            for block in page.get_text('dict')['blocks']:
                if block['type'] == 1:
                    self.assertGreaterEqual(block['bbox'][1], 0)
                    self.assertLess(block['bbox'][3], page.rect.height - 40)
        open('/tmp/print-student.pdf', 'wb').write(student)
        open('/tmp/print-teacher.pdf', 'wb').write(teacher)
        print('pages student/teacher:', len(a), len(b))

    def test_word_versions(self):
        a = Document(io.BytesIO(build_quiz_docx('اختبار', [sample()], include_answers=False)))
        b = Document(io.BytesIO(build_quiz_docx('اختبار', [sample()], include_answers=True)))
        self.assertEqual(len(a.inline_shapes), 1)
        self.assertEqual(len(b.inline_shapes), 2)

    def test_long_question_continues(self):
        q = sample()
        q['question'] = ('اشرح المجموع الحسابي التالي $x^2+1$\n' * 65)
        data = build_quiz_pdf('طويل', [q], include_answers=False)
        doc = fitz.open(stream=data, filetype='pdf')
        self.assertGreater(len(doc), 1)
        for page in doc:
            for block in page.get_text('dict')['blocks']:
                if block['type'] == 1:
                    self.assertLess(block['bbox'][3], page.rect.height - 40)

    def test_unsupported_math_fails_explicitly(self):
        q = sample(); q['options'][0] = r'$\unknowncommand{x}$'
        with self.assertRaises(ExportError):
            build_quiz_pdf('invalid', [q])

    def test_fraction_grouping(self):
        self.assertEqual(latex_to_plain(r'$\frac{n^2+n}{2}$'), '((n²+n)/(2))')

    def test_table_and_matrix(self):
        q = sample()
        q['table'] = {'headers': ['س', 'ص'], 'rows': [['1', '2'], ['3', '4']]}
        q['matrices'] = [{'label': 'A', 'rows': [['1', '2'], ['3', '4']], 'bracket': 'square'}]
        data = build_quiz_pdf('جداول', [q])
        self.assertGreater(len(data), 1000)

if __name__ == '__main__':
    unittest.main()
