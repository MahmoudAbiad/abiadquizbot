"""
helpers/gemini_helper/parsing.py
ترميم JSON القادم من النماذج (backslash/LaTeX) وتحويل الاستجابات المُهيكلة لكائنات Pydantic.
"""

from typing import Any, List
import json

from logger import get_logger, log_warning

logger = get_logger(__name__)


# 🛠️ FIX (أخطر من الإصدار السابق المبني على regex وحيد): ترميم backslash خام غير
# مُهرَّب بشكل صحيح داخل نص JSON قادم من نموذج AI (Groq أو Gemini) قبل json.loads().
#
# المشكلة الأصلية: regex السابق (`\\(?!["\\/bfnrtu])`) كان يرمّم فقط الـ backslash
# المتبوع بحرف *غير* معروف كـ escape شرعي بمعيار JSON (زي \s بـ \sqrt، \a بـ \alpha) -
# وهي الحالة اللي ترمي استثناء "Invalid \escape" فيسقط json.loads() بالكامل (ملحوظ
# ومُعالَج مسبقاً). لكنها كانت **تتجاهل تماماً** الحالة الأخطر: backslash متبوع بحرف
# *هو فعلاً* escape شرعي (b/f/n/r/t) - زي \bar \big \beta، \frac \forall، \neq \nabla
# \nu \notin، \rho \rightarrow، \tan \text \theta \times - وهاي الحالة **لا ترمي أي
# استثناء إطلاقاً**؛ json.loads() ينجح "بصمت" لكنه يُفسّر \b كـ backspace و\f كـ
# form-feed و\r كـ carriage-return و\t كـ tab، فيبتلع الحرف اللي بعد الـ backslash
# ويترك الباقي فقط (\bar{d} → BACKSPACE+"ar{d}"، \text{...} → TAB+"ext{...}") - وهذا
# بالضبط توقيع الخلل المُشاهَد فعلياً بصور الكويزات الرياضية (راجع docs/HISTORY_LOG.md).
# بما أن معظم رموز LaTeX الرياضية المدعومة رسمياً (راجع constants.py) تبدأ تحديداً
# بأحد هذي الأحرف الخمسة (b/f/n/r/t)، فهذا كان يعني أن نجاح/فشل تهريب كل رمز كان
# متروكاً بالكامل لتخمين النموذج نفسه (Gemini/Groq) - أحياناً يضاعف الـ backslash
# صح (\\frac) وأحياناً لأ (\frac)، دون أي شبكة أمان فعلية من جهتنا.
#
# الاستثناء الوحيد المتعمَّد: \n (سطر جديد) *لها* استخدام شرعي موثَّق بهذا المشروع -
# الفاصل بين نص السؤال بالإنجليزية وترجمته العربية بنمط "English + ترجمة عربية"
# (راجع MSG رقم 1ب بموجّهات constants.py: "...بفاصل سطر حقيقي (\n)..."). لذلك \n
# تحديداً تُرمَّم فقط لو الحرف اللي بعدها مباشرة حرف لاتيني (يعني على الأغلب بداية
# أمر LaTeX زي \neq \nabla \nu \notin)، وتبقى كما هي لو الحرف اللي بعدها عربي/رقم/
# مسافة/نهاية النص (على الأغلب فاصل سطر حقيقي مقصود).
_JSON_LEGAL_ALWAYS_ESCAPES = ('"', "\\", "/", "u")  # هذي فقط تبقى كما هي دائماً
_JSON_AMBIGUOUS_ESCAPE_LETTERS = ("b", "f", "r", "t")  # لا استخدام شرعي معروف بهذا المشروع


