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
from typing import Any, Dict, List, Optional, Tuple

from docx import Document
from docx.enum.table import WD_TABLE_ALIGNMENT
from docx.enum.section import WD_SECTION
from docx.enum.text import WD_ALIGN_PARAGRAPH, WD_BREAK
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Cm, Pt, RGBColor

from logger import get_logger, log_error
from services.image_quiz_renderer import render_question_image
from services.latex_text import latex_to_plain
from services.print_quiz_renderer import (
    WIDTH as PRINT_WIDTH, chunk_height_px, chunk_width_px, iter_print_blocks, plan_print_blocks,
    render_print_chunk,
)
from services.export_branding import branding_aspect, branding_png
from services.export_layout import MAX_SPREAD_GAP, pick_max_per_page, plan_column_pages, plan_pages, spread_gap
from services.export_options import (
    DRAFT_HEIGHTS_PT, ExportOptions, ExportOptionsError, MAX_PER_PAGE, exam_meta_line, resolve_draft_style,
    student_line, teacher_text,
)

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
# أكواد قصيرة (حرف واحد) للستايل - مستخدمة بـ callback_data تبع تيليغرام لأنو محدود بـ 64 بايت
STYLE_CODE_TO_NAME = {"s": STYLE_SIMPLE, "m": STYLE_MODERN, "a": STYLE_ACADEMIC}
STYLE_NAME_TO_CODE = {v: k for k, v in STYLE_CODE_TO_NAME.items()}


def normalize_style(style: str) -> str:
    return style if style in VALID_STYLES else DEFAULT_STYLE


class ExportError(Exception):
    """خطأ عام أثناء توليد ملف التصدير (يُعرض للمستخدم برسالة ودّية)."""


def coerce_options(options) -> ExportOptions:
    """None/dict/ExportOptions => ExportOptions صالح. قيمة فاسدة => ExportError برسالة عربية."""
    if isinstance(options, ExportOptions):
        return options
    try:
        return ExportOptions.from_dict(options)
    except ExportOptionsError as exc:
        raise ExportError(str(exc)) from exc


_PARENS_RE = re.compile(r"\([^()]*\)|\uFF08[^\uFF08\uFF09]*\uFF09")
_LATEX_SPAN_RE = re.compile(r"\$[^$]+\$")


def _is_math_question(q: Dict[str, Any]) -> bool:
    """يكتشف هل السؤال من نمط الكويز المصوّر LaTeX - عبر علم is_math الصريح، أو
    كخط دفاع ثانٍ لو كويز قديم محفوظ بالمفضلة قبل إضافة هذا العلم: وجود جدول بيانات
    أو مقطع $...$ فعلي بنص السؤال/الخيارات كافٍ لاعتباره سؤالاً رياضياً يحتاج رسماً
    كصورة بدل نص خام."""
    if q.get("is_math"):
        return True
    if q.get("table"):
        return True
    if q.get("matrices"):
        return True
    question = str(q.get("question", ""))
    if _LATEX_SPAN_RE.search(question):
        return True
    return any(_LATEX_SPAN_RE.search(str(opt)) for opt in (q.get("options") or []))


def _strip_parens(text: str) -> str:
    """يشيل أي نص محصور بين قوسين (زي ترجمة أو توضيح) قبل حساب لغة الكويز -
    حتى ترجمة عربية بين قوسين ضمن سؤال إنكليزي (أو العكس) ما تأثر على تحديد
    اللغة الأساسية للمستند كله."""
    prev = None
    while prev != text:
        prev = text
        text = _PARENS_RE.sub(" ", text)
    return text


def _render_math_question_image_bytes(q: Dict[str, Any], idx0: int, total: int, is_ar: bool) -> Optional[bytes]:
    """يرسم صورة السؤال الرياضي (بنفس محرك عرض الكويز الفعلي services/image_quiz_renderer.py)
    لتضمينها بملف التصدير بدل كتابة رموز LaTeX خام كنص. فشل آمن: يرجع None لو تعذّر الرسم
    لأي سبب، فيسقط المستدعي تلقائياً للمسار النصي العادي بدل تعطيل التصدير بأكمله."""
    try:
        return render_question_image(q, idx0, total, is_ar)
    except Exception as e:
        log_error(logger, f"Failed rendering math question image for export (idx={idx0}): {e}", exception=e)
        return None


def detect_language(questions: List[Dict[str, Any]]) -> str:
    """يكتشف لغة الكويز: 'ar' أو 'en' اعتماداً على نسبة الأحرف العربية في عينة من النص
    (بعد تجاهل أي نص بين قوسين، مثل الترجمات أو التوضيحات)."""
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


def detect_text_language(text: str) -> str:
    """يكتشف لغة نص حر (تفريغ محاضرة/ملخص...، وليس أسئلة كويز): 'ar' أو 'en' اعتماداً
    على نسبة الأحرف العربية بعيّنة من بداية النص (بعد تجاهل أي نص بين قوسين، مثل
    ترجمات أو توضيحات مدسوسة داخل النص الأساسي)."""
    sample = _strip_parens(text[:4000])
    letters = _LETTER_RE.findall(sample)
    if not letters:
        return "en"
    arabic_count = sum(1 for ch in letters if _ARABIC_RE.match(ch))
    return "ar" if (arabic_count / len(letters)) > 0.3 else "en"


_MD_BULLET_RE = re.compile(r"^\s*[-*•]\s+(.*)$")
_MD_H1_RE = re.compile(r"^#\s+(.*)$")
_MD_H2_RE = re.compile(r"^#{2,3}\s+(.*)$")


def _parse_markdown_blocks(text: str) -> List[Tuple[str, str]]:
    """يحلّل نص Markdown بسيط (عناوين # / ## أو ### ونقاط - أو * أو •) إلى قائمة
    (نوع, محتوى) جاهزة للعرض بملفات Word/PDF. تُدمج الأسطر المتتالية غير الفارغة
    التي لا تطابق أي نمط عنوان/نقطة بنفس الفقرة، حتى أول سطر فارغ أو عنصر جديد -
    هيك ما تنكسر فقرة واحدة طويلة إلى أسطر منفصلة بالمستند النهائي."""
    blocks: List[Tuple[str, str]] = []
    para_buffer: List[str] = []

    def _flush() -> None:
        if para_buffer:
            blocks.append(("para", " ".join(para_buffer).strip()))
            para_buffer.clear()

    for raw_line in (text or "").splitlines():
        line = raw_line.strip()
        if not line:
            _flush()
            continue
        h2_match = _MD_H2_RE.match(line) if line.startswith("##") else None
        h1_match = _MD_H1_RE.match(line) if (not h2_match and line.startswith("#")) else None
        bullet_match = _MD_BULLET_RE.match(line)
        if h1_match:
            _flush()
            blocks.append(("h1", h1_match.group(1).strip()))
        elif h2_match:
            _flush()
            blocks.append(("h2", h2_match.group(1).strip()))
        elif bullet_match:
            _flush()
            blocks.append(("bullet", bullet_match.group(1).strip()))
        else:
            para_buffer.append(line)
    _flush()
    return blocks


def build_export_filename(title: str, ext: str) -> str:
    """اسم ملف آمن (بدون رموز قد تكسر بعض الأنظمة) مع الحفاظ على العربية."""
    safe = re.sub(r'[\\/:*?"<>|]+', " ", title or "quiz").strip()
    safe = re.sub(r"\s+", "_", safe)[:60] or "quiz"
    return f"{safe}.{ext}"


def _letters_for(is_ar: bool) -> List[str]:
    return ARABIC_LETTERS if is_ar else ENGLISH_LETTERS


def build_quiz_xlsx(title: str, questions: List[Dict[str, Any]],
                    style: str = DEFAULT_STYLE, include_answers: bool = True, options=None) -> bytes:
    """Editable question bank. Student files never contain answer-key fields.

    Mathematical quizzes open on a rendered preview sheet, while source cells
    remain editable. All strings are literal Excel text.
    """
    import json
    from openpyxl import Workbook
    from openpyxl.cell.cell import ILLEGAL_CHARACTERS_RE
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.utils import get_column_letter

    from services.export_options import ExportOptions, ExportOptionsError

    if not questions:
        raise ExportError("لا توجد أسئلة لتصديرها.")
    # ملاحظة: اسم `options` يُستعمل لاحقاً كمتغير حلقة لخيارات السؤال، لذا نحفظ الإعدادات باسم آخر.
    if isinstance(options, ExportOptions):
        sheet_opts = options
    else:
        try:
            sheet_opts = ExportOptions.from_dict(options)
        except ExportOptionsError as exc:
            raise ExportError(str(exc)) from exc
    # عنوان مخصص يستبدل العنوان، ومفتاح الإجابة فقط يحذف عمود الشرح. غير ذلك: كما كان.
    title = sheet_opts.exam_title or title
    with_explanation = include_answers and sheet_opts.answers_mode == "full"
    option_count = max(len(q.get("options") or []) for q in questions)
    if len(questions) > 1_048_575 or option_count > 16_370:
        raise ExportError("حجم الكويز يتجاوز حدود ملف Excel.")

    is_ar = detect_language(questions) == "ar"
    math_preview = any(_is_math_question(q) or (
        include_answers and _is_math_question(_answer_print_question(q, is_ar))
    ) for q in questions)
    wb = Workbook()
    ws = wb.active
    ws.title = "الأسئلة" if is_ar else "Questions"
    info = wb.create_sheet("معلومات الاختبار" if is_ar else "Quiz Info")
    accent = {STYLE_SIMPLE: "334155", STYLE_MODERN: "2563EB",
              STYLE_ACADEMIC: "374151"}[normalize_style(style)]

    def add_row(sheet, values):
        # Setting data_type explicitly prevents Excel formulas in user/AI text.
        # Reject oversized/invalid text rather than letting openpyxl truncate it.
        row = sheet.max_row + 1 if sheet.cell(1, 1).value is not None else 1
        for col, value in enumerate(values, 1):
            cell = sheet.cell(row, col)
            if isinstance(value, str):
                if len(value) > 32767 or ILLEGAL_CHARACTERS_RE.search(value):
                    raise ExportError("أحد النصوص طويل جداً أو يحتوي رموزاً غير صالحة لملف Excel.")
                cell.value = value
                cell.data_type = "s"
            else:
                cell.value = value

    has_table = any(q.get("table") for q in questions)
    has_matrices = any(q.get("matrices") for q in questions)
    headers = (["رقم السؤال", "نص السؤال", "نوع السؤال"] if is_ar else
               ["Question Number", "Question", "Question Type"])
    headers += [f"الخيار {i}" if is_ar else f"Option {i}" for i in range(1, option_count + 1)]
    headers += ["الدرجة" if is_ar else "Points"]
    if has_table:
        headers += ["الجدول (JSON)" if is_ar else "Table (JSON)"]
    if has_matrices:
        headers += ["المصفوفات (JSON)" if is_ar else "Matrices (JSON)"]
    if include_answers:
        headers += (["رقم الإجابة الصحيحة", "الإجابة الصحيحة"] if is_ar else
                    ["Correct Option Number", "Correct Answer"])
        if with_explanation:
            headers += ["الشرح"] if is_ar else ["Explanation"]
    add_row(ws, headers)
    for number, q in enumerate(questions, 1):
        options = q.get("options") or []
        kind = q.get("type") or ("اختيار من متعدد" if is_ar else "Multiple choice")
        values = [number, str(q.get("question") or ""), str(kind)]
        values += [str(o) for o in options] + [""] * (option_count - len(options))
        values += [q.get("points", 1)]
        if has_table:
            values += [json.dumps(q["table"], ensure_ascii=False) if q.get("table") else ""]
        if has_matrices:
            values += [json.dumps(q["matrices"], ensure_ascii=False) if q.get("matrices") else ""]
        if include_answers:
            try:
                correct = int(q.get("correct_option_id"))
            except (ValueError, TypeError):
                correct = -1
            valid = 0 <= correct < len(options)
            values += [correct + 1 if valid else None, str(options[correct]) if valid else ""]
            if with_explanation:
                values += [str(q.get("explanation") or "")]
        add_row(ws, values)

    metadata = ([
        ["المعلومة", "القيمة"], ["عنوان الاختبار", title or "كويز"],
        ["عدد الأسئلة", len(questions)], ["النسخة", "الأستاذ" if include_answers else "الطالب"],
        ["المدة", sheet_opts.duration or "غير محددة"],
        ["ملاحظات", "ترقيم الخيارات يبدأ من 1. الصيغ الرياضية محفوظة كنص قابل للتعديل."],
    ] if is_ar else [
        ["Field", "Value"], ["Quiz Title", title or "Quiz"],
        ["Question Count", len(questions)], ["Version", "Teacher" if include_answers else "Student"],
        ["Duration", sheet_opts.duration or "Not specified"],
        ["Notes", "Option numbers start at 1. Math notation is preserved as editable text."],
    ])
    # حقول ورقة الاختبار الاختيارية تُضاف قبل «ملاحظات» (آخر صف) فقط عند تعبئتها.
    extra_rows = [(label, value) for label, value in (
        ("المادة" if is_ar else "Subject", sheet_opts.subject),
        ("الصف" if is_ar else "Grade", sheet_opts.grade),
        ("التاريخ" if is_ar else "Date", sheet_opts.exam_date),
        ("العلامة الكلية" if is_ar else "Total Marks", sheet_opts.total_marks),
        ("اسم الأستاذ" if is_ar else "Teacher", sheet_opts.teacher_name),
    ) if value]
    metadata[-1:-1] = [[label, value] for label, value in extra_rows]
    for values in metadata:
        add_row(info, values)
    notes_row = len(metadata)
    for sheet in (ws, info):
        sheet.sheet_view.rightToLeft = is_ar
        sheet.freeze_panes = "A2"
        sheet.auto_filter.ref = sheet.dimensions
        for row in sheet:
            for cell in row:
                cell.alignment = Alignment(horizontal="right" if is_ar else "left",
                                           vertical="top", wrap_text=True)
                cell.font = Font(name="Calibri", size=11)
        for cell in sheet[1]:
            cell.fill = PatternFill("solid", fgColor=accent)
            cell.font = Font(name="Calibri", size=11, bold=True, color="FFFFFF")
        sheet.row_dimensions[1].height = 30
        for col in range(1, sheet.max_column + 1):
            sheet.column_dimensions[get_column_letter(col)].width = 28
        sheet.column_dimensions["B"].width = 65
    if math_preview:
        _add_xlsx_math_preview(wb, title, questions, is_ar, include_answers, accent)
        note = ("افتح ورقة العرض الرياضي لقراءة المعادلات. النص الأصلي في ورقة الأسئلة قابل للتعديل؛ "
                "الصور لا تتحدث عند تعديل الخلايا." if is_ar else
                "Read equations in Math Preview. Source text in Questions remains editable; "
                "images do not update when cells are edited.")
        info.cell(notes_row, 2, note)
    _add_xlsx_branding(ws, ws.max_row + 2, "B")
    _add_xlsx_branding(info, info.max_row + 2, "A")
    buf = io.BytesIO()
    wb.save(buf)
    wb.close()
    return buf.getvalue()


