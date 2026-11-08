"""إعدادات التصدير الإضافية (ورقة الاختبار): تحقق + تطبيع + نصوص الترويسة.

وحدة نقيّة بلا اعتماد على تيليغرام أو reportlab أو python-docx، حتى يستعملها
نفس الكود في: البوت (واجهة الإعدادات)، سيرفر الرسم الثاني (payload)، ومولّدات
PDF/Word/Excel. القيمة الافتراضية لكل حقل = السلوك القديم حرفياً، فكويز بلا
إعدادات (أو إعدادات كلها افتراضية) يُصدَّر تماماً كما كان.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, fields, replace
from typing import Any, Dict, List, Optional, Tuple

MIN_PER_PAGE = 1
# السقف المطلق لعدد الأسئلة بالصفحة. 16 ممكنة عملياً مع تقسيم الصفحة إلى عمودين والأسئلة
# القصيرة (صح/خطأ أو اختيارات قصيرة تُرتَّب شبكة 2×2)؛ بعمود واحد لا يتسع هذا العدد.
# السقف الفعلي لكل كويز وتنسيق أقل غالباً ويُحسب من طول أسئلته (services.export_layout).
MAX_PER_PAGE = 16

# تقسيم الصفحة: عمود واحد (الافتراضي) أو عمودان يفصلهما خط منصّف اختياري.
COLUMN_CHOICES = (1, 2)
COLUMN_LABELS_AR = {1: "عمود واحد", 2: "عمودان"}

# مساحة المسودة تحت كل سؤال. الارتفاع بوحدة pt (نقطة طباعة).
DRAFT_SIZES = ("none", "small", "medium", "large")
DRAFT_SIZE_LABELS_AR = {"none": "بدون", "small": "صغيرة", "medium": "متوسطة", "large": "كبيرة"}
DRAFT_HEIGHTS_PT = {"none": 0.0, "small": 34.0, "medium": 68.0, "large": 112.0}
# شكل المسودة: «تلقائي» يختار حسب نوع السؤال (رياضي/علمي => مربعات، غيره => أسطر).
DRAFT_STYLES = ("auto", "lines", "grid", "box")
DRAFT_STYLE_LABELS_AR = {"auto": "تلقائي", "lines": "أسطر", "grid": "مربعات", "box": "إطار فارغ"}

PLACEMENTS = ("first", "last", "header", "footer")
PLACEMENT_LABELS_AR = {
    "first": "الصفحة الأولى",
    "last": "الصفحة الأخيرة",
    "header": "ترويسة كل صفحة",
    "footer": "تذييل كل صفحة",
}
ANSWERS_MODES = ("full", "key")
ANSWERS_MODE_LABELS_AR = {
    "full": "الإجابة مع الشرح",
    "key": "مفتاح الإجابة فقط",
}

# حقل -> (الاسم بالعربية، الحد الأقصى للطول)
TEXT_FIELDS: Dict[str, Tuple[str, int]] = {
    "exam_title": ("عنوان الاختبار", 100),
    "subject": ("المادة", 40),
    "grade": ("الصف", 30),
    "exam_date": ("التاريخ", 30),
    "duration": ("المدة", 30),
    "total_marks": ("العلامة الكلية", 8),
    "teacher_name": ("اسم الأستاذ", 60),
}

# رموز قصيرة للحقول داخل callback_data (حد تيليغرام 64 بايت)
FIELD_CODES = {"t": "exam_title", "s": "subject", "g": "grade", "d": "exam_date",
               "u": "duration", "m": "total_marks", "n": "teacher_name"}

# قوالب تخطيط جاهزة بضغطة واحدة. كل قالب يغيّر الحقول المدرجة فيه فقط (تخطيط الصفحة والمسودة وترقيم
# الصفحات)، ولا يمس بيانات الورقة ولا اسم الأستاذ ولا وضع الإجابات ولا سطر بيانات الطالب.
# «عادي» يعيد كل حقول التخطيط (ومنها عدد الأسئلة بالصفحة) إلى الافتراضي.
LAYOUT_PRESETS: Dict[str, Dict[str, Any]] = {
    "plain": {
        "icon": "📄", "label": "عادي",
        "desc": "الشكل المعتاد: عمود واحد وبلا مسودة، وعدد الأسئلة بالصفحة تلقائي.",
        "values": {"per_page": None, "columns": 1, "column_divider": True, "draft_size": "none",
                   "draft_style": "auto", "page_numbers": None},
    },
    "exam": {
        "icon": "📝", "label": "ورقة امتحان",
        "desc": "مساحة مسودة متوسطة تحت كل سؤال بعمود واحد، مع ترقيم الصفحات.",
        "values": {"columns": 1, "draft_size": "medium", "draft_style": "auto", "page_numbers": True},
    },
    "save": {
        "icon": "📰", "label": "توفير ورق",
        "desc": "عمودان بخط فاصل وبلا مسودة، فتتسع أسئلة أكثر بالصفحة.",
        "values": {"columns": 2, "column_divider": True, "draft_size": "none", "page_numbers": True},
    },
    "practice": {
        "icon": "✍️", "label": "تدريب بمساحة حل",
        "desc": "مسودة كبيرة تحت كل سؤال بعمود واحد، ليحلّ الطالب على الورقة نفسها.",
        "values": {"columns": 1, "draft_size": "large", "draft_style": "auto", "page_numbers": True},
    },
}

# ---- تقسيم الإعدادات حسب «ما الذي يخصه» ----
# QUIZ: يخص اختباراً بعينه (عنوانه/مادته/تاريخه/مدته/علامته/عدد أسئلته بالصفحة). يُمسح تلقائياً عند
#       الانتقال إلى كويز آخر حتى لا يتسرّب «عنوان» أو «تاريخ» اختبار سابق إلى ورقة جديدة.
# PROFILE: يخص المعلم وذوقه في الورقة (اسمه، الصف، مكان الاسم، الأعمدة، المسودة، ...). يبقى بين
#       الكويزات، ويمكن حفظه دائماً بحسابه («حفظ كإعداداتي») فيُطبَّق تلقائياً بكل جلسة جديدة.
QUIZ_FIELDS = ("exam_title", "subject", "exam_date", "duration", "total_marks", "per_page")
PROFILE_FIELDS = ("grade", "teacher_name", "teacher_placement", "student_fields", "answers_mode",
                  "page_numbers", "columns", "column_divider", "draft_size", "draft_style")

_DIGITS = str.maketrans("٠١٢٣٤٥٦٧٨٩۰۱۲۳۴۵۶۷۸۹٫،", "01234567890123456789.,")
_MARKS_RE = re.compile(r"^\d{1,4}(?:[.,]\d{1,2})?$")
_SPACES_RE = re.compile(r"\s+")


class ExportOptionsError(ValueError):
    """قيمة إعداد غير صالحة؛ نص الرسالة عربي ومناسب للعرض المباشر للمعلم."""


def _clean(raw: Any) -> str:
    """يزيل محارف التحكم والاتجاه المخفية ويطوي الفراغات (سطر واحد دائماً)."""
    text = "" if raw is None else str(raw)
    text = "".join(ch for ch in text if unicodedata.category(ch) not in ("Cc", "Cf", "Cs", "Co") or ch in "\n\t")
    return _SPACES_RE.sub(" ", text).strip()


def clean_text_field(name: str, raw: Any) -> str:
    """يتحقق من حقل نصي ويُرجع قيمته المطبّعة ('' = بلا قيمة). يرمي ExportOptionsError."""
    if name not in TEXT_FIELDS:
        raise ExportOptionsError("حقل غير معروف.")
    label, limit = TEXT_FIELDS[name]
    if raw is not None and not isinstance(raw, str):
        raise ExportOptionsError(f"«{label}» يجب أن يكون نصاً.")
    value = _clean(raw)
    if name == "total_marks" and value:
        value = value.translate(_DIGITS)
        if not _MARKS_RE.match(value):
            raise ExportOptionsError("«العلامة الكلية» يجب أن تكون رقماً موجباً فقط، مثل 100 أو 50.5.")
        number = float(value.replace(",", "."))
        if number <= 0:
            raise ExportOptionsError("«العلامة الكلية» يجب أن تكون أكبر من صفر.")
        value = value.replace(",", ".")
        if "." in value:
            value = value.rstrip("0").rstrip(".")
    if len(value) > limit:
        raise ExportOptionsError(
            f"«{label}» طويل جداً: الحد الأقصى {limit} حرفاً وأدخلت {len(value)}. اختصره وأعد الإرسال.")
    return value


def parse_per_page(raw: Any, max_allowed: int = MAX_PER_PAGE) -> int:
    """يحوّل مدخل المعلم إلى عدد أسئلة بالصفحة ضمن [1, max_allowed] أو يرمي رسالة واضحة."""
    max_allowed = max(MIN_PER_PAGE, min(MAX_PER_PAGE, int(max_allowed)))
    if isinstance(raw, bool):
        raise ExportOptionsError("أرسل رقماً صحيحاً، مثل 4.")
    if isinstance(raw, int):
        value = raw
    else:
        text = _clean(raw).translate(_DIGITS)
        if not re.fullmatch(r"[+-]?\d+", text):
            raise ExportOptionsError(f"أرسل رقماً صحيحاً بين {MIN_PER_PAGE} و{max_allowed}، مثل 4.")
        value = int(text)
    if value < MIN_PER_PAGE:
        raise ExportOptionsError("أقل عدد ممكن هو سؤال واحد في الصفحة.")
    if value > max_allowed:
        if max_allowed < MAX_PER_PAGE:
            raise ExportOptionsError(
                f"أقصى عدد مناسب لهذا الكويز هو {max_allowed} في الصفحة؛ أسئلته طويلة ولن تُقرأ بوضوح "
                f"عند وضع {value}. اختر رقماً من {MIN_PER_PAGE} إلى {max_allowed}.")
        raise ExportOptionsError(
            f"أقصى عدد مسموح هو {MAX_PER_PAGE} أسئلة في الصفحة حتى تبقى الورقة مقروءة. اختر رقماً من {MIN_PER_PAGE} إلى {MAX_PER_PAGE}.")
    return value


@dataclass(frozen=True)
class ExportOptions:
    per_page: Optional[int] = None          # None = تلقائي (كل ما يتسع، السلوك القديم)
    exam_title: str = ""                    # يستبدل عنوان الكويز في الملف واسمه
    subject: str = ""
    grade: str = ""
    exam_date: str = ""
    duration: str = ""
    total_marks: str = ""
    teacher_name: str = ""
    teacher_placement: str = "first"
    student_fields: bool = True             # سطر (الاسم/الشعبة/التاريخ) الموجود أصلاً
    answers_mode: str = "full"              # يخص نسخة الأستاذ فقط
    page_numbers: Optional[bool] = None     # None = كما كان (PDF نعم، Word لا)
    columns: int = 1                        # 1 = كما كان، 2 = عمودان (PDF وWord فقط)
    column_divider: bool = True             # خط منصّف بين العمودين (يُستعمل فقط مع عمودين)
    draft_size: str = "none"                # مساحة مسودة تحت كل سؤال (PDF وWord فقط)
    draft_style: str = "auto"               # auto / lines / grid / box

    # ---------- تحويل وتحقق ----------

    def to_dict(self) -> Dict[str, Any]:
        """الحقول المخالفة للافتراضي فقط (حجم صغير، وتوافق أسهل بين البوت والسيرفر الثاني)."""
        default = ExportOptions()
        return {f.name: getattr(self, f.name) for f in fields(self)
                if getattr(self, f.name) != getattr(default, f.name)}

    @property
    def is_default(self) -> bool:
        return not self.to_dict()

    # ---------- فصل إعدادات الكويز عن إعدادات المعلم ----------

    def profile_dict(self) -> Dict[str, Any]:
        """إعدادات المعلم (غير الافتراضية فقط) القابلة للحفظ الدائم بحسابه."""
        return {k: v for k, v in self.to_dict().items() if k in PROFILE_FIELDS}

    def without_quiz_fields(self) -> "ExportOptions":
        """نسخة بلا إعدادات الكويز (تعود افتراضية) وبقاء إعدادات المعلم كما هي."""
        default = ExportOptions()
        return replace(self, **{name: getattr(default, name) for name in QUIZ_FIELDS})

    @property
    def has_quiz_fields(self) -> bool:
        return any(name in self.to_dict() for name in QUIZ_FIELDS)

    @classmethod
    def from_profile(cls, data: Optional[Dict[str, Any]]) -> "ExportOptions":
        """من ملف المعلم المحفوظ بقاعدة البيانات: يتجاهل أي مفتاح ليس من PROFILE_FIELDS ويتحقق من
        الباقي بصرامة. تالف => الافتراضي (لا نكسر التصدير بسبب صف فاسد)."""
        if not isinstance(data, dict):
            return cls()
        clean: Dict[str, Any] = {}
        for key, value in data.items():
            if key not in PROFILE_FIELDS:
                continue
            try:  # قيمة فاسدة واحدة لا تُسقط بقية الإعدادات المحفوظة
                cls.from_dict({key: value})
            except ExportOptionsError:
                continue
            clean[key] = value
        return cls.from_dict(clean)

    @classmethod
    def from_dict(cls, data: Optional[Dict[str, Any]]) -> "ExportOptions":
        """بناء صارم من dict (FSM أو payload). قيم غير صالحة => ExportOptionsError."""
        if data is None:
            return cls()
        if not isinstance(data, dict):
            raise ExportOptionsError("إعدادات التصدير غير صالحة.")
        names = {f.name for f in fields(cls)}
        unknown = set(data) - names
        if unknown:
            raise ExportOptionsError("إعداد تصدير غير معروف.")
        kwargs: Dict[str, Any] = {}
        for name, raw in data.items():
            if name in TEXT_FIELDS:
                kwargs[name] = clean_text_field(name, raw)
            elif name == "per_page":
                kwargs[name] = None if raw is None else parse_per_page(raw)
            elif name == "teacher_placement":
                if raw not in PLACEMENTS:
                    raise ExportOptionsError("مكان اسم الأستاذ غير صالح.")
                kwargs[name] = raw
            elif name == "answers_mode":
                if raw not in ANSWERS_MODES:
                    raise ExportOptionsError("نمط الإجابات غير صالح.")
                kwargs[name] = raw
            elif name == "student_fields":
                if not isinstance(raw, bool):
                    raise ExportOptionsError("إعداد حقول الطالب غير صالح.")
                kwargs[name] = raw
            elif name == "page_numbers":
                if raw is not None and not isinstance(raw, bool):
                    raise ExportOptionsError("إعداد ترقيم الصفحات غير صالح.")
                kwargs[name] = raw
            elif name == "columns":
                if isinstance(raw, bool) or raw not in COLUMN_CHOICES:
                    raise ExportOptionsError("عدد الأعمدة يجب أن يكون 1 أو 2.")
                kwargs[name] = int(raw)
            elif name == "column_divider":
                if not isinstance(raw, bool):
                    raise ExportOptionsError("إعداد خط الفصل غير صالح.")
                kwargs[name] = raw
            elif name == "draft_size":
                if raw not in DRAFT_SIZES:
                    raise ExportOptionsError("حجم المسودة غير صالح.")
                kwargs[name] = raw
            elif name == "draft_style":
                if raw not in DRAFT_STYLES:
                    raise ExportOptionsError("شكل المسودة غير صالح.")
                kwargs[name] = raw
        return cls(**kwargs)

    @classmethod
    def from_state_data(cls, data: Optional[Dict[str, Any]]) -> "ExportOptions":
        """من بيانات FSM. تالف/قديم => الافتراضي (لا نكسر التصدير بسبب إعداد فاسد)."""
        raw = (data or {}).get("export_options")
        try:
            return cls.from_dict(raw)
        except ExportOptionsError:
            return cls()

    def with_text(self, name: str, raw: Any) -> "ExportOptions":
        return replace(self, **{name: clean_text_field(name, raw)})

    def with_per_page(self, raw: Any, max_allowed: int = MAX_PER_PAGE) -> "ExportOptions":
        return replace(self, per_page=parse_per_page(raw, max_allowed))

    def with_preset(self, name: str) -> "ExportOptions":
        """يطبّق قالب تخطيط جاهزاً (LAYOUT_PRESETS). اسم غير معروف => ExportOptionsError."""
        preset = LAYOUT_PRESETS.get(name)
        if not preset:
            raise ExportOptionsError("قالب غير معروف.")
        return replace(self, **preset["values"])

    # ---------- عرض ----------

    def summary_lines_ar(self) -> List[str]:
        """سطور نصية (بلا HTML) تلخّص ما غُيّر عن الافتراضي، للعرض والتأكيد بعد التصدير."""
        lines: List[str] = []
        if self.per_page:
            lines.append(f"عدد الأسئلة بالصفحة: {self.per_page}")
        for name in ("exam_title", "subject", "grade", "exam_date", "duration", "total_marks"):
            value = getattr(self, name)
            if value:
                lines.append(f"{TEXT_FIELDS[name][0]}: {value}")
        if self.teacher_name:
            lines.append(f"اسم الأستاذ: {self.teacher_name} ({PLACEMENT_LABELS_AR[self.teacher_placement]})")
        if not self.student_fields:
            lines.append("سطر بيانات الطالب: مخفي")
        if self.answers_mode != "full":
            lines.append(f"الإجابات: {ANSWERS_MODE_LABELS_AR[self.answers_mode]}")
        if self.page_numbers is not None:
            lines.append("ترقيم الصفحات: " + ("ظاهر" if self.page_numbers else "مخفي"))
        if self.columns == 2:
            lines.append("تقسيم الصفحة: عمودان" + (" بخط فاصل" if self.column_divider else " بدون خط فاصل"))
        if self.draft_size != "none":
            lines.append(f"مسودة تحت كل سؤال: {DRAFT_SIZE_LABELS_AR[self.draft_size]}"
                         + (f" ({DRAFT_STYLE_LABELS_AR[self.draft_style]})" if self.draft_style != "auto" else ""))
        return lines

    # ---------- مشتقات يستعملها التخطيط والمولّدات ----------

    @property
    def draft_height(self) -> float:
        return DRAFT_HEIGHTS_PT[self.draft_size]

    @property
    def dense(self) -> bool:
        """تخطيط مضغوط: عمودان أو 8 أسئلة فأكثر بالصفحة => خيارات قصيرة تُرتَّب شبكة 2×2."""
        return self.columns == 2 or (self.per_page or 0) >= 8


# ---------- تعبئة بيانات الورقة برسالة واحدة ----------
# اسم الحقل بالرسالة (بعد التطبيع) -> الحقل. التطبيع حرفاً بحرف (لا يغيّر الأطوال): أ/إ/آ=ا، ى=ي، ة=ه.
_BULK_LABELS: Dict[str, Tuple[str, ...]] = {
    "exam_title": ("عنوان الاختبار", "اسم الاختبار", "العنوان", "عنوان", "title"),
    "subject": ("المادة", "مادة", "subject"),
    "grade": ("الصف الدراسي", "الصف", "صف", "grade", "class"),
    "exam_date": ("تاريخ الاختبار", "التاريخ", "تاريخ", "date"),
    "duration": ("مدة الاختبار", "المدة", "مدة", "الزمن", "الوقت", "duration", "time"),
    "total_marks": ("العلامة الكلية", "الدرجة الكلية", "العلامات", "العلامة", "الدرجات", "الدرجة",
                    "total marks", "marks", "total"),
    "teacher_name": ("اسم الاستاذ", "اسم الاستاذه", "اسم المعلم", "اسم المعلمه", "اسم المدرس", "الاستاذه",
                     "الاستاذ", "المعلمه", "المعلم", "المدرسه", "المدرس", "teacher"),
}
_NORM_MAP = str.maketrans({"أ": "ا", "إ": "ا", "آ": "ا", "ٱ": "ا", "ى": "ي", "ة": "ه"})
_BULK_SEPARATORS = "\n\r،,;؛|"


def _norm_label_text(text: str) -> str:
    """تطبيع حرفاً بحرف بحيث تبقى مواضع النص الأصلي صالحة للنص المُطبَّع."""
    out = []
    for ch in text.translate(_NORM_MAP):
        low = ch.lower()
        out.append(low if len(low) == 1 else ch)
    return "".join(out)


def _build_bulk_regex() -> Tuple["re.Pattern[str]", Dict[str, str]]:
    label_to_field: Dict[str, str] = {}
    for field, labels in _BULK_LABELS.items():
        for label in labels:
            label_to_field[_norm_label_text(label)] = field
    parts = []
    for label in sorted(label_to_field, key=len, reverse=True):
        body = "[ \t]+".join(re.escape(word) for word in label.split())
        # «ال» التعريف اختيارية لكل اسم لا يبدأ بها أصلاً
        parts.append(body if label.startswith("ال") else f"(?:ال)?{body}")
    pattern = re.compile(
        r"(?:^|[" + re.escape(_BULK_SEPARATORS) + r"])[ \t•*\-]*(?P<key>" + "|".join(parts) + r")[ \t]*[:：=][ \t]*",
        re.MULTILINE)
    return pattern, label_to_field


_BULK_RE, _BULK_LABEL_TO_FIELD = _build_bulk_regex()

BULK_HELP_AR = ("لم أجد أي حقل مفهوماً. اكتب كل حقل بصيغة «الاسم: القيمة»، مثل:\n"
                "المادة: رياضيات\nالصف: التاسع\nالمدة: 45 دقيقة")


def _bulk_field_of(norm_key: str) -> Optional[str]:
    key = " ".join(norm_key.split())
    if key in _BULK_LABEL_TO_FIELD:
        return _BULK_LABEL_TO_FIELD[key]
    if key.startswith("ال") and key[2:] in _BULK_LABEL_TO_FIELD:
        return _BULK_LABEL_TO_FIELD[key[2:]]
    return None


def parse_bulk_fields(text: Any) -> Tuple[Dict[str, str], List[str]]:
    """يقرأ رسالة واحدة فيها عدة حقول للورقة («المادة: رياضيات» بسطر لكل حقل أو عدة حقول بسطر واحد
    مفصولة بفاصلة) ويُرجع (القيم الصالحة، ملاحظات عن ما تجاوزناه). كل حقل يُتحقق منه كما لو أُدخل وحده.
    لا حقل مفهوم إطلاقاً => ExportOptionsError. حقل فارغ يُتجاهل. تكرار الحقل: الأخير يكسب."""
    raw = "" if text is None else str(text)
    norm = _norm_label_text(raw)
    matches = list(_BULK_RE.finditer(norm))
    if not matches:
        raise ExportOptionsError(BULK_HELP_AR)
    values: Dict[str, str] = {}
    problems: List[str] = []
    if norm[:matches[0].start()].strip(_BULK_SEPARATORS + " \t•*-"):
        problems.append("تجاهلتُ نصاً قبل أول حقل لم أفهمه.")
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(raw)
        chunk = raw[match.end():end]
        first, _, rest = chunk.partition("\n")
        if rest.strip(_BULK_SEPARATORS + " \t"):
            problems.append("تجاهلتُ سطراً لم أفهمه بعد أحد الحقول.")
        value = _clean(first.strip().rstrip(_BULK_SEPARATORS + " \t"))
        field = _bulk_field_of(match.group("key"))
        if field is None or not value:
            continue
        try:
            values[field] = clean_text_field(field, value)
        except ExportOptionsError as exc:
            problems.append(str(exc))
    if not values and not problems:
        raise ExportOptionsError(BULK_HELP_AR)
    return values, problems


def resolve_draft_style(style: str, is_math: bool) -> str:
    """«تلقائي»: مربعات للأسئلة الرياضية/العلمية (مصوّرة)، أسطر لغيرها."""
    if style == "auto":
        return "grid" if is_math else "lines"
    return style


def per_page_hint_ar(per_page: Optional[int], comfortable: Optional[int]) -> str:
    """تنبيه عندما يطلب المعلم عدداً أكبر مما يتسع عادةً لأسئلة هذا الكويز بتنسيقه الحالي.

    الرقم المختار **سقف** لا حصة ثابتة: المخطط يضع أقل منه بالصفحات المزدحمة بدل التزاحم أو القص.
    لذلك نسمح بكل القيم حتى MAX_PER_PAGE، ونوضّح فقط ما الذي سيحدث فعلاً. فارغ = لا تنبيه."""
    if not per_page or not comfortable or per_page <= comfortable:
        return ""
    return (f"⚠️ اخترت {per_page} أسئلة بالصفحة، لكن أسئلة هذا الكويز تتسع عادةً حتى {comfortable} "
            "بهذا التنسيق، فستحتوي الصفحات المزدحمة على أقل من ذلك تلقائياً دون قص. "
            "لرفع الحد: فعّل «عمودين» أو صغّر مساحة المسودة.")


# ==================== نصوص الورقة (مشتركة بين PDF/Word/Excel) ====================

def labels(is_ar: bool) -> Dict[str, str]:
    if is_ar:
        return {"subject": "المادة", "grade": "الصف", "exam_date": "التاريخ", "duration": "المدة",
                "total_marks": "العلامة الكلية", "teacher": "إعداد الأستاذ"}
    return {"subject": "Subject", "grade": "Grade", "exam_date": "Date", "duration": "Duration",
            "total_marks": "Total marks", "teacher": "Prepared by"}


def exam_meta_line(opts: ExportOptions, is_ar: bool) -> str:
    """سطر واحد (يلتف تلقائياً): المادة | الصف | التاريخ | المدة | العلامة. فارغ لو لا حقول."""
    lab = labels(is_ar)
    parts = [f"{lab[name]}: {getattr(opts, name)}"
             for name in ("subject", "grade", "exam_date", "duration", "total_marks") if getattr(opts, name)]
    return "   |   ".join(parts)


def teacher_text(opts: ExportOptions, is_ar: bool) -> str:
    return f"{labels(is_ar)['teacher']}: {opts.teacher_name}" if opts.teacher_name else ""


def student_line(opts: ExportOptions, is_ar: bool, academic: bool) -> str:
    """سطر بيانات الطالب. بدون إعدادات يطابق النص القديم حرفياً؛ إن حُدّد تاريخ الاختبار
    نحذف فراغ «التاريخ» (لا معنى لأن يكتبه الطالب)."""
    if not opts.student_fields:
        return ""
    has_date = bool(opts.exam_date)
    if academic:
        if is_ar:
            return "الاسم: ________________________" + ("" if has_date else "     التاريخ: ______________")
        return "Name: ________________________" + ("" if has_date else "     Date: ______________")
    if is_ar:
        return "الاسم: ______________  الشعبة: ______" + ("" if has_date else "  التاريخ: __________")
    return "Name: ______________  Class: ______" + ("" if has_date else "  Date: __________")
