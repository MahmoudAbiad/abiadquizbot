import io
import logging
import re
import unittest

from docx import Document
from openpyxl import load_workbook

from services.export_layout import pick_max_per_page, plan_pages, spread_gap
from services.export_options import (
    MAX_PER_PAGE, ExportOptions, ExportOptionsError, clean_text_field, exam_meta_line, parse_per_page,
    student_line, teacher_text,
)
from services.export_service import (
    ExportError, build_quiz_docx, build_quiz_pdf, build_quiz_xlsx, max_questions_per_page,
)


def quiz(n=12, long_at=(), lang="ar"):
    out = []
    for i in range(n):
        text = (f"ما ناتج العملية رقم {i + 1} في هذا السؤال التجريبي؟" if lang == "ar"
                else f"What is the result of operation {i + 1}?")
        if i + 1 in long_at:
            text += " نص طويل جداً لاختبار الالتفاف. " * 16
        opts = ["الأول", "الثاني", "الثالث", "الرابع"] if lang == "ar" else ["one", "two", "three", "four"]
        out.append({"question": text, "options": opts,
                    "correct_option_id": i % 4, "explanation": "شرح مختصر"})
    return out


def pdf_pages(data):
    import fitz
    return fitz.open(stream=data, filetype="pdf")


class OptionsValidationTests(unittest.TestCase):
    def test_defaults_are_legacy(self):
        self.assertTrue(ExportOptions().is_default)
        self.assertEqual(ExportOptions().to_dict(), {})
        self.assertTrue(ExportOptions.from_dict({}).is_default)
        self.assertTrue(ExportOptions.from_state_data({}).is_default)

    def test_per_page_bounds_and_messages(self):
        self.assertEqual(parse_per_page("1"), 1)
        self.assertEqual(parse_per_page("٤"), 4)          # أرقام عربية
        self.assertEqual(parse_per_page(MAX_PER_PAGE), MAX_PER_PAGE)
        for bad in ("0", "-3", "abc", "2.5", "", True):
            with self.assertRaises(ExportOptionsError, msg=repr(bad)):
                parse_per_page(bad)
        with self.assertRaises(ExportOptionsError) as ctx:
            parse_per_page("99")
        self.assertIn(str(MAX_PER_PAGE), str(ctx.exception))
        with self.assertRaises(ExportOptionsError) as ctx:
            parse_per_page("9", max_allowed=5)
        self.assertIn("5", str(ctx.exception))
        self.assertIn("طويلة", str(ctx.exception))

    def test_text_fields_sanitised_and_limited(self):
        self.assertEqual(clean_text_field("teacher_name", "  أحمد \n  العلي\u200f "), "أحمد العلي")
        self.assertEqual(clean_text_field("subject", ""), "")
        with self.assertRaises(ExportOptionsError) as ctx:
            clean_text_field("teacher_name", "x" * 61)
        self.assertIn("60", str(ctx.exception))
        self.assertEqual(clean_text_field("total_marks", "١٠٠"), "100")
        self.assertEqual(clean_text_field("total_marks", "50,50"), "50.5")
        for bad in ("abc", "0", "-5", "1e3"):
            with self.assertRaises(ExportOptionsError, msg=bad):
                clean_text_field("total_marks", bad)

    def test_from_dict_is_strict(self):
        for bad in ({"per_page": 0}, {"per_page": 11}, {"teacher_placement": "x"}, {"answers_mode": "x"},
                    {"student_fields": "no"}, {"unknown": 1}, "text", {"duration": 5}):
            with self.assertRaises(ExportOptionsError, msg=repr(bad)):
                ExportOptions.from_dict(bad)
        raw = {"per_page": 3, "teacher_name": "علي", "teacher_placement": "footer", "page_numbers": False}
        self.assertEqual(ExportOptions.from_dict(raw).to_dict(), raw)

    def test_corrupt_state_falls_back_to_defaults(self):
        self.assertTrue(ExportOptions.from_state_data({"export_options": {"per_page": 99}}).is_default)

    def test_legacy_student_line_text_unchanged(self):
        o = ExportOptions()
        self.assertEqual(student_line(o, True, False), "الاسم: ______________  الشعبة: ______  التاريخ: __________")
        self.assertEqual(student_line(o, False, False), "Name: ______________  Class: ______  Date: __________")
        self.assertEqual(student_line(o, True, True), "الاسم: ________________________     التاريخ: ______________")
        self.assertEqual(student_line(o, False, True), "Name: ________________________     Date: ______________")
        self.assertNotIn("التاريخ", student_line(ExportOptions(exam_date="2026/11/07"), True, False))
        self.assertEqual(student_line(ExportOptions(student_fields=False), True, False), "")

    def test_header_texts(self):
        o = ExportOptions(subject="الرياضيات", total_marks="100", teacher_name="علي")
        self.assertIn("المادة: الرياضيات", exam_meta_line(o, True))
        self.assertIn("Total marks: 100", exam_meta_line(o, False))
        self.assertEqual(teacher_text(o, False), "Prepared by: علي")
        self.assertEqual(exam_meta_line(ExportOptions(), True), "")


