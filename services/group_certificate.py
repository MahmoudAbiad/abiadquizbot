"""
services/group_certificate.py
==============================================================================
منطق صرف (بلا I/O ولا PIL ولا aiogram) لشهادات التقدير بالكويز الجماعي:
الإعدادات، قواعد الأهلية، تنظيف الأسماء، الترتيب/التعادل، وبناء payload الرسم.

الرسم نفسه بـ services/certificate_renderer.py (يعمل بالسيرفر الثاني أو محلياً).
==============================================================================
"""

import re
import unicodedata
from typing import Any, Dict, List, Optional, Tuple

TITLE_MAX_CHARS = 60
ISSUER_MAX_CHARS = 40
NAME_MAX_CHARS = 28
GROUP_MAX_CHARS = 48

DEFAULT_TITLE = "كويز"
DEFAULT_THEME = "blue"
COUNT_CHOICES = (1, 3, 5, 10)
DEFAULT_COUNT = 3
MIN_SCORE_FOR_CERT = 1          # لا شهادة لمن درجته صفر

# 6 قوالب جاهزة (لا لون حر): (تسمية عربية، لون الخلفية، الإطار، التمييز، النص، النص الخافت)
THEMES: Dict[str, Dict[str, str]] = {
    "blue":   {"label": "أزرق",   "bg": "#f6f8ff", "border": "#3a5bbf", "accent": "#3a5bbf", "text": "#1c2754", "muted": "#5d6a99"},
    "green":  {"label": "أخضر",   "bg": "#f4faf6", "border": "#2f7d57", "accent": "#2f7d57", "text": "#173d2b", "muted": "#58806c"},
    "gold":   {"label": "ذهبي",   "bg": "#fffaf0", "border": "#b8892b", "accent": "#b8892b", "text": "#4a3512", "muted": "#8a6d3b"},
    "purple": {"label": "بنفسجي", "bg": "#f9f6ff", "border": "#6b4bb8", "accent": "#6b4bb8", "text": "#2c1f52", "muted": "#7b6aa6"},
    "red":    {"label": "أحمر",   "bg": "#fff7f6", "border": "#b23a3a", "accent": "#b23a3a", "text": "#4a1c1c", "muted": "#9a6262"},
    "slate":  {"label": "رمادي",  "bg": "#f7f8f9", "border": "#4b5563", "accent": "#4b5563", "text": "#1f2933", "muted": "#6b7280"},
}
THEME_ORDER = tuple(THEMES.keys())

# عبارة ثابتة غير قابلة للإزالة أو التعديل (لا يستقبلها أي payload من المستخدم).
DISCLAIMER_TEXT = "شهادة تقدير غير رسمية، صادرة عن بوت أبيض"

RANK_LABELS = {1: "المركز الأول", 2: "المركز الثاني", 3: "المركز الثالث", 4: "المركز الرابع", 5: "المركز الخامس"}
GENERIC_RANK_LABEL = "متميّز"


# ==================== تنظيف النصوص ====================
# نسمح فقط بالأحرف اللي الخطوط المرفقة (Amiri / Noto Naskh / Noto Sans) بتغطيها فعلاً:
# عربي (+ملحقاته)، لاتيني (حتى Latin Extended-A)، أرقام، ومسافة وعلامات بسيطة.
# أي إيموجي/صيني/سيريلي/رموز خاصة بتنحذف بدل ما تظهر مربعات.
_ALLOWED_RANGES = (
    (0x0020, 0x007E),   # ASCII
    (0x00C0, 0x017F),   # Latin-1 letters + Latin Extended-A
    (0x0600, 0x06FF),   # Arabic
    (0x0750, 0x077F),   # Arabic Supplement
    (0xFB50, 0xFDFF),   # Arabic Presentation Forms-A
    (0xFE70, 0xFEFF),   # Arabic Presentation Forms-B
    (0x2013, 0x2014),   # en/em dash
    (0x2018, 0x201D),   # typographic quotes (O’Brien)
    (0x2026, 0x2026),   # ellipsis
)
_ARABIC_RE = re.compile(r"[\u0600-\u06FF\u0750-\u077F\uFB50-\uFDFF\uFE70-\uFEFF]")
_HIDDEN_RE = re.compile(r"[\u200b-\u200f\u202a-\u202e\u2066-\u2069\ufeff\u00ad]")
_MULTISPACE_RE = re.compile(r"\s+")


