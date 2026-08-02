# services/math_renderer.py
"""
خدمة رسم وتحويل الأسئلة الرياضية وصيغ LaTeX إلى صور شمولية عالية الدقة.
تستخدم Matplotlib في وضع Headless (بدون واجهة رسومية) لضمان العمل على سيرفرات Railway/Linux.
"""

import os
import re
import uuid
import asyncio
import matplotlib
# تفعيل وضع Server/Headless لمنع استدعاء واجهات النظام الرسومية
matplotlib.use('Agg')
import matplotlib.pyplot as plt

import arabic_reshaper
from bidi.algorithm import get_display
from logger import get_logger

logger = get_logger(__name__)
DOWNLOADS_DIR = "downloads"


def _smart_wrap_text(text: str, max_chars: int = 42) -> str:
    """
    تقسيم النص الذكي إلى أسطر متناسقة مع الحفاظ على صيغ LaTeX $...$ من الانكسار.
    """
    if not text:
        return ""
    
    # تفكيك النص إلى كلمات أو صيغ رياضية متكاملة
    tokens = re.findall(r'\$.*?\$|\S+', text)
    lines = []
    current_line = []
    current_len = 0

    for token in tokens:
        clean_len = len(token.replace('$', ''))
        if current_len + clean_len + 1 > max_chars and current_line:
            lines.append(" ".join(current_line))
            current_line = [token]
            current_len = clean_len
        else:
            current_line.append(token)
            current_len += clean_len + 1

    if current_line:
        lines.append(" ".join(current_line))

    return "\n".join(lines)


def _prepare_arabic_and_math(text: str) -> str:
    """
    عزل صيغ LaTeX المحصورة بين $...$ لحمايتها من الانقلاب أثناء معالجة 
    الاتجاه العربي (BiDi) لضمان رسم المعادلات والكسور بشكل صحيح تماماً.
    """
    if not text:
        return ""

    # 1. استخراج واستبدال كافة صيغ LaTeX بترميز مؤقت
    math_expressions = re.findall(r'(\$.*?\$)', text)
    placeholders = [f"MATHPLACEHOLDER{i}" for i in range(len(math_expressions))]
    
    temp_text = text
    for placeholder, math_expr in zip(placeholders, math_expressions):
        temp_text = temp_text.replace(math_expr, placeholder)

    # 2. معالجة وتشكيل النص العربي سطر بسطر للحفاظ على التنسيق متعدد الأسطر
    lines = temp_text.split('\n')
    reshaped_lines = []
    for line in lines:
        try:
            reshaped = arabic_reshaper.reshape(line)
            bidi_line = get_display(reshaped)
        except Exception:
            bidi_line = line
        reshaped_lines.append(bidi_line)
    
    bidi_text = '\n'.join(reshaped_lines)

    # 3. إعادة صيغ LaTeX كما هي لمواضعها دون تشويه
    for placeholder, math_expr in zip(placeholders, math_expressions):
        bidi_placeholder = get_display(placeholder)
        if bidi_placeholder in bidi_text:
            bidi_text = bidi_text.replace(bidi_placeholder, math_expr)
        else:
            bidi_text = bidi_text.replace(placeholder, math_expr)

    return bidi_text


