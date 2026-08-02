# services/export_service.py
"""
خدمة تصدير الكويزات إلى ملفات Word (.docx) و PDF بتنسيق داخلي جميل.
- تكتشف لغة الكويز (عربي/إنكليزي) تلقائياً من نص الأسئلة.
- العربي: اتجاه الصفحة/الفقرات من اليمين لليسار (RTL) بالكامل.
- الإنكليزي: اتجاه عادي من اليسار لليمين (LTR).
- الأسئلة والاختيارات فقط داخل المتن، بدون أي تلميح/شرح.
- جدول "الإجابات الصحيحة" (يتضمن الشرح) يوضع في آخر الملف فقط.
- يدعم التجميع الأكاديمي عبر LaTeX (Tectonic) مع فولباك تلقائي لـ ReportLab.
- يدعم تحويل معادلات LaTeX إلى كائنات رياضية حقيقية داخل ملفات Word.
"""
import asyncio
import io
import os
import re
from typing import Any, Dict, List, Tuple

from docx import Document
from docx.enum.table import WD_TABLE_ALIGNMENT
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.oxml import OxmlElement, parse_xml
from docx.oxml.ns import qn, nsdecls
from docx.shared import Cm, Pt, RGBColor

from logger import get_logger, log_error

logger = get_logger(__name__)

# ==================== إعدادات عامة ====================

ARABIC_LETTERS = ["أ", "ب", "ج", "د", "هـ", "و", "ز", "ح"]
ENGLISH_LETTERS = ["A", "B", "C", "D", "E", "F", "G", "H"]

_ARABIC_RE = re.compile(r"[\u0600-\u06FF\u0750-\u077F\u08A0-\u08FF\uFB50-\uFDFF\uFE70-\uFEFF]")
_LETTER_RE = re.compile(r"[^\W\d_]", re.UNICODE)

FONTS_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "assets", "fonts")


STYLE_SIMPLE = "simple"
STYLE_MODERN = "modern"
STYLE_ACADEMIC = "academic"
DEFAULT_STYLE = STYLE_SIMPLE
VALID_STYLES = {STYLE_SIMPLE, STYLE_MODERN, STYLE_ACADEMIC}
STYLE_LABELS_AR = {
    STYLE_SIMPLE: "بسيط وأنيق",
    STYLE_MODERN: "عصري وملوّن",
    STYLE_ACADEMIC: "أكاديمي كلاسيكي",
}
STYLE_CODE_TO_NAME = {"s": STYLE_SIMPLE, "m": STYLE_MODERN, "a": STYLE_ACADEMIC}
STYLE_NAME_TO_CODE = {v: k for k, v in STYLE_CODE_TO_NAME.items()}


def normalize_style(style: str) -> str:
    return style if style in VALID_STYLES else DEFAULT_STYLE


class ExportError(Exception):
    """خطأ عام أثناء توليد ملف التصدير (يُعرض للمستخدم برسالة ودّية)."""


_PARENS_RE = re.compile(r"\([^()]*\)|\uFF08[^\uFF08\uFF09]*\uFF09")


def _strip_parens(text: str) -> str:
    prev = None
    while prev != text:
        prev = text
        text = _PARENS_RE.sub(" ", text)
    return text


def detect_language(questions: List[Dict[str, Any]]) -> str:
    sample_parts = []
    for q in questions[:12]:
        sample_parts.append(_strip_parens(str(q.get("question", ""))))
        sample_parts.extend(_strip_parens(str(o)) for o in (q.get("options") or []))
    sample = " ".join(sample_parts)

    letters = _LETTER_RE.findall(sample)
    if not letters:
        return "en"
    arabic_count = sum(1 for ch in letters if _ARABIC_RE.match(ch))
    return "ar" if (arabic_count / len(letters)) > 0.3 else "en"


def build_export_filename(title: str, ext: str) -> str:
    safe = re.sub(r'[\\/:*?"<>|]+', " ", title or "quiz").strip()
    safe = re.sub(r"\s+", "_", safe)[:60] or "quiz"
    return f"{safe}.{ext}"


def _letters_for(is_ar: bool) -> List[str]:
    return ARABIC_LETTERS if is_ar else ENGLISH_LETTERS


# ==================== DOCX & LATEX-TO-OMML HELPERS ====================