def _add_xlsx_branding(sheet, row: int, column: str) -> None:
    """شارة الختام الإجبارية كصورة عائمة تحت آخر صف (لا تدخل بالخلايا فلا تؤثر على الاستيراد)."""
    from openpyxl.drawing.image import Image as XLImage

    img = XLImage(io.BytesIO(branding_png()))
    img.width = 420
    img.height = round(420 * branding_aspect())
    sheet.add_image(img, f"{column}{row}")


def _add_xlsx_math_preview(wb, title, questions, is_ar, include_answers, accent):
    """Lay out bounded print panels across rows, without Excel row-height clipping."""
    import math
    from openpyxl.drawing.image import Image
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.utils import get_column_letter

    sheet = wb.create_sheet("العرض الرياضي" if is_ar else "Math Preview", 0)
    wb.active = 0
    sheet.sheet_view.showGridLines = False
    # Drawing anchors use a left-to-right canvas. Arabic is already shaped
    # inside the PNG, so Excel must not mirror its placement.
    sheet.sheet_view.rightToLeft = False
    sheet.sheet_view.zoomScale = 85
    sheet.sheet_properties.pageSetUpPr.fitToPage = True
    sheet.page_setup.orientation = "portrait"
    sheet.page_setup.paperSize = sheet.PAPERSIZE_A4
    sheet.page_setup.fitToWidth = 1
    sheet.page_setup.fitToHeight = 0
    for col in range(1, 13):
        sheet.column_dimensions[get_column_letter(col)].width = 11
    sheet.merge_cells("A1:L1")
    heading = sheet.cell(1, 1, title or ("كويز" if is_ar else "Quiz"))
    heading.data_type = "s"
    heading.font = Font(name="Calibri", size=18, bold=True, color="FFFFFF")
    heading.fill = PatternFill("solid", fgColor=accent)
    heading.alignment = Alignment(horizontal="right" if is_ar else "left", vertical="center", wrap_text=True)
    sheet.row_dimensions[1].height = 40
    sheet.merge_cells("A2:L2")
    note = ("عرض للقراءة؛ النص القابل للتعديل موجود في ورقة الأسئلة." if is_ar else
            "Reading preview; editable source text is in Questions.")
    sheet.cell(2, 1, note).alignment = Alignment(horizontal="right" if is_ar else "left", wrap_text=True)
    sheet.row_dimensions[2].height = 30
    row = 4

    def add_panels(question, label):
        nonlocal row
        for payload in iter_print_blocks(question, is_ar, label):
            image = Image(io.BytesIO(payload))
            scale = min(1, 950 / image.width)
            image.width = round(image.width * scale)
            image.height = round(image.height * scale)
            # 15 points = 20 pixels at Excel's 96 DPI. Reserve all rows the
            # image covers, including long questions split into multiple panels.
            rows = math.ceil(image.height / 20) + 2
            if row + rows > 1_048_576:
                raise ExportError("العرض الرياضي يتجاوز حدود ملف Excel.")
            sheet.add_image(image, f"A{row}")
            for index in range(row, row + rows):
                sheet.row_dimensions[index].height = 15
            row += rows

    for number, question in enumerate(questions, 1):
        # Only the visible question/choices reach the student renderer.
        visible = {key: question[key] for key in ("question", "options", "table", "matrices") if key in question}
        try:
            add_panels(visible, f"السؤال {number}" if is_ar else f"Question {number}")
            if include_answers:
                add_panels(_answer_print_question(question, is_ar),
                           f"حل السؤال {number}" if is_ar else f"Solution {number}")
        except ExportError:
            raise
        except Exception as exc:
            raise ExportError(f"تعذّر تنسيق السؤال {number} في Excel؛ راجع المعادلات أو حجم الجدول.") from exc
    sheet.print_area = f"A1:L{row - 1}"


# ==================== DOCX ====================

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
    # خط الكتابة المعقّدة (w:cs) بيرسم أي حرف عربي جوا نفس الـ run، حتى لو باقي
    # الفقرة بخط لا يدعم العربي. افتراضياً Arial، بس قابل للتخصيص (مثلاً Amiri
    # لستايل "أكاديمي") حتى تنعكس هوية الخط على أي عربي مدسوس جوا نص إنكليزي.
    rFonts.set(qn("w:cs"), cs_font)
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


def _shade_paragraph(paragraph, hex_color: str) -> None:
    """يلوّن خلفية فقرة كاملة (تمتد من هامش لهامش) - مفيدة كـ"بانر" ملوّن تحت العنوان."""
    pPr = paragraph._p.get_or_add_pPr()
    shd = OxmlElement("w:shd")
    shd.set(qn("w:val"), "clear")
    shd.set(qn("w:color"), "auto")
    shd.set(qn("w:fill"), hex_color)
    pPr.append(shd)


def _shade_run(run, hex_color: str) -> None:
    """يلوّن خلفية جزء نص واحد فقط (run) - مستخدم لعمل تأثير "بادج" حول حرف الخيار."""
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


# إعدادات كل ستايل شكلي (ألوان/خطوط/عناصر) لملف الـ Word
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
    run = p.add_run(text)
    _style_run(run, font_name, size_pt, bold=bold, color=color, rtl=is_ar, cs_font=cs_font)


_DOCX_LINE_FACTOR = 1.32      # ارتفاع السطر في Word ≈ هذا × حجم الخط (محافظ عمداً)
_DOCX_SAFETY = 0.90           # نترك 10% احتياطاً: Word يرتّب الصفحة بنفسه، وتجاوز بسيط يخلق صفحة شاردة


def _docx_text_height(text: str, size: float, width_pt: float, bold: bool, is_ar: bool,
                      space_after: float = 0.0) -> float:
    """تقدير ارتفاع فقرة نصية بـ pt اعتماداً على مقاييس خطوط المشروع (تقريبي لـ Word)."""
    _register_pdf_fonts()
    font_en = "Latin-Bold" if bold and _font_available("Latin-Bold") else (
        "Latin" if _font_available("Latin") else "Helvetica")
    font_ar = ("Arabic-Bold" if bold else "Arabic") if _font_available("Arabic") else font_en
    lines = 0
    for paragraph in (text.split("\n") or [""]):
        lines += len(_wrap_line(latex_to_plain(paragraph), is_ar, font_en, font_ar, size, max(width_pt, 50.0)))
    return lines * size * _DOCX_LINE_FACTOR + space_after


def _docx_question_height(q: Dict[str, Any], idx: int, is_ar: bool, cfg: Dict[str, Any],
                          width_pt: float, label: str, draft_pt: float = 0.0, px_width: int = PRINT_WIDTH) -> float:
    """تقدير ارتفاع سؤال كامل في Word (نص أو صور رياضية) بعرض width_pt + مساحة المسودة إن وُجدت.
    px_width: عرض صور الأسئلة الرياضية بالبكسل (أقل من PRINT_WIDTH عند التقسيم لأعمدة)."""
    extra = (draft_pt + 8) if draft_pt else 0.0
    if _is_math_question(q):
        total = 0.0
        for chunk in plan_print_blocks(q, is_ar, px_width):
            total += width_pt * chunk_height_px(chunk) / chunk_width_px(chunk) + 6 + 4
        return total + extra
    pad = 18 if cfg["question_box"] else 0
    inner = width_pt - pad - (0 if cfg["question_box"] else 23)
    height = _docx_text_height(label + str(q.get("question", "")).strip(), 13, width_pt - pad, True, is_ar, 6)
    for opt in (q.get("options") or []):
        height += _docx_text_height(f"أ) {opt}", 11.5, inner, False, is_ar, 3 if cfg["question_box"] else 2)
    return height + pad + 14 + (8 if cfg["divider"] else 0) + extra


_DOCX_COLUMN_GUTTER_PT = 24.0


def _docx_fix_table_width(tbl, width_cm: float) -> None:
    """يثبّت عرض الجدول (بطاقة السؤال) بعرض العمود؛ بدونه يتمدد الجدول بعرض الصفحة داخل العمود."""
    tbl.autofit = False
    tbl_pr = tbl._tbl.tblPr
    tbl_w = tbl_pr.find(qn("w:tblW"))
    if tbl_w is None:
        tbl_w = OxmlElement("w:tblW")
        tbl_pr.append(tbl_w)
    tbl_w.set(qn("w:type"), "dxa")
    tbl_w.set(qn("w:w"), str(int(width_cm / 2.54 * 1440)))
    tbl.columns[0].width = Cm(width_cm)


