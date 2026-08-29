# services/image_quiz_renderer.py
"""
==============================================================================
MODULE: Image Quiz Renderer (نمط الكويز المصوّر LaTeX)
==============================================================================
الوصف:
يرسم كل سؤال رياضي (نص السؤال + الخيارات الأربعة، المنسّقة بصيغة LaTeX ضمن
علامتي $...$) كصورة PNG واحدة، لأن Telegram Poll لا يدعم عرض معادلات رياضية.
بعدها يُرسل Poll منفصل يحوي فقط حروف الإجابة (أ/ب/ج/د أو A/B/C/D) لأن السؤال
والخيارات موجودة بالكامل داخل الصورة أعلاه.

القرارات الهندسية:
1. Matplotlib mathtext بدل توزيع LaTeX كامل (TeX Live): يدعم مجموعة كافية من
   الرموز الرياضية الشائعة (كسور، أسس، جذور، مجاميع...) دون الحاجة لتثبيت
   توزيعة LaTeX ضخمة على السيرفر - مناسب تماماً لبيئة Docker/Azure خفيفة.
2. فشل آمن لكل سطر على حدة: أي رمز LaTeX غير مدعوم بمحرك mathtext لا يُسقط
   الصورة كاملة - يتم تجريد علامات $ لذلك السطر تحديداً وعرضه كنص عادي بدلاً
   من ذلك (تجربة أفضل من فشل توليد الصورة بالكامل).
3. إعادة استخدام أسلوب Reshape/Bidi نفسه المُثبت مسبقاً في export_service.py
   لدعم النصوص العربية RTL، مع ترك أي رمز رياضي ($...$) كما هو (bidi تتعامل
   معه تلقائياً كوحدة LTR واحدة ضمن السطر العربي).
==============================================================================
"""
import io
import os
import re
from typing import Any, Dict, List, Optional

import matplotlib
matplotlib.use("Agg")
import matplotlib.font_manager as fm
import matplotlib.pyplot as plt
from matplotlib.mathtext import MathTextParser

from logger import get_logger, log_warning

logger = get_logger(__name__)

_BIDI_AVAILABLE = True
try:
    import arabic_reshaper
    from bidi.algorithm import get_display
except ImportError:  # pragma: no cover
    _BIDI_AVAILABLE = False

# ==================== إعدادات عامة ====================
FONTS_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "assets", "fonts")

ARABIC_LETTERS = ["أ", "ب", "ج", "د", "هـ", "و", "ز", "ح"]
ENGLISH_LETTERS = ["A", "B", "C", "D", "E", "F", "G", "H"]

FIG_WIDTH_PX = 1000
DPI = 150
MARGIN_PX = 65            # هامش خارجي بين حافة الصورة ومنطقة المحتوى (زودناه عشان السؤال ميبقاش لازق في حافة الصورة)
HEADER_HEIGHT_PX = 76
QUESTION_LINE_HEIGHT_PX = 52   # تباعد عمودي بين أسطر السؤال (أكبر شوية من الافتراضي)
OPTION_LINE_HEIGHT_PX = 50     # تباعد عمودي بين أسطر الخيار الواحد لو التف على أكتر من سطر
OPTION_GAP_PX = 22
QUESTION_FONT_SIZE = 21
OPTION_FONT_SIZE = 19
BADGE_COLORS = ["#2E86DE", "#10AC84", "#EE5253", "#F5A623", "#8854D0", "#00B8D9", "#EA5455", "#5E5CE6"]

# 🆕 ==================== إعدادات جدول البيانات (Data Table) ====================
# جدول بيانات اختياري (مثال: توزيع تكراري بالإحصاء) يُرسم مباشرة بعناصر Matplotlib
# (مستطيلات + نص) وليس بصيغة LaTeX نصية - لأن محرك mathtext لا يدعم بيئات الجداول
# إطلاقاً (راجع تعليمات SYSTEM_PROMPT_GENERATE_MATH_QUESTIONS بـ constants.py). هيك
# تُرسل المسألة كاملة (سؤال + جدول + خيارات) بصورة واحدة غير قابلة للتقسيم.
TABLE_FONT_SIZE = 16
TABLE_ROW_LINE_HEIGHT_PX = 24
TABLE_CELL_PAD_X = 10
TABLE_CELL_PAD_Y = 10
TABLE_HEADER_BG = "#4C6FFF"
TABLE_ROW_BG_ALT = "#F5F7FF"
TABLE_BORDER_COLOR = "#C7D0E0"
TABLE_TOP_GAP_PX = 18
TABLE_BOTTOM_GAP_PX = 26

