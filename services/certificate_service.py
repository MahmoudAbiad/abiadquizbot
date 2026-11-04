"""Generate the lightweight, non-verifiable certificates used by group quizzes."""

from __future__ import annotations

import re
from datetime import datetime
from io import BytesIO
from pathlib import Path
from typing import Tuple

import arabic_reshaper
from bidi.algorithm import get_display
from PIL import Image, ImageDraw, ImageFont
from reportlab.lib.colors import HexColor
from reportlab.lib.pagesizes import A4, landscape
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen import canvas


_ROOT = Path(__file__).resolve().parents[1]
_ARABIC_REGULAR = _ROOT / "assets" / "fonts" / "NotoNaskhArabic-Regular.ttf"
_ARABIC_BOLD = _ROOT / "assets" / "fonts" / "NotoNaskhArabic-Bold.ttf"


def _rtl(text: str) -> str:
    """Shape Arabic while keeping the function safe for Latin/Telegram names."""
    value = str(text or "").strip()
    if not value:
        return ""
    return get_display(arabic_reshaper.reshape(value))


def _font(path: Path, size: int) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(str(path), size=size)


def _centered(draw: ImageDraw.ImageDraw, xy: tuple[int, int], text: str, font, fill) -> None:
    bbox = draw.textbbox((0, 0), text, font=font)
    draw.text((xy[0] - (bbox[2] - bbox[0]) / 2, xy[1] - (bbox[3] - bbox[1]) / 2), text, font=font, fill=fill)


def _safe_filename(name: str) -> str:
    compact = re.sub(r"[^\w\-]+", "_", str(name or "certificate"), flags=re.UNICODE).strip("_")
    return (compact or "certificate")[:80]


def _register_pdf_fonts() -> None:
    if "CertificateArabic" not in pdfmetrics.getRegisteredFontNames():
        pdfmetrics.registerFont(TTFont("CertificateArabic", str(_ARABIC_REGULAR)))
    if "CertificateArabicBold" not in pdfmetrics.getRegisteredFontNames():
        pdfmetrics.registerFont(TTFont("CertificateArabicBold", str(_ARABIC_BOLD)))


def build_certificate(
    *,
    name: str,
    rank: int,
    score: int,
    total_questions: int,
    quiz_title: str,
    issued_at: datetime | None = None,
) -> Tuple[bytes, bytes, str]:
    """Return PNG bytes, PDF bytes, and a safe base filename.

    This is intentionally a commemorative certificate. It contains no claim of
    verification and no secret or user identifier.
    """
    issued_at = issued_at or datetime.now()
    display_name = str(name or "طالب").strip()[:120]
    title = str(quiz_title or "كويز جماعي").strip()[:160]
    rank = max(1, int(rank))
    score = max(0, int(score))
    total_questions = max(0, int(total_questions))
    date_text = issued_at.strftime("%Y-%m-%d")

    navy = (29, 56, 92)
    gold = (190, 143, 45)
    cream = (250, 247, 239)
    muted = (91, 99, 112)

    image = Image.new("RGB", (1600, 1100), cream)
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle((35, 35, 1565, 1065), radius=22, outline=navy, width=8)
    draw.rounded_rectangle((62, 62, 1538, 1038), radius=16, outline=gold, width=3)

    _centered(draw, (800, 135), _rtl("بوت أبيض"), _font(_ARABIC_BOLD, 58), navy)
    _centered(draw, (800, 235), _rtl("شهادة تقدير"), _font(_ARABIC_BOLD, 98), gold)
    _centered(draw, (800, 320), _rtl("تُمنح هذه الشهادة تقديراً للإنجاز في الكويز الجماعي"), _font(_ARABIC_REGULAR, 38), muted)
    _centered(draw, (800, 465), _rtl(display_name), _font(_ARABIC_BOLD, 78), navy)
    draw.line((420, 545, 1180, 545), fill=gold, width=3)
    _centered(draw, (800, 630), _rtl(f"المركز {rank}  •  النتيجة {score}/{total_questions}"), _font(_ARABIC_BOLD, 42), navy)
    _centered(draw, (800, 710), _rtl(title), _font(_ARABIC_REGULAR, 38), muted)
    _centered(draw, (800, 850), _rtl(f"تاريخ الإصدار: {date_text}"), _font(_ARABIC_REGULAR, 30), muted)
    _centered(draw, (800, 955), _rtl("شهادة رمزية غير قابلة للتحقق"), _font(_ARABIC_REGULAR, 28), muted)

    png = BytesIO()
    image.save(png, format="PNG", optimize=True)

    _register_pdf_fonts()
    pdf = BytesIO()
    page_w, page_h = landscape(A4)
    pdf_canvas = canvas.Canvas(pdf, pagesize=(page_w, page_h))
    pdf_canvas.setFillColor(HexColor("#FAF7EF"))
    pdf_canvas.rect(0, 0, page_w, page_h, fill=1, stroke=0)
    pdf_canvas.setStrokeColor(HexColor("#1D385C"))
    pdf_canvas.setLineWidth(4)
    pdf_canvas.roundRect(18, 18, page_w - 36, page_h - 36, 14, fill=0, stroke=1)
    pdf_canvas.setStrokeColor(HexColor("#BE8F2D"))
    pdf_canvas.setLineWidth(1.5)
    pdf_canvas.roundRect(30, 30, page_w - 60, page_h - 60, 10, fill=0, stroke=1)

    def pdf_center(text: str, y: float, size: int, font: str, color: str) -> None:
        pdf_canvas.setFont(font, size)
        pdf_canvas.setFillColor(HexColor(color))
        pdf_canvas.drawCentredString(page_w / 2, y, _rtl(text))

    pdf_center("بوت أبيض", page_h - 72, 24, "CertificateArabicBold", "#1D385C")
    pdf_center("شهادة تقدير", page_h - 135, 40, "CertificateArabicBold", "#BE8F2D")
    pdf_center("تُمنح هذه الشهادة تقديراً للإنجاز في الكويز الجماعي", page_h - 180, 17, "CertificateArabic", "#5B6370")
    pdf_center(display_name, page_h - 270, 32, "CertificateArabicBold", "#1D385C")
    pdf_canvas.setStrokeColor(HexColor("#BE8F2D"))
    pdf_canvas.line(190, page_h - 300, page_w - 190, page_h - 300)
    pdf_center(f"المركز {rank}  •  النتيجة {score}/{total_questions}", page_h - 355, 20, "CertificateArabicBold", "#1D385C")
    pdf_center(title, page_h - 400, 18, "CertificateArabic", "#5B6370")
    pdf_center(f"تاريخ الإصدار: {date_text}", page_h - 480, 15, "CertificateArabic", "#5B6370")
    pdf_center("شهادة رمزية غير قابلة للتحقق", page_h - 535, 13, "CertificateArabic", "#5B6370")
    pdf_canvas.save()

    return png.getvalue(), pdf.getvalue(), _safe_filename(display_name)