def _docx_set_columns(section, num: int, divider: bool = False,
                      gutter_pt: float = _DOCX_COLUMN_GUTTER_PT) -> None:
    """يضبط عدد أعمدة القسم (w:cols) مع خط فاصل أصلي من Word بين العمودين عند divider."""
    sect_pr = section._sectPr
    cols = sect_pr.find(qn("w:cols"))
    if cols is None:
        cols = OxmlElement("w:cols")
        anchor = sect_pr.find(qn("w:docGrid"))
        if anchor is not None:
            anchor.addprevious(cols)
        else:
            sect_pr.append(cols)
    for attr in ("w:num", "w:sep", "w:space", "w:equalWidth"):
        if cols.get(qn(attr)) is not None:
            del cols.attrib[qn(attr)]
    cols.set(qn("w:space"), str(int(gutter_pt * 20)))
    if num > 1:
        cols.set(qn("w:num"), str(num))
        if divider:
            cols.set(qn("w:sep"), "1")


def _docx_tiny_paragraph(doc):
    """فقرة شبه معدومة الارتفاع (تحمل فاصل صفحة/عمود بلا سطر فارغ ظاهر)."""
    p = doc.add_paragraph()
    p.paragraph_format.space_before = Pt(0)
    p.paragraph_format.space_after = Pt(0)
    pPr = p._p.get_or_add_pPr()
    spacing = pPr.find(qn("w:spacing"))
    if spacing is None:
        spacing = OxmlElement("w:spacing")
        pPr.append(spacing)
    spacing.set(qn("w:line"), "20")
    spacing.set(qn("w:lineRule"), "exact")
    return p


def _docx_column_break(doc) -> None:
    run = _docx_tiny_paragraph(doc).add_run("")
    run.font.size = Pt(1)
    run.add_break(WD_BREAK.COLUMN)


_DRAFT_PX_PER_PT = 3.0


def _draft_png(style: str, width_pt: float, height_pt: float, is_ar: bool) -> bytes:
    """صورة مسودة (أسطر منقّطة / مربعات / إطار) لـ Word: صورة صغيرة بلا اعتماد على جداول معقّدة،
    فتظهر بنفس الشكل في Word وLibreOffice. الحجم بالبايت صغير (خلفية بيضاء + خطوط رفيعة)."""
    from PIL import Image, ImageDraw, ImageFont
    k = _DRAFT_PX_PER_PT
    w, h = max(8, int(width_pt * k)), max(8, int(height_pt * k))
    img = Image.new("RGB", (w, h), "white")
    d = ImageDraw.Draw(img)
    if style == "lines":
        step = 17.0 * k
        y = step
        while y <= h - 1:
            x = 0.0
            while x < w:
                d.line([(x, y), (min(x + 1.5 * k, w), y)], fill=(184, 184, 184), width=max(1, int(0.6 * k)))
                x += 4.0 * k
            y += step
    elif style == "grid":
        step = 11.34 * k
        x = step
        while x < w - 1:
            d.line([(x, 0), (x, h)], fill=(214, 214, 214), width=1)
            x += step
        y = step
        while y < h - 1:
            d.line([(0, y), (w, y)], fill=(214, 214, 214), width=1)
            y += step
        d.rectangle([0, 0, w - 1, h - 1], outline=(168, 168, 168), width=max(1, int(0.7 * k)))
    else:
        dash, gap = 3.0 * k, 2.0 * k
        for x0 in range(0, w, int(dash + gap)):
            d.line([(x0, 0), (min(x0 + dash, w), 0)], fill=(154, 154, 154), width=max(1, int(0.8 * k)))
            d.line([(x0, h - 1), (min(x0 + dash, w), h - 1)], fill=(154, 154, 154), width=max(1, int(0.8 * k)))
        for y0 in range(0, h, int(dash + gap)):
            d.line([(0, y0), (0, min(y0 + dash, h))], fill=(154, 154, 154), width=max(1, int(0.8 * k)))
            d.line([(w - 1, y0), (w - 1, min(y0 + dash, h))], fill=(154, 154, 154), width=max(1, int(0.8 * k)))
    if style != "lines":
        font_file = os.path.join(FONTS_DIR, "NotoNaskhArabic-Regular.ttf" if is_ar else "NotoSans-Regular.ttf")
        try:
            font = ImageFont.truetype(font_file, int(7 * k))
            text = _shape("مسودة", True) if is_ar else "Draft"
            tw = d.textlength(text, font=font)
            x = (w - tw - 5 * k) if is_ar else 5 * k
            d.text((x, 2 * k), text, fill=(154, 154, 154), font=font)
        except Exception:   # الخط اختياري للتسمية؛ المسودة تبقى صالحة بدونها
            pass
    out = io.BytesIO()
    img.save(out, format="PNG", optimize=True)
    return out.getvalue()


def _docx_add_draft(doc, q: Dict[str, Any], opts: ExportOptions, is_ar: bool, width_pt: float,
                    align_center: bool = True) -> None:
    height = opts.draft_height
    if not height:
        return
    style = resolve_draft_style(opts.draft_style, _is_math_question(q))
    p = doc.add_paragraph()
    p.alignment = WD_ALIGN_PARAGRAPH.CENTER if align_center else None
    p.paragraph_format.space_before = Pt(2)
    p.paragraph_format.space_after = Pt(6)
    p.add_run().add_picture(io.BytesIO(_draft_png(style, width_pt, height, is_ar)), width=Pt(width_pt))


def _docx_new_page(doc) -> None:
    """بداية صفحة جديدة بفقرة شبه معدومة الارتفاع (لا سطر فارغ ظاهر أعلى الصفحة)."""
    p = doc.add_paragraph()
    p.paragraph_format.page_break_before = True
    p.paragraph_format.space_before = Pt(0)
    p.paragraph_format.space_after = Pt(0)
    run = p.add_run("")
    run.font.size = Pt(1)
    pPr = p._p.get_or_add_pPr()
    spacing = pPr.find(qn("w:spacing"))
    if spacing is None:
        spacing = OxmlElement("w:spacing")
        pPr.append(spacing)
    spacing.set(qn("w:line"), "20")
    spacing.set(qn("w:lineRule"), "exact")


def _docx_add_page_number(paragraph, size_pt: float = 9) -> None:
    fld = OxmlElement("w:fldSimple")
    fld.set(qn("w:instr"), "PAGE")
    run = OxmlElement("w:r")
    rPr = OxmlElement("w:rPr")
    sz = OxmlElement("w:sz")
    sz.set(qn("w:val"), str(int(size_pt * 2)))
    rPr.append(sz)
    run.append(rPr)
    text = OxmlElement("w:t")
    text.text = "1"
    run.append(text)
    fld.append(run)
    paragraph._p.append(fld)


def _docx_keep_together(paragraph, keep: bool = True) -> None:
    paragraph.paragraph_format.keep_with_next = keep
    paragraph.paragraph_format.keep_together = True


def _docx_add_branding(doc, continuous_single_column: bool = False) -> None:
    """شارة الختام الإجبارية (لوغو أبيض + تلغرام + @AbiadQuizMakerbot) بمنتصف الصفحة."""
    if continuous_single_column:
        _docx_set_columns(doc.add_section(WD_SECTION.CONTINUOUS), 1)
    p = doc.add_paragraph()
    p.alignment = WD_ALIGN_PARAGRAPH.CENTER
    p.paragraph_format.space_before = Pt(14)
    p.paragraph_format.space_after = Pt(0)
    p.add_run().add_picture(io.BytesIO(branding_png()), width=Cm(7.5))


