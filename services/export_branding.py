# services/export_branding.py
"""شارة الختام الإجبارية لكل اختبار مُصدَّر (Word / PDF / Excel).

الشارة = صورة (لوغو + أيقونة تلغرام) + معرّف البوت كـ«نص حقيقي» قابل للتحديد والنسخ
وقابل للضغط (رابط يفتح البوت مباشرة). النص ليس مرسوماً داخل الصورة.
"""
import io
import os
from functools import lru_cache
from typing import Tuple

from PIL import Image, ImageDraw

BOT_HANDLE = "@AbiadQuizMakerbot"
BOT_URL = "https://t.me/AbiadQuizMakerbot"
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOGO_PATH = os.path.join(_ROOT, "assets", "abiad_logo_full_badge.png")

TELEGRAM_BLUE = (34, 158, 217, 255)
TELEGRAM_BLUE_HEX = "229ED9"   # لون نص الرابط (نفس أزرق أيقونة تلغرام)
LINE_COLOR = (203, 213, 225, 255)
BADGE_W, BADGE_H = 400, 220


def _telegram_icon(size: int) -> Image.Image:
    ss = 4
    s = size * ss
    img = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.ellipse((0, 0, s - 1, s - 1), fill=TELEGRAM_BLUE)

    def p(x: float, y: float) -> Tuple[float, float]:
        return (x * s / 100.0, y * s / 100.0)

    white = (255, 255, 255, 255)
    d.polygon([p(22, 48), p(77, 26), p(68, 72), p(52, 61), p(44, 70), p(42, 56)], fill=white)
    d.polygon([p(42, 56), p(44, 70), p(52, 61)], fill=(198, 218, 232, 255))
    d.line([p(42, 56), p(70, 34)], fill=TELEGRAM_BLUE, width=max(1, int(s * 0.012)))
    return img.resize((size, size), Image.LANCZOS)


@lru_cache(maxsize=1)
def branding_png() -> bytes:
    """صورة الشارة: لوغو + فاصل + أيقونة تلغرام (بلا أي نص). المعرّف نص حقيقي بجانبها."""
    if not os.path.exists(LOGO_PATH):
        raise FileNotFoundError(f"ملف لوغو أبيض غير موجود: {LOGO_PATH}")
    canvas = Image.new("RGBA", (BADGE_W, BADGE_H), (0, 0, 0, 0))
    d = ImageDraw.Draw(canvas)
    logo_h = BADGE_H - 20
    logo = Image.open(LOGO_PATH).convert("RGBA").resize((logo_h, logo_h), Image.LANCZOS)
    tg_size = 96
    gap = 44
    total_w = logo_h + gap + 3 + gap + tg_size
    x = (BADGE_W - total_w) // 2
    canvas.alpha_composite(logo, (x, (BADGE_H - logo_h) // 2))
    x += logo_h + gap
    d.rectangle((x, 40, x + 2, BADGE_H - 40), fill=LINE_COLOR)
    x += 3 + gap
    canvas.alpha_composite(_telegram_icon(tg_size), (x, (BADGE_H - tg_size) // 2))
    out = io.BytesIO()
    canvas.save(out, format="PNG", optimize=True)
    return out.getvalue()


def branding_aspect() -> float:
    return BADGE_H / BADGE_W