def has_arabic(text: str) -> bool:
    return bool(_ARABIC_RE.search(text or ""))


def clean_text(value: Any, max_len: int, *, ellipsis: bool = True) -> str:
    """نص حر من المستخدم -> نص آمن للرسم: بلا تحكّم/اتجاه خفي/إيموجي، مسافات موحّدة، وقصّ بطول أقصى
    (عند حدّ كلمة إن أمكن). `ellipsis=False` للأسماء: قصّ نظيف بلا «…»."""
    if value is None:
        return ""
    raw = str(value)
    text = unicodedata.normalize("NFKC", raw) if not _ARABIC_RE.search(raw) else raw
    text = _HIDDEN_RE.sub("", text)
    kept = []
    for ch in text:
        cp = ord(ch)
        if ch in "\n\r\t":
            kept.append(" ")
        elif any(lo <= cp <= hi for lo, hi in _ALLOWED_RANGES):
            if unicodedata.category(ch).startswith("C"):
                continue
            kept.append(ch)
    text = _MULTISPACE_RE.sub(" ", "".join(kept)).strip()
    if len(text) > max_len:
        room = max_len - 1 if ellipsis else max_len
        cut = text[:room]
        if " " in cut and text[room] != " ":
            cut = cut[: cut.rfind(" ")]
        text = cut.rstrip() + ("…" if ellipsis else "")
    return text


def display_name(first_name: Any, last_name: Any, username: Any, user_id: Any) -> str:
    """الاسم الأول + الأخير من حساب تيليجرام. لو طلع فاضي بعد التنظيف: @username، وإلا «متفوّق»."""
    first = clean_text(first_name, NAME_MAX_CHARS, ellipsis=False)
    last = clean_text(last_name, NAME_MAX_CHARS, ellipsis=False)
    full = clean_text(f"{first} {last}", NAME_MAX_CHARS, ellipsis=False)
    if full:
        return full
    handle = clean_text(username, NAME_MAX_CHARS, ellipsis=False).lstrip("@")
    if handle:
        return "@" + handle
    return "متفوّق"


# ==================== الإعدادات ====================
def default_config(title: str = DEFAULT_TITLE) -> Dict[str, Any]:
    return {
        "enabled": False, "title": clean_text(title, TITLE_MAX_CHARS) or DEFAULT_TITLE,
        "theme": DEFAULT_THEME, "show_date": True, "show_rank": True, "show_score": True,
        "issuer": "", "count": DEFAULT_COUNT,
    }


def normalize_config(raw: Any, *, default_title: str = DEFAULT_TITLE) -> Dict[str, Any]:
    """يقبل أي شي (None، {}، JSON ناقص أو فاسد) ويرجّع إعدادات صالحة دائماً. اسم الاختبار لا يكون فاضي أبداً."""
    base = default_config(default_title)
    if not isinstance(raw, dict):
        return base
    cfg = dict(base)
    cfg["enabled"] = raw.get("enabled") is True
    title = clean_text(raw.get("title"), TITLE_MAX_CHARS)
    cfg["title"] = title or base["title"]
    cfg["theme"] = raw.get("theme") if raw.get("theme") in THEMES else DEFAULT_THEME
    for key in ("show_date", "show_rank", "show_score"):
        if isinstance(raw.get(key), bool):
            cfg[key] = raw[key]
    cfg["issuer"] = clean_text(raw.get("issuer"), ISSUER_MAX_CHARS)
    count = raw.get("count")
    cfg["count"] = count if isinstance(count, int) and not isinstance(count, bool) and count in COUNT_CHOICES else DEFAULT_COUNT
    return cfg


def eligibility(*, anonymous: bool, names: bool, pace: str, is_channel: bool) -> Tuple[bool, Optional[str]]:
    """هل الشهادات ممكنة مع هالإعدادات؟ (ok, سبب_العطل). القواعد: تنافسي + أسماء ظاهرة + ترتيب نهائي منشور."""
    if is_channel:
        return False, "الشهادات مو متاحة بالقنوات (القنوات مجهولة دائماً)"
    if anonymous:
        return False, "الشهادات بتحتاج النمط التنافسي (بالمجهول ما في نقاط ولا أسماء)"
    if not names:
        return False, "الشهادات بتحتاج إظهار الأسماء (الاسم بينطبع على الشهادة)"
    if pace == "b":
        return False, "الشهادات ما بتشتغل مع الإرسال دفعة واحدة (ما بينشر ترتيب نهائي)"
    return True, None