def _repair_json_backslashes(text: str) -> str:
    """يمرّ على النص حرفاً حرفاً (بدل regex وحيد) ليرمّم أي backslash قد يُفسَّر خطأً
    كـ escape sequence شرعي بمعيار JSON بينما هو فعلياً بداية أمر LaTeX من نموذج AI لم
    يُضاعِف الـ backslash بشكل صحيح. آمن على أي نص لا يحوي LaTeX إطلاقاً - يرجعه دون
    أي تغيير فعلي."""
    if "\\" not in text:
        return text
    out: List[str] = []
    i = 0
    n = len(text)
    while i < n:
        ch = text[i]
        if ch != "\\" or i + 1 >= n:
            out.append(ch)
            i += 1
            continue
        nxt = text[i + 1]
        if nxt in _JSON_LEGAL_ALWAYS_ESCAPES:
            out.append(text[i:i + 2])
            i += 2
        elif nxt in _JSON_AMBIGUOUS_ESCAPE_LETTERS:
            out.append("\\\\")  # backslash حرفي مضاعف - نترك nxt ليُكتب بالتكرار التالي
            i += 1
        elif nxt == "n":
            after = text[i + 2] if i + 2 < n else ""
            if after and after.isascii() and after.isalpha():
                out.append("\\\\")
                i += 1
            else:
                out.append(text[i:i + 2])
                i += 2
        else:
            # حرف غير معروف إطلاقاً كـ escape شرعي بمعيار JSON (\s \a \p \g \d \l...) -
            # نضاعف الـ backslash لمنع استثناء "Invalid \escape" بـ json.loads()
            out.append("\\\\")
            i += 1
    return "".join(out)
def _parse_structured_gemini_response(response: Any, response_schema: type, model: str) -> Any:
    """يحوّل استجابة Gemini المُهيكلة (JSON Schema) إلى كائن Pydantic، مع تفضيل صريح
    لنص JSON الخام (response.text) + _repair_json_backslashes قبل json.loads()، بدل
    الاعتماد المباشر على response.parsed الجاهز من الـ SDK.

    🛠️ FIX (السبب الجذري - راجع docs/HISTORY_LOG.md): كان الافتراض السابق أن response_schema
    (توليد مُقيَّد بمخطط) يضمن تهريب JSON سليم دائماً بغض النظر عن محتوى الـ backslash -
    وهذا **غير صحيح فعلياً**. التوليد المُقيَّد يضمن فقط أن الناتج **صحيح نحوياً** كـ JSON
    (valid syntax)، لا أن النموذج ينوي فعلياً كل escape sequence يكتبها: \\bar{d} بنص
    JSON صحيح نحوياً (لأن \\b escape شرعي بمعيار JSON)، لكنها تُفهم كـ backspace + "ar{d}"
    وليس "\\bar{d}" الحرفية المقصودة - وهذا بالضبط سبب ظهور "S_{ar{d}}" بدل "S_{\\bar{d}}"
    و"ext{...}" بدل "\\text{...}" بصور الكويزات الرياضية رغم استخدام response_schema.
    الـ SDK يبني response.parsed داخلياً من نفس نص JSON الخام (response.text) عبر
    json.loads() قياسي - فالخلل موجود بأي الحالتين؛ الفرق أن الاعتماد على response.text
    يفتح لنا نافذة لتطبيق _repair_json_backslashes **قبل** الـ parsing، وهو غير ممكن لو
    اكتفينا بـ response.parsed الجاهز (الضرر يكون قد وقع فعلاً داخل الـ SDK).

    عند فشل هذا المسار (نص غير متاح، أو JSON تالف حتى بعد الترميم)، نرجع لـ response.parsed
    كخط دفاع أخير بدل إسقاط الاستجابة بالكامل."""
    raw_text = getattr(response, "text", None)
    if raw_text:
        try:
            repaired = _repair_json_backslashes(raw_text)
            return response_schema(**json.loads(repaired))
        except Exception as exc:
            log_warning(
                logger,
                f"{model}: raw-text structured parse failed after backslash repair "
                f"({exc}); falling back to SDK response.parsed",
            )
    parsed = getattr(response, "parsed", None)
    if parsed is None:
        raise ValueError(f"{model} returned no structured content")
    return parsed
