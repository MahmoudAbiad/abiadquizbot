"""
services/group_ceremony.py
==============================================================================
«حفل التكريم» بالكويز الجماعي: القواعد الصرفة فقط (إعدادات، أهلية، اختيار الأوائل، بناء payload الرسم).
بلا I/O ولا aiogram ولا Pillow - قابلة للاختبار مباشرة. الرسم الفعلي بـ services/ceremony_renderer.py.

الإعداد بيتخزّن كـ JSONB واحد بعمود `group_quiz_sessions.ceremony_config`:
    {enabled, title, count, format: "video"|"image", music}
القيمة الفارغة {} = غير مفعّل (الافتراضي). الأهلية نفس قواعد الشهادات (تنافسي + أسماء ظاهرة +
ترتيب نهائي منشور)، وبتعيد استخدام تنظيف الأسماء وتوزيع المراكز من group_certificate.
==============================================================================
"""

import os
from typing import Any, Dict, List, Optional, Tuple

from services.group_certificate import (
    DEFAULT_TITLE,
    NAME_MAX_CHARS,
    TITLE_MAX_CHARS,
    assign_ranks,
    clean_text,
    display_name,
)

ASSETS_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "assets", "ceremony")
THEME_AUDIO_PATH = os.path.join(ASSETS_DIR, "theme.mp3")
THEME_AUDIO_TITLE = "حفل التكريم"
VIDEO_WIDTH, VIDEO_HEIGHT = 720, 900     # أبعاد الفيديو (الحجم الزوجي مطلوب لـ send_video)
MUSIC_SECONDS = 18.9                     # مدة theme.mp3 (الفيديو بيتقصّ عليها)
SILENT_SECONDS = 12.0                    # مدة الفيديو لو الأغنية مطفية

COUNT_CHOICES = (3, 5, 10)
DEFAULT_COUNT = 5
MAX_WINNERS = 12                     # نفس سقف ceremony_renderer.MAX_WINNERS
FORMAT_VIDEO, FORMAT_IMAGE = "video", "image"
FORMATS = (FORMAT_VIDEO, FORMAT_IMAGE)
DEFAULT_FORMAT = FORMAT_VIDEO
MIN_SCORE = 1                        # ما بيُكرَّم من درجته صفر
PREVIEW_NAMES = (
    "سارة الأحمد", "محمد العلي", "ليلى خالد", "عبد الرحمن", "نور الهدى",
    "يوسف حداد", "ريم", "أحمد", "هبة الله", "عمر",
)


def default_config(title: str = DEFAULT_TITLE) -> Dict[str, Any]:
    return {
        "enabled": False, "title": clean_text(title, TITLE_MAX_CHARS) or DEFAULT_TITLE,
        "count": DEFAULT_COUNT, "format": DEFAULT_FORMAT, "music": True,
    }


def normalize_config(raw: Any, *, default_title: str = DEFAULT_TITLE) -> Dict[str, Any]:
    """يقبل أي شي (None، {}، JSON ناقص أو فاسد) ويرجّع إعدادات صالحة دائماً. العنوان لا يكون فاضي أبداً."""
    base = default_config(default_title)
    if not isinstance(raw, dict):
        return base
    cfg = dict(base)
    cfg["enabled"] = raw.get("enabled") is True
    cfg["title"] = clean_text(raw.get("title"), TITLE_MAX_CHARS) or base["title"]
    count = raw.get("count")
    cfg["count"] = count if isinstance(count, int) and not isinstance(count, bool) and count in COUNT_CHOICES else DEFAULT_COUNT
    cfg["format"] = raw.get("format") if raw.get("format") in FORMATS else DEFAULT_FORMAT
    if isinstance(raw.get("music"), bool):
        cfg["music"] = raw["music"]
    return cfg


