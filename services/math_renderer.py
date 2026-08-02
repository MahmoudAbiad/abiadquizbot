# services/math_renderer.py
"""
خدمة رسم وتحويل الأسئلة الرياضية وصيغ LaTeX إلى صور شمولية عالية الدقة.
تستخدم Matplotlib في وضع Headless مع التغليف الذكي للنصوص وضبط اتجاه العربي التلقائي السليم.
"""

import os
import re
import uuid
import asyncio
import matplotlib

# تفعيل وضع Server/Headless لمنع استدعاء واجهات النظام الرسومية
matplotlib.use('Agg')
import matplotlib.pyplot as plt

# ضبط خط المعادلات الرياضية ليكون واضحاً وأكاديمياً
plt.rcParams['mathtext.fontset'] = 'stix'
plt.rcParams['font.family'] = 'sans-serif'

import arabic_reshaper
from bidi.algorithm import get_display
from logger import get_logger

logger = get_logger(__name__)
DOWNLOADS_DIR = "downloads"


def _normalize_latex(text: str) -> str:
    """تحويل أقواس LaTeX المباشرة مثل \(...\) و \[...\] إلى $...$ القياسية ليفهمها Matplotlib."""
    if not text:
        return ""
    text = re.sub(r'\\\(|\\\)', '$', text)
    text = re.sub(r'\\\[|\\\]', '$', text)
    return text


def _smart_wrap_text(text: str, max_chars: int = 42) -> str:
    """تقسيم النص الذكي إلى أسطر متناسقة مع الحفاظ على صيغ LaTeX $...$ من الانكسار."""
    if not text:
        return ""

    text = _normalize_latex(text)
    tokens = re.findall(r'\$.*?\$|\S+', text)
    lines = []
    current_line = []
    current_len = 0

    for token in tokens:
        clean_token = token.replace('$', '') if token.startswith('$') else token
        token_len = len(clean_token)

        if current_len + token_len + 1 > max_chars and current_line:
            lines.append(" ".join(current_line))
            current_line = [token]
            current_len = token_len
        else:
            current_line.append(token)
            current_len += token_len + 1

    if current_line:
        lines.append(" ".join(current_line))

    return "\n".join(lines)


def _fix_ar(text: str) -> str:
    """إصلاح وتشكيل النص العربي مع فرض اتجاه القراءة من اليمين لليسار (base_dir='R')."""
    if not text:
        return ""
    try:
        reshaped = arabic_reshaper.reshape(text)
        return get_display(reshaped, base_dir='R')
    except Exception:
        return text


def _prepare_arabic_and_math(text: str) -> str:
    """عزل صيغ LaTeX المحصورة بين $...$ وتشكيل النص العربي المحيط بها دون إفساد اتجاه أي منهما."""
    if not text:
        return ""

    text = _normalize_latex(text)

    # 1. استخراج صيغ LaTeX واستبدالها بنصوص مؤقتة نظيفة
    math_expressions = re.findall(r'(\$.*?\$)', text)
    if not math_expressions:
        return _fix_ar(text)

    placeholders = [f"MATHPLACEHOLDER{i}" for i in range(len(math_expressions))]

    temp_text = text
    for placeholder, math_expr in zip(placeholders, math_expressions):
        temp_text = temp_text.replace(math_expr, placeholder, 1)

    # 2. تشكيل النص العربي مع الحفاظ على اتجاه RTL الصريح
    lines = temp_text.split('\n')
    fixed_lines = [_fix_ar(line) for line in lines]
    bidi_text = '\n'.join(fixed_lines)

    # 3. إعادة صيغ LaTeX إلى أماكنها الصحيحة
    for placeholder, math_expr in zip(placeholders, math_expressions):
        bidi_placeholder = _fix_ar(placeholder)
        if bidi_placeholder in bidi_text:
            bidi_text = bidi_text.replace(bidi_placeholder, math_expr, 1)
        elif placeholder in bidi_text:
            bidi_text = bidi_text.replace(placeholder, math_expr, 1)

    return bidi_text