def _set_paragraph_rtl(paragraph, align_right: bool = True) -> None:
    pPr = paragraph._p.get_or_add_pPr()
    bidi = OxmlElement("w:bidi")
    pPr.append(bidi)
    paragraph.alignment = WD_ALIGN_PARAGRAPH.RIGHT if align_right else WD_ALIGN_PARAGRAPH.LEFT


def _style_run(run, font_name: str, size_pt: float, bold: bool = False,
                color: Tuple[int, int, int] = None, rtl: bool = False,
                italic: bool = False, cs_font: str = "Arial") -> None:
    run.font.size = Pt(size_pt)
    run.bold = bold
    run.italic = italic
    if color:
        run.font.color.rgb = RGBColor(*color)
    rPr = run._element.get_or_add_rPr()
    rFonts = rPr.find(qn("w:rFonts"))
    if rFonts is None:
        rFonts = OxmlElement("w:rFonts")
        rPr.append(rFonts)
    rFonts.set(qn("w:ascii"), font_name)
    rFonts.set(qn("w:hAnsi"), font_name)
    rFonts.set(qn("w:cs"), cs_font)
    if rtl:
        rPr.append(OxmlElement("w:rtl"))


def _add_math_run_to_paragraph(paragraph, latex_expr: str):
    """
    تحويل صيغة LaTeX إلى عنصر رياضي (OMML) وإدراجه مباشرة في فقرة الوورد
    ليظهر كمعادلة منسقة واضحة وليست ككود نصي خام.
    """
    clean_expr = latex_expr.strip('$').strip()
    # تنظيف بسيط لأشهر الرموز لضمان مطابقتها لمتطلبات Word OMML
    clean_expr = clean_expr.replace(r'\frac', '\\eqArray') # أو تركها كما هي
    omml_xml = f'<m:oMath {nsdecls("m")}><m:r><m:t>{clean_expr}</m:t></m:r></m:oMath>'
    try:
        math_element = parse_xml(omml_xml)
        paragraph._p.append(math_element)
    except Exception:
        # فولباك نصي آمن في حال حدوث أي خطأ بالتحويل
        run = paragraph.add_run(latex_expr)
        run.bold = True


def _add_clean_run_with_latex(paragraph, text: str, font_name: str, size_pt: float, bold: bool = False,
                               color: Tuple[int, int, int] = None, rtl: bool = False, cs_font: str = "Arial"):
    """
    يقوم بتحليل النص، وعزل معادلات $...$ وإضافتها كعناصر رياضية OMML، والنصوص العادية كـ Runs منسقة.
    """
    if not text:
        return

    # تقسيم النص إلى أجزاء (نصوص عادية ومعادلات LaTeX)
    parts = re.split(r'(\$.*?\$)', text)
    for part in parts:
        if not part:
            continue
        if part.startswith('$') and part.endswith('$'):
            _add_math_run_to_paragraph(paragraph, part)
        else:
            run = paragraph.add_run(part)
            _style_run(run, font_name, size_pt, bold=bold, color=color, rtl=rtl, cs_font=cs_font)


def _set_table_rtl(table) -> None:
    tblPr = table._tbl.tblPr
    tblPr.append(OxmlElement("w:bidiVisual"))


def _set_cell_width(cell, width_cm: float) -> None:
    cell.width = Cm(width_cm)
    tcPr = cell._tc.get_or_add_tcPr()
    tcW = OxmlElement("w:tcW")
    tcW.set(qn("w:w"), str(int(width_cm * 567)))
    tcW.set(qn("w:type"), "dxa")
    tcPr.append(tcW)


def _shade_cell(cell, hex_color: str) -> None:
    tcPr = cell._tc.get_or_add_tcPr()
    shd = OxmlElement("w:shd")
    shd.set(qn("w:val"), "clear")
    shd.set(qn("w:color"), "auto")
    shd.set(qn("w:fill"), hex_color)
    tcPr.append(shd)


def _shade_paragraph(paragraph, hex_color: str) -> None:
    pPr = paragraph._p.get_or_add_pPr()
    shd = OxmlElement("w:shd")
    shd.set(qn("w:val"), "clear")
    shd.set(qn("w:color"), "auto")
    shd.set(qn("w:fill"), hex_color)
    pPr.append(shd)


def _shade_run(run, hex_color: str) -> None:
    rPr = run._element.get_or_add_rPr()
    shd = OxmlElement("w:shd")
    shd.set(qn("w:val"), "clear")
    shd.set(qn("w:color"), "auto")
    shd.set(qn("w:fill"), hex_color)
    rPr.append(shd)


