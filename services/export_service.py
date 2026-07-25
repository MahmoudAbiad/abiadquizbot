# services/export_service.py
"""
خدمة تصدير الكويزات إلى ملفات Word (.docx) و PDF بتنسيق داخلي جميل.
- تكتشف لغة الكويز (عربي/إنكليزي) تلقائياً من نص الأسئلة.
- العربي: اتجاه الصفحة/الفقرات من اليمين لليسار (RTL) بالكامل.
- الإنكليزي: اتجاه عادي من اليسار لليمين (LTR).
- الأسئلة والاختيارات فقط داخل المتن، بدون أي تلميح/شرح.
- جدول "الإجابات الصحيحة" (يتضمن الشرح) يوضع في آخر الملف فقط.
"""
import io
import os
import re
from typing import Any, Dict, List, Tuple

from docx import Document
from docx.enum.table import WD_TABLE_ALIGNMENT
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Cm, Pt, RGBColor

from logger import get_logger, log_error

logger = get_logger(__name__)

# ==================== إعدادات عامة ====================

ARABIC_LETTERS = ["أ", "ب", "ج", "د", "هـ", "و", "ز", "ح"]
ENGLISH_LETTERS = ["A", "B", "C", "D", "E", "F", "G", "H"]

_ARABIC_RE = re.compile(r"[\u0600-\u06FF\u0750-\u077F\u08A0-\u08FF\uFB50-\uFDFF\uFE70-\uFEFF]")
_LETTER_RE = re.compile(r"[^\W\d_]", re.UNICODE)

FONTS_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "assets", "fonts")


class ExportError(Exception):
    """خطأ عام أثناء توليد ملف التصدير (يُعرض للمستخدم برسالة ودّية)."""


def detect_language(questions: List[Dict[str, Any]]) -> str:
    """يكتشف لغة الكويز: 'ar' أو 'en' اعتماداً على نسبة الأحرف العربية في عينة من النص."""
    sample_parts = []
    for q in questions[:12]:
        sample_parts.append(str(q.get("question", "")))
        sample_parts.extend(str(o) for o in (q.get("options") or []))
    sample = " ".join(sample_parts)

    letters = _LETTER_RE.findall(sample)
    if not letters:
        return "en"
    arabic_count = sum(1 for ch in letters if _ARABIC_RE.match(ch))
    return "ar" if (arabic_count / len(letters)) > 0.3 else "en"


def build_export_filename(title: str, ext: str) -> str:
    """اسم ملف آمن (بدون رموز قد تكسر بعض الأنظمة) مع الحفاظ على العربية."""
    safe = re.sub(r'[\\/:*?"<>|]+', " ", title or "quiz").strip()
    safe = re.sub(r"\s+", "_", safe)[:60] or "quiz"
    return f"{safe}.{ext}"


def _letters_for(is_ar: bool) -> List[str]:
    return ARABIC_LETTERS if is_ar else ENGLISH_LETTERS


# ==================== DOCX ====================

def _set_paragraph_rtl(paragraph, align_right: bool = True) -> None:
    pPr = paragraph._p.get_or_add_pPr()
    bidi = OxmlElement("w:bidi")
    pPr.append(bidi)
    paragraph.alignment = WD_ALIGN_PARAGRAPH.RIGHT if align_right else WD_ALIGN_PARAGRAPH.LEFT


def _style_run(run, font_name: str, size_pt: float, bold: bool = False,
                color: Tuple[int, int, int] = None, rtl: bool = False) -> None:
    run.font.size = Pt(size_pt)
    run.bold = bold
    if color:
        run.font.color.rgb = RGBColor(*color)
    rPr = run._element.get_or_add_rPr()
    rFonts = rPr.find(qn("w:rFonts"))
    if rFonts is None:
        rFonts = OxmlElement("w:rFonts")
        rPr.append(rFonts)
    rFonts.set(qn("w:ascii"), font_name)
    rFonts.set(qn("w:hAnsi"), font_name)
    rFonts.set(qn("w:cs"), font_name)
    if rtl:
        rPr.append(OxmlElement("w:rtl"))