def build_quiz_docx(title: str, questions: List[Dict[str, Any]], style: str = DEFAULT_STYLE,
                    include_answers: bool = True, options=None) -> bytes:
    """يبني ملف Word جميل التنسيق للكويز، مع اتجاه تلقائي حسب اللغة وستايل شكلي قابل للاختيار
    (simple / modern / academic). options: إعدادات ورقة الاختبار الاختيارية (ExportOptions
    أو dict)؛ بدونها (أو بقيمها الافتراضية) المخرجات كما كانت."""
    if not questions:
        raise ExportError("لا توجد أسئلة لتصديرها.")
    if len(questions) > 200 or sum(len(str(q)) for q in questions) > 500000:
        raise ExportError("هذا الكويز كبير للتصدير دفعة واحدة؛ قسّمه إلى اختبارات أصغر (حتى ٢٠٠ سؤال).")

    opts = coerce_options(options)
    title = opts.exam_title or title
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

    # اتجاه الصفحة بالكامل RTL للعربي
    if is_ar:
        for sec in doc.sections:
            sec._sectPr.append(OxmlElement("w:bidi"))

    # ==================== ترويسة/تذييل الصفحات (اسم الأستاذ + ترقيم) ====================
    teacher = teacher_text(opts, is_ar)
    if teacher and opts.teacher_placement == "header":
        hp = section.header.paragraphs[0]
        if is_ar:
            _set_paragraph_rtl(hp, align_right=False)
        hp.alignment = WD_ALIGN_PARAGRAPH.CENTER
        _style_run(hp.add_run(teacher), body_font, 9, color=cfg["muted"], rtl=is_ar, cs_font=cs_font)
    show_numbers = opts.page_numbers is True
    if (teacher and opts.teacher_placement == "footer") or show_numbers:
        fp = section.footer.paragraphs[0]
        if is_ar:
            _set_paragraph_rtl(fp, align_right=False)
        fp.alignment = WD_ALIGN_PARAGRAPH.CENTER
        if teacher and opts.teacher_placement == "footer":
            _style_run(fp.add_run(teacher), body_font, 9, color=cfg["muted"], rtl=is_ar, cs_font=cs_font)
            if show_numbers:
                fp = section.footer.add_paragraph()
                if is_ar:
                    _set_paragraph_rtl(fp, align_right=False)
                fp.alignment = WD_ALIGN_PARAGRAPH.CENTER
        if show_numbers:
            _docx_add_page_number(fp)

    # ==================== العنوان ====================
    title_p = doc.add_paragraph()
    title_p.alignment = WD_ALIGN_PARAGRAPH.CENTER
    if is_ar:
        _set_paragraph_rtl(title_p, align_right=False)
        title_p.alignment = WD_ALIGN_PARAGRAPH.CENTER
    title_run = title_p.add_run(title or ("كويز" if is_ar else "Quiz"))
    title_color = (255, 255, 255) if cfg["header_banner"] else cfg["accent"]
    _style_run(title_run, body_font, 20, bold=True, color=title_color, rtl=is_ar, cs_font=cs_font)
    if cfg["header_banner"]:
        _shade_paragraph(title_p, accent_hex)
        title_p.paragraph_format.space_before = Pt(6)
        title_p.paragraph_format.space_after = Pt(6)

    sub_p = doc.add_paragraph()
    sub_text = f"عدد الأسئلة: {len(questions)}" if is_ar else f"Total Questions: {len(questions)}"
    sub_run = sub_p.add_run(sub_text)
    _style_run(sub_run, body_font, 11, color=cfg["muted"], rtl=is_ar, cs_font=cs_font)
    sub_p.alignment = WD_ALIGN_PARAGRAPH.CENTER
    if is_ar:
        _set_paragraph_rtl(sub_p, align_right=False)
        sub_p.alignment = WD_ALIGN_PARAGRAPH.CENTER

    # بيانات الورقة الاختيارية (المادة/الصف/التاريخ/المدة/العلامة + اسم الأستاذ بالصفحة الأولى)
    header_lines = 1
    extra_header = [exam_meta_line(opts, is_ar)]
    if teacher and opts.teacher_placement == "first":
        extra_header.append(teacher)
    for text in filter(None, extra_header):
        meta_p = doc.add_paragraph()
        _style_run(meta_p.add_run(text), body_font, 11, bold=(text == teacher),
                   color=cfg["muted"], rtl=is_ar, cs_font=cs_font)
        meta_p.alignment = WD_ALIGN_PARAGRAPH.CENTER
        if is_ar:
            _set_paragraph_rtl(meta_p, align_right=False)
            meta_p.alignment = WD_ALIGN_PARAGRAPH.CENTER
        header_lines += 1 + text.count("|") // 3

    info_text = student_line(opts, is_ar, academic=(style == STYLE_ACADEMIC))
    if style == STYLE_ACADEMIC:
        # سطر معلومات شبيه بورقة امتحان رسمية: الاسم / التاريخ
        info_p = doc.add_paragraph()
        info_run = info_p.add_run(info_text)
        _style_run(info_run, body_font, 11, color=(30, 30, 30), rtl=is_ar, cs_font=cs_font)
        if is_ar:
            _set_paragraph_rtl(info_p, align_right=True)
        info_p.paragraph_format.space_before = Pt(10)
        _set_paragraph_border_bottom(info_p, "333333", size=10)
        info_p.paragraph_format.space_after = Pt(14)
    elif info_text:
        info = doc.add_paragraph(info_text)
        if is_ar:
            _set_paragraph_rtl(info)

    # ==================== تخطيط الصفحات (اختياري) ====================
    usable_width_pt = usable_width_cm * 28.3465
    col_mode = opts.columns == 2
    draft_pt = opts.draft_height
    col_w_pt = (usable_width_pt - _DOCX_COLUMN_GUTTER_PT) / 2 if col_mode else usable_width_pt
    col_w_cm = col_w_pt / 28.3465
    col_px = int(round(PRINT_WIDTH * col_w_pt / usable_width_pt))
    group_starts: set = set()
    breaks: Dict[int, str] = {}      # رقم السؤال (من 1) -> نوع الفاصل قبله: page / col / full_page / cols_page
    wide_idx: set = set()            # أسئلة تُرسم بعرض الصفحة كاملاً في وضع العمودين
    col_pages: List[Any] = []
    page_h_pt = (section.page_height - section.top_margin - section.bottom_margin) / 12700
    first_cap = (page_h_pt - 95 - 16 * header_lines) * _DOCX_SAFETY
    cap = page_h_pt * _DOCX_SAFETY
    if col_mode:
        heights = []
        for idx, q in enumerate(questions, 1):
            label = f"السؤال {idx}: " if is_ar else f"Question {idx}: "
            try:
                heights.append(_docx_question_height(q, idx, is_ar, cfg, col_w_pt, label, draft_pt, col_px))
            except Exception as exc:
                if not _is_math_question(q):
                    raise ExportError(f"تعذّر تنسيق السؤال {idx} للطباعة؛ راجع صيغة المعادلات أو حجم الجدول.") from exc
                try:   # لا يتسع بعرض العمود: نجرّبه بعرض الصفحة؛ فشله هناك يرفع الخطأ الصريح المعتاد
                    _docx_question_height(q, idx, is_ar, cfg, usable_width_pt, label, draft_pt)
                except Exception as exc2:
                    raise ExportError(f"تعذّر تنسيق السؤال {idx} للطباعة؛ راجع صيغة المعادلات أو حجم الجدول.") from exc2
                wide_idx.add(idx)
                heights.append(cap + 1)
        if opts.per_page or wide_idx:
            # تخطيط صريح (فواصل صفحات/أعمدة). بدونه (كل ما يتسع، بلا أسئلة عريضة) يرتّب Word العمودين تلقائياً.
            col_pages = plan_column_pages(heights, opts.per_page, 2, first_cap, cap,
                                          frozenset(i - 1 for i in wide_idx))
            prev_full = False
            for page_no, page in enumerate(col_pages):
                first_idx = page.columns[0][0] + 1
                if page.full:
                    if page_no:
                        breaks[first_idx] = "full_page"
                    prev_full = True
                    continue
                if page_no:
                    breaks[first_idx] = "cols_page" if prev_full else "page"
                if len(page.columns) > 1:
                    breaks[page.columns[1][0] + 1] = "col"
                prev_full = False
    elif opts.per_page:
        heights = []
        for idx, q in enumerate(questions, 1):
            label = f"السؤال {idx}: " if is_ar else f"Question {idx}: "
            try:
                heights.append(_docx_question_height(q, idx, is_ar, cfg, usable_width_pt, label, draft_pt))
            except Exception as exc:
                raise ExportError(f"تعذّر تنسيق السؤال {idx} للطباعة؛ راجع صيغة المعادلات أو حجم الجدول.") from exc
        pages = plan_pages(heights, opts.per_page, first_cap, cap)
        group_starts = {page[0] + 1 for page in pages[1:]}
    keep = bool(opts.per_page) or col_mode or draft_pt > 0

    # عنوان الورقة أعلاه بعمود واحد؛ الأسئلة تبدأ بقسم متصل بعمودين (خط فاصل أصلي من Word).
    if col_mode and not (col_pages and col_pages[0].full):
        _docx_set_columns(doc.add_section(WD_SECTION.CONTINUOUS), 2, opts.column_divider)

    # ==================== الأسئلة ====================
    total_questions = len(questions)
    for idx, q in enumerate(questions, 1):
        q_text = str(q.get("question", "")).strip()
        options = q.get("options") or []
        label = f"السؤال {idx}: " if is_ar else f"Question {idx}: "
        brk = breaks.get(idx)
        if brk == "page":
            _docx_new_page(doc)
        elif brk == "col":
            _docx_column_break(doc)
        elif brk == "full_page":
            _docx_set_columns(doc.add_section(WD_SECTION.NEW_PAGE), 1)
        elif brk == "cols_page":
            _docx_set_columns(doc.add_section(WD_SECTION.NEW_PAGE), 2, opts.column_divider)
        if idx in group_starts:
            _docx_new_page(doc)
        # عرض هذا السؤال: عرض العمود في وضع العمودين، وعرض الصفحة للعريض أو بعمود واحد
        wide_q = (not col_mode) or (idx in wide_idx)
        q_w_cm = usable_width_cm if wide_q else col_w_cm
        q_w_pt = q_w_cm * 28.3465
        q_px = PRINT_WIDTH if wide_q else col_px

        # 🆕 سؤال رياضي (نمط الكويز المصوّر LaTeX، أو جدول بيانات): يُرسم كصورة
        # نظيفة بنفس محرك عرض الكويز الفعلي بدل كتابة رموز LaTeX الخام كنص -
        # هيك تُصدَّر المسألة كاملة (نص + معادلات + جدول لو وُجد) في وحدة واحدة
        # غير قابلة للتقسيم أو التشويه، تماماً كما تظهر للطالب أثناء حل الكويز.
        if _is_math_question(q):
            try:
                for img_bytes in iter_print_blocks(q, is_ar, label, q_px):
                    img_p = doc.add_paragraph()
                    img_p.alignment = WD_ALIGN_PARAGRAPH.CENTER
                    img_p.add_run().add_picture(io.BytesIO(img_bytes), width=Cm(q_w_cm))
                    img_p.paragraph_format.space_after = Pt(6)
                    if draft_pt:
                        img_p.paragraph_format.keep_with_next = True
                    del img_bytes
            except Exception as exc:
                raise ExportError(f"تعذّر تنسيق السؤال {idx} للطباعة؛ راجع صيغة المعادلات أو حجم الجدول.") from exc
            _docx_add_draft(doc, q, opts, is_ar, q_w_pt)
            continue

        if cfg["question_box"]:
            tbl = doc.add_table(rows=1, cols=1)
            tbl.alignment = WD_TABLE_ALIGNMENT.CENTER
            if is_ar:
                _set_table_rtl(tbl)
            cell = tbl.cell(0, 0)
            if col_mode:
                _docx_fix_table_width(tbl, q_w_cm)
            _set_cell_width(cell, q_w_cm)
            _shade_cell(cell, cfg["question_box_bg"])
            _set_cell_borders(cell, cfg["question_box_border"])
            if keep:
                # لا يُقسَّم صندوق السؤال بين صفحتين
                trPr = tbl.rows[0]._tr.get_or_add_trPr()
                trPr.append(OxmlElement("w:cantSplit"))

            qp = cell.paragraphs[0]
            if is_ar:
                _set_paragraph_rtl(qp, align_right=True)
            run = qp.add_run(label + q_text)
            _style_run(run, body_font, 13, bold=True, color=cfg["accent"], rtl=is_ar, cs_font=cs_font)
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
                    text_run = op.add_run(opt_text)
                    _style_run(text_run, body_font, 11.5, rtl=is_ar, cs_font=cs_font)
                else:
                    run = op.add_run(f"{letter}) {opt_text}")
                    _style_run(run, body_font, 11.5, rtl=is_ar, cs_font=cs_font)
                op.paragraph_format.space_after = Pt(3)

            if draft_pt:
                for cell_p in cell.paragraphs:
                    cell_p.paragraph_format.keep_with_next = True
                _docx_add_draft(doc, q, opts, is_ar, q_w_pt)
            spacer = doc.add_paragraph()
            spacer.paragraph_format.space_after = Pt(10)
        else:
            qp = doc.add_paragraph()
            run = qp.add_run(label + q_text)
            _style_run(run, body_font, 13, bold=True,
                       color=cfg["accent"] if style == STYLE_ACADEMIC else None, rtl=is_ar, cs_font=cs_font)
            if is_ar:
                _set_paragraph_rtl(qp, align_right=True)
            qp.paragraph_format.space_after = Pt(6)
            if keep:
                _docx_keep_together(qp, bool(options))

            for opt_idx, opt_text in enumerate(options):
                letter = letters[opt_idx] if opt_idx < len(letters) else str(opt_idx + 1)
                op = doc.add_paragraph()
                sep = "." if style == STYLE_ACADEMIC else ")"
                run = op.add_run(f"{letter}{sep} {opt_text}")
                _style_run(run, body_font, 11.5, rtl=is_ar, cs_font=cs_font)
                if is_ar:
                    _set_paragraph_rtl(op, align_right=True)
                    op.paragraph_format.right_indent = Cm(0.8)
                else:
                    op.paragraph_format.left_indent = Cm(0.8)
                op.paragraph_format.space_after = Pt(2)
                if keep:
                    _docx_keep_together(op, opt_idx < len(options) - 1 or draft_pt > 0)

            _docx_add_draft(doc, q, opts, is_ar, q_w_pt)
            spacer = doc.add_paragraph()
            if cfg["divider"]:
                spacer.paragraph_format.space_before = Pt(4)
                _set_paragraph_border_bottom(spacer, "AAAAAA", size=6)
            spacer.paragraph_format.space_after = Pt(10)

    if teacher and opts.teacher_placement == "last":
        if col_mode:
            _docx_set_columns(doc.add_section(WD_SECTION.CONTINUOUS), 1)
        sign_p = doc.add_paragraph()
        sign_p.paragraph_format.space_before = Pt(10)
        _set_paragraph_border_bottom(sign_p, "AAAAAA", size=6)
        sign_p.alignment = WD_ALIGN_PARAGRAPH.CENTER
        _style_run(sign_p.add_run(teacher), body_font, 12, bold=True, color=cfg["accent"],
                   rtl=is_ar, cs_font=cs_font)
        if is_ar:
            _set_paragraph_rtl(sign_p, align_right=False)
            sign_p.alignment = WD_ALIGN_PARAGRAPH.CENTER

    # شارة إجبارية بنهاية الاختبار (نهاية الأسئلة، قبل مفتاح الإجابات إن وُجد).
    _docx_add_branding(doc, continuous_single_column=col_mode)

    if include_answers:
        if col_mode:
            _docx_set_columns(doc.add_section(WD_SECTION.NEW_PAGE), 1)   # الإجابات بعمود واحد
        else:
            doc.add_page_break()
        doc.add_heading("الإجابات والحلول" if is_ar else "Answers and solutions", level=1)
        key_only = opts.answers_mode == "key"
        for idx, q in enumerate(questions, 1):
            answer = _answer_print_question(q, is_ar, with_explanation=not key_only)
            if key_only and not _is_math_question(q) and not _is_math_question(answer):
                # مفتاح مضغوط: سطر نصي واحد لكل سؤال (الصور فقط للمحتوى الرياضي)
                text = answer["question"].strip().replace("\n", " ")
                key_p = doc.add_paragraph()
                _style_run(key_p.add_run(f"{idx}) {text}"), body_font, 11.5, rtl=is_ar, cs_font=cs_font)
                if is_ar:
                    _set_paragraph_rtl(key_p, align_right=True)
                key_p.paragraph_format.space_after = Pt(3)
                continue
            try:
                for img_bytes in iter_print_blocks(answer, is_ar, f"{idx}"):
                    p = doc.add_paragraph()
                    p.add_run().add_picture(io.BytesIO(img_bytes), width=Cm(usable_width_cm))
                    del img_bytes
            except Exception as exc:
                raise ExportError(f"تعذّر تنسيق إجابة السؤال {idx}؛ راجع المعادلات.") from exc
        _docx_add_branding(doc)   # نهاية ملف نسخة الأستاذ
    out = io.BytesIO()
    doc.save(out)
    return out.getvalue()