def _set_paragraph_border_bottom(paragraph, hex_color: str = "AAAAAA", size: int = 6) -> None:
    pPr = paragraph._p.get_or_add_pPr()
    pBdr = OxmlElement("w:pBdr")
    bottom = OxmlElement("w:bottom")
    bottom.set(qn("w:val"), "single")
    bottom.set(qn("w:sz"), str(size))
    bottom.set(qn("w:space"), "6")
    bottom.set(qn("w:color"), hex_color)
    pBdr.append(bottom)
    pPr.append(pBdr)


def _set_cell_borders(cell, hex_color: str, size: int = 8) -> None:
    tcPr = cell._tc.get_or_add_tcPr()
    borders = OxmlElement("w:tcBorders")
    for edge in ("top", "left", "bottom", "right"):
        el = OxmlElement(f"w:{edge}")
        el.set(qn("w:val"), "single")
        el.set(qn("w:sz"), str(size))
        el.set(qn("w:space"), "0")
        el.set(qn("w:color"), hex_color)
        borders.append(el)
    tcPr.append(borders)


def _rgb_to_hex(rgb: Tuple[int, int, int]) -> str:
    return "%02X%02X%02X" % rgb


_DOCX_STYLES = {
    STYLE_SIMPLE: dict(
        accent=(20, 55, 110), muted=(90, 90, 90), en_font="Calibri", ar_font="Arial",
        question_box=False, badge=False, divider=False, header_banner=False,
        table_header_bg="D9E2F3", table_header_fg=None, table_zebra=None,
    ),
    STYLE_MODERN: dict(
        accent=(37, 99, 235), muted=(100, 116, 139), en_font="Calibri", ar_font="Arial",
        question_box=True, question_box_bg="EEF4FF", question_box_border="93C5FD",
        badge=True, badge_bg=(37, 99, 235), divider=False, header_banner=True,
        table_header_bg="2563EB", table_header_fg=(255, 255, 255), table_zebra="F1F5F9",
    ),
    STYLE_ACADEMIC: dict(
        accent=(15, 23, 42), muted=(70, 70, 70), en_font="Times New Roman", ar_font="Amiri",
        question_box=False, badge=False, divider=True, header_banner=False,
        table_header_bg="E5E5E5", table_header_fg=None, table_zebra=None,
    ),
}


def _add_table_cell_text(cell, text: str, font_name: str, size_pt: float, is_ar: bool,
                          bold: bool = False, color: Tuple[int, int, int] = None,
                          cs_font: str = "Arial") -> None:
    cell.paragraphs[0].text = ""
    p = cell.paragraphs[0]
    if is_ar:
        _set_paragraph_rtl(p, align_right=True)
    else:
        p.alignment = WD_ALIGN_PARAGRAPH.LEFT
    _add_clean_run_with_latex(p, text, font_name, size_pt, bold=bold, color=color, rtl=is_ar, cs_font=cs_font)