# 🆕 ==================== إعدادات المصفوفات (Matrices) ====================
# مصفوفة/محدّد رياضي اختياري يُرسم مباشرة بعناصر Matplotlib (خطوط للأقواس + شبكة نص)
# بنفس مبدأ جدول البيانات أعلاه بالضبط - لأن mathtext لا يدعم \begin{matrix}/\begin{pmatrix}
# إطلاقاً (راجع تعليمات SYSTEM_PROMPT_GENERATE_MATH_QUESTIONS بـ constants.py).
MATRIX_FONT_SIZE = 19
MATRIX_LABEL_FONT_SIZE = 19
MATRIX_CELL_PAD_X = 22          # هامش أفقي بين الخلايا (كل خلية بعرض موحّد داخل نفس المصفوفة)
MATRIX_ROW_GAP_PX = 30          # تباعد عمودي بين مراكز صفوف المصفوفة
MATRIX_BRACKET_MARGIN_PX = 16   # مسافة بين آخر عمود/أول عمود وخط القوس
MATRIX_BRACKET_CAP_PX = 12      # امتداد "خطاف" القوس المربع أعلى/أسفل الشبكة، ونصف قطر القوس الدائري
MATRIX_BRACKET_LINEWIDTH = 1.8
MATRIX_LABEL_GAP_PX = 10        # مسافة بين نص التسمية (label) والقوس
MATRIX_GAP_BETWEEN_PX = 46      # مسافة أفقية بين مصفوفتين متجاورتين
MATRIX_TOP_GAP_PX = 20
MATRIX_BOTTOM_GAP_PX = 28
MATRIX_COLOR = "#1a1a1a"

_ARABIC_RE = re.compile(r"[\u0600-\u06FF\u0750-\u077F\u08A0-\u08FF\uFB50-\uFDFF\uFE70-\uFEFF]")
_LETTER_RE = re.compile(r"[^\W\d_]", re.UNICODE)
_MATH_SPAN_RE = re.compile(r"\$[^$]+\$")
# 🛠️ FIX: حروف تحكّم ASCII خام (form-feed \x0c، tab \x09، إلخ) قد تتسرّب لنص السؤال/
# الخيارات لو نجا JSON تالف (backslash غير مُهرَّب بشكل صحيح) من مرحلة التوليد - matplotlib
# mathtext لا يرمي استثناءً لحرف تحكّم غير معروف (يستبدله بصمت برمز/صندوق فارغ)، فلا يكفي
# الاعتماد على try/except في _sanitize_line_for_mathtext وحده لاكتشاف هذه الحالة تحديداً.
_CONTROL_CHARS_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")

_MATH_PARSER = MathTextParser("agg")
_FONT_CACHE: Dict[Any, fm.FontProperties] = {}


# ==================== أدوات اللغة والحروف ====================
def looks_arabic(text: str) -> bool:
    """يكتشف هل النص عربي بالأغلبية اعتماداً على نسبة الأحرف العربية بين كل الأحرف."""
    letters = _LETTER_RE.findall(text or "")
    if not letters:
        return True  # افتراضياً نعتبره عربياً (لغة البوت الأساسية) لو تعذر الحكم
    arabic_count = sum(1 for ch in letters if _ARABIC_RE.match(ch))
    return (arabic_count / len(letters)) > 0.3


def letters_for(is_ar: bool, count: int) -> List[str]:
    """يرجع قائمة حروف الإجابة (أ/ب/ج/د أو A/B/C/D) بعدد الخيارات الفعلي."""
    pool = ARABIC_LETTERS if is_ar else ENGLISH_LETTERS
    if count <= len(pool):
        return pool[:count]
    return pool + [str(i + 1) for i in range(len(pool), count)]


# ==================== الخطوط ====================
def _font_prop(is_ar: bool, bold: bool = False) -> fm.FontProperties:
    key = (is_ar, bold)
    if key not in _FONT_CACHE:
        if is_ar:
            filename = "Amiri-Bold.ttf" if bold else "Amiri-Regular.ttf"
        else:
            filename = "NotoSans-Bold.ttf" if bold else "NotoSans-Regular.ttf"
        path = os.path.join(FONTS_DIR, filename)
        _FONT_CACHE[key] = fm.FontProperties(fname=path) if os.path.exists(path) else fm.FontProperties(weight="bold" if bold else "normal")
    return _FONT_CACHE[key]


# ==================== تجزئة النص والالتفاف (Wrapping) مع مراعاة $...$ ====================
def _tokenize_mixed(text: str) -> List[str]:
    """يقسّم النص إلى كلمات، مع إبقاء كل مقطع رياضي $...$ ككلمة واحدة غير قابلة للتجزئة
    (حتى لو حوى مسافات داخلية) لمنع كسر معادلة في منتصفها عند الالتفاف على الأسطر."""
    tokens: List[str] = []
    pos = 0
    for m in _MATH_SPAN_RE.finditer(text):
        tokens.extend(text[pos:m.start()].split())
        tokens.append(m.group(0))
        pos = m.end()
    tokens.extend(text[pos:].split())
    return tokens


def _font_prop_sized(is_ar: bool, bold: bool, size: float) -> fm.FontProperties:
    """نسخة من _font_prop لكن بحجم خط محدد - مطلوبة لقياس العرض الفعلي بالبكسل
    (القياس بيختلف كتير حسب الحجم، فمينفعش نستخدم FontProperties افتراضي)."""
    prop = _font_prop(is_ar, bold).copy()
    prop.set_size(size)
    return prop