def _answer_print_question(q, is_ar, with_explanation=True):
    options = q.get("options") or []
    try:
        correct = int(q.get("correct_option_id"))
    except (ValueError, TypeError):
        correct = -1
    letters = _letters_for(is_ar)
    label = letters[correct] if 0 <= correct < len(letters) else "-"
    answer = str(options[correct]) if 0 <= correct < len(options) else "-"
    prefix = "الإجابة الصحيحة" if is_ar else "Correct answer"
    explanation = str(q.get("explanation") or "").strip() if with_explanation else ""
    text = f"{prefix}: {label}) {answer}" + (f"\n{explanation}" if with_explanation else "\n")
    return {"question": text, "options": []}


def build_document_docx(title: str, text: str, style: str = DEFAULT_STYLE) -> bytes:
    """يبني ملف Word منسّق لنص حر كامل (تفريغ محاضرة، ملخص، أي نص تعليمي عام) بعنوان
    وفقرات، مع اكتشاف تلقائي للغة (عربي/إنكليزي)، دعم RTL كامل للعربي (اتجاه الصفحة/
    الفقرات وخط الكتابة المعقّدة w:cs)، وطباعة عربية/إنكليزية سليمة - بنفس الستايلات
    الشكلية (simple/modern/academic) المتوفرة بتصدير الكويز. يدعم Markdown بسيط: عناوين
    (# / ## / ###) تُعرض كعناوين مُنسّقة، ونقاط (- أو * أو •) تُعرض كقائمة نقطية، وباقي
    الأسطر تُدمج كفقرات نصّية عادية."""
    if not text or not text.strip():
        raise ExportError("لا يوجد نص لتصديره.")

    style = normalize_style(style)
    cfg = _DOCX_STYLES[style]

    is_ar = detect_text_language(text) == "ar"
    body_font = cfg["ar_font"] if is_ar else cfg["en_font"]
    cs_font = cfg["ar_font"]
    accent_hex = _rgb_to_hex(cfg["accent"])

    doc = Document()

    # اتجاه الصفحة بالكامل RTL للعربي
    if is_ar:
        for sec in doc.sections:
            sec._sectPr.append(OxmlElement("w:bidi"))

    # ==================== العنوان ====================
    title_p = doc.add_paragraph()
    title_p.alignment = WD_ALIGN_PARAGRAPH.CENTER
    if is_ar:
        _set_paragraph_rtl(title_p, align_right=False)
        title_p.alignment = WD_ALIGN_PARAGRAPH.CENTER
    title_run = title_p.add_run(title or ("مستند" if is_ar else "Document"))
    title_color = (255, 255, 255) if cfg["header_banner"] else cfg["accent"]
    _style_run(title_run, body_font, 20, bold=True, color=title_color, rtl=is_ar, cs_font=cs_font)
    if cfg["header_banner"]:
        _shade_paragraph(title_p, accent_hex)
        title_p.paragraph_format.space_before = Pt(6)
        title_p.paragraph_format.space_after = Pt(6)

    if style == STYLE_ACADEMIC:
        # سطر معلومات شبيه بغلاف مستند أكاديمي رسمي: الاسم / التاريخ
        info_p = doc.add_paragraph()
        info_text = "الاسم: ________________________     التاريخ: ______________" if is_ar \
            else "Name: ________________________     Date: ______________"
        info_run = info_p.add_run(info_text)
        _style_run(info_run, body_font, 11, color=(30, 30, 30), rtl=is_ar, cs_font=cs_font)
        if is_ar:
            _set_paragraph_rtl(info_p, align_right=True)
        info_p.paragraph_format.space_before = Pt(10)
        _set_paragraph_border_bottom(info_p, "333333", size=10)
        info_p.paragraph_format.space_after = Pt(14)
    else:
        doc.add_paragraph()

    # ==================== الفقرات (Markdown مبسّط) ====================
    for block_type, content in _parse_markdown_blocks(text):
        if not content.strip():
            continue
        p = doc.add_paragraph()
        if is_ar:
            _set_paragraph_rtl(p, align_right=True)
        else:
            p.alignment = WD_ALIGN_PARAGRAPH.LEFT

        if block_type == "h1":
            run = p.add_run(content)
            _style_run(run, body_font, 16, bold=True, color=cfg["accent"], rtl=is_ar, cs_font=cs_font)
            p.paragraph_format.space_before = Pt(14)
            p.paragraph_format.space_after = Pt(6)
            if cfg["divider"]:
                _set_paragraph_border_bottom(p, "AAAAAA", size=6)
        elif block_type == "h2":
            run = p.add_run(content)
            _style_run(run, body_font, 13.5, bold=True, color=cfg["accent"], rtl=is_ar, cs_font=cs_font)
            p.paragraph_format.space_before = Pt(10)
            p.paragraph_format.space_after = Pt(4)
        elif block_type == "bullet":
            run = p.add_run(f"• {content}")
            _style_run(run, body_font, 11.5, rtl=is_ar, cs_font=cs_font)
            if is_ar:
                p.paragraph_format.right_indent = Cm(0.8)
            else:
                p.paragraph_format.left_indent = Cm(0.8)
            p.paragraph_format.space_after = Pt(3)
        else:
            run = p.add_run(content)
            _style_run(run, body_font, 11.5, rtl=is_ar, cs_font=cs_font)
            p.paragraph_format.space_after = Pt(8)
            p.paragraph_format.line_spacing = 1.3

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
from reportlab.lib.utils import ImageReader
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen import canvas

_FONT_FILES = {
    "Arabic": "NotoNaskhArabic-Regular.ttf",
    "Arabic-Bold": "NotoNaskhArabic-Bold.ttf",
    "Latin": "NotoSans-Regular.ttf",
    "Latin-Bold": "NotoSans-Bold.ttf",
    # اختياري: يُستخدم فقط بستايل "أكاديمي" العربي إذا موجود، وإلا بيرجع تلقائياً
    # لخط Noto Naskh Arabic العادي (مافي داعي نوقف تصدير PDF لغيابه).
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
    """يهيئ وييعيد ترتيب النص للعرض الصحيح (bidi) - يشتغل كل ما كان في نص عربي
    بالسطر، بغض النظر عن كون الفقرة ككل عربي أو إنكليزي. base_ar يحدد اتجاه
    القراءة الأساسي للسطر (يهم بترتيب الأجزاء غير-العربية المدسوسة وسطه)."""
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
    """يقسّم نص (بعد التهيئة/bidi) إلى أجزاء متتالية عربي/غير-عربي، بنفس ترتيب
    الرسم من اليسار لليمين على الصفحة - كل جزء جاهز يترسم بخطّه المناسب."""
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
                      extra_indent: float = 0, center: bool = False,
                      x_left: Optional[float] = None, x_right: Optional[float] = None) -> None:
    """يرسم سطر واحد ممكن يحوي عربي وإنكليزي مع بعض، كل جزء بخطّه الصحيح،
    مع محاذاة يمين/يسار/وسط محسوبة على العرض الكلي الحقيقي للسطر."""
    shaped = _shape(text, base_ar)
    runs = _split_script_runs(shaped)
    total_w = sum(pdfmetrics.stringWidth(seg, font_ar if seg_is_ar else font_en, size)
                  for seg, seg_is_ar in runs)
    # x_left/x_right: حدود الصندوق الحالي (عمود مثلاً)؛ بدونهما الهوامش المتماثلة كما كانت.
    left = margin if x_left is None else x_left
    right = page_w - margin if x_right is None else x_right
    if center:
        x = (left + right) / 2 - total_w / 2
    elif base_ar:
        x = right - extra_indent - total_w
    else:
        x = left + extra_indent
    c.setFillColor(HexColor(color))
    for seg, seg_is_ar in runs:
        font = font_ar if seg_is_ar else font_en
        c.setFont(font, size)
        c.drawString(x, y, seg)
        x += pdfmetrics.stringWidth(seg, font, size)


# إعدادات كل ستايل شكلي للـ PDF
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


class _NullCanvas:
    """قماش وهمي يتجاهل كل أوامر الرسم؛ يُستخدم لقياس ارتفاع السؤال بنفس كود الرسم الفعلي."""

    def __getattr__(self, name):
        return lambda *args, **kwargs: None