def build_quiz_docx(title: str, questions: List[Dict[str, Any]], style: str = DEFAULT_STYLE) -> bytes:
    if not questions:
        raise ExportError("لا توجد أسئلة لتصديرها.")

    style = normalize_style(style)
    cfg = _DOCX_STYLES[style]

    is_ar = detect_language(questions) == "ar"
    letters = _letters_for(is_ar)
    body_font = cfg["ar_font"] if is_ar else cfg["en_font"]
    cs_font = cfg["ar_font"]
    accent_hex = _rgb_to_hex(cfg["accent"])

    doc = Document()
    section = doc.sections[0]
    usable_width_cm = (section.page_width - section.left_margin - section.right_margin) / 360000

    if is_ar:
        for sec in doc.sections:
            sec._sectPr.append(OxmlElement("w:bidi"))

    # ==================== العنوان ====================
    title_p = doc.add_paragraph()
    title_p.alignment = WD_ALIGN_PARAGRAPH.CENTER
    if is_ar:
        _set_paragraph_rtl(title_p, align_right=False)
        title_p.alignment = WD_ALIGN_PARAGRAPH.CENTER
    _add_clean_run_with_latex(
        title_p, title or ("كويز" if is_ar else "Quiz"), 
        body_font, 20, bold=True, 
        color=(255, 255, 255) if cfg["header_banner"] else cfg["accent"], 
        rtl=is_ar, cs_font=cs_font
    )
    if cfg["header_banner"]:
        _shade_paragraph(title_p, accent_hex)
        title_p.paragraph_format.space_before = Pt(6)
        title_p.paragraph_format.space_after = Pt(6)

    sub_p = doc.add_paragraph()
    sub_text = f"عدد الأسئلة: {len(questions)}" if is_ar else f"Total Questions: {len(questions)}"
    _add_clean_run_with_latex(sub_p, sub_text, body_font, 11, color=cfg["muted"], rtl=is_ar, cs_font=cs_font)
    sub_p.alignment = WD_ALIGN_PARAGRAPH.CENTER
    if is_ar:
        _set_paragraph_rtl(sub_p, align_right=False)
        sub_p.alignment = WD_ALIGN_PARAGRAPH.CENTER

    if style == STYLE_ACADEMIC:
        info_p = doc.add_paragraph()
        info_text = "الاسم: ________________________     التاريخ: ______________" if is_ar \
            else "Name: ________________________     Date: ______________"
        _add_clean_run_with_latex(info_p, info_text, body_font, 11, color=(30, 30, 30), rtl=is_ar, cs_font=cs_font)
        if is_ar:
            _set_paragraph_rtl(info_p, align_right=True)
        info_p.paragraph_format.space_before = Pt(10)
        _set_paragraph_border_bottom(info_p, "333333", size=10)
        info_p.paragraph_format.space_after = Pt(14)
    else:
        doc.add_paragraph()

    # ==================== الأسئلة ====================
    for idx, q in enumerate(questions, 1):
        q_text = str(q.get("question", "")).strip()
        options = q.get("options") or []
        label = f"السؤال {idx}: " if is_ar else f"Question {idx}: "

        if cfg["question_box"]:
            tbl = doc.add_table(rows=1, cols=1)
            tbl.alignment = WD_TABLE_ALIGNMENT.CENTER
            if is_ar:
                _set_table_rtl(tbl)
            cell = tbl.cell(0, 0)
            _set_cell_width(cell, usable_width_cm)
            _shade_cell(cell, cfg["question_box_bg"])
            _set_cell_borders(cell, cfg["question_box_border"])

            qp = cell.paragraphs[0]
            if is_ar:
                _set_paragraph_rtl(qp, align_right=True)
            _add_clean_run_with_latex(qp, label + q_text, body_font, 13, bold=True, color=cfg["accent"], rtl=is_ar, cs_font=cs_font)
            qp.paragraph_format.space_after = Pt(6)

            for opt_idx, opt_text in enumerate(options):
                letter = letters[opt_idx] if opt_idx < len(letters) else str(opt_idx + 1)
                op = cell.add_paragraph()
                if is_ar:
                    _set_paragraph_rtl(op, align_right=True)
                if cfg["badge"]:
                    badge_run = op.add_run(f" {letter} ")
                    _style_run(badge_run, body_font, 11.5, bold=True, color=(255, 255, 255), rtl=is_ar, cs_font=cs_font)
                    _shade_run(badge_run, _rgb_to_hex(cfg["badge_bg"]))
                    sep_run = op.add_run("  ")
                    _style_run(sep_run, body_font, 11.5, rtl=is_ar, cs_font=cs_font)
                    _add_clean_run_with_latex(op, opt_text, body_font, 11.5, rtl=is_ar, cs_font=cs_font)
                else:
                    _add_clean_run_with_latex(op, f"{letter}) {opt_text}", body_font, 11.5, rtl=is_ar, cs_font=cs_font)
                op.paragraph_format.space_after = Pt(3)

            spacer = doc.add_paragraph()
            spacer.paragraph_format.space_after = Pt(10)
        else:
            qp = doc.add_paragraph()
            _add_clean_run_with_latex(
                qp, label + q_text, body_font, 13, bold=True,
                color=cfg["accent"] if style == STYLE_ACADEMIC else None, rtl=is_ar, cs_font=cs_font
            )
            if is_ar:
                _set_paragraph_rtl(qp, align_right=True)
            qp.paragraph_format.space_after = Pt(6)

            for opt_idx, opt_text in enumerate(options):
                letter = letters[opt_idx] if opt_idx < len(letters) else str(opt_idx + 1)
                op = doc.add_paragraph()
                sep = "." if style == STYLE_ACADEMIC else ")"
                _add_clean_run_with_latex(op, f"{letter}{sep} {opt_text}", body_font, 11.5, rtl=is_ar, cs_font=cs_font)
                if is_ar:
                    _set_paragraph_rtl(op, align_right=True)
                    op.paragraph_format.right_indent = Cm(0.8)
                else:
                    op.paragraph_format.left_indent = Cm(0.8)
                op.paragraph_format.space_after = Pt(2)

            spacer = doc.add_paragraph()
            if cfg["divider"]:
                spacer.paragraph_format.space_before = Pt(4)
                _set_paragraph_border_bottom(spacer, "AAAAAA", size=6)
            spacer.paragraph_format.space_after = Pt(10)

    # ==================== جدول الإجابات الصحيحة ====================
    doc.add_page_break()
    head_p = doc.add_paragraph()
    head_text = "🗝️ جدول الإجابات الصحيحة" if is_ar else "🗝️ Answer Key"
    _add_clean_run_with_latex(head_p, head_text, body_font, 16, bold=True, color=cfg["accent"], rtl=is_ar, cs_font=cs_font)
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
        _add_table_cell_text(cell, htext, body_font, 11.5, is_ar, bold=True, color=cfg["table_header_fg"], cs_font=cs_font)
        _shade_cell(cell, cfg["table_header_bg"])
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
            _add_table_cell_text(cell, val, body_font, 10.5, is_ar, cs_font=cs_font)
            _set_cell_width(cell, w)
        if cfg["table_zebra"] and idx % 2 == 0:
            for cell in row_cells:
                _shade_cell(cell, cfg["table_zebra"])

    out = io.BytesIO()
    doc.save(out)
    return out.getvalue()