class LayoutPlanningTests(unittest.TestCase):
    def test_plan_respects_cap_and_capacity(self):
        self.assertEqual(plan_pages([100] * 5, 2, 500, 500), [[0, 1], [2, 3], [4]])
        # الصفحة لا تتسع: أقل من المطلوب بدل التزاحم
        self.assertEqual(plan_pages([300, 300, 100], 3, 500, 500), [[0], [1, 2]])
        self.assertEqual(plan_pages([100] * 4, 1, 500, 500), [[0], [1], [2], [3]])

    def test_oversized_question_gets_own_page(self):
        self.assertEqual(plan_pages([100, 900, 100], 3, 500, 500), [[0], [1], [2]])
        self.assertEqual(plan_pages([900], 3, 500, 500), [[0]])

    def test_first_page_has_smaller_capacity(self):
        self.assertEqual(plan_pages([200, 200], 2, 300, 600), [[0], [1]])

    def test_spread_and_max(self):
        self.assertEqual(spread_gap(-5, 3), 0)
        self.assertEqual(spread_gap(1000, 2), 42.0)
        self.assertAlmostEqual(spread_gap(30, 3), 10)
        self.assertEqual(pick_max_per_page([100] * 10, 700, 10), 7)
        self.assertEqual(pick_max_per_page([30] * 10, 700, 10), 10)
        self.assertEqual(pick_max_per_page([2000] * 3, 700, 10), 1)
        # سؤال شاذ واحد لا يخفّض الحد للكويز كله
        self.assertEqual(pick_max_per_page([100] * 9 + [5000], 700, 10), 7)