class _QuizPDFRenderer:
    def __init__(self, title: str, questions: List[Dict[str, Any]], is_ar: bool, style: str = DEFAULT_STYLE,
                 include_answers: bool = True, options: Optional[ExportOptions] = None):
        self.options = options or ExportOptions()
        self.title = self.options.exam_title or title or ("كويز" if is_ar else "Quiz")
        self.questions = questions
        self.include_answers = include_answers
        self._plans: Dict[int, Any] = {}       # تخطيط الصور الرياضية المحسوب أثناء القياس
        self._keep_plans = False
        self.is_ar = is_ar
        self.letters = _letters_for(is_ar)
        self.style = normalize_style(style)
        self.cfg = _PDF_STYLES[self.style]

        self.page_w, self.page_h = A4
        self.margin = 48
        # صندوق المحتوى الحالي: الصفحة كاملة افتراضياً، وعند تقسيمها لأعمدة يصير حدود العمود.
        self.box_left = self.margin
        self.box_right = self.page_w - self.margin
        self.force_dense: Optional[bool] = None   # للقياس فقط (max_questions_per_page)
        self.buf = io.BytesIO()
        self.c = canvas.Canvas(self.buf, pagesize=A4)
        self.y = self.page_h - self.margin
        self.page_num = 1

        _register_pdf_fonts()
        if self.cfg["academic_fonts"] and _font_available("Arabic-Academic"):
            # خط Amiri الكلاسيكي متوفر - نستخدمه لستايل "أكاديمي" العربي تحديداً
            self.font_ar_regular = "Arabic-Academic"
            self.font_ar_bold = "Arabic-Academic-Bold" if _font_available("Arabic-Academic-Bold") \
                else "Arabic-Academic"
        else:
            self.font_ar_regular = "Arabic" if _font_available("Arabic") else None
            self.font_ar_bold = "Arabic-Bold" if _font_available("Arabic-Bold") else None
        if self.cfg["academic_fonts"]:
            # Times-Roman/Times-Bold من الخطوط الأساسية الـ 14 المدمجة بـ reportlab -
            # موجودة دايماً بدون ما نحتاج ملف خط، وبتعطي طابع أكاديمي/رسمي كلاسيكي.
            self.font_en_regular = "Times-Roman"
            self.font_en_bold = "Times-Bold"
        else:
            self.font_en_regular = "Latin" if _font_available("Latin") else "Helvetica"
            self.font_en_bold = "Latin-Bold" if _font_available("Latin-Bold") else "Helvetica-Bold"

        if is_ar:
            self.font_regular = self.font_ar_regular
            self.font_bold = self.font_ar_bold
        else:
            self.font_regular = self.font_en_regular
            self.font_bold = self.font_en_bold

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
        self._draw_page_header()

    def _page_is_fresh(self) -> bool:
        return self.y >= self.page_h - self.margin - 1

    @property
    def _box_w(self) -> float:
        return self.box_right - self.box_left

    def _print_px_width(self) -> int:
        """عرض صور الأسئلة الرياضية بالبكسل لعرض الصندوق الحالي، بنفس مقياس الرسم (حجم الخط ثابت)."""
        return int(round(PRINT_WIDTH * self._box_w / (self.page_w - 2 * self.margin)))

    @property
    def _dense(self) -> bool:
        return self.options.dense if self.force_dense is None else self.force_dense

    def _draw_teacher_line(self, y: float) -> None:
        text = teacher_text(self.options, self.is_ar)
        font_en = self.font_en_regular
        font_ar = self.font_ar_regular or font_en
        _draw_mixed_line(self.c, text, self.is_ar, font_en, font_ar, 9, y, "#777777",
                         self.margin, self.page_w, center=True)

    def _draw_page_header(self) -> None:
        """ترويسة كل صفحة: اسم الأستاذ فقط عند اختيار «ترويسة». داخل الهامش العلوي."""
        if self.options.teacher_name and self.options.teacher_placement == "header":
            self._draw_teacher_line(self.page_h - 28)

    def _draw_footer(self):
        if self.options.page_numbers is not False:
            self.c.setFont(self.font_regular, 9)
            self.c.setFillColor(HexColor("#888888"))
            self.c.drawCentredString(self.page_w / 2, 25, str(self.page_num))
        if self.options.teacher_name and self.options.teacher_placement == "footer":
            self._draw_teacher_line(40)

    def _ensure_space(self, needed: float):
        if self.y - needed < self.margin + 25:
            self._new_page()

    def _draw_paragraph(self, text: str, size: float, bold: bool = False,
                         color: str = "#111111", extra_indent: float = 0, gap_after: float = 6,
                         center: bool = False, is_ar: bool = None):
        # is_ar=None يعني "استخدم اتجاه المستند العام"؛ تمرير قيمة صريحة يسمح
        # برسم فقرة واحدة بعكس اتجاه بقية المستند لو احتجنا لهيك مستقبلاً.
        direction_ar = self.is_ar if is_ar is None else is_ar
        font_en = self.font_en_bold if bold else self.font_en_regular
        font_ar = (self.font_ar_bold if bold else self.font_ar_regular) or font_en
        max_width = self._box_w - extra_indent
        lines = _wrap_line(text, direction_ar, font_en, font_ar, size, max_width)
        for line in lines:
            self._ensure_space(size + 5)
            _draw_mixed_line(self.c, line, direction_ar, font_en, font_ar, size, self.y, color,
                              self.margin, self.page_w, extra_indent=extra_indent, center=center,
                              x_left=self.box_left, x_right=self.box_right)
            self.y -= (size + 5)
        self.y -= gap_after

    # ---------- الأقسام ----------

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

        # بيانات الورقة الاختيارية بين عدد الأسئلة وسطر الطالب. بدونها: السلوك القديم حرفياً.
        extra = [exam_meta_line(self.options, self.is_ar)]
        if self.options.teacher_name and self.options.teacher_placement == "first":
            extra.append(teacher_text(self.options, self.is_ar))
        extra = [text for text in extra if text]

        sub = f"عدد الأسئلة: {len(self.questions)}" if self.is_ar else f"Total Questions: {len(self.questions)}"
        self._draw_paragraph(sub, 10.5, color=cfg["muted"],
                              gap_after=(10 if self.style == STYLE_ACADEMIC else 16) if not extra else 4,
                              center=True)
        for position, text in enumerate(extra):
            last = position == len(extra) - 1
            self._draw_paragraph(text, 10.5, bold=(text.startswith(("إعداد", "Prepared"))), color=cfg["muted"],
                                  gap_after=(10 if self.style == STYLE_ACADEMIC else 16) if last else 3, center=True)

        info = student_line(self.options, self.is_ar, academic=(self.style == STYLE_ACADEMIC))
        if self.style == STYLE_ACADEMIC:
            if info:
                self._draw_paragraph(info, 11, color="#1E1E1E", gap_after=2)
            self.c.setStrokeColor(HexColor("#333333"))
            self.c.setLineWidth(1)
            self.c.line(self.margin, self.y + 8, self.page_w - self.margin, self.y + 8)
            self.y -= 12
        elif info:
            self._draw_paragraph(info, 10, gap_after=10)

    def _draw_print_question(self, q, heading, plan=None):
        if plan is None:
            blocks = iter_print_blocks(q, self.is_ar, heading, self._print_px_width())
        else:
            def _render_planned():
                from mem_utils import release_memory_to_os
                for index, chunk in enumerate(plan):
                    yield render_print_chunk(chunk, self.is_ar, heading, index)
                    release_memory_to_os()
            blocks = _render_planned()
        for img_bytes in blocks:
            reader = ImageReader(io.BytesIO(img_bytes))
            w, h = reader.getSize()
            display_w = self._box_w
            display_h = display_w * h / w
            self._ensure_space(display_h + 10)
            self.c.drawImage(reader, self.box_left, self.y - display_h,
                             width=display_w, height=display_h)
            self.y -= display_h + 10
            del reader, img_bytes

    def _plan_question(self, idx0: int, q: Dict[str, Any]):
        """تخطيط كتل السؤال الرياضي؛ يُحفظ أثناء القياس ليُعاد استعماله عند الرسم الفعلي."""
        plan = self._plans.get(idx0)
        if plan is None:
            plan = plan_print_blocks(q, self.is_ar)
            if self._keep_plans:
                self._plans[idx0] = plan
        return plan

    def _draw_math_question_image(self, q: Dict[str, Any], idx0: int) -> bool:
        try:
            heading = f"السؤال {idx0 + 1}" if self.is_ar else f"Question {idx0 + 1}"
            plan = self._plans.pop(idx0, None) if self._keep_plans else None
            self._draw_print_question(q, heading, plan)
            return True
        except Exception as exc:
            raise ExportError(f"تعذّر تنسيق السؤال {idx0 + 1} للطباعة؛ راجع صيغة المعادلات أو حجم الجدول.") from exc

    def _draw_question(self, idx: int, q: Dict[str, Any]) -> None:
        """يرسم سؤالاً واحداً (idx يبدأ من 1) بحسب الستايل - نفس الكود للرسم والقياس."""
        if _is_math_question(q) and self._draw_math_question_image(q, idx - 1):
            self._draw_draft(q)
            return
        if self.cfg["box"]:
            self._draw_question_boxed(idx, q)
        else:
            self._draw_question_plain(idx, q)

    # ---------- خيارات مضغوطة + مساحة المسودة ----------

    _OPTION_GRID_GAP = 12.0
    _DRAFT_GAP_BEFORE = 4.0
    _DRAFT_GAP_AFTER = 6.0

    def _compact_rows(self, items: List[str], size: float, width: float) -> Optional[List[List[str]]]:
        """شبكة 2×2 للخيارات القصيرة في التخطيط المضغوط (عمودان أو 8+ أسئلة بالصفحة).
        تُرجع صفوفاً من خيارين، أو None إن لم يكن التخطيط مضغوطاً أو لم يتسع كل خيار بسطر واحد
        بنصف العرض (فتبقى القائمة العمودية المعتادة بلا قص ولا تصغير للخط)."""
        if not self._dense or len(items) < 3:
            return None
        cell_w = (width - self._OPTION_GRID_GAP) / 2
        font_en = self.font_en_regular
        font_ar = self.font_ar_regular or font_en
        for text in items:
            if len(_wrap_line(text, self.is_ar, font_en, font_ar, size, cell_w)) > 1:
                return None
        return [items[i:i + 2] for i in range(0, len(items), 2)]

    def _draw_options_block(self, items: List[str], size: float, extra_indent: float,
                            gap_after: float, width: float) -> None:
        """يرسم الخيارات: قائمة عمودية، أو شبكة 2×2 إن كان التخطيط مضغوطاً وتتسع."""
        rows = self._compact_rows(items, size, width)
        if rows is None:
            for text in items:
                self._draw_paragraph(text, size, extra_indent=extra_indent, gap_after=gap_after)
            return
        font_en = self.font_en_regular
        font_ar = self.font_ar_regular or font_en
        cell_w = (width - self._OPTION_GRID_GAP) / 2
        for row in rows:
            self._ensure_space(size + 5)
            for k, text in enumerate(row):
                shift = k * (cell_w + self._OPTION_GRID_GAP)
                if self.is_ar:
                    right = self.box_right - extra_indent - shift
                    left = right - cell_w
                else:
                    left = self.box_left + extra_indent + shift
                    right = left + cell_w
                _draw_mixed_line(self.c, text, self.is_ar, font_en, font_ar, size, self.y, "#111111",
                                 self.margin, self.page_w, x_left=left, x_right=right)
            self.y -= (size + 5) + gap_after

    def _draft_total(self) -> float:
        height = self.options.draft_height
        return height + self._DRAFT_GAP_BEFORE + self._DRAFT_GAP_AFTER if height else 0.0

    def _draw_draft(self, q: Dict[str, Any]) -> None:
        """مساحة مسودة تحت السؤال (أسطر / مربعات / إطار فارغ). «تلقائي»: مربعات للرياضي وأسطر لغيره."""
        height = self.options.draft_height
        if not height:
            return
        self._ensure_space(self._draft_total())
        self.y -= self._DRAFT_GAP_BEFORE
        top, bottom = self.y, self.y - height
        left, right = self.box_left, self.box_right
        style = resolve_draft_style(self.options.draft_style, _is_math_question(q))
        c = self.c
        c.saveState()
        if style == "lines":
            c.setStrokeColor(HexColor("#B8B8B8"))
            c.setLineWidth(0.6)
            c.setDash(1.5, 2.5)
            line_y = top - 17.0
            while line_y >= bottom - 0.5:
                c.line(left, line_y, right, line_y)
                line_y -= 17.0
        elif style == "grid":
            step = 11.34  # 4 مم
            c.setStrokeColor(HexColor("#D6D6D6"))
            c.setLineWidth(0.4)
            x = left + step
            while x < right - 0.5:
                c.line(x, top, x, bottom)
                x += step
            line_y = top - step
            while line_y > bottom + 0.5:
                c.line(left, line_y, right, line_y)
                line_y -= step
            c.setStrokeColor(HexColor("#A8A8A8"))
            c.setLineWidth(0.7)
            c.rect(left, bottom, right - left, height, stroke=1, fill=0)
        else:  # box
            c.setStrokeColor(HexColor("#9A9A9A"))
            c.setLineWidth(0.8)
            c.setDash(3, 2)
            c.roundRect(left, bottom, right - left, height, 4, stroke=1, fill=0)
        c.restoreState()
        if style != "lines":
            font_en = self.font_en_regular
            font_ar = self.font_ar_regular or font_en
            _draw_mixed_line(self.c, "مسودة" if self.is_ar else "Draft", self.is_ar, font_en, font_ar, 7,
                             top - 9, "#9A9A9A", self.margin, self.page_w, extra_indent=5,
                             x_left=left, x_right=right)
        self.y = bottom - self._DRAFT_GAP_AFTER

    def _draw_question_plain(self, idx: int, q: Dict[str, Any]) -> None:
        cfg = self.cfg
        label = f"{'السؤال' if self.is_ar else 'Question'} {idx}: {latex_to_plain(str(q.get('question', '')).strip())}"
        self._ensure_space(30)
        label_color = cfg["accent"] if self.style == STYLE_ACADEMIC else "#111111"
        self._draw_paragraph(label, 12.5, bold=True, color=label_color, gap_after=4)
        sep = "." if self.style == STYLE_ACADEMIC else ")"
        items = []
        for oidx, opt in enumerate(q.get("options") or []):
            letter = self.letters[oidx] if oidx < len(self.letters) else str(oidx + 1)
            items.append(f"{letter}{sep} {latex_to_plain(str(opt))}")
        self._draw_options_block(items, 11, 18, 2, self._box_w - 18)
        self._draw_draft(q)
        if cfg["divider"]:
            self._ensure_space(14)
            self.c.setStrokeColor(HexColor("#AAAAAA"))
            self.c.setLineWidth(0.75)
            self.c.line(self.box_left, self.y, self.box_right, self.y)
            self.y -= 6
        self.y -= 8

    def _render_questions(self):
        if self.options.columns == 2:
            self._render_questions_columns()
            return
        if self.options.per_page:
            self._render_questions_paged()
            return
        for idx, q in enumerate(self.questions, 1):
            self._draw_question(idx, q)

    def _draw_question_boxed(self, idx: int, q: Dict[str, Any]) -> None:
        """ستايل 'modern': كل سؤال جوا بطاقة (مستطيل بحواف دائرية) بخلفية فاتحة."""
        cfg = self.cfg
        pad = 12
        font_en, font_ar = self.font_en_regular, (self.font_ar_regular or self.font_en_regular)
        font_en_b, font_ar_b = self.font_en_bold, (self.font_ar_bold or self.font_en_bold)
        max_w = self._box_w - 2 * pad

        label = f"{'السؤال' if self.is_ar else 'Question'} {idx}: {latex_to_plain(str(q.get('question', '')).strip())}"
        options = [latex_to_plain(str(o)) for o in (q.get("options") or [])]

        q_lines = _wrap_line(label, self.is_ar, font_en_b, font_ar_b, 12.5, max_w)
        content_h = 2 * pad + len(q_lines) * (12.5 + 5) + 6
        items = [f"{self.letters[oidx] if oidx < len(self.letters) else str(oidx + 1)}) {opt}"
                 for oidx, opt in enumerate(options)]
        rows = self._compact_rows(items, 11, max_w - 10)
        if rows is not None:
            content_h += len(rows) * (11 + 4) + 3 * len(rows)
        else:
            for text in items:
                lines = _wrap_line(text, self.is_ar, font_en, font_ar, 11, max_w - 10)
                content_h += len(lines) * (11 + 4) + 3

        if content_h + 14 > self.page_h - 2 * self.margin - 25:
            # بطاقة أطول من صفحة كاملة: كانت تُقصّ خارج الصفحة. نرسمها كنص عادي يتابع
            # عبر الصفحات بدل البطاقة (لا قص ولا فقدان محتوى).
            self._draw_question_plain(idx, q)
            return

        self._ensure_space(content_h + 14)
        top_y = self.y
        self.c.setFillColor(HexColor(cfg["box_bg"]))
        self.c.roundRect(self.box_left, top_y - content_h, self._box_w, content_h,
                          6, fill=1, stroke=0)
        self.c.setStrokeColor(HexColor(cfg["box_border"]))
        self.c.setLineWidth(1)
        self.c.roundRect(self.box_left, top_y - content_h, self._box_w, content_h,
                          6, fill=0, stroke=1)

        self.y = top_y - pad
        self._draw_paragraph(label, 12.5, bold=True, color=cfg["accent"], extra_indent=pad, gap_after=6)
        self._draw_options_block(items, 11, pad + 10, 3, max_w - 10)
        self.y = top_y - content_h - 12
        self._draw_draft(q)

    # ---------- عدد الأسئلة بالصفحة ----------

    def _measuring_copy(self) -> "_QuizPDFRenderer":
        """نسخة على قماش وهمي وصفحة ضخمة: نفس كود الرسم يقيس الارتفاع الحقيقي بلا رسم."""
        probe = _QuizPDFRenderer(self.title, self.questions, self.is_ar, style=self.style,
                                 include_answers=False, options=self.options)
        probe.force_dense = self.force_dense
        probe.box_left, probe.box_right = self.box_left, self.box_right
        probe.c = _NullCanvas()
        probe.page_h = 1e7
        probe.y = probe.page_h - probe.margin
        probe._plans = self._plans
        probe._keep_plans = True
        probe._draw_print_question = probe._measure_print_question
        return probe

    def _measure_print_question(self, q, heading, plan=None):
        display_w = self._box_w
        for chunk in (plan if plan is not None else plan_print_blocks(q, self.is_ar, self._print_px_width())):
            self.y -= display_w * chunk_height_px(chunk) / chunk_width_px(chunk) + 10

    def _measure_question(self, probe: "_QuizPDFRenderer", idx: int, q: Dict[str, Any]) -> float:
        probe.y = probe.page_h - probe.margin
        start = probe.y
        if _is_math_question(q):
            # التخطيط يُحسب مرة هنا ويُعاد استعماله عند الرسم الفعلي.
            try:
                probe._plans[idx - 1] = plan_print_blocks(q, self.is_ar, probe._print_px_width())
            except Exception as exc:
                raise ExportError(f"تعذّر تنسيق السؤال {idx} للطباعة؛ راجع صيغة المعادلات أو حجم الجدول.") from exc
            for chunk in probe._plans[idx - 1]:
                probe.y -= probe._box_w * chunk_height_px(chunk) / chunk_width_px(chunk) + 10
            probe.y -= probe._draft_total()
        else:
            probe._draw_question(idx, q)
        return start - probe.y

    def _render_questions_paged(self) -> None:
        """يوزّع الأسئلة على صفحات بحد أقصى per_page للصفحة، بقياس حقيقي لكل سؤال.

        المحتوى الطويل يخفّض عدد أسئلة تلك الصفحة تلقائياً بدل التزاحم أو القص،
        والفراغ المتبقي يُوزَّع بين الأسئلة لتبدو الصفحة ورقة امتحان متوازنة."""
        self._keep_plans = True
        probe = self._measuring_copy()
        heights = [self._measure_question(probe, idx, q) for idx, q in enumerate(self.questions, 1)]
        bottom = self.margin + 25
        tolerance = 3.0
        first_cap = self.y - bottom - tolerance
        cap = self.page_h - self.margin - bottom - tolerance
        pages = plan_pages(heights, self.options.per_page, first_cap, cap)
        for page_no, group in enumerate(pages):
            if page_no and not self._page_is_fresh():
                self._new_page()
            used = sum(heights[i] for i in group)
            free = (first_cap if page_no == 0 else cap) - used
            gap = spread_gap(free, len(group))
            for i in group:
                self._draw_question(i + 1, self.questions[i])
                self.y -= gap

    COLUMN_GUTTER = 24.0

    def _render_questions_columns(self) -> None:
        """صفحة بعمودين يفصلهما خط منصّف اختياري. القياس الحقيقي بعرض العمود، والتخطيط
        (export_layout.plan_column_pages) يوزّع الأسئلة؛ ما لا يتسع بعرض العمود (معادلة/جدول
        عريض أو سؤال أطول من عمود) يأخذ صفحة بعرض كامل. للعربي العمود الأول هو الأيمن."""
        self._keep_plans = True
        full_left, full_right = self.box_left, self.box_right
        col_w = (full_right - full_left - self.COLUMN_GUTTER) / 2
        probe = self._measuring_copy()
        probe.box_left, probe.box_right = full_left, full_left + col_w
        full_probe = self._measuring_copy()
        bottom = self.margin + 25
        tolerance = 3.0
        first_cap = self.y - bottom - tolerance
        cap = self.page_h - self.margin - bottom - tolerance

        heights: List[float] = []
        wide = set()
        for idx, q in enumerate(self.questions, 1):
            try:
                heights.append(self._measure_question(probe, idx, q))
            except ExportError:
                if not _is_math_question(q):
                    raise
                # لا يتسع بعرض العمود: نخطّطه بعرض الصفحة؛ فشله هناك يرفع الخطأ الصريح المعتاد.
                self._measure_question(full_probe, idx, q)
                wide.add(idx - 1)
                heights.append(cap + 1)
        pages = plan_column_pages(heights, self.options.per_page, 2, first_cap, cap, frozenset(wide))

        # ترتيب القراءة: العربي من اليمين لليسار.
        lefts = [full_left, full_left + col_w + self.COLUMN_GUTTER]
        if self.is_ar:
            lefts.reverse()
        for page_no, page in enumerate(pages):
            if page_no and not self._page_is_fresh():
                self._new_page()
            if page.full:
                (i,) = page.columns[0]
                self.box_left, self.box_right = full_left, full_right
                if _is_math_question(self.questions[i]):
                    self._plans.pop(i, None)
                    self._measure_question(full_probe, i + 1, self.questions[i])   # تخطيط بعرض الصفحة
                self._draw_question(i + 1, self.questions[i])
                continue
            page_cap = first_cap if page_no == 0 else cap
            top_y = self.y
            lowest = top_y
            # مسافة موحّدة بين الأسئلة في العمودين (الأصغر) فتصطف الصفوف ويبقى شكل الورقة متوازناً.
            gap = min(spread_gap(page_cap - sum(heights[i] for i in group), len(group))
                      for group in page.columns)
            for position, group in enumerate(page.columns):
                self.box_left = lefts[position]
                self.box_right = self.box_left + col_w
                self.y = top_y
                for i in group:
                    self._draw_question(i + 1, self.questions[i])
                    self.y -= gap
                lowest = min(lowest, self.y)
            if self.options.column_divider and len(page.columns) == 2:
                mid = full_left + col_w + self.COLUMN_GUTTER / 2
                self.c.saveState()
                self.c.setStrokeColor(HexColor("#9A9A9A"))
                self.c.setLineWidth(0.6)
                self.c.line(mid, top_y + 2, mid, top_y - page_cap)
                self.c.restoreState()
            self.y = lowest
        self.box_left, self.box_right = full_left, full_right

    def _draw_branding(self) -> None:
        """شارة الختام الإجبارية (لوغو أبيض + تلغرام + @AbiadQuizMakerbot) بمنتصف الصفحة.

        إن كانت الصفحة ممتلئة تماماً (مثلاً «16 سؤال بالصفحة») تُرسم نسخة مصغّرة بالهامش السفلي
        بدل دفعها لصفحة جديدة؛ ولا تُرسم فوق سطر الأستاذ بالتذييل (عندها تنتقل لصفحة جديدة).
        """
        aspect = branding_aspect()
        image = ImageReader(io.BytesIO(branding_png()))
        full_w = 210.0
        if self.y - (full_w * aspect + 24) < self.margin + 25:
            compact_w = 150.0
            compact_h = compact_w * aspect
            footer_line = bool(self.options.teacher_name and self.options.teacher_placement == "footer")
            floor = 52.0 if footer_line else 37.0   # أعلى من رقم الصفحة وسطر الأستاذ
            if self.y - 4 - compact_h >= floor:
                self.c.drawImage(image, (self.page_w - compact_w) / 2, self.y - 4 - compact_h,
                                 width=compact_w, height=compact_h, mask="auto")
                self.y -= compact_h + 4
                return
        height = full_w * aspect
        self._ensure_space(height + 24)
        self.y -= 14
        self.c.drawImage(image, (self.page_w - full_w) / 2, self.y - height,
                         width=full_w, height=height, mask="auto")
        self.y -= height + 6

    def _render_teacher_closing(self) -> None:
        """«إعداد الأستاذ» في آخر صفحة الأسئلة (قبل صفحة الإجابات إن وُجدت)."""
        self._ensure_space(60)
        self.y -= 10
        self.c.setStrokeColor(HexColor("#AAAAAA"))
        self.c.setLineWidth(0.75)
        self.c.line(self.margin, self.y, self.page_w - self.margin, self.y)
        self.y -= 22
        self._draw_paragraph(teacher_text(self.options, self.is_ar), 12, bold=True,
                             color=self.cfg["accent"], center=True)

    def _render_answer_table(self):
        self._new_page()
        self._draw_paragraph("الإجابات والحلول" if self.is_ar else "Answers and solutions",
                             15, bold=True, gap_after=12)
        key_only = self.options.answers_mode == "key"
        for idx, q in enumerate(self.questions, 1):
            answer = _answer_print_question(q, self.is_ar, with_explanation=not key_only)
            if key_only and not _is_math_question(q) and not _is_math_question(answer):
                # مفتاح مضغوط: سطر نصي واحد لكل سؤال (الصور فقط للمحتوى الرياضي)
                self._draw_paragraph(f"{idx}) " + answer["question"].strip().replace("\n", " "), 11.5,
                                     gap_after=3)
                continue
            try:
                self._draw_print_question(answer, str(idx))
            except Exception as exc:
                raise ExportError(f"تعذّر تنسيق إجابة السؤال {idx}؛ راجع المعادلات.") from exc

    def render(self) -> bytes:
        self._draw_page_header()
        self._render_header()
        self._render_questions()
        if self.options.teacher_name and self.options.teacher_placement == "last":
            self._render_teacher_closing()
        self._draw_branding()   # إجباري: نهاية الأسئلة
        if self.include_answers:
            self._render_answer_table()
            self._draw_branding()   # إجباري: نهاية ملف نسخة الأستاذ
        self._draw_footer()
        self.c.save()
        self.buf.seek(0)
        return self.buf.getvalue()


