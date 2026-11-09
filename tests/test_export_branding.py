"""شارة الختام (لوغو أبيض + تلغرام + @AbiadQuizMakerbot) إجبارية بكل صيغ تصدير الاختبار."""
import io
import unittest

import fitz
from docx import Document
from openpyxl import load_workbook

from services.export_branding import BOT_HANDLE, branding_png
from services.export_options import ExportOptions
from services.export_service import build_quiz_docx, build_quiz_pdf, build_quiz_xlsx


def badges(page):
    return sum(1 for img in page.get_images(full=True) if (img[2], img[3]) == (1200, 220))


def quiz(n, lang="ar"):
    ar = lang == "ar"
    return [{"question": (f"سؤال {i}؟" if ar else f"Question {i}?"), "options": ["أ", "ب", "ج", "د"] if ar
             else ["a", "b", "c", "d"], "correct_option_id": 1, "explanation": "شرح" if ar else "why"}
            for i in range(1, n + 1)]


class BrandingTests(unittest.TestCase):
    def test_badge_png_is_valid_and_contains_handle_constant(self):
        self.assertEqual(BOT_HANDLE, "@AbiadQuizMakerbot")
        self.assertTrue(branding_png().startswith(b"\x89PNG"))

    def test_pdf_has_badge_on_last_questions_page_and_end_of_teacher_file(self):
        for lang in ("ar", "en"):
            student = fitz.open(stream=build_quiz_pdf("t", quiz(5, lang), "simple", False), filetype="pdf")
            self.assertEqual(sum(badges(p) for p in student), 1)
            teacher = fitz.open(stream=build_quiz_pdf("t", quiz(5, lang), "simple", True), filetype="pdf")
            self.assertEqual(sum(badges(p) for p in teacher), 2)

    def test_full_page_does_not_push_badge_to_extra_page(self):
        data = build_quiz_pdf("t", quiz(16), "simple", False,
                              ExportOptions(columns=2, per_page=16, student_fields=False))
        doc = fitz.open(stream=data, filetype="pdf")
        self.assertEqual(len(doc), 1)
        self.assertEqual(badges(doc[0]), 1)

    def test_docx_has_badge_in_every_style_and_layout(self):
        for style in ("simple", "modern", "academic"):
            for columns in (1, 2):
                data = build_quiz_docx("t", quiz(4), style, True, ExportOptions(columns=columns))
                media = Document(io.BytesIO(data)).part.package.parts
                self.assertGreaterEqual(sum(1 for part in media if getattr(part, "blob", None) == branding_png()), 1)

    def test_docx_student_version_has_exactly_one_badge(self):
        data = build_quiz_docx("t", quiz(4), "simple", False)
        self.assertEqual(len(Document(io.BytesIO(data)).inline_shapes), 1)

    def test_xlsx_badge_below_last_row_and_does_not_break_cells(self):
        data = build_quiz_xlsx("t", quiz(3), "simple", False)
        wb = load_workbook(io.BytesIO(data))
        for sheet in wb:
            self.assertEqual(len(sheet._images), 1)
            self.assertGreater(sheet._images[0].anchor._from.row + 1, sheet.max_row)


if __name__ == "__main__":
    unittest.main()
