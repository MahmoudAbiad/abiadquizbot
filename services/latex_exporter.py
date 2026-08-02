# services/latex_exporter.py
import os
import uuid
import asyncio
import jinja2
from utils import safe_file_cleanup
from logger import get_logger

logger = get_logger(__name__)

ARABIC_LETTERS = ["أ", "ب", "ج", "د"]

# إعدادات الستايلات الثلاثة الخاصة بـ LaTeX
LATEX_STYLES_CONFIG = {
    "simple": {
        "accent_color": "14376E",      # كحلي كلاسيكي
        "header_banner": False,
        "show_student_info": False,
    },
    "modern": {
        "accent_color": "2563EB",      # أزرق عصري حيوية
        "header_banner": True,
        "show_student_info": False,
    },
    "academic": {
        "accent_color": "0F172A",      # كحلي داكن جداً / أسود رسمية
        "header_banner": False,
        "show_student_info": True,     # سطر الاسم والتاريخ الرسمي
    }
}

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
            "question": q.get("question", ""),
            "options": q.get("options", []),
            "correct_letter": c_letter,
            "explanation": q.get("explanation", "لا يوجد شرح")
        })
    return prepared


async def build_quiz_pdf_tectonic(title: str, questions: list, style: str = "simple") -> bytes:
    """
    يحقن الأسئلة والستايل المختار في قالب Jinja2 ويولد ملف PDF محترف ونقي عبر Tectonic.
    """
    file_id = uuid.uuid4().hex
    tex_path = os.path.join("downloads", f"quiz_{file_id}.tex")
    pdf_path = os.path.join("downloads", f"quiz_{file_id}.pdf")

    # جلب إعدادات الستايل المختار (افتراضياً simple)
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
        
        # 🟢 تمرير الستايل والإعدادات إلى القالب
        rendered_tex = template.render(
            title=title or "كويز تفاعلي",
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
            logger.error(f"Tectonic compilation failed: {stderr.decode()}")
            raise RuntimeError("فشل تجميع ملف الـ PDF بـ LaTeX")

        with open(pdf_path, "rb") as f:
            pdf_bytes = f.read()

        return pdf_bytes

    finally:
        safe_file_cleanup(tex_path)
        safe_file_cleanup(pdf_path)