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
from typing import Any, Dict, List

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
MARGIN_PX = 55
HEADER_HEIGHT_PX = 76
LINE_HEIGHT_PX = 44
OPTION_GAP_PX = 22
QUESTION_FONT_SIZE = 21
OPTION_FONT_SIZE = 19
BADGE_COLORS = ["#2E86DE", "#10AC84", "#EE5253", "#F5A623", "#8854D0", "#00B8D9", "#EA5455", "#5E5CE6"]

_ARABIC_RE = re.compile(r"[\u0600-\u06FF\u0750-\u077F\u08A0-\u08FF\uFB50-\uFDFF\uFE70-\uFEFF]")
_LETTER_RE = re.compile(r"[^\W\d_]", re.UNICODE)
_MATH_SPAN_RE = re.compile(r"\$[^$]+\$")

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
    بدل تعطيل توليد الصورة بأكملها."""
    if "$" not in line:
        return line
    try:
        _MATH_PARSER.parse(line, dpi=100)
        return line
    except Exception:
        return line.replace("$", "")


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

    q_lines = _wrap_and_shape(question_text, is_ar, max_width_px, q_font_prop)

    option_blocks: List[List[str]] = []
    for letter, opt in zip(letters, options):
        combined = f"({letter} {opt}" if is_ar else f"{letter}) {opt}"
        option_blocks.append(_wrap_and_shape(combined, is_ar, max_width_px, opt_font_prop))

    total_lines = len(q_lines) + sum(len(block) for block in option_blocks)
    height_px = (
        HEADER_HEIGHT_PX + MARGIN_PX * 2
        + total_lines * LINE_HEIGHT_PX
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

    for line in q_lines:
        ax.text(x, y, _sanitize_line_for_mathtext(line), ha=align, va="top",
                fontsize=QUESTION_FONT_SIZE, color="#1a1a1a", fontweight="bold",
                fontproperties=_font_prop(is_ar, bold=True))
        y -= LINE_HEIGHT_PX

    y -= OPTION_GAP_PX
    for i, block in enumerate(option_blocks):
        if i > 0:
            sep_y = y + OPTION_GAP_PX / 2
            ax.plot([MARGIN_PX, FIG_WIDTH_PX - MARGIN_PX], [sep_y, sep_y], color="#e8e8e8", linewidth=1.2)
        color = BADGE_COLORS[i % len(BADGE_COLORS)]
        # نقطة ملوّنة صغيرة بجانب كل خيار لتمييزه بصرياً (بدون تكرار الحرف - موجود أصلاً بالنص)
        badge_x = MARGIN_PX - 20 if not is_ar else FIG_WIDTH_PX - MARGIN_PX + 20
        ax.scatter([badge_x], [y + 10], s=90, color=color, zorder=3, clip_on=False)
        for line in block:
            ax.text(x, y, _sanitize_line_for_mathtext(line), ha=align, va="top",
                    fontsize=OPTION_FONT_SIZE, color="#2c2c2c",
                    fontproperties=_font_prop(is_ar))
            y -= LINE_HEIGHT_PX
        y -= OPTION_GAP_PX

    buf = io.BytesIO()
    try:
        fig.savefig(buf, format="png", facecolor=fig.get_facecolor())
    finally:
        plt.close(fig)
    buf.seek(0)
    return buf.getvalue()
