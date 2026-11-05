"""
services/certificate_renderer.py
==============================================================================
رسم شهادة التقدير (PNG) بـ Pillow. يعمل بالسيرفر الثاني (render_worker) عبر
`POST /v1/render/certificate`، أو محلياً كاحتياط عند تعذّره (نفس نمط صور الأسئلة).

⚠️ ما بيستورد aiogram ولا قاعدة البيانات. الرسم CPU-bound فلا يُستدعى على حلقة الأحداث
مباشرة: استخدم `render_certificate_async`.

العربي: arabic-reshaper + python-bidi (نفس اعتماد image_quiz_renderer)، مع الخط بنمط BASIC
حتى لا يعيد raqm عكس النص المُشكَّل مسبقاً. لو المكتبتين غير متوفرتين (بيئة تطوير فقط) نرجع لـ raqm.
==============================================================================
"""

import asyncio
import io
import os
from typing import Any, Dict, Optional, Tuple

from PIL import Image, ImageDraw, ImageFont, features

from services.group_certificate import (
    DEFAULT_TITLE, DISCLAIMER_TEXT, GENERIC_RANK_LABEL, GROUP_MAX_CHARS, ISSUER_MAX_CHARS,
    NAME_MAX_CHARS, THEMES, TITLE_MAX_CHARS, clean_text, has_arabic, rank_label,
)

try:  # production: requirements.txt / requirements.worker.txt
    import arabic_reshaper
    from bidi.algorithm import get_display
    _SHAPERS = True
except ImportError:  # pragma: no cover - dev machines only
    _SHAPERS = False

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FONTS_DIR = os.path.join(_ROOT, "assets", "fonts")
LOGO_PATH = os.path.join(_ROOT, "assets", "abiad_logo_full_badge.png")

WIDTH, HEIGHT = 1600, 1130

_FONT_FILES = {
    "title": "Amiri-Bold.ttf",
    "name_ar": "Amiri-Bold.ttf",
    "name_lat": "NotoSans-Bold.ttf",
    "body": "NotoNaskhArabic-Regular.ttf",
    "bold": "NotoNaskhArabic-Bold.ttf",
    "body_lat": "NotoSans-Regular.ttf",
}

_FONT_CACHE: Dict[Tuple[str, int], ImageFont.FreeTypeFont] = {}
_LOGO_CACHE: Dict[int, Image.Image] = {}


class CertificateError(ValueError):
    """مدخلات رسم غير صالحة."""


def _engine():
    if _SHAPERS or not features.check("raqm"):
        return ImageFont.Layout.BASIC
    return ImageFont.Layout.RAQM


def _font(kind: str, size: int) -> ImageFont.FreeTypeFont:
    key = (kind, size)
    if key not in _FONT_CACHE:
        path = os.path.join(FONTS_DIR, _FONT_FILES[kind])
        _FONT_CACHE[key] = ImageFont.truetype(path, size, layout_engine=_engine())
    return _FONT_CACHE[key]


def _shape(text: str) -> str:
    if _SHAPERS and has_arabic(text):
        return get_display(arabic_reshaper.reshape(text))
    return text


def _kind_for(text: str, arabic_kind: str, latin_kind: str) -> str:
    return arabic_kind if has_arabic(text) else latin_kind


def _fit(draw: ImageDraw.ImageDraw, text: str, kind: str, start: int, minimum: int, max_width: int) -> Tuple[ImageFont.FreeTypeFont, str]:
    """أكبر حجم خط يتسع بالعرض؛ لو حتى الأصغر ما اتسع نقصّ النص بـ «…» (لا يتجاوز الإطار أبداً)."""
    size = start
    while size >= minimum:
        font = _font(kind, size)
        if draw.textlength(_shape(text), font=font) <= max_width:
            return font, text
        size -= 4
    font = _font(kind, minimum)
    trimmed = text
    while len(trimmed) > 1 and draw.textlength(_shape(trimmed + "…"), font=font) > max_width:
        trimmed = trimmed[:-1]
    return font, trimmed.rstrip() + "…"