def _measure_px(text: str, font_prop: fm.FontProperties) -> float:
    """يقيس العرض الفعلي بالبكسل لأي مقطع (نص عادي أو معادلة $...$) باستخدام نفس
    محرك mathtext وحجم/نوع الخط اللي هيُستخدم فعلياً في الرسم النهائي - أدق بكتير
    من عدّ عدد الحروف، خصوصاً إن طول مصدر LaTeX مش له علاقة بعرضه المرسوم فعلياً."""
    if not text:
        return 0.0
    try:
        return _MATH_PARSER.parse(text, dpi=DPI, prop=font_prop).width
    except Exception:
        # سطر LaTeX غير صالح لسه ما اتلفش على $ - نرجّع تقدير تقريبي بدل ما نفشل
        return len(text) * font_prop.get_size() * 0.62


def _measure_line_height_px(text: str, font_prop: fm.FontProperties, fallback_px: float) -> float:
    """يقيس الارتفاع الفعلي (height + depth) لسطر واحد باستخدام نفس محرك mathtext،
    بدل الاعتماد على ثابت واحد لكل الأسطر. مهم جداً للأسطر يلي فيها كسور/جذور/مجاميع
    بحدود، لأنها بترتفع فعلياً أكتر بكتير من سطر نص عادي (مثلاً \\sum_{i=1}^{n} ممكن
    يطلع أطول من السطر العادي بـ3 أضعاف)، وبدون القياس ده الأسطر بتتراكب فوق بعضها
    أو بتنقص عند حافة الصورة السفلية. بنضيف هامش أمان 20% فوق القيمة المقاسة."""
    if not text:
        return fallback_px
    try:
        r = _MATH_PARSER.parse(text, dpi=DPI, prop=font_prop)
        # 🛠️ FIX: رفعنا هامش الأمان من 1.2 إلى 1.3 لتقليل حالات "overflow detected,
        # extending canvas" الملحوظة حتى مع أسطر LaTeX سليمة تماماً (كسور/جذور مرتفعة).
        measured = (r.height + r.depth) * 1.3
        return max(measured, fallback_px)
    except Exception:
        return fallback_px


def _wrap_tokens(tokens: List[str], max_width_px: float, is_ar: bool, font_prop: fm.FontProperties) -> List[str]:
    """يلف الكلمات على أسطر بالاعتماد على العرض الفعلي بالبكسل (مش عدد الحروف)،
    مع تشكيل أي كلمة عربية قبل قياسها عشان القياس يطابق شكلها الحقيقي وقت الرسم."""
    space_w = _measure_px(" ", font_prop)
    lines: List[str] = []
    current: List[str] = []
    current_w = 0.0
    for tok in tokens:
        tok_for_measure = _shape_line(tok) if is_ar else tok
        tok_w = _measure_px(tok_for_measure, font_prop)
        added_w = tok_w + (space_w if current else 0.0)
        if current and current_w + added_w > max_width_px:
            lines.append(" ".join(current))
            current, current_w = [], 0.0
            added_w = tok_w
        current.append(tok)
        current_w += added_w
    if current:
        lines.append(" ".join(current))
    return lines or [""]


def _shape_line(line: str) -> str:
    """يهيّئ ويعيد ترتيب سطر عربي للعرض الصحيح (Reshape + Bidi) - نفس الأسلوب المُثبت
    مسبقاً في services/export_service.py.

    ملاحظة مهمة: تطبيق get_display() على السطر كاملاً وهو يحوي مقطعاً رياضياً $...$
    يكسر المعادلة - لأن خوارزمية bidi بتعمل mirroring للأقواس ()/{} وإعادة ترتيب
    لمحتوى الرن اللاتيني جوه سياق عربي (اتحقّق منه فعلياً: "$(x+y)^2$" بيتحول
    لـ"x+y)^2$)$"). المفروض نحمي المقطع الرياضي بعلامات Unicode directional
    isolate (FSI/PDI)، لكن نسخة python-bidi المُثبّتة (0.4.2) لا تدعمها أصلاً.
    الحل البديل: نفصل السطر يدوياً لمقاطع (نص عربي / معادلة) بترتيبها المنطقي،
    نُشكّل ونعيد ترتيب كل مقطع عربي بمفرده (bidi على نص عربي خالص دايماً سليم)،
    ونترك أي مقطع رياضي كما هو حرفياً بدون أي لمسة، ثم نعكس ترتيب المقاطع نفسها
    (مش محتواها) عشان يطابق اتجاه القراءة RTL - فتطلع المعادلة في مكانها الصحيح
    جوه الجملة ومحتواها سليم 100% زي ما ولّده النموذج."""
    if not _BIDI_AVAILABLE or not _ARABIC_RE.search(line):
        return line
    try:
        text_segments = _MATH_SPAN_RE.split(line)
        math_segments = _MATH_SPAN_RE.findall(line)
        parts: List[str] = []
        for i, seg in enumerate(text_segments):
            if seg:
                parts.append(get_display(arabic_reshaper.reshape(seg)) if _ARABIC_RE.search(seg) else seg)
            if i < len(math_segments):
                parts.append(math_segments[i])
        return "".join(reversed(parts))
    except Exception:
        return line