def _set_table_rtl(table) -> None:
    tblPr = table._tbl.tblPr
    tblPr.append(OxmlElement("w:bidiVisual"))


def _set_cell_width(cell, width_cm: float) -> None:
    cell.width = Cm(width_cm)
    tcPr = cell._tc.get_or_add_tcPr()
    tcW = OxmlElement("w:tcW")
    tcW.set(qn("w:w"), str(int(width_cm * 567)))  # 1cm ≈ 567 twips
    tcW.set(qn("w:type"), "dxa")
    tcPr.append(tcW)


def _shade_cell(cell, hex_color: str) -> None:
    tcPr = cell._tc.get_or_add_tcPr()
    shd = OxmlElement("w:shd")
    shd.set(qn("w:val"), "clear")
    shd.set(qn("w:color"), "auto")
    shd.set(qn("w:fill"), hex_color)
    tcPr.append(shd)


def _add_table_cell_text(cell, text: str, font_name: str, size_pt: float, is_ar: bool,
                          bold: bool = False) -> None:
    cell.paragraphs[0].text = ""
    p = cell.paragraphs[0]
    if is_ar:
        _set_paragraph_rtl(p, align_right=True)
    else:
        p.alignment = WD_ALIGN_PARAGRAPH.LEFT
    run = p.add_run(text)
    _style_run(run, font_name, size_pt, bold=bold, rtl=is_ar)


def build_quiz_docx(title: str, questions: List[Dict[str, Any]]) -> bytes:
    """يبني ملف Word جميل التنسيق للكويز، مع اتجاه تلقائي حسب اللغة."""
    if not questions:
        raise ExportError("لا توجد أسئلة لتصديرها.")

    is_ar = detect_language(questions) == "ar"
    letters = _letters_for(is_ar)
    body_font = "Arial" if is_ar else "Calibri"

    doc = Document()

    # اتجاه الصفحة بالكامل RTL للعربي
    if is_ar:
        for section in doc.sections:
            sectPr = section._sectPr
            sectPr.append(OxmlElement("w:bidi"))

    # العنوان
    title_p = doc.add_paragraph()
    if is_ar:
        _set_paragraph_rtl(title_p, align_right=False)
        title_p.alignment = WD_ALIGN_PARAGRAPH.CENTER
    else:
        title_p.alignment = WD_ALIGN_PARAGRAPH.CENTER
    title_run = title_p.add_run(title or ("كويز" if is_ar else "Quiz"))
    _style_run(title_run, body_font, 20, bold=True, color=(20, 55, 110), rtl=is_ar)

    sub_p = doc.add_paragraph()
    sub_p.alignment = WD_ALIGN_PARAGRAPH.CENTER
    sub_text = f"عدد الأسئلة: {len(questions)}" if is_ar else f"Total Questions: {len(questions)}"
    sub_run = sub_p.add_run(sub_text)
    _style_run(sub_run, body_font, 11, color=(90, 90, 90), rtl=is_ar)
    if is_ar:
        _set_paragraph_rtl(sub_p, align_right=False)
        sub_p.alignment = WD_ALIGN_PARAGRAPH.CENTER

    # خط فاصل بسيط
    doc.add_paragraph()

    for idx, q in enumerate(questions, 1):
        q_text = str(q.get("question", "")).strip()
        options = q.get("options") or []

        qp = doc.add_paragraph()
        label = f"السؤال {idx}: " if is_ar else f"Question {idx}: "
        run = qp.add_run(label + q_text)
        _style_run(run, body_font, 13, bold=True, rtl=is_ar)
        if is_ar:
            _set_paragraph_rtl(qp, align_right=True)
        qp.paragraph_format.space_after = Pt(6)

        for opt_idx, opt_text in enumerate(options):
            letter = letters[opt_idx] if opt_idx < len(letters) else str(opt_idx + 1)
            op = doc.add_paragraph()
            run = op.add_run(f"{letter}) {opt_text}")
            _style_run(run, body_font, 11.5, rtl=is_ar)
            if is_ar:
                _set_paragraph_rtl(op, align_right=True)
                op.paragraph_format.right_indent = Cm(0.8)
            else:
                op.paragraph_format.left_indent = Cm(0.8)
            op.paragraph_format.space_after = Pt(2)

        spacer = doc.add_paragraph()
        spacer.paragraph_format.space_after = Pt(6)

    # ==================== جدول الإجابات الصحيحة ====================
    doc.add_page_break()
    head_p = doc.add_paragraph()
    head_text = "🗝️ جدول الإجابات الصحيحة" if is_ar else "🗝️ Answer Key"
    head_run = head_p.add_run(head_text)
    _style_run(head_run, body_font, 16, bold=True, color=(20, 55, 110), rtl=is_ar)
    if is_ar:
        _set_paragraph_rtl(head_p, align_right=True)
    head_p.paragraph_format.space_after = Pt(10)

    table = doc.add_table(rows=1, cols=3)
    table.style = "Table Grid"
    table.alignment = WD_TABLE_ALIGNMENT.CENTER
    if is_ar:
        _set_table_rtl(table)

    headers = ["#", "الإجابة الصحيحة", "الشرح"] if is_ar else ["#", "Correct Answer", "Explanation"]
    widths = [1.5, 5.0, 9.5]
    hdr_cells = table.rows[0].cells
    for cell, htext, w in zip(hdr_cells, headers, widths):
        _add_table_cell_text(cell, htext, body_font, 11.5, is_ar, bold=True)
        _shade_cell(cell, "D9E2F3")
        _set_cell_width(cell, w)

    for idx, q in enumerate(questions, 1):
        options = q.get("options") or []
        correct_idx = q.get("correct_option_id")
        try:
            correct_idx = int(correct_idx)
        except (TypeError, ValueError):
            correct_idx = -1
        correct_letter = letters[correct_idx] if 0 <= correct_idx < len(letters) else "-"
        correct_text = options[correct_idx] if 0 <= correct_idx < len(options) else "-"
        explanation = str(q.get("explanation") or "").strip() or ("لا يوجد شرح" if is_ar else "No explanation")

        row_cells = table.add_row().cells
        values = [str(idx), f"{correct_letter}) {correct_text}", explanation]
        for cell, val, w in zip(row_cells, values, widths):
            _add_table_cell_text(cell, val, body_font, 10.5, is_ar)
            _set_cell_width(cell, w)

    out = io.BytesIO()
    doc.save(out)
    return out.getvalue()