def _center(draw, text: str, y: int, font, fill, x: int = WIDTH // 2) -> None:
    draw.text((x, y), _shape(text), font=font, fill=fill, anchor="mm")


def _logo(size: int) -> Optional[Image.Image]:
    if size not in _LOGO_CACHE:
        try:
            with Image.open(LOGO_PATH) as src:
                _LOGO_CACHE[size] = src.convert("RGBA").resize((size, size), Image.LANCZOS)
        except Exception:
            return None
    return _LOGO_CACHE[size]


def _pill(draw, cx: int, cy: int, text: str, font, fill_bg, fill_fg, outline=None, pad_x=34, pad_y=18) -> int:
    shaped = _shape(text)
    w = int(draw.textlength(shaped, font=font))
    h = font.size
    box = (cx - w // 2 - pad_x, cy - h // 2 - pad_y, cx + w // 2 + pad_x, cy + h // 2 + pad_y)
    draw.rounded_rectangle(box, radius=(box[3] - box[1]) // 2, fill=fill_bg, outline=outline, width=3 if outline else 0)
    draw.text((cx, cy), shaped, font=font, fill=fill_fg, anchor="mm")
    return box[2] - box[0]


def validate_payload(payload: Dict[str, Any]) -> Dict[str, Any]:
    """تحقق دفاعي (السيرفر الثاني مكشوف للإنترنت): أنواع، حدود، تنظيف نصوص. يرمي CertificateError."""
    if not isinstance(payload, dict):
        raise CertificateError("payload must be an object")
    theme = payload.get("theme")
    if theme not in THEMES:
        raise CertificateError("theme is not supported")
    for key in ("show_rank", "show_score", "preview"):
        if not isinstance(payload.get(key), bool):
            raise CertificateError(f"{key} must be a boolean")
    for key, lo, hi in (("rank", 1, 1000), ("score", 0, 100000), ("total", 1, 100000)):
        value = payload.get(key)
        if isinstance(value, bool) or not isinstance(value, int) or not lo <= value <= hi:
            raise CertificateError(f"{key} must be an integer in [{lo}, {hi}]")
    date_text = payload.get("date_text", "")
    if not isinstance(date_text, str) or len(date_text) > 20:
        raise CertificateError("date_text must be a short string")
    for key in ("name", "title", "group_name", "issuer"):
        if not isinstance(payload.get(key, ""), str) or len(payload.get(key, "")) > 400:
            raise CertificateError(f"{key} must be a string")
    return {
        "theme": theme, "show_rank": payload["show_rank"], "show_score": payload["show_score"], "preview": payload["preview"],
        "rank": payload["rank"], "score": payload["score"], "total": payload["total"],
        "date_text": clean_text(date_text, 20),
        "name": clean_text(payload.get("name"), NAME_MAX_CHARS, ellipsis=False) or "متفوّق",
        "title": clean_text(payload.get("title"), TITLE_MAX_CHARS) or DEFAULT_TITLE,
        "group_name": clean_text(payload.get("group_name"), GROUP_MAX_CHARS),
        "issuer": clean_text(payload.get("issuer"), ISSUER_MAX_CHARS),
    }


def render_certificate(payload: Dict[str, Any]) -> bytes:
    """يرسم الشهادة ويرجّع PNG. العبارة غير الرسمية ثابتة هون ولا تأتي من الـ payload."""
    p = validate_payload(payload)
    t = THEMES[p["theme"]]
    img = Image.new("RGB", (WIDTH, HEIGHT), t["bg"])
    d = ImageDraw.Draw(img)

    # الإطار: خارجي سميك + داخلي رفيع + زوايا
    d.rectangle((14, 14, WIDTH - 15, HEIGHT - 15), outline=t["border"], width=14)
    d.rectangle((44, 44, WIDTH - 45, HEIGHT - 45), outline=t["border"], width=3)
    for cx, cy in ((44, 44), (WIDTH - 45, 44), (44, HEIGHT - 45), (WIDTH - 45, HEIGHT - 45)):
        d.ellipse((cx - 16, cy - 16, cx + 16, cy + 16), fill=t["bg"], outline=t["border"], width=4)
        d.ellipse((cx - 6, cy - 6, cx + 6, cy + 6), fill=t["accent"])

    logo = _logo(150)
    if logo is not None:
        img.paste(logo, (WIDTH // 2 - 75, 78), logo)

    _center(d, "شهادة تقدير", 300, _font("title", 104), t["accent"])
    d.line((WIDTH // 2 - 220, 372, WIDTH // 2 - 24, 372), fill=t["accent"], width=3)
    d.line((WIDTH // 2 + 24, 372, WIDTH // 2 + 220, 372), fill=t["accent"], width=3)
    d.polygon([(WIDTH // 2, 360), (WIDTH // 2 + 12, 372), (WIDTH // 2, 384), (WIDTH // 2 - 12, 372)], fill=t["accent"])

    _center(d, "تُمنح هذه الشهادة إلى", 440, _font("body", 38), t["muted"])

    name_kind = _kind_for(p["name"], "name_ar", "name_lat")
    name_font, name_text = _fit(d, p["name"], name_kind, 120, 52, 1180)
    _center(d, name_text, 538, name_font, t["text"])
    d.line((WIDTH // 2 - 430, 610, WIDTH // 2 + 430, 610), fill=t["border"], width=2)

    _center(d, "تقديراً للأداء المتميّز في اختبار", 672, _font("body", 38), t["muted"])
    title_text = f"«{p['title']}»" if has_arabic(p["title"]) else f"“{p['title']}”"
    tfont, ttext = _fit(d, title_text, _kind_for(title_text, "bold", "body_lat"), 66, 34, 1240)
    _center(d, ttext, 750, tfont, t["accent"])

    if p["group_name"]:
        gtext = f"ضمن مجموعة: {p['group_name']}"
        gfont, gt = _fit(d, gtext, _kind_for(gtext, "body", "body_lat"), 38, 26, 1200)
        _center(d, gt, 826, gfont, t["muted"])

    pills = []
    if p["show_rank"]:
        pills.append((rank_label(p["rank"]), True))
    else:
        pills.append((GENERIC_RANK_LABEL, True))
    if p["show_score"]:
        pills.append((f"الدرجة: {p['score']}/{p['total']}", False))
    pill_font = _font("bold", 40)
    gap = 36
    widths = [int(d.textlength(_shape(txt), font=pill_font)) + 68 for txt, _ in pills]
    x = WIDTH // 2 + (sum(widths) + gap * (len(pills) - 1)) // 2
    for (txt, solid), w in zip(pills, widths):
        cx = x - w // 2
        if solid:
            _pill(d, cx, 900, txt, pill_font, t["accent"], "#ffffff")
        else:
            _pill(d, cx, 900, txt, pill_font, t["bg"], t["accent"], outline=t["accent"])
        x -= w + gap

    footer_items = []
    if p["issuer"]:
        footer_items.append(p["issuer"])
    if p["date_text"]:
        footer_items.append(p["date_text"])
    if footer_items:
        slots = [WIDTH // 2] if len(footer_items) == 1 else [WIDTH * 3 // 4, WIDTH // 4]
        for item, cx in zip(footer_items, slots):
            ifont, itxt = _fit(d, item, _kind_for(item, "body", "body_lat"), 34, 24, 560)
            d.line((cx - 200, 978, cx + 200, 978), fill=t["border"], width=2)
            _center(d, itxt, 1006, ifont, t["text"], x=cx)

    _center(d, DISCLAIMER_TEXT, 1050, _font("body", 28), t["muted"])

    if p["preview"]:
        overlay = Image.new("RGBA", (WIDTH, HEIGHT), (0, 0, 0, 0))
        od = ImageDraw.Draw(overlay)
        od.text((WIDTH // 2, HEIGHT // 2), _shape("معاينة"), font=_font("title", 300), fill=(120, 120, 120, 46), anchor="mm")
        overlay = overlay.rotate(24, resample=Image.BICUBIC, center=(WIDTH // 2, HEIGHT // 2))
        img = Image.alpha_composite(img.convert("RGBA"), overlay).convert("RGB")

    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    return buf.getvalue()


# ==================== الواجهة غير المتزامنة (للبوت) ====================
_LOCAL_SEMAPHORE: Optional[asyncio.Semaphore] = None


async def render_certificate_async(payload: Dict[str, Any]) -> bytes:
    """السيرفر الثاني أولاً (لو مفعّل)، ثم احتياط محلي بخيط واحد فقط (حماية ذاكرة 512MB).
    ترمي استثناء عند الفشل - المستدعي (إرسال الشهادات/المعاينة) مسؤول عن ابتلاعه بحيث لا يتعطّل إنهاء الجلسة."""
    global _LOCAL_SEMAPHORE
    from services import offload

    remote = await offload.remote_render_certificate(payload)
    if remote is not None:
        return remote
    if _LOCAL_SEMAPHORE is None:
        _LOCAL_SEMAPHORE = asyncio.Semaphore(1)
    async with _LOCAL_SEMAPHORE:
        return await asyncio.to_thread(render_certificate, payload)