def _wrap_and_shape(text: str, is_ar: bool, max_width_px: float, font_prop: fm.FontProperties) -> List[str]:
    lines = _wrap_tokens(_tokenize_mixed(text), max_width_px, is_ar, font_prop)
    if is_ar:
        return [_shape_line(line) for line in lines]
    return lines


def _sanitize_line_for_mathtext(line: str) -> str:
    """يتحقق أن matplotlib قادر فعلاً على تفسير أي صيغة LaTeX موجودة بالسطر قبل رسمه؛
    لو فشل (رمز LaTeX غير مدعوم أرسله النموذج)، يجرّد علامات $ ويعرض السطر كنص عادي
    بدل تعطيل توليد الصورة بأكملها.

    🛠️ FIX: نتحقق أولاً من وجود حروف تحكّم خام (راجع _CONTROL_CHARS_RE) لأن mathtext
    لا يرمي استثناءً لحرف تحكّم غير معروف (ينجح التحليل ويستبدله بصمت بصندوق فارغ) -
    فبدون هذا الفحص كانت هذه الحالة تحديداً تفلت من try/except أدناه بالكامل."""
    if _CONTROL_CHARS_RE.search(line):
        return _CONTROL_CHARS_RE.sub("", line).replace("$", "")
    if "$" not in line:
        return line
    try:
        _MATH_PARSER.parse(line, dpi=DPI)
        return line
    except Exception:
        return line.replace("$", "")


# ==================== جدول البيانات (Data Table) ====================
def _prepare_table(table: Any, is_ar: bool, max_width_px: float) -> Optional[Dict[str, Any]]:
    """يجهّز بيانات الجدول للرسم: أعمدة متساوية العرض، كل خلية مُلفوفة ومُهيّأة (RTL
    لو عربي) ومُطهّرة من رموز LaTeX غير المدعومة بنفس أسلوب سطور السؤال/الخيارات.
    يرجع None لو الجدول فارغ أو غير صالح (بدون أعمدة فعلية)."""
    if not table:
        return None
    headers = [str(h).strip() for h in (table.get("headers") or [])]
    rows = [[str(c).strip() for c in (row or [])] for row in (table.get("rows") or [])]
    ncols = len(headers) if headers else (len(rows[0]) if rows else 0)
    if ncols == 0:
        return None
    # توحيد عدد أعمدة كل صف مع رأس الجدول (حماية من صفوف ناقصة/زائدة يرجعها النموذج)
    rows = [(row + [""] * ncols)[:ncols] for row in rows]

    col_width = max_width_px / ncols
    cell_font_prop = _font_prop_sized(is_ar, bold=False, size=TABLE_FONT_SIZE)
    header_font_prop = _font_prop_sized(is_ar, bold=True, size=TABLE_FONT_SIZE)
    cell_wrap_width = max(col_width - 2 * TABLE_CELL_PAD_X, 30)

    def _prep_row(cells: List[str], font_prop: fm.FontProperties) -> List[List[str]]:
        return [
            [_sanitize_line_for_mathtext(line) for line in _wrap_and_shape(cell, is_ar, cell_wrap_width, font_prop)]
            for cell in cells
        ]

    header_lines = _prep_row(headers, header_font_prop) if headers else []
    row_lines = [_prep_row(row, cell_font_prop) for row in rows]

    def _row_height(lines_per_cell: List[List[str]]) -> float:
        max_lines = max((len(lines) for lines in lines_per_cell), default=1) or 1
        return max_lines * TABLE_ROW_LINE_HEIGHT_PX + 2 * TABLE_CELL_PAD_Y

    header_h = _row_height(header_lines) if header_lines else 0.0
    row_heights = [_row_height(lines) for lines in row_lines]
    total_h = header_h + sum(row_heights)
    if total_h <= 0:
        return None

    return {
        "ncols": ncols, "col_width": col_width,
        "header_lines": header_lines, "header_h": header_h,
        "row_lines": row_lines, "row_heights": row_heights,
        "total_h": total_h,
    }