def eligibility(*, anonymous: bool, names: bool, pace: str, is_channel: bool) -> Tuple[bool, Optional[str]]:
    """هل حفل التكريم ممكن مع هالإعدادات؟ (ok, سبب_العطل). تنافسي + أسماء ظاهرة + ترتيب نهائي منشور."""
    if is_channel:
        return False, "حفل التكريم مو متاح بالقنوات (القنوات مجهولة دائماً)"
    if anonymous:
        return False, "حفل التكريم بيحتاج النمط التنافسي (بالمجهول ما في نقاط ولا أسماء)"
    if not names:
        return False, "حفل التكريم بيحتاج إظهار الأسماء (الأسماء بتنكتب بالصورة)"
    if pace == "b":
        return False, "حفل التكريم ما بيشتغل مع الإرسال دفعة واحدة (ما بينشر ترتيب نهائي)"
    return True, None


def apply_config_change(cfg: Dict[str, Any], value: str) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """يطبّق قيمة callback واحدة. (cfg_جديد, None) أو (None, رسالة_خطأ). لا يتحقق من الأهلية (دور المستدعي).
    القيم: 1/0 تفعيل · n<عدد> · fv/fi صيغة (فيديو/صورة) · m1/m0 الأغنية."""
    cfg = dict(cfg)
    if value in ("1", "0"):
        cfg["enabled"] = value == "1"
    elif value.startswith("n") and value[1:].isdigit() and int(value[1:]) in COUNT_CHOICES:
        cfg["count"] = int(value[1:])
    elif value in ("fv", "fi"):
        cfg["format"] = FORMAT_VIDEO if value == "fv" else FORMAT_IMAGE
    elif value in ("m1", "m0"):
        cfg["music"] = value == "m1"
    else:
        return None, "❌ إعداد غير صالح"
    return cfg, None


def select_winners(rows: List[Dict[str, Any]], cfg: Dict[str, Any], top_n: int) -> List[Dict[str, Any]]:
    """من بيظهر بالحفل: ضمن «الأول N» المنشور نصياً، وضمن عدد الحفل المختار، ودرجته ≥ الحد الأدنى."""
    limit = max(0, min(int(cfg.get("count") or DEFAULT_COUNT), int(top_n), MAX_WINNERS))
    return [r for r in assign_ranks(rows)[:limit] if int(r.get("score") or 0) >= MIN_SCORE]


def build_payload(
    cfg: Dict[str, Any], winners: List[Dict[str, Any]], *, group_name: str, total: int, preview: bool = False,
) -> Dict[str, Any]:
    """payload الرسم من صفوف مرتّبة (فيها rank). كل النصوص تُنظَّف هون، والرسم بيتحقق مرة تانية (دفاعي).
    `total` = عدد الأسئلة؛ ما بينزل عن درجة أي فائز (تحوّط من أسئلة محذوفة)."""
    total = max(1, int(total))
    out: List[Dict[str, Any]] = []
    for w in winners[:MAX_WINNERS]:
        score = int(w.get("score") or 0)
        out.append({
            "rank": max(1, int(w["rank"])), "score": score, "total": max(total, score),
            "name": clean_text(
                display_name(w.get("first_name"), w.get("last_name"), w.get("username"), w.get("user_id")),
                NAME_MAX_CHARS, ellipsis=False) or "متفوّق",
        })
    return {
        "fmt": "mp4" if cfg.get("format") == FORMAT_VIDEO else "png",
        "music": bool(cfg.get("music", True)),
        "preview": bool(preview),
        "title": clean_text(cfg.get("title"), TITLE_MAX_CHARS) or DEFAULT_TITLE,
        "group_name": clean_text(group_name, 48),
        "winners": out,
    }


def preview_winners(count: int, total: int) -> List[Dict[str, Any]]:
    """فائزون وهميون للمعاينة: أسماء عامة، درجات متناقصة، وتعادل على المركز الثالث (ليظهر شكل التعادل)."""
    total = max(1, int(total))
    n = max(1, min(int(count), len(PREVIEW_NAMES), MAX_WINNERS))
    drops = (0, 1, 2, 2, 3, 4, 5, 6, 7, 8)
    rows: List[Dict[str, Any]] = []
    for i in range(n):
        tie_with_prev = i == 3
        rows.append({
            "user_id": i + 1, "first_name": PREVIEW_NAMES[i], "last_name": None, "username": None,
            "score": max(1, total - drops[i]),
            "total_answer_time_ms": 1000 * (i if tie_with_prev else i + 1),
        })
    return assign_ranks(rows)
