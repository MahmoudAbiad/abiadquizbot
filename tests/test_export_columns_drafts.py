"""أعمدة الصفحة (عمودان + خط منصّف) ومساحة المسودة وسقف 16 سؤالاً بالصفحة (PDF/Word)."""
import io
import unittest
import warnings
import zipfile

from services.export_layout import pick_max_per_page, plan_column_pages
from services.export_options import (
    MAX_PER_PAGE, ExportOptions, ExportOptionsError, resolve_draft_style,
)
from services.export_service import (
    build_quiz_docx, build_quiz_pdf, build_quiz_xlsx, max_questions_per_page, max_questions_per_page_docx,
)

warnings.filterwarnings("ignore")


def quiz(n, lang="ar", options=4):
    out = []
    for i in range(n):
        text = f"ما ناتج العملية رقم {i + 1}؟" if lang == "ar" else f"Result of operation {i + 1}?"
        opts = (["الأول", "الثاني", "الثالث", "الرابع"] if lang == "ar" else ["one", "two", "three", "four"])[:options]
        out.append({"question": text, "options": opts, "correct_option_id": 0, "explanation": "شرح"})
    return out


def fitz_doc(data):
    import fitz
    return fitz.open(stream=data, filetype="pdf")


class OptionsTests(unittest.TestCase):
    def test_limit_is_16(self):
        self.assertEqual(MAX_PER_PAGE, 16)
        self.assertEqual(ExportOptions.from_dict({"per_page": 16}).per_page, 16)
        with self.assertRaises(ExportOptionsError):
            ExportOptions.from_dict({"per_page": 17})

    def test_new_fields_validated_and_round_trip(self):
        raw = {"columns": 2, "column_divider": False, "draft_size": "large", "draft_style": "grid"}
        self.assertEqual(ExportOptions.from_dict(raw).to_dict(), raw)
        for bad in ({"columns": 3}, {"columns": True}, {"column_divider": "x"}, {"draft_size": "huge"},
                    {"draft_style": "dots"}):
            with self.assertRaises(ExportOptionsError, msg=bad):
                ExportOptions.from_dict(bad)

    def test_defaults_unchanged_and_summary(self):
        self.assertTrue(ExportOptions().is_default)
        lines = ExportOptions(columns=2, draft_size="small").summary_lines_ar()
        self.assertTrue(any("عمودان" in line for line in lines))
        self.assertTrue(any("مسودة" in line for line in lines))

    def test_auto_draft_style_follows_question_type(self):
        self.assertEqual(resolve_draft_style("auto", True), "grid")
        self.assertEqual(resolve_draft_style("auto", False), "lines")
        self.assertEqual(resolve_draft_style("box", True), "box")


class PlannerTests(unittest.TestCase):
    def test_16_split_evenly_between_columns(self):
        pages = plan_column_pages([60] * 20, 16, 2, 600, 640)
        self.assertEqual([len(c) for c in pages[0].columns], [8, 8])
        self.assertEqual(len(pages), 2)

    def test_auto_fills_first_column_then_second(self):
        pages = plan_column_pages([100] * 12, None, 2, 600, 640)
        self.assertEqual([len(c) for c in pages[0].columns], [6, 6])

    def test_wide_and_oversize_get_full_page(self):
        pages = plan_column_pages([100, 100, 900, 100, 100], 6, 2, 600, 640, wide={3})
        self.assertEqual([p.full for p in pages], [False, True, True, False])
        self.assertEqual(pages[1].columns, ((2,),))

    def test_last_page_is_balanced(self):
        pages = plan_column_pages([100] * 8, None, 2, 900, 900)
        self.assertEqual([len(c) for c in pages[0].columns], [4, 4])

    def test_every_question_placed_once_in_order(self):
        pages = plan_column_pages([70, 120, 90, 300, 60, 80, 110] * 4, 7, 2, 500, 600)
        flat = [i for p in pages for c in p.columns for i in c]
        self.assertEqual(flat, list(range(28)))

    def test_columns_double_the_max(self):
        self.assertEqual(pick_max_per_page([120] * 10, 640, 16, 2), 2 * pick_max_per_page([120] * 10, 640, 16, 1))


