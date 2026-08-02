# services/latex_exporter.py
import os
import re
import uuid
import asyncio
import jinja2
from utils import safe_file_cleanup
from logger import get_logger

logger = get_logger(__name__)

ARABIC_LETTERS = ["أ", "ب", "ج", "د"]

LATEX_STYLES_CONFIG = {
    "simple": {
        "accent_color": "14376E",
        "header_banner": False,
        "show_student_info": False,
    },
    "modern": {
        "accent_color": "2563EB",
        "header_banner": True,
        "show_student_info": False,
    },
    "academic": {
        "accent_color": "0F172A",
        "header_banner": False,
        "show_student_info": True,
    }
}


def _normalize_math_delimiters(text: str) -> str:
    """تحويل أقواس LaTeX المباشرة مثل \(...\) و \[...\] إلى $...$ القياسية."""
    if not text:
        return ""
    text = re.sub(r'\\\(', '$', text)
    text = re.sub(r'\\\)', '$', text)
    text = re.sub(r'\\\[', '$', text)
    text = re.sub(r'\\\]', '$', text)
    return text


def _sanitize_arabic_in_math(math_str: str) -> str:
    """
    تغليف أي نصوص عربية داخل صيغ $...$ بـ \text{...} لمنع انهيار التجميع في XeLaTeX/Tectonic.
    """
    arabic_pattern = r'[\u0600-\u06FF\u0750-\u077F\u08A0-\u08FF\uFB50-\uFDFF\uFE70-\uFEFF]+'

    # تقسيم المحتوى بحسب \text{...} الموجودة مسبقاً لتجنب التغليف المضاعف
    parts = re.split(r'(\\text\{.*?\})', math_str)
    sanitized_parts = []

    for part in parts:
        if part.startswith(r'\text{'):
            sanitized_parts.append(part)
        else:
            # تغليف الكلمات العربية المستقلة داخل الرياضيات بـ \text{}
            fixed_part = re.sub(arabic_pattern, lambda m: f"\\text{{{m.group(0)}}}", part)
            sanitized_parts.append(fixed_part)

    return "".join(sanitized_parts)


def _escape_latex_text(text: str) -> str:
    """
    تهريب الرموز الخاصة في LaTeX خارج نطاق معادلات $...$ لحماية Tectonic من الأخطاء،
    مع الحفاظ التام على معادلات LaTeX المنسقة لتظهر كرموز وقوانين رياضية حقيقية.
    """
    if not text:
        return ""

    # 1. توحيد صيغ المحددات
    text = _normalize_math_delimiters(text)

    # 2. حماية من عدم تكافؤ علامات الـ $ (الأعداد الفردية)
    single_dollars = len(re.findall(r'(?<!\$)\$(?!\$)', text))
    if single_dollars % 2 != 0:
        text = text.replace('$', r'\$')

    # 3. تقسيم النص لعزل صيغ $...$ أو $$...$$
    parts = re.split(r'(\$\$.*?\$\$|\$.*?\$)', text, flags=re.DOTALL)
    escaped_parts = []

    for part in parts:
        if (part.startswith('$') and part.endswith('$')) or (part.startswith('$$') and part.endswith('$$')):
            # معالجة النصوص العربية داخل الرياضيات لمنع استثناءات الخطوط
            sanitized_math = _sanitize_arabic_in_math(part)
            escaped_parts.append(sanitized_math)
        else:
            # تهريب الرموز الخاصة بالنص العادي فقط
            escaped = (
                part
                .replace('\\', r'\textbackslash{}')
                .replace('&', r'\&')
                .replace('%', r'\%')
                .replace('#', r'\#')
                .replace('_', r'\_')
                .replace('{', r'\{')
                .replace('}', r'\}')
                .replace('~', r'\textasciitilde{}')
                .replace('^', r'\textasciicircum{}')
            )
            escaped_parts.append(escaped)

    return "".join(escaped_parts)


def _prepare_questions_for_latex(questions: list) -> list:
    prepared = []
    for q in questions:
        c_id = q.get("correct_option_id", 0)
        try:
            c_id = int(c_id)
        except (ValueError, TypeError):
            c_id = 0

        c_letter = ARABIC_LETTERS[c_id] if 0 <= c_id < len(ARABIC_LETTERS) else "أ"

        prepared.append({
            "question": _escape_latex_text(q.get("question", "")),
            "options": [_escape_latex_text(opt) for opt in q.get("options", [])],
            "correct_letter": c_letter,
            "explanation": _escape_latex_text(q.get("explanation", "لا يوجد شرح"))
        })
    return prepared


async def build_quiz_pdf_tectonic(title: str, questions: list, style: str = "simple") -> bytes:
    file_id = uuid.uuid4().hex
    os.makedirs("downloads", exist_ok=True)
    tex_path = os.path.join("downloads", f"quiz_{file_id}.tex")
    pdf_path = os.path.join("downloads", f"quiz_{file_id}.pdf")

    style_cfg = LATEX_STYLES_CONFIG.get(style, LATEX_STYLES_CONFIG["simple"])

    try:
        env = jinja2.Environment(
            block_start_string='BLOCK',
            block_end_string='ENDBLOCK',
            variable_start_string='<<',
            variable_end_string='>>',
            loader=jinja2.FileSystemLoader('templates')
        )

        template = env.get_template('quiz_template.tex')
        prepared_q = _prepare_questions_for_latex(questions)
        escaped_title = _escape_latex_text(title or "كويز تفاعلي")

        rendered_tex = template.render(
            title=escaped_title,
            questions=prepared_q,
            style=style,
            cfg=style_cfg
        )

        with open(tex_path, "w", encoding="utf-8") as f:
            f.write(rendered_tex)

        process = await asyncio.create_subprocess_exec(
            "tectonic", tex_path,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        stdout, stderr = await process.communicate()

        if process.returncode != 0:
            err_msg = stderr.decode('utf-8', errors='ignore')
            logger.error(f"Tectonic compilation failed: {err_msg}")
            raise RuntimeError(f"فشل تجميع ملف الـ PDF بـ LaTeX: {err_msg[:200]}")

        with open(pdf_path, "rb") as f:
            pdf_bytes = f.read()

        return pdf_bytes

    finally:
        safe_file_cleanup(tex_path)
        safe_file_cleanup(pdf_path)