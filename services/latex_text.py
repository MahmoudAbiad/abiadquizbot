# services/latex_text.py
r"""
==============================================================================
MODULE: LaTeX → Plain Text Converter
==============================================================================
الوصف:
يحوّل نص يحتوي على صيغ LaTeX بسيطة (زي اللي يولّدها نموذج الكويز الرياضي:
$x^{2}$, \frac{a}{b}, \sqrt{x+1}, \leq, \alpha ...) إلى نص عادي مقروء بدون
أي رموز LaTeX خام - مخصص للأماكن التي لا يمكنها عرض LaTeX إطلاقاً مثل:
- تنبيه "طلب تلميح" (Telegram call.answer alert)
- حقل "explanation" بداخل Telegram Poll
- جدول "الإجابات الصحيحة" بملفات Word/PDF المُصدَّرة

ملاحظة: هذا المحوّل غير مخصص لعرض نص السؤال/الخيارات نفسها (تلك تُرسم كصورة
عبر services/image_quiz_renderer.py الذي يدعم LaTeX فعلياً عبر matplotlib) -
فقط للحقول النصية البحتة (hint/explanation) التي يُفترض ألا تحوي LaTeX أصلاً
حسب البرومبت (راجع constants.py)، لكن هذا المحوّل يبقى كخط دفاع ثانٍ (Defense
in Depth) لو تسرّب رمز LaTeX رغم التعليمات.
==============================================================================
"""
import re
from typing import Callable, Dict

# ==================== خرائط الرموز ====================
_GREEK_MAP: Dict[str, str] = {
    "alpha": "α", "beta": "β", "gamma": "γ", "delta": "δ", "epsilon": "ε",
    "zeta": "ζ", "eta": "η", "theta": "θ", "iota": "ι", "kappa": "κ",
    "lambda": "λ", "mu": "μ", "nu": "ν", "xi": "ξ", "pi": "π", "rho": "ρ",
    "sigma": "σ", "tau": "τ", "upsilon": "υ", "phi": "φ", "chi": "χ",
    "psi": "ψ", "omega": "ω",
    "Gamma": "Γ", "Delta": "Δ", "Theta": "Θ", "Lambda": "Λ", "Xi": "Ξ",
    "Pi": "Π", "Sigma": "Σ", "Upsilon": "Υ", "Phi": "Φ", "Psi": "Ψ",
    "Omega": "Ω",
}

_SYMBOL_MAP: Dict[str, str] = {
    "leq": "≤", "geq": "≥", "neq": "≠", "approx": "≈", "equiv": "≡",
    "times": "×", "div": "÷", "pm": "±", "mp": "∓", "cdot": "·",
    "rightarrow": "→", "to": "→", "leftarrow": "←", "Rightarrow": "⇒",
    "infty": "∞", "sum": "∑", "int": "∫", "lim": "lim", "prod": "∏",
    "partial": "∂", "nabla": "∇", "in": "∈", "notin": "∉", "subset": "⊂",
    "cup": "∪", "cap": "∩", "forall": "∀", "exists": "∃", "cong": "≅",
    "perp": "⊥", "parallel": "∥", "angle": "∠", "degree": "°",
    "%": "%",
}

_SUPERSCRIPT_MAP = {
    "0": "⁰", "1": "¹", "2": "²", "3": "³", "4": "⁴", "5": "⁵", "6": "⁶",
    "7": "⁷", "8": "⁸", "9": "⁹", "+": "⁺", "-": "⁻", "=": "⁼", "(": "⁽",
    ")": "⁾", "n": "ⁿ", "i": "ⁱ",
}
_SUBSCRIPT_MAP = {
    "0": "₀", "1": "₁", "2": "₂", "3": "₃", "4": "₄", "5": "₅", "6": "₆",
    "7": "₇", "8": "₈", "9": "₉", "+": "₊", "-": "₋", "=": "₌", "(": "₍",
    ")": "₎", "a": "ₐ", "e": "ₑ", "i": "ᵢ", "o": "ₒ", "u": "ᵤ", "x": "ₓ",
    "n": "ₙ", "k": "ₖ", "j": "ⱼ",
}

_COMMANDS_TO_DROP = ("left", "right", "big", "Big", "displaystyle", "text",
                      "mathrm", "mathbf", "operatorname")


def _find_matching_brace(s: str, open_idx: int) -> int:
    """يرجع فهرس القوس المغلق } المطابق للقوس المفتوح { عند open_idx، أو -1 لو ما لقاه."""
    depth = 0
    for i in range(open_idx, len(s)):
        if s[i] == "{":
            depth += 1
        elif s[i] == "}":
            depth -= 1
            if depth == 0:
                return i
    return -1


def _replace_braced_command(text: str, command: str, build: Callable[[str], str]) -> str:
    """يستبدل كل ظهور \\command{...} بنتيجة build(inner) - يدعم أي تداخل أقواس
    داخلياً عبر مطابقة الأقواس الفعلية بدل Regex غير-جشع (اللي بينكسر مع التداخل)."""
    pattern = re.compile(r"\\" + re.escape(command) + r"\{")
    out = []
    i = 0
    while True:
        m = pattern.search(text, i)
        if not m:
            out.append(text[i:])
            break
        out.append(text[i:m.start()])
        open_idx = m.end() - 1
        close_idx = _find_matching_brace(text, open_idx)
        if close_idx == -1:
            out.append(text[m.start():])
            break
        inner = text[open_idx + 1:close_idx]
        out.append(build(inner))
        i = close_idx + 1
    return "".join(out)