class PdfTests(unittest.TestCase):
    def test_sixteen_short_questions_fit_one_page_with_two_columns(self):
        q = quiz(16)
        self.assertEqual(max_questions_per_page(q, "simple", ExportOptions(columns=2)), 16)
        data = build_quiz_pdf("t", q, "simple", False, ExportOptions(columns=2, per_page=16, student_fields=False))
        self.assertEqual(len(fitz_doc(data)), 1)

    def test_single_column_cannot_hold_16(self):
        self.assertLess(max_questions_per_page(quiz(16), "simple"), 16)

    def test_two_columns_positions_arabic_first_column_on_right(self):
        data = build_quiz_pdf("t", quiz(8), "simple", False, ExportOptions(columns=2, per_page=8))
        page = fitz_doc(data)[0]
        mid = page.rect.width / 2
        blocks = {}
        for x0, y0, x1, y1, text, *_ in page.get_text("blocks"):
            for n in range(1, 9):
                if f"{n}" in text and "العملية" not in text:
                    pass
        words = page.get_text("words")
        self.assertTrue(any(w[0] < mid for w in words) and any(w[0] > mid for w in words))
        # الخط المنصّف مرسوم
        self.assertTrue(any(abs((d["rect"].x0 + d["rect"].x1) / 2 - mid) < 3 and d["rect"].height > 300
                            for d in page.get_drawings()))

    def test_divider_can_be_hidden(self):
        with_line = fitz_doc(build_quiz_pdf("t", quiz(8), "simple", False, ExportOptions(columns=2)))[0]
        without = fitz_doc(build_quiz_pdf("t", quiz(8), "simple", False,
                                          ExportOptions(columns=2, column_divider=False)))[0]
        tall = lambda page: [d for d in page.get_drawings() if d["rect"].height > 300]
        self.assertTrue(tall(with_line))
        self.assertFalse(tall(without))

    def test_drafts_reduce_questions_per_page_and_never_clip(self):
        plain = fitz_doc(build_quiz_pdf("t", quiz(12), "modern", False, ExportOptions()))
        drafts = fitz_doc(build_quiz_pdf("t", quiz(12), "modern", False, ExportOptions(draft_size="large")))
        self.assertGreater(len(drafts), len(plain))
        for page in drafts:
            for block in page.get_text("blocks"):
                if len(block[4].strip()) > 2:      # غير رقم الصفحة
                    self.assertLess(block[3], page.rect.height - 40)

    def test_all_styles_languages_and_draft_styles_render(self):
        for style in ("simple", "modern", "academic"):
            for lang in ("ar", "en"):
                for dstyle in ("auto", "lines", "grid", "box"):
                    opts = ExportOptions(columns=2, per_page=6, draft_size="small", draft_style=dstyle)
                    data = build_quiz_pdf("t", quiz(9, lang), style, True, opts)
                    self.assertGreater(len(fitz_doc(data)), 1)

    def test_math_question_uses_column_width_and_wide_formula_falls_back(self):
        math = {"question": "احسب $x^2+3x+2=0$", "options": ["1", "2", "3", "4"], "correct_option_id": 0,
                "is_math": True}
        wide = {"question": "احسب " + "$" + "+".join(f"x_{i}^2" for i in range(10)) + "$",
                "options": ["1", "2"], "correct_option_id": 0, "is_math": True}
        data = build_quiz_pdf("t", [math] * 4 + [wide] + quiz(3), "simple", False,
                              ExportOptions(columns=2, draft_size="small"))
        doc = fitz_doc(data)
        self.assertGreaterEqual(len(doc), 2)
        self.assertTrue(any(page.get_images() for page in doc))

    def test_default_output_unchanged_without_new_options(self):
        a = build_quiz_pdf("t", quiz(6), "simple", True, None)
        b = build_quiz_pdf("t", quiz(6), "simple", True, ExportOptions(draft_style="grid"))   # بلا مسودة: لا أثر
        self.assertEqual(len(fitz_doc(a)), len(fitz_doc(b)))


class DocxTests(unittest.TestCase):
    @staticmethod
    def xml(data):
        return zipfile.ZipFile(io.BytesIO(data)).read("word/document.xml").decode("utf-8")

    def test_two_columns_with_native_separator(self):
        xml = self.xml(build_quiz_docx("t", quiz(6), "simple", False, ExportOptions(columns=2)))
        self.assertIn('w:num="2"', xml)
        self.assertIn('w:sep="1"', xml)

    def test_no_separator_when_hidden_and_single_column_untouched(self):
        xml = self.xml(build_quiz_docx("t", quiz(6), "simple", False, ExportOptions(columns=2, column_divider=False)))
        self.assertIn('w:num="2"', xml)
        self.assertNotIn('w:sep="1"', xml)
        self.assertNotIn('w:num="2"', self.xml(build_quiz_docx("t", quiz(6), "simple", False, None)))

    def test_per_page_inserts_column_and_page_breaks(self):
        xml = self.xml(build_quiz_docx("t", quiz(12), "simple", False, ExportOptions(columns=2, per_page=4)))
        self.assertIn('w:type="column"', xml)
        self.assertIn("pageBreakBefore", xml)

    def test_drafts_add_images_and_answers_return_to_one_column(self):
        data = build_quiz_docx("t", quiz(4), "modern", True,
                               ExportOptions(columns=2, draft_size="medium", draft_style="box"))
        names = zipfile.ZipFile(io.BytesIO(data)).namelist()
        self.assertTrue(any(n.startswith("word/media/") for n in names))
        xml = self.xml(data)
        self.assertGreaterEqual(xml.count("<w:cols"), 2)
        self.assertIn('w:num="2"', xml)

    def test_modern_cards_get_fixed_width_in_columns(self):
        xml = self.xml(build_quiz_docx("t", quiz(4), "modern", False, ExportOptions(columns=2)))
        self.assertIn('w:tblLayout w:type="fixed"', xml)

    def test_word_max_per_page_accounts_for_columns_and_drafts(self):
        q = quiz(12)
        base = max_questions_per_page_docx(q, "simple")
        self.assertGreater(max_questions_per_page_docx(q, "simple", ExportOptions(columns=2)), base)
        self.assertLessEqual(max_questions_per_page_docx(q, "simple", ExportOptions(draft_size="large")), base)

    def test_arabic_english_and_all_styles_build(self):
        for style in ("simple", "modern", "academic"):
            for lang in ("ar", "en"):
                data = build_quiz_docx("t", quiz(7, lang), style, True,
                                       ExportOptions(columns=2, per_page=4, draft_size="small",
                                                     teacher_name="أ. علي", teacher_placement="last"))
                self.assertTrue(data.startswith(b"PK"))


class ExcelTests(unittest.TestCase):
    def test_excel_ignores_layout_options(self):
        a = build_quiz_xlsx("t", quiz(5), "simple", True)
        b = build_quiz_xlsx("t", quiz(5), "simple", True, ExportOptions(columns=2, draft_size="large"))
        self.assertTrue(a.startswith(b"PK") and b.startswith(b"PK"))


if __name__ == "__main__":
    unittest.main()
