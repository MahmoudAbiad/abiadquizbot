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

    # 2. معالجة وتشكيل النص العربي
    try:
        reshaped = arabic_reshaper.reshape(temp_text)
        bidi_text = get_display(reshaped)
    except Exception:
        bidi_text = temp_text

    # 3. إعادة صيغ LaTeX كما هي لمواضعها دون تشويه
    for placeholder, math_expr in zip(placeholders, math_expressions):
        # البحث عن المعرف المؤقت في النص المعكوس وإعادة كود LaTeX الأصلي
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

    # إعداد شاشة الرسم (Canvas) باللون الأبيض الأكاديمي
    fig, ax = plt.subplots(figsize=(9, 5.5), dpi=200)
    fig.patch.set_facecolor('#FFFFFF')
    ax.set_facecolor('#FFFFFF')
    ax.axis('off')

    # 1. رأس البطاقة: رقم السؤال
    header_raw = f"السؤال {current_idx} من {total_count}"
    header_text = _prepare_arabic_and_math(header_raw)
    ax.text(0.95, 0.92, header_text, fontsize=13, fontweight='bold', color='#2563EB',
            ha='right', va='top', transform=ax.transAxes)

    # خط فاصل ناعم تحت رأس البطاقة
    ax.plot([0.05, 0.95], [0.86, 0.86], color='#E2E8F0', linewidth=1.2, transform=ax.transAxes)

    # 2. متن السؤال الرئيسي
    q_text_prepared = _prepare_arabic_and_math(question_text)
    ax.text(0.95, 0.78, q_text_prepared, fontsize=12, color='#0F172A',
            ha='right', va='top', transform=ax.transAxes, multialignment='right')

    # 3. الخيارات الأربعة
    y_start = 0.52
    y_step = 0.12

    for i, opt in enumerate(options[:4]):
        label_char = labels[i] if i < len(labels) else str(i + 1)
        opt_raw = f"[{label_char}]   {opt}"
        opt_prepared = _prepare_arabic_and_math(opt_raw)

        y_pos = y_start - (i * y_step)

        # مربع إطار رمزي لكل خيار
        bbox_props = dict(boxstyle="round,pad=0.4", fc="#F8FAFC", ec="#CBD5E1", lw=0.8)

        ax.text(0.93, y_pos, opt_prepared, fontsize=11, color='#1E293B',
                ha='right', va='top', transform=ax.transAxes, bbox=bbox_props)

    # حفظ الصورة وتفريغ الذاكرة
    os.makedirs(DOWNLOADS_DIR, exist_ok=True)
    plt.savefig(output_path, bbox_inches='tight', facecolor=fig.get_facecolor(), edgecolor='none', pad_inches=0.3)
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