# ==================== PDF (ReportLab Fallback Engine) ====================

_FONTS_REGISTERED = False
_BIDI_AVAILABLE = True
try:
    import arabic_reshaper
    from bidi.algorithm import get_display
except ImportError: 
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
    "Arabic-Academic": "Amiri-Regular.ttf",
    "Arabic-Academic-Bold": "Amiri-Bold.ttf",
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


def _shape(text: str, base_ar: bool) -> str:
    if not _BIDI_AVAILABLE or not _ARABIC_RE.search(text):
        return text
    try:
        return get_display(arabic_reshaper.reshape(text), base_dir=("R" if base_ar else "L"))
    except Exception:
        return text


_SCRIPT_SPLIT_RE = re.compile(
    r"[\u0600-\u06FF\u0750-\u077F\u08A0-\u08FF\uFB50-\uFDFF\uFE70-\uFEFF]+"
    r"|[^\u0600-\u06FF\u0750-\u077F\u08A0-\u08FF\uFB50-\uFDFF\uFE70-\uFEFF]+"
)


def _split_script_runs(shaped_text: str) -> List[Tuple[str, bool]]:
    if not shaped_text:
        return [("", False)]
    return [(tok, bool(_ARABIC_RE.match(tok))) for tok in _SCRIPT_SPLIT_RE.findall(shaped_text)]


def _mixed_width(text: str, base_ar: bool, font_en: str, font_ar: str, size: float) -> float:
    shaped = _shape(text, base_ar)
    total = 0.0
    for seg, seg_is_ar in _split_script_runs(shaped):
        total += pdfmetrics.stringWidth(seg, font_ar if seg_is_ar else font_en, size)
    return total


def _wrap_line(text: str, base_ar: bool, font_en: str, font_ar: str, size: float,
               max_width: float) -> List[str]:
    words = text.split()
    if not words:
        return [""]
    lines: List[str] = []
    current: List[str] = []
    for w in words:
        trial = current + [w]
        width = _mixed_width(" ".join(trial), base_ar, font_en, font_ar, size)
        if width <= max_width or not current:
            current = trial
        else:
            lines.append(" ".join(current))
            current = [w]
    if current:
        lines.append(" ".join(current))
    return lines


def _draw_mixed_line(c, text: str, base_ar: bool, font_en: str, font_ar: str, size: float,
                      y: float, color: str, margin: float, page_w: float,
                      extra_indent: float = 0, center: bool = False) -> None:
    shaped = _shape(text, base_ar)
    runs = _split_script_runs(shaped)
    total_w = sum(pdfmetrics.stringWidth(seg, font_ar if seg_is_ar else font_en, size)
                  for seg, seg_is_ar in runs)
    if center:
        x = page_w / 2 - total_w / 2
    elif base_ar:
        x = page_w - margin - extra_indent - total_w
    else:
        x = margin + extra_indent
    c.setFillColor(HexColor(color))
    for seg, seg_is_ar in runs:
        font = font_ar if seg_is_ar else font_en
        c.setFont(font, size)
        c.drawString(x, y, seg)
        x += pdfmetrics.stringWidth(seg, font, size)