_THEME_PREFIX = "th"


def apply_config_change(cfg: Dict[str, Any], value: str) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """يطبّق قيمة callback واحدة على الإعدادات. (cfg_جديد, None) أو (None, رسالة_خطأ). لا يتحقق من الأهلية (هاد دور المستدعي)."""
    cfg = dict(cfg)
    if value in ("1", "0"):
        cfg["enabled"] = value == "1"
    elif value.startswith(_THEME_PREFIX) and value[len(_THEME_PREFIX):] in THEMES:
        cfg["theme"] = value[len(_THEME_PREFIX):]
    elif value in ("d1", "d0"):
        cfg["show_date"] = value == "d1"
    elif value in ("r1", "r0"):
        cfg["show_rank"] = value == "r1"
    elif value in ("sc1", "sc0"):
        cfg["show_score"] = value == "sc1"
    elif value.startswith("n") and value[1:].isdigit() and int(value[1:]) in COUNT_CHOICES:
        cfg["count"] = int(value[1:])
    else:
        return None, "❌ إعداد غير صالح"
    return cfg, None


# ==================== الأهلية والترتيب ====================
def assign_ranks(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """الصفوف مرتّبة أصلاً (أعلى نقاط ثم أسرع). التعادل: نفس النقاط ونفس مجموع الزمن = نفس المركز.
    غير هيك المركز = موقعه بالترتيب (نفس ترتيب الرسالة النصية بالضبط). يرجّع نسخاً مع مفتاح `rank`."""
    ranked: List[Dict[str, Any]] = []
    prev_key = None
    prev_rank = 0
    for position, row in enumerate(rows, start=1):
        key = (int(row.get("score") or 0), int(row.get("total_answer_time_ms") or 0))
        rank = prev_rank if key == prev_key else position
        ranked.append({**row, "rank": rank})
        prev_key, prev_rank = key, rank
    return ranked


def select_winners(rows: List[Dict[str, Any]], cfg: Dict[str, Any], top_n: int) -> List[Dict[str, Any]]:
    """من يستحق شهادة: ضمن «الأول N» المنشور نصياً، وضمن عدد الشهادات المختار، ودرجته ≥ الحد الأدنى."""
    limit = max(0, min(int(cfg.get("count") or DEFAULT_COUNT), int(top_n)))
    eligible = [r for r in assign_ranks(rows)[:limit] if int(r.get("score") or 0) >= MIN_SCORE_FOR_CERT]
    return eligible


def rank_label(rank: int) -> str:
    return RANK_LABELS.get(int(rank), f"المركز {int(rank)}")


def format_date(now_utc) -> str:
    """التاريخ بتوقيت سوريا (UTC+3 ثابت، نفس الاعتماد ببقية المشروع)، بأرقام لاتينية: 2026/11/05."""
    import datetime
    local = now_utc + datetime.timedelta(hours=3)
    return local.strftime("%Y/%m/%d")


def build_payload(
    cfg: Dict[str, Any], *, name: str, group_name: str, rank: int, score: int, total: int,
    date_text: str, preview: bool = False,
) -> Dict[str, Any]:
    """payload الرسم. كل النصوص تُنظَّف هون، والسيرفر الثاني يتحقق مرة تانية (دفاعي)."""
    return {
        "name": clean_text(name, NAME_MAX_CHARS, ellipsis=False) or "متفوّق",
        "title": clean_text(cfg.get("title"), TITLE_MAX_CHARS) or DEFAULT_TITLE,
        "group_name": clean_text(group_name, GROUP_MAX_CHARS),
        "issuer": clean_text(cfg.get("issuer"), ISSUER_MAX_CHARS),
        "theme": cfg.get("theme") if cfg.get("theme") in THEMES else DEFAULT_THEME,
        "rank": max(1, int(rank)),
        "show_rank": bool(cfg.get("show_rank", True)),
        "score": max(0, int(score)),
        "total": max(1, int(total)),
        "show_score": bool(cfg.get("show_score", True)),
        "date_text": date_text if cfg.get("show_date", True) else "",
        "preview": bool(preview),
    }
