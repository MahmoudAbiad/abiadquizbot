"""
services/study_schedule_renderer.py
==============================================================================
محرك التصميم والرسم عالي الدقة للجداول الدراسية الأسبوعية والسنوية.
ينتج صور PNG فائقة النقاء + ملفات PDF جاهزة للطباعة بجودة 300 DPI.
يدعم الخط العربي الأصيل وتشكيل الحروف الصحيح 100%، و4 سمات لونية وتصميمية.
==============================================================================
"""

import asyncio
import io
import os
from typing import Any, Dict, List, Optional, Tuple

from PIL import Image, ImageDraw, ImageFont, features

from services.study_schedule_data import (
    DEFAULT_THEME,
    THEMES,
    SCHEDULE_TYPE_WEEKLY,
    SCHEDULE_TYPE_ANNUAL,
)

try:
    import arabic_reshaper
    from bidi.algorithm import get_display
    _SHAPERS = True
except ImportError:  # pragma: no cover
    _SHAPERS = False

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FONTS_DIR = os.path.join(_ROOT, "assets", "fonts")
LOGO_PATH = os.path.join(_ROOT, "assets", "abiad_logo_full_badge.png")

# أبعاد الصورة والصفحة (A4 Portrait عالي الدقة 1600 × 2400)
WIDTH = 1600
HEIGHT_WEEKLY = 2450
HEIGHT_ANNUAL = 2550

_FONT_FILES = {
    "title": "Amiri-Bold.ttf",
    "bold": "NotoNaskhArabic-Bold.ttf",
    "regular": "NotoNaskhArabic-Regular.ttf",
    "latin_bold": "NotoSans-Bold.ttf",
    "latin_regular": "NotoSans-Regular.ttf",
}

_FONT_CACHE: Dict[Tuple[str, int], ImageFont.FreeTypeFont] = {}
_LOGO_CACHE: Dict[int, Image.Image] = {}


def has_arabic(text: str) -> bool:
    """كشف احتواء النص على أحرف عربية."""
    return any("\u0600" <= ch <= "\u06ff" or "\u0750" <= ch <= "\u077f" for ch in text)


def _engine():
    if _SHAPERS or not features.check("raqm"):
        return ImageFont.Layout.BASIC
    return ImageFont.Layout.RAQM


def _get_font(kind: str, size: int) -> ImageFont.FreeTypeFont:
    """جلب الخط من الكاش لتسريع الرسم وتوفير الذاكرة."""
    key = (kind, size)
    if key not in _FONT_CACHE:
        filename = _FONT_FILES.get(kind, "NotoNaskhArabic-Regular.ttf")
        path = os.path.join(FONTS_DIR, filename)
        if not os.path.exists(path):
            # Fallback to system default if font asset is missing
            try:
                _FONT_CACHE[key] = ImageFont.load_default()
            except Exception:
                pass
        else:
            _FONT_CACHE[key] = ImageFont.truetype(path, size, layout_engine=_engine())
    return _FONT_CACHE[key]


def _shape(text: str) -> str:
    """إعادة تشكيل النص العربي وعكسه bidi لعرض متصل وصحيح."""
    if not text:
        return ""
    if _SHAPERS and has_arabic(text):
        return get_display(arabic_reshaper.reshape(text))
    return text


def _logo(size: int) -> Optional[Image.Image]:
    """جلب شعار البوت بالأبعاد المطلوبة."""
    if size not in _LOGO_CACHE:
        try:
            if os.path.exists(LOGO_PATH):
                with Image.open(LOGO_PATH) as src:
                    _LOGO_CACHE[size] = src.convert("RGBA").resize((size, size), Image.LANCZOS)
        except Exception:
            return None
    return _LOGO_CACHE.get(size)