class PdfLayoutTests(unittest.TestCase):
    def setUp(self):
        logging.disable(logging.CRITICAL)
        self.addCleanup(logging.disable, logging.NOTSET)

    def test_never_more_than_requested_per_page_and_nothing_clipped(self):
        for style in ("simple", "modern", "academic"):
            for per_page in (1, 3, 5):
                data = build_quiz_pdf("اختبار", quiz(13, long_at=(4,)), style, False,
                                      ExportOptions(per_page=per_page))
                doc = pdf_pages(data)
                total = 0
                for page in doc:
                    found = len(re.findall(r"(?:السؤال|Question)\s*\d+|\b\d+\s*:", page.get_text()))
                    self.assertLessEqual(found, per_page + 1, (style, per_page))  # +1: سطر ترويسة قد يطابق النمط
                    for block in page.get_text("dict")["blocks"]:
                        if block["type"] == 1:
                            self.assertLess(block["bbox"][3], page.rect.height - 40)
                    total += 1
                self.assertGreaterEqual(total, 13 // per_page)

    def test_all_questions_present_with_long_content(self):
        qs = quiz(8, long_at=(2, 5))
        data = build_quiz_pdf("t", qs, "modern", False, ExportOptions(per_page=4))
        text = " ".join(page.get_text() for page in pdf_pages(data))
        for i in range(1, 9):
            self.assertIn(str(i), text)

    def test_default_options_equal_no_options(self):
        a = build_quiz_pdf("t", quiz(6), "simple", True)
        b = build_quiz_pdf("t", quiz(6), "simple", True, ExportOptions())
        self.assertEqual(len(pdf_pages(a)), len(pdf_pages(b)))
        self.assertEqual([p.get_text() for p in pdf_pages(a)], [p.get_text() for p in pdf_pages(b)])

    def test_sheet_fields_and_teacher_placements(self):
        base = dict(subject="MATH", grade="G9", exam_date="2026-11-07", duration="45min", total_marks="100",
                    teacher_name="TEACHERNAME")
        qs = quiz(10, lang="en")
        for placement in ("first", "last", "header", "footer"):
            doc = pdf_pages(build_quiz_pdf("Exam", qs, "simple", False,
                                           ExportOptions(teacher_placement=placement, per_page=5, **base)))
            texts = [p.get_text() for p in doc]
            joined = " ".join(texts)
            for token in ("MATH", "G9", "2026-11-07", "45min", "100", "TEACHERNAME"):
                self.assertIn(token, joined, placement)
            if placement in ("header", "footer"):
                self.assertTrue(all("TEACHERNAME" in t for t in texts), placement)
            elif placement == "first":
                self.assertIn("TEACHERNAME", texts[0])
                self.assertNotIn("TEACHERNAME", texts[-1])
            else:
                self.assertIn("TEACHERNAME", texts[-1])
                self.assertNotIn("TEACHERNAME", texts[0])

    def test_hide_student_fields_and_page_numbers(self):
        qs = quiz(4, lang="en")
        shown = pdf_pages(build_quiz_pdf("Exam", qs, "simple", False))[0].get_text()
        hidden = pdf_pages(build_quiz_pdf("Exam", qs, "simple", False,
                                          ExportOptions(student_fields=False, page_numbers=False)))[0].get_text()
        self.assertIn("Name:", shown)
        self.assertNotIn("Name:", hidden)
        self.assertNotRegex(hidden.strip().splitlines()[-1], r"^1$")

    def test_answer_key_only_is_shorter_and_has_no_explanations(self):
        qs = quiz(12)
        full = pdf_pages(build_quiz_pdf("t", qs, "simple", True))
        key = pdf_pages(build_quiz_pdf("t", qs, "simple", True, ExportOptions(answers_mode="key")))
        self.assertLess(len(key), len(full))

    def test_math_questions_paged_and_not_clipped(self):
        q = {"is_math": True, "question": r"أوجد $x^2+1$", "options": [r"$x$", r"$x^2$", r"$1$", r"$2$"],
             "correct_option_id": 1, "explanation": "تفسير"}
        data = build_quiz_pdf("m", [dict(q) for _ in range(5)], "simple", False, ExportOptions(per_page=2))
        doc = pdf_pages(data)
        self.assertGreaterEqual(len(doc), 3)
        for page in doc:
            for block in page.get_text("dict")["blocks"]:
                if block["type"] == 1:
                    self.assertLess(block["bbox"][3], page.rect.height - 40)

    def test_invalid_options_surface_as_export_error(self):
        with self.assertRaises(ExportError):
            build_quiz_pdf("t", quiz(2), "simple", False, {"per_page": 0})

    def test_max_questions_per_page_depends_on_content_and_style(self):
        short = max_questions_per_page(quiz(10), "simple")
        longer = max_questions_per_page(quiz(10, long_at=range(1, 11)), "simple")
        boxed = max_questions_per_page(quiz(10), "modern")
        self.assertTrue(1 <= longer < short <= MAX_PER_PAGE)
        self.assertLessEqual(boxed, short)

    def test_oversized_boxed_question_is_not_clipped(self):
        q = quiz(1)[0]
        q["question"] = "كلمة " * 1500
        doc = pdf_pages(build_quiz_pdf("t", [q], "modern", False))
        self.assertGreater(len(doc), 1)


class WordAndExcelTests(unittest.TestCase):
    def setUp(self):
        logging.disable(logging.CRITICAL)
        self.addCleanup(logging.disable, logging.NOTSET)

    def texts(self, data):
        return [p.text for p in Document(io.BytesIO(data)).paragraphs]

    def test_word_default_unchanged_and_fields_added(self):
        qs = quiz(6, lang="en")
        self.assertEqual(self.texts(build_quiz_docx("Exam", qs, "simple", False)),
                         self.texts(build_quiz_docx("Exam", qs, "simple", False, ExportOptions())))
        data = build_quiz_docx("Exam", qs, "simple", False,
                               ExportOptions(subject="MATH", teacher_name="TEACHERNAME", exam_title="Final",
                                             teacher_placement="last"))
        joined = "\n".join(self.texts(data))
        self.assertIn("MATH", joined)
        self.assertIn("TEACHERNAME", joined)
        self.assertIn("Final", joined)
        self.assertNotIn("Exam\n", joined)

    def test_word_header_footer_and_page_numbers(self):
        data = build_quiz_docx("Exam", quiz(3, lang="en"), "simple", False,
                               ExportOptions(teacher_name="HDR", teacher_placement="header", page_numbers=True))
        doc = Document(io.BytesIO(data))
        self.assertIn("HDR", doc.sections[0].header.paragraphs[0].text)
        self.assertIn('w:instr="PAGE"', doc.sections[0].footer._element.xml.replace("'", '"'))

    def test_word_page_breaks_follow_per_page(self):
        data = build_quiz_docx("t", quiz(9), "simple", False, ExportOptions(per_page=3))
        doc = Document(io.BytesIO(data))
        breaks = [p for p in doc.paragraphs if p.paragraph_format.page_break_before]
        self.assertEqual(len(breaks), 2)

    def test_word_answer_key_is_text(self):
        qs = quiz(5)
        data = build_quiz_docx("t", qs, "simple", True, ExportOptions(answers_mode="key"))
        self.assertEqual(len(Document(io.BytesIO(data)).inline_shapes), 0)

    def test_excel_info_sheet_and_notes_row(self):
        qs = quiz(3, lang="en")
        legacy = load_workbook(io.BytesIO(build_quiz_xlsx("Exam", qs, "simple", True)))["Quiz Info"]
        self.assertEqual(legacy.cell(5, 2).value, "Not specified")
        data = build_quiz_xlsx("Exam", qs, "simple", True,
                               ExportOptions(duration="45min", subject="MATH", teacher_name="T", answers_mode="key"))
        wb = load_workbook(io.BytesIO(data))
        rows = {r[0].value: r[1].value for r in wb["Quiz Info"].iter_rows()}
        self.assertEqual(rows["Duration"], "45min")
        self.assertEqual(rows["Subject"], "MATH")
        self.assertEqual(rows["Teacher"], "T")
        self.assertEqual(rows["Notes"][:6], "Option")
        headers = [c.value for c in wb["Questions"][1]]
        self.assertNotIn("Explanation", headers)


if __name__ == "__main__":
    unittest.main()
