"""شارة الختام (لوغو أبيض + تلغرام + @AbiadQuizMakerbot) إجبارية بكل صيغ تصدير الاختبار.

المعرّف نص حقيقي قابل للتحديد وللضغط (يفتح البوت)، وليس مرسوماً داخل الصورة."""
import io
import unittest
from pathlib import Path

import fitz
from docx import Document
from docx.opc.constants import RELATIONSHIP_TYPE as RT
from docx.oxml.ns import qn
from openpyxl import load_workbook

from services.export_branding import BADGE_H, BADGE_W, BOT_HANDLE, BOT_URL, branding_png
from services.export_options import ExportOptions
from services.export_service import build_quiz_docx, build_quiz_pdf, build_quiz_xlsx


def badges(page):
    return sum(1 for img in page.get_images(full=True) if (img[2], img[3]) == (BADGE_W, BADGE_H))


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
            for doc, expected in ((student, 1), (teacher, 2)):
                self.assertEqual(sum(page.get_text().count(BOT_HANDLE) for page in doc), expected)
                uris = [link.get("uri") for page in doc for link in page.get_links()]
                self.assertEqual(uris.count(BOT_URL), expected)

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

    def test_docx_handle_is_real_clickable_text(self):
        for lang in ("ar", "en"):
            for answers, expected in ((False, 1), (True, 2)):
                doc = Document(io.BytesIO(build_quiz_docx("t", quiz(4, lang), "simple", answers)))
                links = list(doc.element.body.iter(qn("w:hyperlink")))
                self.assertEqual(len(links), expected)
                for link in links:
                    self.assertEqual("".join(t.text for t in link.iter(qn("w:t"))), BOT_HANDLE)
                    rel = doc.part.rels[link.get(qn("r:id"))]
                    self.assertEqual((rel.reltype, rel.target_ref, rel.is_external), (RT.HYPERLINK, BOT_URL, True))

    def test_docx_handle_paragraph_is_ltr_so_at_sign_stays_first(self):
        doc = Document(io.BytesIO(build_quiz_docx("t", quiz(4), "simple", False, ExportOptions(columns=2))))
        link = next(doc.element.body.iter(qn("w:hyperlink")))
        self.assertIsNone(link.getparent().find(qn("w:pPr")).find(qn("w:bidi")))

    def test_xlsx_badge_below_last_row_and_does_not_break_cells(self):
        data = build_quiz_xlsx("t", quiz(3), "simple", False)
        wb = load_workbook(io.BytesIO(data))
        for sheet in wb:
            self.assertEqual(len(sheet._images), 1)
            image_row = sheet._images[0].anchor._from.row + 1
            handles = [c for row in sheet.iter_rows() for c in row if c.value == BOT_HANDLE]
            self.assertEqual(len(handles), 1)
            cell = handles[0]
            self.assertEqual(cell.hyperlink.target, BOT_URL)
            self.assertEqual(cell.data_type, "s")
            self.assertGreater(image_row, 4)          # تحت رأس الجدول + 3 أسئلة (أو جدول المعلومات)
            self.assertGreater(cell.row, image_row)   # المعرّف تحت الصورة

    def test_xlsx_handle_row_is_not_imported_as_a_question(self):
        import tempfile
        from services.excel_import import read_quiz_xlsx
        data = build_quiz_xlsx("t", quiz(3, "en"), "simple", True)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "export.xlsx"
            path.write_bytes(data)
            _, questions = read_quiz_xlsx(str(path))
        self.assertEqual(len(questions), 3)


if __name__ == "__main__":
    unittest.main()
