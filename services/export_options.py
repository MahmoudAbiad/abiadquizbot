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
# سقف مطلق عملي لورقة امتحان A4: أقصر سؤال صح/خطأ (خياران) يأخذ ~60pt من ~700pt متاحة،
# أي نحو 11 سؤالاً. أكثر من 10 يصبح خطاً صغيراً مزدحماً غير مقروء عند الطباعة.
# السقف الفعلي لكل كويز أقل غالباً ويُحسب من طول أسئلته (services.export_layout).
MAX_PER_PAGE = 10

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

    # ---------- تحويل وتحقق ----------

    def to_dict(self) -> Dict[str, Any]:
        """الحقول المخالفة للافتراضي فقط (حجم صغير، وتوافق أسهل بين البوت والسيرفر الثاني)."""
        default = ExportOptions()
        return {f.name: getattr(self, f.name) for f in fields(self)
                if getattr(self, f.name) != getattr(default, f.name)}

    @property
    def is_default(self) -> bool:
        return not self.to_dict()

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
        return lines


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
