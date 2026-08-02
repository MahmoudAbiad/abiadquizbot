# services/math_renderer.py
"""
خدمة رسم وتحويل الأسئلة الرياضية وصيغ LaTeX إلى صور شمولية عالية الدقة.
تعتمد على Tectonic (مجمع LaTeX الحقيقي) وتحويل النتيجة إلى PNG عبر PyMuPDF (fitz)
لضمان سلامة الاتجاه العربي وصيغ الرياضيات بنسبة 100%.
"""

import os
import re
import uuid
import asyncio
import fitz  # PyMuPDF مثبتة بالفعل في المشروع
from utils import safe_file_cleanup
from logger import get_logger

logger = get_logger(__name__)
DOWNLOADS_DIR = "downloads"

ARABIC_LETTERS = ["أ", "ب", "ج", "د"]


def _normalize_math_delimiters(text: str) -> str:
    """تحويل محددات LaTeX المباشرة إلى $...$ القياسية."""
    if not text:
        return ""
    text = re.sub(r'\\\(', '$', text)
    text = re.sub(r'\\\)', '$', text)
    text = re.sub(r'\\\[', '$$', text)
    text = re.sub(r'\\\]', '$$', text)
    return text


def _escape_latex_text(text: str) -> str:
    """تهريب الرموز الخاصة في LaTeX خارج نطاق $...$ لحماية التجميع من الأخطاء."""
    if not text:
        return ""

    text = _normalize_math_delimiters(text)
    parts = re.split(r'(\$\$.*?\$\$|\$.*?\$)', text, flags=re.DOTALL)
    escaped_parts = []

    for part in parts:
        if (part.startswith('$') and part.endswith('$')) or (part.startswith('$$') and part.endswith('$$')):
            # تغليف أي نص عربي داخل صيغة الرياضيات بـ \text{} لضمان عدم تعطل الخط
            arabic_pattern = r'[\u0600-\u06FF\u0750-\u077F\u08A0-\u08FF\uFB50-\uFDFF\uFE70-\uFEFF]+'
            sanitized_math = re.sub(arabic_pattern, lambda m: f"\\text{{{m.group(0)}}}", part)
            escaped_parts.append(sanitized_math)
        else:
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


def _build_card_tex_code(question_data: dict, current_idx: int, total_count: int) -> str:
    """بناء كود LaTeX مخصص لبطاقة السؤال بأبعاد متناسقة للصور."""
    question_text = _escape_latex_text(question_data.get("question", ""))
    options = question_data.get("options", [])
    
    options_tex = ""
    for i, opt in enumerate(options[:4]):
        letter = ARABIC_LETTERS[i] if i < len(ARABIC_LETTERS) else str(i + 1)
        clean_opt = _escape_latex_text(str(opt))
        options_tex += f"\\item[{[{letter}]}] {clean_opt}\n"

    header_str = f"السؤال {current_idx} من {total_count}"

    return f"""\\documentclass[12pt, a4paper]{{article}}
\\usepackage[top=1cm, bottom=1cm, left=1.2cm, right=1.2cm, paperwidth=16cm, paperheight=12cm]{{geometry}}
\\usepackage{{amsmath, amssymb}}
\\usepackage{{xcolor}}
\\usepackage{{enumitem}}

\\definecolor{{primary}}{{HTML}}{{2563EB}}

\\usepackage{{polyglossia}}
\\setmainlanguage{{arabic}}
\\setotherlanguage{{english}}
\\newfontfamily\\arabicfont[Script=Arabic]{{Amiri}}

\\pagestyle{{empty}}

\\begin{{document}}

\\noindent
{{\\color{{primary}}\\Large\\textbf{{{header_str}}}}}
\\vspace{{0.2cm}}
\\hrule height 1.5pt
\\vspace{{0.5cm}}

\\noindent
{{\\large {question_text}}}

\\vspace{{0.6cm}}

\\begin{{enumerate}}[label=\\textbf{{[\\arabic*]}}, leftmargin=*, itemsep=0.35cm]
{options_tex}
\\end{{enumerate}}

\\end{{document}}
"""


async def render_question_image(question_data: dict, current_idx: int, total_count: int) -> str:
    """
    توليد بطاقة السؤال عبر Tectonic وتحويلها مباشرة إلى صورة PNG عالية الجودة عبر PyMuPDF.
    """
    file_id = uuid.uuid4().hex
    os.makedirs(DOWNLOADS_DIR, exist_ok=True)
    
    tex_path = os.path.join(DOWNLOADS_DIR, f"card_{file_id}.tex")
    pdf_path = os.path.join(DOWNLOADS_DIR, f"card_{file_id}.pdf")
    png_path = os.path.join(DOWNLOADS_DIR, f"math_q_{file_id}.png")

    try:
        # 1. كتابة ملف الـ TeX المؤقت
        tex_code = _build_card_tex_code(question_data, current_idx, total_count)
        with open(tex_path, "w", encoding="utf-8") as f:
            f.write(tex_code)

        # 2. تجميع الملف عبر Tectonic
        process = await asyncio.create_subprocess_exec(
            "tectonic", tex_path,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        await process.communicate()

        # 3. تحويل الصفحة الأولى من الـ PDF الناتج إلى صورة PNG
        if process.returncode == 0 and os.path.exists(pdf_path):
            doc = fitz.open(pdf_path)
            page = doc[0]
            # dpi=200 تعطي صورة عالية الوضوح وسريعة المعالجة
            pix = page.get_pixmap(dpi=200)
            pix.save(png_path)
            doc.close()
            return png_path
        else:
            logger.error("Tectonic card rendering failed.")
            raise RuntimeError("فشل إنشاء صورة البطاقة عبر Tectonic")

    finally:
        safe_file_cleanup(tex_path)
        safe_file_cleanup(pdf_path)