def _draw_table(ax, table_data: Dict[str, Any], x_left: float, y_top: float,
                 max_width_px: float, is_ar: bool) -> float:
    """يرسم الجدول المُجهَّز مسبقاً (_prepare_table) داخل الـ Axes الحالي، ويرجع
    إحداثي y أسفل الجدول (بعد آخر صف) ليكمل الرسم من هناك (الخيارات...)."""
    ncols = table_data["ncols"]
    col_w = table_data["col_width"]
    header_font_prop = _font_prop_sized(is_ar, bold=True, size=TABLE_FONT_SIZE)
    cell_font_prop = _font_prop_sized(is_ar, bold=False, size=TABLE_FONT_SIZE)

    # ترتيب الأعمدة: RTL للعربي (أول عمود منطقي يبدأ من أقصى اليمين)
    col_x_starts = [x_left + i * col_w for i in range(ncols)]
    if is_ar:
        col_x_starts = list(reversed(col_x_starts))

    y = y_top

    def _draw_row(lines_per_cell: List[List[str]], row_h: float, bg: Optional[str], font_prop: fm.FontProperties):
        nonlocal y
        for col_idx in range(ncols):
            cx = col_x_starts[col_idx]
            if bg:
                ax.add_patch(plt.Rectangle((cx, y - row_h), col_w, row_h,
                                            facecolor=bg, edgecolor=TABLE_BORDER_COLOR, linewidth=0.8))
            else:
                ax.add_patch(plt.Rectangle((cx, y - row_h), col_w, row_h,
                                            facecolor="none", edgecolor=TABLE_BORDER_COLOR, linewidth=0.8))
            lines = lines_per_cell[col_idx] if col_idx < len(lines_per_cell) else [""]
            cell_center_x = cx + col_w / 2
            text_y = y - TABLE_CELL_PAD_Y - TABLE_ROW_LINE_HEIGHT_PX / 2
            for line in lines:
                ax.text(cell_center_x, text_y, line, ha="center", va="center",
                        fontsize=TABLE_FONT_SIZE, color="#1a1a1a" if bg != TABLE_HEADER_BG else "white",
                        fontproperties=font_prop)
                text_y -= TABLE_ROW_LINE_HEIGHT_PX
        y -= row_h

    if table_data["header_lines"]:
        _draw_row(table_data["header_lines"], table_data["header_h"], TABLE_HEADER_BG, header_font_prop)

    for i, (lines, row_h) in enumerate(zip(table_data["row_lines"], table_data["row_heights"])):
        bg = TABLE_ROW_BG_ALT if i % 2 == 1 else None
        _draw_row(lines, row_h, bg, cell_font_prop)

    return y


# ==================== 🆕 المصفوفات (Matrices) ====================
def _prepare_matrices(matrices: Any, is_ar: bool) -> Optional[List[Dict[str, Any]]]:
    """يجهّز قائمة المصفوفات للرسم: يقيس عرض/ارتفاع كل مصفوفة (عمود واحد بعرض موحّد
    = أعرض خلية بالمصفوفة، حتى تبقى شبكة نظيفة بلا خطوط داخلية زي جدول البيانات).
    يرجع None لو ما فيه مصفوفات فعلية (قائمة فارغة/غير صالحة)."""
    if not matrices:
        return None
    cell_font_prop = _font_prop_sized(is_ar, bold=False, size=MATRIX_FONT_SIZE)
    label_font_prop = _font_prop_sized(is_ar, bold=True, size=MATRIX_LABEL_FONT_SIZE)

    prepared: List[Dict[str, Any]] = []
    for m in matrices:
        rows = [[str(c).strip() for c in (row or [])] for row in (m.get("rows") or [])]
        rows = [r for r in rows if r]
        if not rows:
            continue
        ncols = max(len(r) for r in rows)
        rows = [(r + [""] * ncols)[:ncols] for r in rows]
        bracket = m.get("bracket") or "square"
        if bracket not in ("square", "round", "bar"):
            bracket = "square"
        label = _sanitize_line_for_mathtext(str(m.get("label") or "").strip())

        cell_lines = [[_sanitize_line_for_mathtext(c) for c in row] for row in rows]
        col_w = max(
            (_measure_px(c, cell_font_prop) for row in cell_lines for c in row if c),
            default=20.0,
        ) + 2 * MATRIX_CELL_PAD_X
        row_h = MATRIX_ROW_GAP_PX
        nrows = len(rows)
        grid_w = col_w * ncols
        grid_h = row_h * nrows
        label_w = _measure_px(label, label_font_prop) + MATRIX_LABEL_GAP_PX if label else 0.0
        total_w = label_w + grid_w + 2 * (MATRIX_BRACKET_MARGIN_PX + (MATRIX_BRACKET_CAP_PX if bracket == "round" else 4))

        prepared.append({
            "rows": cell_lines, "ncols": ncols, "nrows": nrows,
            "bracket": bracket, "label": label,
            "col_w": col_w, "row_h": row_h, "grid_w": grid_w, "grid_h": grid_h,
            "label_w": label_w, "total_w": total_w,
        })

    if not prepared:
        return None
    return prepared


