# services/math_text.py
"""
تحويل نصوص رياضية بصيغة LaTeX-lite إلى رموز نصية عادية (يونيكود)
موجودة أصلاً على أي كيبورد/هاتف — بدون أي رسم أو صورة.

يُستخدم حالياً في مسار الـ Hint (get_hint)، وقابل لإعادة الاستخدام لاحقاً
في عمود الشرح بالتصدير (export_service.py) لتقليل عدد الصور المطلوب توليدها.

⚠️ هذا المحوّل تقريبي بالتصميم: هدفه القراءة السريعة على تيليغرام
(alert بحد أقصى ~200 حرف)، مش إعادة إنتاج LaTeX بصرياً مطابق 100%.
أي صيغة معقدة (مصفوفات، تكامل، مجاميع) بتبقى تحتاج رسم فعلي —
هاد المحوّل ما بيغطيها عمداً، وبيرجّعها زي ما هي (بعد تنضيف $ و {}) بدل ما يكسرها.
"""

import re

# رموز يونانية شائعة
_GREEK = {
    r"\\alpha": "α", r"\\beta": "β", r"\\gamma": "γ", r"\\Gamma": "Γ",
    r"\\theta": "θ", r"\\Theta": "Θ", r"\\pi": "π", r"\\lambda": "λ",
    r"\\Lambda": "Λ", r"\\mu": "μ", r"\\sigma": "σ", r"\\Sigma": "Σ",
    r"\\delta": "δ", r"\\Delta": "Δ", r"\\phi": "φ", r"\\omega": "ω",
    r"\\epsilon": "ε", r"\\rho": "ρ", r"\\tau": "τ",
}

# عمليات ورموز رياضية شائعة (الترتيب مهم: الأطول أولاً كي لا يبتلع pattern أقصر جزءاً منه)
_OPS = [
    (r"\\times", "×"), (r"\\div", "÷"), (r"\\cdot", "*"),
    (r"\\pm", "±"), (r"\\mp", "∓"),
    (r"\\leq", "≤"), (r"\\geq", "≥"), (r"\\neq", "≠"), (r"\\approx", "≈"),
    (r"\\infty", "∞"), (r"\\to", "->"), (r"\\rightarrow", "->"),
    (r"\\in", "∈"), (r"\\notin", "∉"), (r"\\subset", "⊂"),
    (r"\\cup", "∪"), (r"\\cap", "∩"), (r"\\forall", "∀"), (r"\\exists", "∃"),
    (r"\\%", "%"), (r"\\,", " "),
]

_SUPER_MAP = str.maketrans("0123456789-+n", "⁰¹²³⁴⁵⁶⁷⁸⁹⁻⁺ⁿ")
_SUB_MAP = str.maketrans("0123456789", "₀₁₂₃₄₅₆₇₈₉")

# حد أقصى معقول لعرض التلميح داخل alert تيليغرام (المتاح فعلياً أقل من 200 لإفساح مجال للعنوان)
ALERT_SAFE_LIMIT = 180


_FRAC_RE = re.compile(r"\\frac\{([^{}]+)\}\{([^{}]+)\}")
_SQRT_N_RE = re.compile(r"\\sqrt\[(\d+)\]\{([^{}]+)\}")
_SQRT_RE = re.compile(r"\\sqrt\{([^{}]+)\}")


def _convert_frac_and_sqrt(text: str) -> str:
    r"""
    \frac و \sqrt بيترابطوا ببعض كتير بصيغ شائعة (زي معادلة الدرجة الثانية:
    \frac{\sqrt{...}}{2a})، وكل وحدة إلها regex بيرفض الأقواس المتداخلة عمداً
    (كي يضل بسيط وسريع). فبدل ما نطبّق كل وحدة مرة وحدة بترتيب ثابت (وقد
    يفشل لو الترتيب عكسي بالنص)، منلف على الاثنتين مع بعض لحد ما يستقر
    النص — هيك مهما كان ترتيب التعشيش (sqrt جوا frac أو العكس)، كل تمرير
    بيحل الطبقة الأعمق المتاحة وبيفتح المجال للطبقة يلي فوقها بالتمرير الجاي.
    """
    prev = None
    while prev != text:
        prev = text
        text = _FRAC_RE.sub(r"(\1)/(\2)", text)
        text = _SQRT_N_RE.sub(
            lambda m: f"{m.group(1).translate(_SUPER_MAP)}√({m.group(2)})", text)
        text = _SQRT_RE.sub(r"√(\1)", text)
    return text


def _convert_super_sub(text: str) -> str:
    def sup_repl(m):
        body = m.group(1)
        return body.translate(_SUPER_MAP) if len(body) <= 3 and re.fullmatch(r"-?[\dn+]+", body) else f"^({body})"

    def sub_repl(m):
        body = m.group(1)
        return body.translate(_SUB_MAP) if body.isdigit() else f"_{body}"

    text = re.sub(r"\^\{([^{}]+)\}", sup_repl, text)
    text = re.sub(r"\^(-?\w)", lambda m: sup_repl(re.match(r"(.*)", m.group(1))), text)
    text = re.sub(r"_\{([^{}]+)\}", sub_repl, text)
    text = re.sub(r"_(\w)", lambda m: sub_repl(re.match(r"(.*)", m.group(1))), text)
    return text


def latex_to_plain(text: str) -> str:
    """
    يحوّل نص فيه صيغ LaTeX-lite (بين $...$ أو بدونها) إلى رموز نصية
    عادية قابلة للعرض على أي كيبورد. آمن للاستدعاء حتى لو النص لا يحوي رياضيات إطلاقاً.
    """
    if not text:
        return ""

    text = _convert_frac_and_sqrt(text)
    text = _convert_super_sub(text)

    for pattern, repl in _OPS:
        text = re.sub(pattern, repl, text)
    for pattern, repl in _GREEK.items():
        text = re.sub(pattern, repl, text)

    # تنضيف ما تبقى من رموز LaTeX التنظيمية (أقواس التجميع وعلامات $)
    text = text.replace("$", "").replace("{", "").replace("}", "")
    text = re.sub(r"\\left|\\right", "", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def format_hint(hint_raw: str) -> tuple[str, bool]:
    """
    يجهّز نص التلميح للعرض ويقرر القناة المناسبة.
    يرجّع (النص الجاهز, هل يُعرض كـ alert).
    - alert: للتلميحات القصيرة (الحالة الشائعة) — أسرع تجربة للطالب، صفر رسائل إضافية.
    - رسالة عادية (as_alert=False): للتلميحات الطويلة أو متعددة الأسطر (نادرة، غالباً
      فيها ترتيب يحتاج محاذاة)، تُرسل بصيغة <code> كي تحافظ تيليغرام على المحاذاة.
    """
    plain = latex_to_plain(hint_raw)
    if len(plain) <= ALERT_SAFE_LIMIT and "\n" not in plain:
        return plain, True
    return plain, False