def _replace_frac(text: str) -> str:
    pattern = re.compile(r"\\frac\{")
    out = []
    i = 0
    while True:
        m = pattern.search(text, i)
        if not m:
            out.append(text[i:])
            break
        out.append(text[i:m.start()])
        open1 = m.end() - 1
        close1 = _find_matching_brace(text, open1)
        if close1 == -1:
            out.append(text[m.start():])
            break
        rest_start = close1 + 1
        if rest_start < len(text) and text[rest_start] == "{":
            close2 = _find_matching_brace(text, rest_start)
            if close2 != -1:
                num = text[open1 + 1:close1]
                den = text[rest_start + 1:close2]
                out.append(f"({num}/{den})")
                i = close2 + 1
                continue
        # صياغة غير مكتملة - نتجاهل \frac ونكمل بعد القوس الأول فقط
        out.append(text[open1 + 1:close1])
        i = close1 + 1
    return "".join(out)


def _replace_sqrt(text: str) -> str:
    # \sqrt[n]{x} أو \sqrt{x}
    pattern = re.compile(r"\\sqrt(\[(?P<deg>[^\]]*)\])?\{")
    out = []
    i = 0
    while True:
        m = pattern.search(text, i)
        if not m:
            out.append(text[i:])
            break
        out.append(text[i:m.start()])
        open_idx = m.end() - 1
        close_idx = _find_matching_brace(text, open_idx)
        if close_idx == -1:
            out.append(text[m.start():])
            break
        inner = text[open_idx + 1:close_idx]
        deg = m.group("deg")
        out.append(f"√({inner})" if not deg or deg.strip() in ("", "2") else f"({deg})√({inner})")
        i = close_idx + 1
    return "".join(out)


def _to_script(inner: str, mapping: Dict[str, str], fallback_fmt: str) -> str:
    if inner and all(ch in mapping for ch in inner):
        return "".join(mapping[ch] for ch in inner)
    return fallback_fmt.format(inner)


def _replace_scripts(text: str) -> str:
    """يحوّل ^{...}/^x و _{...}/_x لرموز Unicode Superscript/Subscript لو ممكن،
    وإلا صيغة نصية بديلة مقروءة "أس(...)" / "دليل(...)"."""
    # الحالات المحاطة بأقواس {}
    def sup_build(inner: str) -> str:
        return _to_script(inner, _SUPERSCRIPT_MAP, "^({0})")

    def sub_build(inner: str) -> str:
        return _to_script(inner, _SUBSCRIPT_MAP, "_({0})")

    # ^{...}
    text = re.sub(r"\^\{([^{}]*)\}", lambda m: sup_build(m.group(1)), text)
    # _{...}
    text = re.sub(r"_\{([^{}]*)\}", lambda m: sub_build(m.group(1)), text)
    # ^x أو _x (حرف/رقم واحد بدون أقواس)
    text = re.sub(r"\^(\w)", lambda m: sup_build(m.group(1)), text)
    text = re.sub(r"_(\w)", lambda m: sub_build(m.group(1)), text)
    return text


def latex_to_plain(text: str) -> str:
    """يحوّل نصاً قد يحوي صيغ LaTeX بسيطة إلى نص عادي مقروء بالكامل، بدون أي
    علامات $ أو أوامر backslash متبقية. آمن على أي نص عادي (بدون LaTeX) - يرجعه
    كما هو تقريباً بدون أي تغيير فعلي."""
    if not text:
        return text

    result = str(text)

    # 1) شيل علامات $...$ (Inline Math) لكن أبقِ المحتوى
    result = result.replace("$", "")

    # 2) الكسور والجذور (تدعم تداخل بسيط عبر مطابقة أقواس فعلية)
    result = _replace_frac(result)
    result = _replace_sqrt(result)

    # 3) الأس والدليل السفلي
    result = _replace_scripts(result)

    # 4) أوامر بأقواس بيتشال منها القوس بس (زي \text{...} \mathrm{...})
    for cmd in _COMMANDS_TO_DROP:
        result = _replace_braced_command(result, cmd, lambda inner: inner)

    # 5) الحروف اليونانية والرموز الرياضية الشائعة
    def _sym_or_greek(m: "re.Match") -> str:
        name = m.group(1)
        if name in _GREEK_MAP:
            return _GREEK_MAP[name]
        if name in _SYMBOL_MAP:
            return _SYMBOL_MAP[name]
        return ""  # أمر غير معروف - نحذفه ونُبقي المحتوى المجاور

    result = re.sub(r"\\([A-Za-z]+)", _sym_or_greek, result)

    # 6) تنظيف أقواس LaTeX المتبقية بدون أمر (زي {x+y} وحدها) وأي backslash تائه
    result = result.replace("\\,", " ").replace("\\;", " ").replace("\\!", "")
    result = result.replace("{", "").replace("}", "")
    result = result.replace("\\", "")

    # 7) تبسيط المسافات المتكررة
    result = re.sub(r"[ \t]+", " ", result).strip()

    return result