def _draw_bracket_pair(ax, x_left: float, x_right: float, y_top: float, y_bottom: float, bracket: str) -> None:
    """يرسم زوج الأقواس المحيط بالمصفوفة (يسار/يمين) بعناصر رسم مباشرة - matplotlib
    مافيه رمز '[' أو '(' قابل للتمديد الرأسي زي LaTeX، فنرسم الشكل يدوياً بخطوط/قوس."""
    color = MATRIX_COLOR
    lw = MATRIX_BRACKET_LINEWIDTH
    if bracket == "bar":
        ax.plot([x_left, x_left], [y_top, y_bottom], color=color, linewidth=lw, solid_capstyle="butt")
        ax.plot([x_right, x_right], [y_top, y_bottom], color=color, linewidth=lw, solid_capstyle="butt")
        return
    if bracket == "round":
        height = y_top - y_bottom
        # قوس دائري (نصف قطر أفقي صغير) يعطي شكل "(" و ")" مفتوح للداخل
        rx = MATRIX_BRACKET_CAP_PX
        left_arc = plt.matplotlib.patches.Arc((x_left + rx, (y_top + y_bottom) / 2), 2 * rx, height,
                                               angle=0, theta1=100, theta2=260, color=color, linewidth=lw)
        right_arc = plt.matplotlib.patches.Arc((x_right - rx, (y_top + y_bottom) / 2), 2 * rx, height,
                                                angle=0, theta1=280, theta2=80, color=color, linewidth=lw)
        ax.add_patch(left_arc)
        ax.add_patch(right_arc)
        return
    # square (افتراضي): خط عمودي + "خطاف" أفقي قصير أعلى وأسفل، متجه للداخل
    cap = MATRIX_BRACKET_CAP_PX
    ax.plot([x_left, x_left], [y_top, y_bottom], color=color, linewidth=lw, solid_capstyle="butt")
    ax.plot([x_left, x_left + cap], [y_top, y_top], color=color, linewidth=lw, solid_capstyle="butt")
    ax.plot([x_left, x_left + cap], [y_bottom, y_bottom], color=color, linewidth=lw, solid_capstyle="butt")
    ax.plot([x_right, x_right], [y_top, y_bottom], color=color, linewidth=lw, solid_capstyle="butt")
    ax.plot([x_right - cap, x_right], [y_top, y_top], color=color, linewidth=lw, solid_capstyle="butt")
    ax.plot([x_right - cap, x_right], [y_bottom, y_bottom], color=color, linewidth=lw, solid_capstyle="butt")


def _draw_matrices(ax, matrices_data: List[Dict[str, Any]], x_left: float, y_top: float,
                    max_width_px: float, is_ar: bool) -> float:
    """يرسم كل المصفوفات جنباً إلى جنب أفقياً (بترتيب RTL للعربي) داخل الـ Axes الحالي،
    ويرجع إحداثي y أسفل أطول مصفوفة ليكمل الرسم من هناك (الخيارات...). يلتف المصفوفات
    لصف جديد تلقائياً لو تجاوز إجمالي عرضها عرض الصورة المتاح."""
    cell_font_prop = _font_prop_sized(is_ar, bold=False, size=MATRIX_FONT_SIZE)
    label_font_prop = _font_prop_sized(is_ar, bold=True, size=MATRIX_LABEL_FONT_SIZE)

    # تجميع المصفوفات بصفوف أفقية حسب العرض المتاح (نادراً ما يلزم أكثر من صف واحد)
    display_order = list(reversed(matrices_data)) if is_ar else matrices_data
    lines: List[List[Dict[str, Any]]] = [[]]
    cur_w = 0.0
    for m in display_order:
        add_w = m["total_w"] + (MATRIX_GAP_BETWEEN_PX if lines[-1] else 0.0)
        if lines[-1] and cur_w + add_w > max_width_px:
            lines.append([])
            cur_w = 0.0
            add_w = m["total_w"]
        lines[-1].append(m)
        cur_w += add_w

    y = y_top
    for row_group in lines:
        row_max_h = max(m["grid_h"] for m in row_group)
        row_total_w = sum(m["total_w"] for m in row_group) + MATRIX_GAP_BETWEEN_PX * (len(row_group) - 1)
        # توسيط أفقي لصف المصفوفات بالكامل ضمن عرض المحتوى
        cx = x_left + (max_width_px - row_total_w) / 2
        grid_y_top = y
        grid_y_bottom = y - row_max_h
        for m in row_group:
            # كل مصفوفة تتمركز عمودياً ضمن ارتفاع أطول مصفوفة بنفس الصف
            m_y_top = (grid_y_top + grid_y_bottom) / 2 + m["grid_h"] / 2
            m_y_bottom = m_y_top - m["grid_h"]
            if m["label"]:
                ax.text(cx + m["label_w"] - MATRIX_LABEL_GAP_PX, (m_y_top + m_y_bottom) / 2, m["label"],
                        ha="right", va="center", fontsize=MATRIX_LABEL_FONT_SIZE, color=MATRIX_COLOR,
                        fontweight="bold", fontproperties=label_font_prop)
            bracket_x_left = cx + m["label_w"]
            grid_x_left = bracket_x_left + MATRIX_BRACKET_MARGIN_PX
            grid_x_right = grid_x_left + m["grid_w"]
            bracket_x_right = grid_x_right + MATRIX_BRACKET_MARGIN_PX
            cap_extra = MATRIX_BRACKET_CAP_PX * 0.4
            _draw_bracket_pair(ax, bracket_x_left, bracket_x_right,
                                m_y_top + cap_extra, m_y_bottom - cap_extra, m["bracket"])
            for r_idx, row in enumerate(m["rows"]):
                cell_y = m_y_top - m["row_h"] / 2 - r_idx * m["row_h"]
                for c_idx, cell in enumerate(row):
                    cell_x = grid_x_left + c_idx * m["col_w"] + m["col_w"] / 2
                    ax.text(cell_x, cell_y, cell, ha="center", va="center",
                            fontsize=MATRIX_FONT_SIZE, color=MATRIX_COLOR, fontproperties=cell_font_prop)
            cx += m["total_w"] + MATRIX_GAP_BETWEEN_PX
        y -= row_max_h + MATRIX_GAP_BETWEEN_PX * 0.6

    return y + MATRIX_GAP_BETWEEN_PX * 0.6


