# services/export_branding.py
"""شارة الختام الإجبارية لكل اختبار مُصدَّر (Word / PDF / Excel)."""
import io
import os
from functools import lru_cache
from typing import Tuple

from PIL import Image, ImageDraw, ImageFont

BOT_HANDLE = "@AbiadQuizMakerbot"
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOGO_PATH = os.path.join(_ROOT, "assets", "abiad_logo_full_badge.png")
_FONT_PATH = os.path.join(_ROOT, "assets", "fonts", "NotoSans-Bold.ttf")

TELEGRAM_BLUE = (34, 158, 217, 255)
TEXT_COLOR = (51, 65, 85, 255)
LINE_COLOR = (203, 213, 225, 255)
CANVAS_W, CANVAS_H = 1200, 220


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
    if not os.path.exists(LOGO_PATH):
        raise FileNotFoundError(f"ملف لوغو أبيض غير موجود: {LOGO_PATH}")
    canvas = Image.new("RGBA", (CANVAS_W, CANVAS_H), (0, 0, 0, 0))
    d = ImageDraw.Draw(canvas)
    logo_h = CANVAS_H - 20
    logo = Image.open(LOGO_PATH).convert("RGBA").resize((logo_h, logo_h), Image.LANCZOS)
    tg_size = 96
    font = ImageFont.truetype(_FONT_PATH, 66)
    text_w = int(d.textlength(BOT_HANDLE, font=font))
    gap_logo, gap_icon = 44, 22
    total_w = logo_h + gap_logo + 3 + gap_logo + tg_size + gap_icon + text_w
    x = (CANVAS_W - total_w) // 2
    canvas.alpha_composite(logo, (x, (CANVAS_H - logo_h) // 2))
    x += logo_h + gap_logo
    d.rectangle((x, 40, x + 2, CANVAS_H - 40), fill=LINE_COLOR)
    x += 3 + gap_logo
    canvas.alpha_composite(_telegram_icon(tg_size), (x, (CANVAS_H - tg_size) // 2))
    x += tg_size + gap_icon
    bbox = d.textbbox((0, 0), BOT_HANDLE, font=font)
    ty = (CANVAS_H - (bbox[3] - bbox[1])) // 2 - bbox[1]
    d.text((x, ty), BOT_HANDLE, font=font, fill=TEXT_COLOR)
    out = io.BytesIO()
    canvas.save(out, format="PNG", optimize=True)
    return out.getvalue()


def branding_aspect() -> float:
    return CANVAS_H / CANVAS_W