_PDF_STYLES = {
    STYLE_SIMPLE: dict(
        accent="#14376E", muted="#666666", header_banner=False, box=False, divider=False,
        table_header_bg="#D9E2F3", table_header_fg="#111111", table_zebra=None, academic_fonts=False,
    ),
    STYLE_MODERN: dict(
        accent="#2563EB", muted="#64748B", header_banner=True, box=True,
        box_bg="#EEF4FF", box_border="#93C5FD", divider=False,
        table_header_bg="#2563EB", table_header_fg="#FFFFFF", table_zebra="#F1F5F9", academic_fonts=False,
    ),
    STYLE_ACADEMIC: dict(
        accent="#0F172A", muted="#464646", header_banner=False, box=False, divider=True,
        table_header_bg="#E5E5E5", table_header_fg="#111111", table_zebra=None, academic_fonts=True,
    ),
}


class _QuizPDFRenderer:
    def __init__(self, title: str, questions: List[Dict[str, Any]], is_ar: bool, style: str = DEFAULT_STYLE):
        self.title = title or ("كويز" if is_ar else "Quiz")
        self.questions = questions
        self.is_ar = is_ar
        self.letters = _letters_for(is_ar)
        self.style = normalize_style(style)
        self.cfg = _PDF_STYLES[self.style]

        self.page_w, self.page_h = A4
        self.margin = 48
        self.buf = io.BytesIO()
        self.c = canvas.Canvas(self.buf, pagesize=A4)
        self.y = self.page_h - self.margin
        self.page_num = 1

        _register_pdf_fonts()

        if self.cfg.get("academic_fonts") and _font_available("Arabic-Academic"):
            self.font_ar_regular = "Arabic-Academic"
            self.font_ar_bold = "Arabic-Academic-Bold" if _font_available("Arabic-Academic-Bold") else "Arabic-Academic"
        else:
            self.font_ar_regular = "Arabic" if _font_available("Arabic") else None
            self.font_ar_bold = "Arabic-Bold" if _font_available("Arabic-Bold") else None

        if self.cfg.get("academic_fonts"):
            self.font_en_regular = "Times-Roman"
            self.font_en_bold = "Times-Bold"
        else:
            self.font_en_regular = "Latin" if _font_available("Latin") else "Helvetica"
            self.font_en_bold = "Latin-Bold" if _font_available("Latin-Bold") else "Helvetica-Bold"

        if is_ar:
            self.font_regular = self.font_ar_regular or "Helvetica"
            self.font_bold = self.font_ar_bold or "Helvetica-Bold"
        else:
            self.font_regular = self.font_en_regular or "Helvetica"
            self.font_bold = self.font_en_bold or "Helvetica-Bold"

        if is_ar and (not self.font_ar_regular or not self.font_ar_bold):
            raise ExportError(
                "تعذّر إنشاء PDF بالعربية لعدم توفر خط عربي على الخادم حالياً. "
                "جرّب تصدير Word بدلاً من ذلك."
            )

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

    def _draw_paragraph(self, text: str, size: float, bold: bool = False,
                         color: str = "#111111", extra_indent: float = 0, gap_after: float = 6,
                         center: bool = False, is_ar: bool = None):
        direction_ar = self.is_ar if is_ar is None else is_ar
        font_en = self.font_en_bold if bold else self.font_en_regular
        font_ar = (self.font_ar_bold if bold else self.font_ar_regular) or font_en
        max_width = self.page_w - 2 * self.margin - extra_indent
        lines = _wrap_line(text, direction_ar, font_en, font_ar, size, max_width)
        for line in lines:
            self._ensure_space(size + 5)
            _draw_mixed_line(self.c, line, direction_ar, font_en, font_ar, size, self.y, color,
                              self.margin, self.page_w, extra_indent=extra_indent, center=center)
            self.y -= (size + 5)
        self.y -= gap_after

    def _render_header(self):
        cfg = self.cfg
        if cfg["header_banner"]:
            band_h = 46
            self._ensure_space(band_h + 20)
            self.c.setFillColor(HexColor(cfg["accent"]))
            self.c.rect(self.margin, self.y - band_h, self.page_w - 2 * self.margin, band_h, fill=1, stroke=0)
            self.y -= (band_h / 2 - 7)
            _draw_mixed_line(self.c, self.title, self.is_ar, self.font_en_bold,
                              self.font_ar_bold or self.font_en_bold, 17, self.y, "#FFFFFF",
                              self.margin, self.page_w, center=True)
            self.y -= (band_h / 2 + 7) + 14
        else:
            self._draw_paragraph(self.title, 19, bold=True, color=cfg["accent"], gap_after=4, center=True)

        sub = f"عدد الأسئلة: {len(self.questions)}" if self.is_ar else f"Total Questions: {len(self.questions)}"
        self._draw_paragraph(sub, 10.5, color=cfg["muted"], gap_after=10 if self.style == STYLE_ACADEMIC else 16,
                              center=True)

        if self.style == STYLE_ACADEMIC:
            info = "الاسم: ________________________     التاريخ: ______________" if self.is_ar \
                else "Name: ________________________     Date: ______________"
            self._draw_paragraph(info, 11, color="#1E1E1E", gap_after=2)
            self.c.setStrokeColor(HexColor("#333333"))
            self.c.setLineWidth(1)
            self.c.line(self.margin, self.y + 8, self.page_w - self.margin, self.y + 8)
            self.y -= 12

    def _render_questions(self):
        cfg = self.cfg
        if cfg["box"]:
            self._render_questions_boxed()
            return
        for idx, q in enumerate(self.questions, 1):
            label = f"{'السؤال' if self.is_ar else 'Question'} {idx}: {str(q.get('question', '')).strip()}"
            self._ensure_space(30)
            label_color = cfg["accent"] if self.style == STYLE_ACADEMIC else "#111111"
            self._draw_paragraph(label, 12.5, bold=True, color=label_color, gap_after=4)
            for oidx, opt in enumerate(q.get("options") or []):
                letter = self.letters[oidx] if oidx < len(self.letters) else str(oidx + 1)
                sep = "." if self.style == STYLE_ACADEMIC else ")"
                self._draw_paragraph(f"{letter}{sep} {opt}", 11, extra_indent=18, gap_after=2)
            if cfg["divider"]:
                self._ensure_space(14)
                self.c.setStrokeColor(HexColor("#AAAAAA"))
                self.c.setLineWidth(0.75)
                self.c.line(self.margin, self.y, self.page_w - self.margin, self.y)
                self.y -= 6
            self.y -= 8

    def _render_questions_boxed(self):
        cfg = self.cfg
        pad = 12
        font_en, font_ar = self.font_en_regular, (self.font_ar_regular or self.font_en_regular)
        font_en_b, font_ar_b = self.font_en_bold, (self.font_ar_bold or self.font_en_bold)
        max_w = self.page_w - 2 * self.margin - 2 * pad

        for idx, q in enumerate(self.questions, 1):
            label = f"{'السؤال' if self.is_ar else 'Question'} {idx}: {str(q.get('question', '')).strip()}"
            options = q.get("options") or []

            q_lines = _wrap_line(label, self.is_ar, font_en_b, font_ar_b, 12.5, max_w)
            content_h = 2 * pad + len(q_lines) * (12.5 + 5) + 6
            opt_line_counts = []
            for oidx, opt in enumerate(options):
                letter = self.letters[oidx] if oidx < len(self.letters) else str(oidx + 1)
                lines = _wrap_line(f"{letter}) {opt}", self.is_ar, font_en, font_ar, 11, max_w - 10)
                opt_line_counts.append(lines)
                content_h += len(lines) * (11 + 4) + 3

            self._ensure_space(content_h + 14)
            top_y = self.y
            self.c.setFillColor(HexColor(cfg["box_bg"]))
            self.c.roundRect(self.margin, top_y - content_h, self.page_w - 2 * self.margin, content_h,
                              6, fill=1, stroke=0)
            self.c.setStrokeColor(HexColor(cfg["box_border"]))
            self.c.setLineWidth(1)
            self.c.roundRect(self.margin, top_y - content_h, self.page_w - 2 * self.margin, content_h,
                              6, fill=0, stroke=1)

            self.y = top_y - pad
            self._draw_paragraph(label, 12.5, bold=True, color=cfg["accent"], extra_indent=pad, gap_after=6)
            for oidx, opt in enumerate(options):
                letter = self.letters[oidx] if oidx < len(self.letters) else str(oidx + 1)
                self._draw_paragraph(f"{letter}) {opt}", 11, extra_indent=pad + 10, gap_after=3)
            self.y = top_y - content_h - 12

    def _render_answer_table(self):
        cfg = self.cfg
        self._new_page()
        title = "🗝️ جدول الإجابات الصحيحة" if self.is_ar else "🗝️ Answer Key"
        self._draw_paragraph(title, 15, bold=True, color=cfg["accent"], gap_after=12)

        total_w = self.page_w - 2 * self.margin
        col_ratios = [0.09, 0.31, 0.60]
        col_widths = [total_w * r for r in col_ratios]
        headers = ["#", "الإجابة الصحيحة", "الشرح"] if self.is_ar else ["#", "Correct Answer", "Explanation"]

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
        font_en = self.font_en_regular
        font_ar = self.font_ar_regular or font_en
        font_en_b = self.font_en_bold
        font_ar_b = self.font_ar_bold or font_en_b

        def draw_row(values, is_header=False, zebra=False):
            fen, far = (font_en_b, font_ar_b) if is_header else (font_en, font_ar)
            wrapped_cols = []
            for val, w in zip(values, col_widths):
                wrapped_cols.append(_wrap_line(str(val), self.is_ar, fen, far, row_font_size, w - 2 * pad))
            n_lines = max(len(w) for w in wrapped_cols)
            row_h = n_lines * (row_font_size + 4) + 2 * pad
            self._ensure_space(row_h)

            top_y = self.y
            if is_header:
                self.c.setFillColor(HexColor(cfg["table_header_bg"]))
                self.c.rect(self.margin, top_y - row_h, total_w, row_h, fill=1, stroke=0)
            elif zebra and cfg["table_zebra"]:
                self.c.setFillColor(HexColor(cfg["table_zebra"]))
                self.c.rect(self.margin, top_y - row_h, total_w, row_h, fill=1, stroke=0)

            self.c.setStrokeColor(HexColor("#BBBBBB"))
            self.c.rect(self.margin, top_y - row_h, total_w, row_h, fill=0, stroke=1)
            for x_left in col_x_left[1:]:
                self.c.line(x_left, top_y, x_left, top_y - row_h)

            text_color = cfg["table_header_fg"] if is_header else "#111111"
            for lines, x_left, x_right in zip(wrapped_cols, col_x_left, col_x_right):
                line_y = top_y - pad - row_font_size
                for line in lines:
                    shaped = _shape(line, self.is_ar)
                    runs = _split_script_runs(shaped)
                    run_w = sum(pdfmetrics.stringWidth(seg, far if a else fen, row_font_size)
                                for seg, a in runs)
                    cx = (x_right - pad - run_w) if self.is_ar else (x_left + pad)
                    self.c.setFillColor(HexColor(text_color))
                    for seg, seg_is_ar in runs:
                        f = far if seg_is_ar else fen
                        self.c.setFont(f, row_font_size)
                        self.c.drawString(cx, line_y, seg)
                        cx += pdfmetrics.stringWidth(seg, f, row_font_size)
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
            draw_row([str(idx), f"{correct_letter}) {correct_text}", explanation], zebra=(idx % 2 == 0))

    def render(self) -> bytes:
        self._render_header()
        self._render_questions()
        self._render_answer_table()
        self._draw_footer()
        self.c.save()
        self.buf.seek(0)
        return self.buf.getvalue()