# ==================== PDF ====================

_FONTS_REGISTERED = False
_BIDI_AVAILABLE = True
try:
    import arabic_reshaper
    from bidi.algorithm import get_display
except ImportError:  # pragma: no cover
    _BIDI_AVAILABLE = False

from reportlab.lib.colors import HexColor
from reportlab.lib.pagesizes import A4
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen import canvas

_FONT_FILES = {
    "Arabic": "NotoNaskhArabic-Regular.ttf",
    "Arabic-Bold": "NotoNaskhArabic-Bold.ttf",
    "Latin": "NotoSans-Regular.ttf",
    "Latin-Bold": "NotoSans-Bold.ttf",
}


def _register_pdf_fonts() -> None:
    global _FONTS_REGISTERED
    if _FONTS_REGISTERED:
        return
    for font_name, filename in _FONT_FILES.items():
        path = os.path.join(FONTS_DIR, filename)
        if os.path.exists(path):
            try:
                pdfmetrics.registerFont(TTFont(font_name, path))
            except Exception as e:
                log_error(logger, f"Failed registering font {font_name}: {e}", exception=e)
    _FONTS_REGISTERED = True


def _font_available(name: str) -> bool:
    return name in pdfmetrics.getRegisteredFontNames()


def _shape(text: str, is_ar: bool) -> str:
    if not is_ar or not _BIDI_AVAILABLE:
        return text
    try:
        return get_display(arabic_reshaper.reshape(text))
    except Exception:
        return text