def _render_sync(question_data: dict, current_idx: int, total_count: int, output_path: str) -> str:
    """
    الوظيفة التنفيذية المباشرة لرسم البطاقة الأكاديمية للسؤال والخيارات.
    """
    question_text = question_data.get("question", "")
    options = question_data.get("options", [])
    labels = ["أ", "ب", "ج", "د"]

    # 1. التغليف الذكي لنص السؤال وحساب عدد أسطره
    wrapped_q = _smart_wrap_text(question_text, max_chars=40)
    q_text_prepared = _prepare_arabic_and_math(wrapped_q)
    q_lines_count = q_text_prepared.count('\n') + 1

    # 2. فحص أطوال الخيارات: استخدام شبكة 2x2 إذا كانت قصيرة
    max_opt_len = max([len(str(opt)) for opt in options]) if options else 0
    is_grid_layout = (max_opt_len <= 22) and (len(options) == 4)

    # حساب الارتفاع المريح للبطاقة بحسب عدد أسطر السؤال
    fig_height = 5.8 + (q_lines_count * 0.45)
    fig, ax = plt.subplots(figsize=(8.2, fig_height), dpi=220)
    fig.patch.set_facecolor('#FFFFFF')
    ax.set_facecolor('#FFFFFF')
    ax.axis('off')

    # 3. رأس البطاقة: رقم السؤال
    word_q = get_display(arabic_reshaper.reshape("السؤال"))
    word_of = get_display(arabic_reshaper.reshape("من"))
    header_text = f"{word_q} {current_idx} {word_of} {total_count}"

    header_y = 0.94
    ax.text(0.95, header_y, header_text, fontsize=15, fontweight='bold', color='#2563EB',
            ha='right', va='top', transform=ax.transAxes)

    # خط فاصل تحت الرأس
    line_y = header_y - 0.05
    ax.plot([0.05, 0.95], [line_y, line_y], color='#E2E8F0', linewidth=1.5, transform=ax.transAxes)

    # 4. متن السؤال الرئيسي
    q_y = line_y - 0.04
    ax.text(0.95, q_y, q_text_prepared, fontsize=14.5, color='#0F172A',
            ha='right', va='top', transform=ax.transAxes, multialignment='right', linespacing=1.4)

    # حساب موضع بداية الخيارات بناءً على عمق أسطر السؤال
    options_start_y = q_y - (q_lines_count * 0.09) - 0.06
    bbox_props = dict(boxstyle="round,pad=0.5", fc="#F8FAFC", ec="#CBD5E1", lw=1.0)

    # 5. رسم الخيارات (شبكة 2x2 أو عمود متتالي 1x4)
    if is_grid_layout:
        # ترتيب الشبكة 2x2:
        # السطر الأول: [أ] يمين | [ب] يسار
        # السطر الثاني: [ج] يمين | [د] يسار
        positions = [
            (0.93, options_start_y),                 # [أ]
            (0.46, options_start_y),                 # [ب]
            (0.93, options_start_y - 0.13),          # [ج]
            (0.46, options_start_y - 0.13)           # [د]
        ]
        for i, opt in enumerate(options[:4]):
            label_char = labels[i] if i < len(labels) else str(i + 1)
            opt_raw = f"[{label_char}]  {opt}"
            opt_prepared = _prepare_arabic_and_math(opt_raw)
            x_pos, y_pos = positions[i]

            ax.text(x_pos, y_pos, opt_prepared, fontsize=13.5, color='#1E293B',
                    ha='right', va='top', transform=ax.transAxes, bbox=bbox_props)
    else:
        # الترتيب العمودي للخيارات الطويلة
        y_step = 0.12
        for i, opt in enumerate(options[:4]):
            label_char = labels[i] if i < len(labels) else str(i + 1)
            opt_raw = f"[{label_char}]  {opt}"
            opt_prepared = _prepare_arabic_and_math(opt_raw)

            y_pos = options_start_y - (i * y_step)

            ax.text(0.93, y_pos, opt_prepared, fontsize=13.5, color='#1E293B',
                    ha='right', va='top', transform=ax.transAxes, bbox=bbox_props)

    # حفظ الصورة وتفريغ الذاكرة
    os.makedirs(DOWNLOADS_DIR, exist_ok=True)
    plt.savefig(output_path, bbox_inches='tight', facecolor=fig.get_facecolor(), edgecolor='none', pad_inches=0.25)
    plt.close(fig)

    return output_path


async def render_question_image(question_data: dict, current_idx: int, total_count: int) -> str:
    """
    دالة Async غير معطلة للسيرفر، تطلق رسم الصورة في مهمة خلفية (Thread)
    وترجع مسار الصورة الناتجة في مجلد downloads/.
    """
    output_filename = f"math_q_{uuid.uuid4().hex}.png"
    output_path = os.path.join(DOWNLOADS_DIR, output_filename)

    return await asyncio.to_thread(
        _render_sync, question_data, current_idx, total_count, output_path
    )