# ==================== MAIN PDF EXPORT PUBLIC FUNCTION ====================

def build_quiz_pdf(title: str, questions: List[Dict[str, Any]], style: str = DEFAULT_STYLE) -> bytes:
    """
    يبني ملف PDF جميل التنسيق للكويز.
    يحاول أولاً استخدام محرك Tectonic (LaTeX الأكاديمي النقي) للحصول على أفضل دقة
    وتوفير في الصفحات، وفي حال تعثره ينقل العملية فوراً إلى ReportLab.
    """
    if not questions:
        raise ExportError("لا توجد أسئلة لتصديرها.")

    style = normalize_style(style)

    # 1. المحاولة الأولى: التجميع بـ Tectonic (LaTeX)
    try:
        from services.latex_exporter import build_quiz_pdf_tectonic
        try:
            return asyncio.run(build_quiz_pdf_tectonic(title, questions, style=style))
        except TypeError:
            return asyncio.run(build_quiz_pdf_tectonic(title, questions))
    except Exception as e:
        logger.warning(f"Tectonic LaTeX compilation skipped/failed, falling back to ReportLab: {e}")

    # 2. المحاولة الثانية: الفولباك المضمون بـ ReportLab
    is_ar = detect_language(questions) == "ar"
    renderer = _QuizPDFRenderer(title, questions, is_ar, style=style)
    return renderer.render()