def _wrap_line(text: str, font_name: str, size: float, max_width: float, is_ar: bool) -> List[str]:
    words = text.split()
    if not words:
        return [""]
    lines: List[str] = []
    current: List[str] = []
    for w in words:
        trial = current + [w]
        trial_text = " ".join(trial)
        measure = _shape(trial_text, is_ar)
        width = pdfmetrics.stringWidth(measure, font_name, size)
        if width <= max_width or not current:
            current = trial
        else:
            lines.append(" ".join(current))
            current = [w]
    if current:
        lines.append(" ".join(current))
    return lines


class _QuizPDFRenderer:
    def __init__(self, title: str, questions: List[Dict[str, Any]], is_ar: bool):
        self.title = title or ("كويز" if is_ar else "Quiz")
        self.questions = questions
        self.is_ar = is_ar
        self.letters = _letters_for(is_ar)

        self.page_w, self.page_h = A4
        self.margin = 48
        self.buf = io.BytesIO()
        self.c = canvas.Canvas(self.buf, pagesize=A4)
        self.y = self.page_h - self.margin
        self.page_num = 1

        _register_pdf_fonts()
        if is_ar:
            self.font_regular = "Arabic" if _font_available("Arabic") else None
            self.font_bold = "Arabic-Bold" if _font_available("Arabic-Bold") else None
        else:
            self.font_regular = "Latin" if _font_available("Latin") else "Helvetica"
            self.font_bold = "Latin-Bold" if _font_available("Latin-Bold") else "Helvetica-Bold"

        if is_ar and (not self.font_regular or not self.font_bold):
            raise ExportError(
                "تعذّر إنشاء PDF بالعربية لعدم توفر خط عربي على الخادم حالياً. "
                "جرّب تصدير Word بدلاً من ذلك."
            )

    # ---------- أدوات رسم أساسية ----------

    def _new_page(self):
        self._draw_footer()
        self.c.showPage()
        self.page_num += 1
        self.y = self.page_h - self.margin

    def _draw_footer(self):
        self.c.setFont(self.font_regular, 9)
        self.c.setFillColor(HexColor("#888888"))
        self.c.drawCentredString(self.page_w / 2, 25, str(self.page_num))

    def _ensure_space(self, needed: float):
        if self.y - needed < self.margin + 25:
            self._new_page()

    def _draw_paragraph(self, text: str, font: str, size: float, bold: bool = False,
                         color: str = "#111111", extra_indent: float = 0, gap_after: float = 6,
                         center: bool = False):
        use_font = self.font_bold if bold else font
        max_width = self.page_w - 2 * self.margin - extra_indent
        lines = _wrap_line(text, use_font, size, max_width, self.is_ar)
        self.c.setFillColor(HexColor(color))
        for line in lines:
            self._ensure_space(size + 5)
            draw_text = _shape(line, self.is_ar)
            self.c.setFont(use_font, size)
            if center:
                self.c.drawCentredString(self.page_w / 2, self.y, draw_text)
            elif self.is_ar:
                x = self.page_w - self.margin - extra_indent
                self.c.drawRightString(x, self.y, draw_text)
            else:
                x = self.margin + extra_indent
                self.c.drawString(x, self.y, draw_text)
            self.y -= (size + 5)
        self.y -= gap_after

    # ---------- الأقسام ----------

    def _render_header(self):
        self._draw_paragraph(self.title, self.font_regular, 19, bold=True,
                              color="#14376E", gap_after=4, center=True)
        sub = f"عدد الأسئلة: {len(self.questions)}" if self.is_ar else f"Total Questions: {len(self.questions)}"
        self._draw_paragraph(sub, self.font_regular, 10.5, color="#666666", gap_after=16, center=True)

    def _render_questions(self):
        for idx, q in enumerate(self.questions, 1):
            label = f"{'السؤال' if self.is_ar else 'Question'} {idx}: {str(q.get('question', '')).strip()}"
            self._ensure_space(30)
            self._draw_paragraph(label, self.font_regular, 12.5, bold=True, gap_after=4)
            for oidx, opt in enumerate(q.get("options") or []):
                letter = self.letters[oidx] if oidx < len(self.letters) else str(oidx + 1)
                self._draw_paragraph(f"{letter}) {opt}", self.font_regular, 11,
                                      extra_indent=18, gap_after=2)
            self.y -= 8

    def _render_answer_table(self):
        self._new_page()
        title = "🗝️ جدول الإجابات الصحيحة" if self.is_ar else "🗝️ Answer Key"
        self._draw_paragraph(title, self.font_regular, 15, bold=True, color="#14376E", gap_after=12)

        total_w = self.page_w - 2 * self.margin
        col_ratios = [0.09, 0.31, 0.60]
        col_widths = [total_w * r for r in col_ratios]
        headers = ["#", "الإجابة الصحيحة", "الشرح"] if self.is_ar else ["#", "Correct Answer", "Explanation"]

        # للعربي: أول عمود منطقي يكون في أقصى اليمين
        if self.is_ar:
            col_x_right = [self.page_w - self.margin]
            for w in col_widths[:-1]:
                col_x_right.append(col_x_right[-1] - w)
            col_x_left = [x - w for x, w in zip(col_x_right, col_widths)]
        else:
            col_x_left = [self.margin]
            for w in col_widths[:-1]:
                col_x_left.append(col_x_left[-1] + w)
            col_x_right = [x + w for x, w in zip(col_x_left, col_widths)]

        row_font_size = 9.5
        pad = 5

        def draw_row(values, is_header=False):
            font = self.font_bold if is_header else self.font_regular
            # التفاف كل خلية أولاً لتحديد ارتفاع الصف
            wrapped_cols = []
            for val, w in zip(values, col_widths):
                wrapped_cols.append(_wrap_line(str(val), font, row_font_size, w - 2 * pad, self.is_ar))
            n_lines = max(len(w) for w in wrapped_cols)
            row_h = n_lines * (row_font_size + 4) + 2 * pad
            self._ensure_space(row_h)

            top_y = self.y
            if is_header:
                self.c.setFillColor(HexColor("#D9E2F3"))
                self.c.rect(self.margin, top_y - row_h, total_w, row_h, fill=1, stroke=0)

            self.c.setStrokeColor(HexColor("#BBBBBB"))
            self.c.rect(self.margin, top_y - row_h, total_w, row_h, fill=0, stroke=1)
            for x_left in col_x_left[1:]:
                self.c.line(x_left, top_y, x_left, top_y - row_h)

            self.c.setFont(font, row_font_size)
            self.c.setFillColor(HexColor("#111111"))
            for lines, x_left, x_right in zip(wrapped_cols, col_x_left, col_x_right):
                line_y = top_y - pad - row_font_size
                for line in lines:
                    draw_text = _shape(line, self.is_ar)
                    if self.is_ar:
                        self.c.drawRightString(x_right - pad, line_y, draw_text)
                    else:
                        self.c.drawString(x_left + pad, line_y, draw_text)
                    line_y -= (row_font_size + 4)
            self.y = top_y - row_h

        draw_row(headers, is_header=True)
        for idx, q in enumerate(self.questions, 1):
            options = q.get("options") or []
            correct_idx = q.get("correct_option_id")
            try:
                correct_idx = int(correct_idx)
            except (TypeError, ValueError):
                correct_idx = -1
            correct_letter = self.letters[correct_idx] if 0 <= correct_idx < len(self.letters) else "-"
            correct_text = options[correct_idx] if 0 <= correct_idx < len(options) else "-"
            explanation = str(q.get("explanation") or "").strip() or ("لا يوجد شرح" if self.is_ar else "No explanation")
            draw_row([str(idx), f"{correct_letter}) {correct_text}", explanation])

    def render(self) -> bytes:
        self._render_header()
        self._render_questions()
        self._render_answer_table()
        self._draw_footer()
        self.c.save()
        self.buf.seek(0)
        return self.buf.getvalue()


def build_quiz_pdf(title: str, questions: List[Dict[str, Any]]) -> bytes:
    """يبني ملف PDF جميل التنسيق للكويز، مع اتجاه تلقائي حسب اللغة (يتطلب خطوط assets/fonts للعربي)."""
    if not questions:
        raise ExportError("لا توجد أسئلة لتصديرها.")
    is_ar = detect_language(questions) == "ar"
    renderer = _QuizPDFRenderer(title, questions, is_ar)
    return renderer.render()