def _wrap_text_ar(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.FreeTypeFont, max_width: int) -> List[str]:
    """
    تقسيم النص العربي إلى عدة أسطر تتسع داخل max_width.
    قاعدة ذهبية: التقسيم يتم على النص الأصلي أولاً، ثم التشكيل بـ _shape لكل سطر منفرداً.
    """
    words = text.split()
    if not words:
        return []
    lines = []
    curr = []
    for w in words:
        test = " ".join(curr + [w])
        if draw.textlength(_shape(test), font=font) <= max_width:
            curr.append(w)
        else:
            if curr:
                lines.append(" ".join(curr))
                curr = [w]
            else:
                lines.append(w)
                curr = []
    if curr:
        lines.append(" ".join(curr))
    return lines


def _draw_pill(
    draw: ImageDraw.ImageDraw,
    cx: int,
    cy: int,
    text: str,
    font: ImageFont.FreeTypeFont,
    bg_color: str,
    fg_color: str,
    outline_color: Optional[str] = None,
    pad_x: int = 24,
    pad_y: int = 12,
    radius: Optional[int] = None,
) -> Tuple[int, int, int, int]:
    """رسم كبسولة/شارة ملونة ناعمة للنصوص والملاحظات."""
    shaped = _shape(text)
    w = int(draw.textlength(shaped, font=font))
    h = font.size
    x1 = cx - w // 2 - pad_x
    y1 = cy - h // 2 - pad_y
    x2 = cx + w // 2 + pad_x
    y2 = cy + h // 2 + pad_y
    rad = radius if radius is not None else (y2 - y1) // 2
    draw.rounded_rectangle((x1, y1, x2, y2), radius=rad, fill=bg_color, outline=outline_color, width=2 if outline_color else 0)
    draw.text((cx, cy), shaped, font=font, fill=fg_color, anchor="mm")
    return (x1, y1, x2, y2)