def build_quiz_pdf(title: str, questions: List[Dict[str, Any]], style: str = DEFAULT_STYLE,
                   include_answers: bool = True, options=None) -> bytes:
    """يبني ملف PDF جميل التنسيق للكويز، مع اتجاه تلقائي حسب اللغة وستايل شكلي قابل للاختيار
    (يتطلب خطوط assets/fonts للعربي)."""
    if not questions:
        raise ExportError("لا توجد أسئلة لتصديرها.")
    if len(questions) > 200 or sum(len(str(q)) for q in questions) > 500000:
        raise ExportError("هذا الكويز كبير للتصدير دفعة واحدة؛ قسّمه إلى اختبارات أصغر (حتى ٢٠٠ سؤال).")
    is_ar = detect_language(questions) == "ar"
    renderer = _QuizPDFRenderer(title, questions, is_ar, style=style, include_answers=include_answers,
                                options=coerce_options(options))
    return renderer.render()


def max_questions_per_page_docx(questions: List[Dict[str, Any]], style: str = DEFAULT_STYLE,
                                options=None) -> int:
    """نفس الفكرة لملف Word: صفحته Letter بهوامش أكبر فيتسع أقل مما في PDF (تقدير).
    options: العمودان يضاعفان السعة ويضيّقان العرض، والمسودة تزيد ارتفاع كل سؤال.
    ملاحظة: خيارات الأسئلة في Word تبقى بقائمة عمودية (بلا شبكة 2×2 كما في PDF)، فالتقدير أقل."""
    if not questions:
        return MAX_PER_PAGE
    opts = coerce_options(options)
    is_ar = detect_language(questions) == "ar"
    cfg = _DOCX_STYLES[normalize_style(style)]
    section = Document().sections[0]
    full_w = (section.page_width - section.left_margin - section.right_margin) / 12700
    columns = 2 if opts.columns == 2 else 1
    width_pt = full_w if columns == 1 else (full_w - _DOCX_COLUMN_GUTTER_PT) / 2
    px = int(round(PRINT_WIDTH * width_pt / full_w))
    capacity = (section.page_height - section.top_margin - section.bottom_margin) / 12700 * _DOCX_SAFETY
    draft = opts.draft_height
    heights = []
    for idx, q in enumerate(questions, 1):
        if _is_math_question(q):
            heights.append(_estimate_math_height(q, width_pt, full_w, (draft + 8) if draft else 0.0))
        else:
            label = f"السؤال {idx}: " if is_ar else f"Question {idx}: "
            heights.append(_docx_question_height(q, idx, is_ar, cfg, width_pt, label, draft, px))
    return pick_max_per_page(heights, capacity, MAX_PER_PAGE, columns)