# ==================== الرسم الفعلي ====================
def render_question_image(question: Dict[str, Any], idx: int, total: int, is_ar: bool) -> bytes:
    """
    يرسم صورة PNG واحدة تحوي نص السؤال (idx+1 من total) وكل الخيارات، مع دعم LaTeX
    للمعادلات المضمّنة بعلامتي $...$ ودعم عربي RTL كامل. يُستدعى دائماً عبر
    asyncio.to_thread من services/quiz_engine.py لأنه عملية رسم CPU-bound متزامنة.
    """
    plt.rcParams["mathtext.fontset"] = "cm"

    question_text = str(question.get("question", "")).strip()
    options = [str(o).strip() for o in (question.get("options") or [])]
    letters = letters_for(is_ar, len(options))

    max_width_px = FIG_WIDTH_PX - 2 * MARGIN_PX
    q_font_prop = _font_prop_sized(is_ar, bold=True, size=QUESTION_FONT_SIZE)
    opt_font_prop = _font_prop_sized(is_ar, bold=False, size=OPTION_FONT_SIZE)

    # هامش أمان إضافي عند لف نص السؤال تحديداً: قياس عرض الأسطر المختلطة (عربي + LaTeX)
    # بيبقى أحياناً أقل شوية من العرض الفعلي وقت الرسم، فبيخلي آخر سطر يوصل لحافة
    # الصورة تقريباً. نلف السؤال على عرض أضيق شوية (96%) عشان يفضل فيه هامش واضح دايماً.
    question_wrap_width = max_width_px * 0.96
    q_lines = _wrap_and_shape(question_text, is_ar, question_wrap_width, q_font_prop)

    # نفس هامش الأمان المستخدم مع نص السؤال (0.96) - القياس التقريبي وقت اللف بيكون
    # أحياناً أقل شوية من العرض الفعلي وقت الرسم، فبدون هامش الخيارات بتوصل لحافة
    # الصورة تقريباً (كانت هاي المشكلة الأصلية بالخيارات تحديداً).
    option_wrap_width = max_width_px * 0.96
    option_blocks: List[List[str]] = []
    for letter, opt in zip(letters, options):
        combined = f"({letter} {opt}" if is_ar else f"{letter}) {opt}"
        option_blocks.append(_wrap_and_shape(combined, is_ar, option_wrap_width, opt_font_prop))

    # نقيس ارتفاع كل سطر فعلياً (بدل الاعتماد على ثابت واحد لكل الأسطر) - ضروري
    # للأسطر يلي فيها كسور/جذور/مجاميع بحدود لأنها بترتفع أكتر بكتير من سطر نص عادي.
    # نجهّز السطر النهائي (بعد sanitize) مرة واحدة ونستخدمه للقياس وللرسم معاً،
    # عشان القياس يطابق تماماً الشيء يلي رح يترسم فعلياً.
    q_render_lines = [_sanitize_line_for_mathtext(line) for line in q_lines]
    q_line_heights = [_measure_line_height_px(line, q_font_prop, QUESTION_LINE_HEIGHT_PX) for line in q_render_lines]

    # 🆕 جدول بيانات اختياري (راجع QuizTable بـ helpers/gemini_helper.py) - يُرسم بين
    # نص السؤال والخيارات مباشرة عبر مستطيلات/نص Matplotlib بدل LaTeX نصي.
    table_data = _prepare_table(question.get("table"), is_ar, max_width_px)
    table_extra_h = (TABLE_TOP_GAP_PX + table_data["total_h"] + TABLE_BOTTOM_GAP_PX) if table_data else 0.0

    # 🆕 مصفوفة/محدّد اختياري أو أكثر (راجع QuizMatrix بـ helpers/gemini_helper.py) -
    # تُرسم بعد الجدول (لو وُجد) مباشرة وقبل الخيارات، بنفس مبدأ الجدول (عناصر رسم
    # مباشرة بدل LaTeX نصي غير مدعوم لبيئات \begin{matrix}).
    matrices_data = _prepare_matrices(question.get("matrices"), is_ar)
    matrices_extra_h = 0.0
    if matrices_data:
        # نحسب الارتفاع الفعلي بنفس منطق _draw_matrices (التفاف صفوف حسب العرض المتاح)
        display_order = list(reversed(matrices_data)) if is_ar else matrices_data
        rows_groups: List[List[Dict[str, Any]]] = [[]]
        cur_w = 0.0
        for m in display_order:
            add_w = m["total_w"] + (MATRIX_GAP_BETWEEN_PX if rows_groups[-1] else 0.0)
            if rows_groups[-1] and cur_w + add_w > max_width_px:
                rows_groups.append([])
                cur_w = 0.0
                add_w = m["total_w"]
            rows_groups[-1].append(m)
            cur_w += add_w
        matrices_extra_h = MATRIX_TOP_GAP_PX + MATRIX_BOTTOM_GAP_PX + sum(
            max(m["grid_h"] for m in group) + MATRIX_GAP_BETWEEN_PX * 0.6 for group in rows_groups
        )

    option_render_blocks: List[List[str]] = []
    option_block_heights: List[List[float]] = []
    for block in option_blocks:
        rendered = [_sanitize_line_for_mathtext(line) for line in block]
        heights = [_measure_line_height_px(line, opt_font_prop, OPTION_LINE_HEIGHT_PX) for line in rendered]
        option_render_blocks.append(rendered)
        option_block_heights.append(heights)

    height_px = (
        HEADER_HEIGHT_PX + MARGIN_PX * 2
        + sum(q_line_heights)
        + table_extra_h
        + matrices_extra_h
        + sum(sum(heights) for heights in option_block_heights)
        + len(option_blocks) * OPTION_GAP_PX
    )
    height_px = max(height_px, 380)

    fig = plt.figure(figsize=(FIG_WIDTH_PX / DPI, height_px / DPI), dpi=DPI)
    fig.patch.set_facecolor("#ffffff")
    ax = fig.add_axes((0, 0, 1, 1))
    ax.axis("off")
    ax.set_xlim(0, FIG_WIDTH_PX)
    ax.set_ylim(0, height_px)

    # شريط العنوان العلوي
    ax.add_patch(plt.Rectangle((0, height_px - HEADER_HEIGHT_PX), FIG_WIDTH_PX, HEADER_HEIGHT_PX,
                                facecolor="#4C6FFF", edgecolor="none"))
    header_text = f"السؤال {idx + 1} من {total}" if is_ar else f"Question {idx + 1} of {total}"
    header_text = _shape_line(header_text)
    ax.text(FIG_WIDTH_PX / 2, height_px - HEADER_HEIGHT_PX / 2, _sanitize_line_for_mathtext(header_text),
            ha="center", va="center", fontsize=22, color="white",
            fontproperties=_font_prop(is_ar, bold=True))

    align = "right" if is_ar else "left"
    x = FIG_WIDTH_PX - MARGIN_PX if is_ar else MARGIN_PX
    y = height_px - HEADER_HEIGHT_PX - MARGIN_PX + 6

    for line, line_h in zip(q_render_lines, q_line_heights):
        ax.text(x, y, line, ha=align, va="top",
                fontsize=QUESTION_FONT_SIZE, color="#1a1a1a", fontweight="bold",
                fontproperties=_font_prop(is_ar, bold=True))
        y -= line_h

    if table_data:
        # الجدول يُرسم بعرض المحتوى الكامل (من x=MARGIN_PX)، بمحاذاة يمين/يسار
        # منطقية داخلياً عبر _draw_table نفسه (RTL حسب is_ar) - لسنا بحاجة x/align هون.
        y -= TABLE_TOP_GAP_PX
        y = _draw_table(ax, table_data, MARGIN_PX, y, max_width_px, is_ar)
        y -= TABLE_BOTTOM_GAP_PX

    if matrices_data:
        y -= MATRIX_TOP_GAP_PX
        y = _draw_matrices(ax, matrices_data, MARGIN_PX, y, max_width_px, is_ar)
        y -= MATRIX_BOTTOM_GAP_PX

    y -= OPTION_GAP_PX
    for i, (block, heights) in enumerate(zip(option_render_blocks, option_block_heights)):
        if i > 0:
            sep_y = y + OPTION_GAP_PX / 2
            ax.plot([MARGIN_PX, FIG_WIDTH_PX - MARGIN_PX], [sep_y, sep_y], color="#e8e8e8", linewidth=1.2)
        color = BADGE_COLORS[i % len(BADGE_COLORS)]
        # نقطة ملوّنة صغيرة بجانب كل خيار لتمييزه بصرياً (بدون تكرار الحرف - موجود أصلاً بالنص)
        badge_x = MARGIN_PX - 20 if not is_ar else FIG_WIDTH_PX - MARGIN_PX + 20
        ax.scatter([badge_x], [y + 10], s=90, color=color, zorder=3, clip_on=False)
        for line, line_h in zip(block, heights):
            ax.text(x, y, line, ha=align, va="top",
                    fontsize=OPTION_FONT_SIZE, color="#2c2c2c",
                    fontproperties=_font_prop(is_ar))
            y -= line_h
        y -= OPTION_GAP_PX

    # شبكة أمان أخيرة: لو رغم القياس المسبق (بهامش 20%) في حالة متطرفة نادرة خلت
    # آخر سطر يوصل لتحت حدود الصورة (y سالب)، نمدّد الصورة فعلياً لتحت بدل ما نقص
    # المحتوى. العرض بيضل ثابت (1000px)، بس الارتفاع بيتمدد حسب المحتوى الحقيقي.
    bottom_y = min(0, y - MARGIN_PX)
    if bottom_y < 0:
        extra_px = -bottom_y
        log_warning(logger, f"image_quiz_renderer: overflow detected, extending canvas by {extra_px:.0f}px")
        fig.set_size_inches(FIG_WIDTH_PX / DPI, (height_px + extra_px) / DPI)
        ax.set_ylim(bottom_y, height_px)

    buf = io.BytesIO()
    try:
        fig.savefig(buf, format="png", facecolor=fig.get_facecolor())
    finally:
        plt.close(fig)
    buf.seek(0)
    return buf.getvalue()