def _draw_header(
    draw: ImageDraw.ImageDraw,
    img: Image.Image,
    title: str,
    subtitle: str,
    badge_label: str,
    quote: str,
    theme: Dict[str, Any],
    top_y: int = 50,
) -> int:
    """رسم ترويسة أنيقة وعصرية تتضمن الشعار، العنوان الرئيسي، والشارة، والاقتباس التحفيزي."""
    # خلفية الترويسة الرئيسية
    header_h = 240
    header_box = (50, top_y, WIDTH - 50, top_y + header_h)
    draw.rounded_rectangle(header_box, radius=28, fill=theme["header_bg"], outline=theme.get("accent_border"), width=2)

    # الشعار في الزاوية
    logo_img = _logo(110)
    if logo_img:
        img.paste(logo_img, (WIDTH - 50 - 130, top_y + 35), logo_img)

    # شارة البوت والهوية العلوية
    brand_font = _get_font("bold", 24)
    draw.text(
        (100, top_y + 40),
        _shape("بوت أبيض الذكي · تنظيم الوقت والتفوق الأكاديمي"),
        font=brand_font,
        fill=theme.get("header_subtitle", "#38BDF8"),
        anchor="lm",
    )

    # العنوان الرئيسي الكبير
    title_font = _get_font("title", 54)
    title_x = (WIDTH - 180) if logo_img else (WIDTH - 100)
    draw.text((title_x, top_y + 95), _shape(title), font=title_font, fill=theme["header_text"], anchor="rm")

    # العنوان الفرعي أو الفئة
    sub_font = _get_font("bold", 28)
    draw.text(
        (title_x, top_y + 155),
        _shape(f"{badge_label} · {subtitle}"),
        font=sub_font,
        fill=theme.get("header_subtitle", "#38BDF8"),
        anchor="rm",
    )

    # شريط الاقتباس التحفيزي أسفل الترويسة
    quote_y = top_y + header_h + 20
    quote_h = 70
    q_box = (50, quote_y, WIDTH - 50, quote_y + quote_h)
    draw.rounded_rectangle(q_box, radius=18, fill=theme["card_bg"], outline=theme["card_border"], width=2)

    q_font = _get_font("bold", 24)
    draw.text((WIDTH // 2, quote_y + quote_h // 2), _shape(quote), font=q_font, fill=theme["primary"], anchor="mm")

    return quote_y + quote_h + 25


def _draw_footer(
    draw: ImageDraw.ImageDraw,
    y: int,
    theme: Dict[str, Any],
    student_name: str = "",
    target_info: str = "",
) -> None:
    """رسم تذييل الصفحة مع حقوق البوت وخانة اسم الطالب."""
    footer_box = (50, y, WIDTH - 50, y + 80)
    draw.rounded_rectangle(footer_box, radius=20, fill=theme["footer_bg"], outline=theme["card_border"], width=1)

    font_muted = _get_font("regular", 22)
    font_bold = _get_font("bold", 24)

    # يمين: حقوق وتطوير
    draw.text((WIDTH - 90, y + 40), _shape("صُمم خصيصاً عبر بوت أبيض · @AbiadQuizMakerbot"), font=font_muted, fill=theme["muted"], anchor="rm")

    # يسار: الهدف أو اسم الطالب
    left_text = f"الطالب: {student_name}" if student_name else (target_info or "خطة دراسية منظمة · بالتوفيق والنجاح")
    draw.text((90, y + 40), _shape(left_text), font=font_bold, fill=theme["primary"], anchor="lm")


# ==============================================================================
# رندر الجدول الأسبوعي (Weekly Schedule Renderer)
# ==============================================================================

def render_weekly_schedule(
    schedule_data: Dict[str, Any],
    theme_id: str = DEFAULT_THEME,
    student_name: str = "",
) -> Tuple[bytes, bytes]:
    """
    رسم الجدول الأسبوعي النموذجي وإرجاع صورتين:
    (png_bytes, pdf_bytes).
    """
    theme = THEMES.get(theme_id, THEMES[DEFAULT_THEME])
    img = Image.new("RGB", (WIDTH, HEIGHT_WEEKLY), theme["bg"])
    draw = ImageDraw.Draw(img)

    title = schedule_data.get("title", "الجدول الأسبوعي النموذجي")
    subtitle = schedule_data.get("subtitle", "تنظيم متوازن للمهام الأسبوعية")
    quote = schedule_data.get("quote", "«الانضباط اليومي هو سر الوصول إلى القمة»")
    target_hours = schedule_data.get("target_hours", "6 - 8 ساعات يومياً")

    # 1. الترويسة
    curr_y = _draw_header(
        draw,
        img,
        title=title,
        subtitle=subtitle,
        badge_label=f"⏱️ معدل الدراسة: {target_hours}",
        quote=quote,
        theme=theme,
        top_y=45,
    )

    # 2. بطاقات أيام الأسبوع السبعة
    days = schedule_data.get("days", [])
    card_margin_x = 50
    card_w = WIDTH - (card_margin_x * 2)
    card_h = 225
    card_gap = 20

    day_font = _get_font("title", 36)
    theme_lbl_font = _get_font("bold", 22)
    slot_label_font = _get_font("bold", 22)
    slot_text_font = _get_font("regular", 22)
    check_font = _get_font("bold", 20)

    for idx, day_info in enumerate(days):
        box_y1 = curr_y + idx * (card_h + card_gap)
        box_y2 = box_y1 + card_h
        card_box = (card_margin_x, box_y1, card_margin_x + card_w, box_y2)

        # رسم كرت اليوم
        draw.rounded_rectangle(card_box, radius=22, fill=theme["card_bg"], outline=theme["card_border"], width=2)

        # عمود اليوم (يمين الكرت) - خلفية مميزة مع الاسم
        day_col_w = 230
        day_col_x1 = card_margin_x + card_w - day_col_w
        day_box = (day_col_x1, box_y1, card_margin_x + card_w, box_y2)
        draw.rounded_rectangle(day_box, radius=22, fill=theme["accent_bg"])
        # خط فاصل ناعم
        draw.line((day_col_x1, box_y1, day_col_x1, box_y2), fill=theme["card_border"], width=2)

        # اسم اليوم
        day_name = day_info.get("day", "")
        draw.text((day_col_x1 + day_col_w // 2, box_y1 + 55), _shape(day_name), font=day_font, fill=theme["primary"], anchor="mm")

        # شارة اليوم
        theme_label = day_info.get("theme_label", "")
        if theme_label:
            wrapped_lbl = _wrap_text_ar(draw, theme_label, theme_lbl_font, day_col_w - 30)
            lbl_y = box_y1 + 115
            for l in wrapped_lbl[:2]:
                draw.text((day_col_x1 + day_col_w // 2, lbl_y), _shape(l), font=theme_lbl_font, fill=theme["muted"], anchor="mm")
                lbl_y += 30

        # محتوى الفترات الدراسية (يسار عمود اليوم)
        content_x_right = day_col_x1 - 30
        content_w = content_x_right - card_margin_x - 30

        # الفترة الصباحية
        m_y = box_y1 + 40
        draw.text((content_x_right, m_y), _shape("🌅 الفترة الصباحية:"), font=slot_label_font, fill=theme["primary"], anchor="rm")
        m_txt = day_info.get("morning", "")
        draw.text((content_x_right - 200, m_y), _shape(m_txt), font=slot_text_font, fill=theme["text"], anchor="rm")

        # فترة بعد الظهر
        a_y = box_y1 + 95
        draw.text((content_x_right, a_y), _shape("☀️ بعد الظهر:"), font=slot_label_font, fill=theme.get("secondary", theme["primary"]), anchor="rm")
        a_txt = day_info.get("afternoon", "")
        draw.text((content_x_right - 200, a_y), _shape(a_txt), font=slot_text_font, fill=theme["text"], anchor="rm")

        # الفترة المسائية
        e_y = box_y1 + 150
        draw.text((content_x_right, e_y), _shape("🌙 الفترة المسائية:"), font=slot_label_font, fill=theme["muted"], anchor="rm")
        e_txt = day_info.get("evening", "")
        draw.text((content_x_right - 200, e_y), _shape(e_txt), font=slot_text_font, fill=theme["text"], anchor="rm")

        # خانات التحديد والإنجاز (Checkboxes) أسفل يسار الكرت
        checkboxes = day_info.get("checkboxes", [])
        if checkboxes:
            chk_x = card_margin_x + 35
            for c_txt in reversed(checkboxes):
                c_shaped = _shape(f"▢ {c_txt}")
                w_c = int(draw.textlength(c_shaped, font=check_font))
                _draw_pill(
                    draw,
                    cx=chk_x + w_c // 2,
                    cy=box_y2 - 32,
                    text=f"▢ {c_txt}",
                    font=check_font,
                    bg_color=theme["pill_bg"],
                    fg_color=theme["pill_text"],
                    outline_color=theme.get("checkbox_border"),
                    pad_x=16,
                    pad_y=8,
                    radius=12,
                )
                chk_x += w_c + 45

    curr_y += len(days) * (card_h + card_gap) + 10

    # 3. صندوق النصائح الذهبية
    notes = schedule_data.get("notes", [])
    if notes:
        tip_box = (card_margin_x, curr_y, card_margin_x + card_w, curr_y + 110)
        draw.rounded_rectangle(tip_box, radius=20, fill=theme["accent_bg"], outline=theme["card_border"], width=1)
        tip_title_font = _get_font("bold", 24)
        tip_body_font = _get_font("regular", 22)
        draw.text((card_margin_x + card_w - 30, curr_y + 35), _shape("💡 نصيحة ذهبية للتفوق:"), font=tip_title_font, fill=theme["primary"], anchor="rm")
        combined_tips = "  •  ".join(notes)
        draw.text((card_margin_x + card_w - 30, curr_y + 75), _shape(combined_tips), font=tip_body_font, fill=theme["text"], anchor="rm")
        curr_y += 130

    # 4. التذييل
    _draw_footer(draw, curr_y, theme, student_name=student_name, target_info=f"معدل الساعات: {target_hours}")

    # التصدير إلى بايتات PNG و PDF
    png_buf = io.BytesIO()
    img.save(png_buf, format="PNG", optimize=True)
    png_bytes = png_buf.getvalue()

    pdf_buf = io.BytesIO()
    img.save(pdf_buf, format="PDF", resolution=300.0)
    pdf_bytes = pdf_buf.getvalue()

    return png_bytes, pdf_bytes


# ==============================================================================
# رندر البرنامج السنوي (Annual Schedule Roadmap Renderer)
# ==============================================================================

def render_annual_schedule(
    schedule_data: Dict[str, Any],
    theme_id: str = DEFAULT_THEME,
    student_name: str = "",
) -> Tuple[bytes, bytes]:
    """
    رسم خارطة الطريق السنوية الشاملة للعام الدراسي وإرجاع صورتين:
    (png_bytes, pdf_bytes).
    """
    theme = THEMES.get(theme_id, THEMES[DEFAULT_THEME])
    img = Image.new("RGB", (WIDTH, HEIGHT_ANNUAL), theme["bg"])
    draw = ImageDraw.Draw(img)

    title = schedule_data.get("title", "البرنامج السنوي الشامل")
    subtitle = schedule_data.get("subtitle", "خارطة طريق مدروسة للعام الدراسي")
    quote = schedule_data.get("quote", "«القمة تتسع للجميع، ولكنها تطلب عملاً حقيقياً»")
    target = schedule_data.get("target", "العلامة التامة")

    # 1. الترويسة
    curr_y = _draw_header(
        draw,
        img,
        title=title,
        subtitle=subtitle,
        badge_label=f"🎯 المستهدف: {target}",
        quote=quote,
        theme=theme,
        top_y=45,
    )

    # 2. بطاقات المراحل الأربعة السنوية (The 4 Seasons/Phases)
    phases = schedule_data.get("phases", [])
    card_margin_x = 50
    card_w = WIDTH - (card_margin_x * 2)
    card_h = 360
    card_gap = 25

    phase_title_font = _get_font("title", 36)
    period_font = _get_font("bold", 24)
    goal_font = _get_font("regular", 23)
    strat_label_font = _get_font("bold", 22)
    strat_font = _get_font("regular", 22)

    progress_percents = [25, 50, 80, 100]

    for idx, phase in enumerate(phases):
        box_y1 = curr_y + idx * (card_h + card_gap)
        box_y2 = box_y1 + card_h
        card_box = (card_margin_x, box_y1, card_margin_x + card_w, box_y2)

        # رسم كرت المرحلة
        draw.rounded_rectangle(card_box, radius=24, fill=theme["card_bg"], outline=theme["card_border"], width=2)

        # ترويسة الكرت الداخلية
        p_header_h = 75
        p_header_box = (card_margin_x, box_y1, card_margin_x + card_w, box_y1 + p_header_h)
        draw.rounded_rectangle(p_header_box, radius=24, fill=theme["accent_bg"])
        # تعديل الزوايا السفلية للترويسة لتكون مستقيمة مع الكرت
        draw.rectangle((card_margin_x, box_y1 + 40, card_margin_x + card_w, box_y1 + p_header_h), fill=theme["accent_bg"])
        draw.line((card_margin_x, box_y1 + p_header_h, card_margin_x + card_w, box_y1 + p_header_h), fill=theme["card_border"], width=2)

        # عنوان المرحلة والفترة الزمنية
        p_period = phase.get("period", "")
        draw.text((card_margin_x + card_w - 30, box_y1 + 38), _shape(f"🗓️ {p_period}"), font=phase_title_font, fill=theme["primary"], anchor="rm")

        # شارة نسبة الإنجاز التراكمي
        pct = progress_percents[idx] if idx < len(progress_percents) else 100
        _draw_pill(
            draw,
            cx=card_margin_x + 130,
            cy=box_y1 + 38,
            text=f"إنجاز {pct}% من المنهاج",
            font=period_font,
            bg_color=theme["pill_bg"],
            fg_color=theme["pill_text"],
            outline_color=theme["primary"],
            pad_x=20,
            pad_y=10,
            radius=15,
        )

        # مستهدفات المرحلة (Goals bullets)
        goals = phase.get("goals", [])
        g_y = box_y1 + p_header_h + 30
        for g_idx, goal_txt in enumerate(goals):
            bullet = "✦"
            line_str = f"{bullet}  {goal_txt}"
            draw.text((card_margin_x + card_w - 35, g_y), _shape(line_str), font=goal_font, fill=theme["text"], anchor="rm")
            g_y += 38

        # استراتيجية المرحلة وصندوق النصيحة (Strategy box)
        strategy = phase.get("strategy", "")
        if strategy:
            s_box_y = box_y2 - 65
            s_box = (card_margin_x + 25, s_box_y, card_margin_x + card_w - 25, box_y2 - 15)
            draw.rounded_rectangle(s_box, radius=14, fill=theme["footer_bg"], outline=theme["card_border"], width=1)
            draw.text((card_margin_x + card_w - 45, s_box_y + 25), _shape(f"💡 الاستراتيجية: {strategy}"), font=strat_font, fill=theme["muted"], anchor="rm")

    curr_y += len(phases) * (card_h + card_gap) + 15

    # 3. قسم أسرار وتوجيهات الامتحانات في سوريا (Syria Exam Secrets)
    tips = schedule_data.get("syria_tips", [])
    if tips:
        secrets_h = 175
        secrets_box = (card_margin_x, curr_y, card_margin_x + card_w, curr_y + secrets_h)
        draw.rounded_rectangle(secrets_box, radius=22, fill=theme["accent_bg"], outline=theme["card_border"], width=2)

        sec_title_font = _get_font("bold", 26)
        sec_body_font = _get_font("regular", 23)

        draw.text((card_margin_x + card_w - 35, curr_y + 40), _shape("🇸🇾 أسرار هامة للتعامل مع المناهج وسلالم التصحيح الوزارية:"), font=sec_title_font, fill=theme["primary"], anchor="rm")

        tip_y = curr_y + 80
        for t_txt in tips:
            draw.text((card_margin_x + card_w - 45, tip_y), _shape(f"✔ {t_txt}"), font=sec_body_font, fill=theme["text"], anchor="rm")
            tip_y += 36

        curr_y += secrets_h + 20

    # 4. التذييل
    _draw_footer(draw, curr_y, theme, student_name=student_name, target_info=f"المستهدف العام: {target}")

    # التصدير إلى بايتات PNG و PDF
    png_buf = io.BytesIO()
    img.save(png_buf, format="PNG", optimize=True)
    png_bytes = png_buf.getvalue()

    pdf_buf = io.BytesIO()
    img.save(pdf_buf, format="PDF", resolution=300.0)
    pdf_bytes = pdf_buf.getvalue()

    return png_bytes, pdf_bytes


# دوال غير حاجزة للخيط الرئيسي (Async Wrappers)
async def render_weekly_schedule_async(
    schedule_data: Dict[str, Any],
    theme_id: str = DEFAULT_THEME,
    student_name: str = "",
) -> Tuple[bytes, bytes]:
    """توليد الجدول الأسبوعي عبر خيط منفصل لحماية الـ Event Loop."""
    return await asyncio.to_thread(render_weekly_schedule, schedule_data, theme_id, student_name)


async def render_annual_schedule_async(
    schedule_data: Dict[str, Any],
    theme_id: str = DEFAULT_THEME,
    student_name: str = "",
) -> Tuple[bytes, bytes]:
    """توليد الخطة السنوية عبر خيط منفصل لحماية الـ Event Loop."""
    return await asyncio.to_thread(render_annual_schedule, schedule_data, theme_id, student_name)