def _estimate_math_height(q: Dict[str, Any], box_w: float, full_w: float, draft: float) -> float:
    """تقدير محافظ لارتفاع سؤال رياضي مصوّر بعرض box_w دون رسمه (الأسطر تتضاعف بالعمود الضيق)."""
    px_w = PRINT_WIDTH * box_w / full_w
    text_len = len(str(q.get("question", ""))) + sum(len(str(o)) for o in (q.get("options") or []))
    lines = 1 + int(text_len // max(15.0, 60.0 * px_w / PRINT_WIDTH))
    px = 90 + lines * 50 + 2 * (46 + 32) + (260 if (q.get("table") or q.get("matrices")) else 0)
    return box_w * px / px_w + 10 + draft


def max_questions_per_page(questions: List[Dict[str, Any]], style: str = DEFAULT_STYLE,
                           options=None) -> int:
    """أعلى «عدد أسئلة بالصفحة» مريح لهذا الكويز وهذا التنسيق (1..MAX_PER_PAGE).

    يُحسب من القياس الحقيقي للأسئلة النصية (نفس كود الرسم) وبتقدير محافظ للأسئلة الرياضية
    المصوّرة (دون رسمها). options تؤثر: عمودان (ارتفاع العمود × 2 وعرضه نصف) ومساحة المسودة
    والتخطيط المضغوط. المخطط يخفّض العدد بالصفحات التي فيها أسئلة أطول من النموذجي، فالقيمة هنا
    تضبط ما يعرضه البوت للمعلم لا ما يحمي من القص (ذلك مضمون عند التوليد)."""
    if not questions:
        return MAX_PER_PAGE
    opts = coerce_options(options)
    is_ar = detect_language(questions) == "ar"
    columns = 2 if opts.columns == 2 else 1
    renderer = _QuizPDFRenderer("", questions, is_ar, style=style, include_answers=False, options=opts)
    full_w = renderer.page_w - 2 * renderer.margin
    box_w = full_w if columns == 1 else (full_w - renderer.COLUMN_GUTTER) / 2
    capacity = renderer.page_h - renderer.margin - (renderer.margin + 25) - 3.0

    def heights_for(dense: bool) -> List[float]:
        renderer.force_dense = dense
        probe = renderer._measuring_copy()
        probe.box_right = probe.box_left + box_w
        heights: List[float] = []
        for idx, q in enumerate(questions, 1):
            if _is_math_question(q):
                heights.append(_estimate_math_height(q, box_w, full_w, probe._draft_total()))
                continue
            probe.y = probe.page_h - probe.margin
            start = probe.y
            probe._draw_question(idx, q)
            heights.append(start - probe.y)
        return heights

    best = pick_max_per_page(heights_for(opts.dense), capacity, MAX_PER_PAGE, columns)
    if not opts.dense and best < 8:
        # اختيار 8+ أسئلة يفعّل التخطيط المضغوط (شبكة خيارات 2×2)؛ فقد يتسع أكثر مما قيس أولاً.
        compact = pick_max_per_page(heights_for(True), capacity, MAX_PER_PAGE, columns)
        if compact >= 8:
            best = compact
    return best


class _DocumentPDFRenderer:
    """يبني PDF لنص حر (تفريغ محاضرة، ملخص، أي نص تعليمي عام) بترقيم صفحات وتشكيل
    bidi صحيح للعربي، باستخدام نفس منطق تسجيل الخطوط (Noto/Amiri) وتقسيم النص إلى
    أجزاء عربي/إنكليزي المستخدم بمُصدِّر الكويز - مكرَّر هون عمداً بدل الوراثة من
    _QuizPDFRenderer حتى يبقى مسار الكويز الحالي دون أي تعديل."""

    def __init__(self, title: str, text: str, is_ar: bool, style: str = DEFAULT_STYLE):
        self.title = title or ("مستند" if is_ar else "Document")
        self.blocks = _parse_markdown_blocks(text)
        self.is_ar = is_ar
        self.style = normalize_style(style)
        self.cfg = _PDF_STYLES[self.style]

        self.page_w, self.page_h = A4
        self.margin = 48
        self.buf = io.BytesIO()
        self.c = canvas.Canvas(self.buf, pagesize=A4)
        self.y = self.page_h - self.margin
        self.page_num = 1

        _register_pdf_fonts()
        if self.cfg["academic_fonts"] and _font_available("Arabic-Academic"):
            # خط Amiri الكلاسيكي متوفر - نستخدمه لستايل "أكاديمي" العربي تحديداً
            self.font_ar_regular = "Arabic-Academic"
            self.font_ar_bold = "Arabic-Academic-Bold" if _font_available("Arabic-Academic-Bold") \
                else "Arabic-Academic"
        else:
            self.font_ar_regular = "Arabic" if _font_available("Arabic") else None
            self.font_ar_bold = "Arabic-Bold" if _font_available("Arabic-Bold") else None
        if self.cfg["academic_fonts"]:
            # Times-Roman/Times-Bold من الخطوط الأساسية الـ 14 المدمجة بـ reportlab -
            # موجودة دايماً بدون ما نحتاج ملف خط، وبتعطي طابع أكاديمي/رسمي كلاسيكي.
            self.font_en_regular = "Times-Roman"
            self.font_en_bold = "Times-Bold"
        else:
            self.font_en_regular = "Latin" if _font_available("Latin") else "Helvetica"
            self.font_en_bold = "Latin-Bold" if _font_available("Latin-Bold") else "Helvetica-Bold"

        if is_ar:
            self.font_regular = self.font_ar_regular
            self.font_bold = self.font_ar_bold
        else:
            self.font_regular = self.font_en_regular
            self.font_bold = self.font_en_bold

        if is_ar and (not self.font_regular or not self.font_bold):
            raise ExportError(
                "تعذّر إنشاء PDF بالعربية لعدم توفر خط عربي على الخادم حالياً. "
                "جرّب تصدير Word بدلاً من ذلك."
            )

    # ---------- أدوات رسم أساسية (نفس منطق _QuizPDFRenderer) ----------

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
                         center: bool = False):
        font_en = self.font_en_bold if bold else self.font_en_regular
        font_ar = (self.font_ar_bold if bold else self.font_ar_regular) or font_en
        max_width = self.page_w - 2 * self.margin - extra_indent
        lines = _wrap_line(text, self.is_ar, font_en, font_ar, size, max_width)
        for line in lines:
            self._ensure_space(size + 5)
            _draw_mixed_line(self.c, line, self.is_ar, font_en, font_ar, size, self.y, color,
                              self.margin, self.page_w, extra_indent=extra_indent, center=center)
            self.y -= (size + 5)
        self.y -= gap_after

    # ---------- الأقسام ----------

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
            self._draw_paragraph(self.title, 19, bold=True, color=cfg["accent"], gap_after=14, center=True)

        if self.style == STYLE_ACADEMIC:
            info = "الاسم: ________________________     التاريخ: ______________" if self.is_ar \
                else "Name: ________________________     Date: ______________"
            self._draw_paragraph(info, 11, color="#1E1E1E", gap_after=2)
            self.c.setStrokeColor(HexColor("#333333"))
            self.c.setLineWidth(1)
            self.c.line(self.margin, self.y + 8, self.page_w - self.margin, self.y + 8)
            self.y -= 12

    def _render_body(self):
        cfg = self.cfg
        for block_type, content in self.blocks:
            if not content.strip():
                continue
            if block_type == "h1":
                self._ensure_space(30)
                self._draw_paragraph(content, 15, bold=True, color=cfg["accent"], gap_after=8)
                if cfg["divider"]:
                    self.c.setStrokeColor(HexColor("#AAAAAA"))
                    self.c.setLineWidth(0.75)
                    self.c.line(self.margin, self.y + 4, self.page_w - self.margin, self.y + 4)
                    self.y -= 4
            elif block_type == "h2":
                self._ensure_space(24)
                self._draw_paragraph(content, 13, bold=True, color=cfg["accent"], gap_after=6)
            elif block_type == "bullet":
                self._draw_paragraph(f"• {content}", 11, color="#111111",
                                      extra_indent=16, gap_after=4)
            else:
                self._draw_paragraph(content, 11, color="#111111", gap_after=8)

    def render(self) -> bytes:
        self._render_header()
        self._render_body()
        self._draw_footer()
        self.c.save()
        self.buf.seek(0)
        return self.buf.getvalue()


def build_document_pdf(title: str, text: str, style: str = DEFAULT_STYLE) -> bytes:
    """يبني ملف PDF منسّق لنص حر كامل (تفريغ محاضرة، ملخص...) بخطوط Noto Naskh Arabic/
    Amiri للعربي وNoto Sans/Times للإنكليزي، تشكيل bidi صحيح لأي نص عربي مدسوس وسط
    نص إنكليزي أو العكس، وترقيم صفحات تلقائي - بنفس الستايلات الشكلية المتوفرة
    بتصدير الكويز (simple / modern / academic)."""
    if not text or not text.strip():
        raise ExportError("لا يوجد نص لتصديره.")
    is_ar = detect_text_language(text) == "ar"
    renderer = _DocumentPDFRenderer(title, text, is_ar, style=style)
    return renderer.render()