def _render_sync(question_data: dict, current_idx: int, total_count: int, output_path: str) -> str:
    """الوظيفة التنفيذية المباشرة لرسم البطاقة الأكاديمية للسؤال والخيارات."""
    question_text = question_data.get("question", "")
    options = question_data.get("options", [])
    labels = ["أ", "ب", "ج", "د"]

    # 1. تغليف نص السؤال وحساب عدد الأسطر
    wrapped_q = _smart_wrap_text(question_text, max_chars=40)
    q_text_prepared = _prepare_arabic_and_math(wrapped_q)
    q_lines_count = q_text_prepared.count('\n') + 1

    # 2. فحص أطوال الخيارات لتحديد التصميم (شبكة 2x2 أم قائمة عمودية)
    max_opt_len = max([len(str(opt)) for opt in options]) if options else 0
    is_grid_layout = (max_opt_len <= 22) and (len(options) == 4)

    fig_height = 5.8 + (q_lines_count * 0.45)
    fig, ax = plt.subplots(figsize=(8.2, fig_height), dpi=220)

    try:
        fig.patch.set_facecolor('#FFFFFF')
        ax.set_facecolor('#FFFFFF')
        ax.axis('off')

        # 3. رأس البطاقة: (السؤال X من Y)
        header_text = f"السؤال {current_idx} من {total_count}"
        header_prepared = _fix_ar(header_text)

        header_y = 0.94
        ax.text(0.95, header_y, header_prepared, fontsize=15, fontweight='bold', color='#2563EB',
                ha='right', va='top', transform=ax.transAxes)

        line_y = header_y - 0.05
        ax.plot([0.05, 0.95], [line_y, line_y], color='#E2E8F0', linewidth=1.5, transform=ax.transAxes)

        # 4. نص السؤال الرئيسي
        q_y = line_y - 0.04
        ax.text(0.95, q_y, q_text_prepared, fontsize=14.5, color='#0F172A',
                ha='right', va='top', transform=ax.transAxes, multialignment='right', linespacing=1.4)

        options_start_y = q_y - (q_lines_count * 0.09) - 0.06
        bbox_props = dict(boxstyle="round,pad=0.5", fc="#F8FAFC", ec="#CBD5E1", lw=1.0)

        # 5. رسم الخيارات
        if is_grid_layout:
            positions = [
                (0.93, options_start_y),                 # [أ]
                (0.46, options_start_y),                 # [ب]
                (0.93, options_start_y - 0.13),          # [ج]
                (0.46, options_start_y - 0.13)           # [د]
            ]
            for i, opt in enumerate(options[:4]):
                label_char = labels[i] if i < len(labels) else str(i + 1)
                label_ar = _fix_ar(f"[{label_char}]")
                opt_raw = f"{label_ar}   {opt}"
                opt_prepared = _prepare_arabic_and_math(opt_raw)
                x_pos, y_pos = positions[i]

                ax.text(x_pos, y_pos, opt_prepared, fontsize=13.5, color='#1E293B',
                        ha='right', va='top', transform=ax.transAxes, bbox=bbox_props)
        else:
            y_step = 0.12
            for i, opt in enumerate(options[:4]):
                label_char = labels[i] if i < len(labels) else str(i + 1)
                label_ar = _fix_ar(f"[{label_char}]")
                opt_raw = f"{label_ar}   {opt}"
                opt_prepared = _prepare_arabic_and_math(opt_raw)
                y_pos = options_start_y - (i * y_step)

                ax.text(0.93, y_pos, opt_prepared, fontsize=13.5, color='#1E293B',
                        ha='right', va='top', transform=ax.transAxes, bbox=bbox_props)

        os.makedirs(DOWNLOADS_DIR, exist_ok=True)
        plt.savefig(output_path, bbox_inches='tight', facecolor=fig.get_facecolor(), edgecolor='none', pad_inches=0.25)
        return output_path

    finally:
        # ضمان تفريغ الذاكرة فوراً لمنع استهلاك الـ RAM على السيرفر
        plt.close(fig)


async def render_question_image(question_data: dict, current_idx: int, total_count: int) -> str:
    """دالة Async غير معطلة، تشغل الرسم في Thread منفصل لعدم تجميد خادم البوت."""
    output_filename = f"math_q_{uuid.uuid4().hex}.png"
    output_path = os.path.join(DOWNLOADS_DIR, output_filename)
    return await asyncio.to_thread(_render_sync, question_data, current_idx, total_count, output_path)