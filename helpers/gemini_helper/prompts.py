"""
helpers/gemini_helper/prompts.py
تحويل خيارات الصعوبة ونوع الأسئلة إلى تعليمات تُحقن بالموجّه.
"""

from typing import Optional

from constants import (
    DIFFICULTY_MEDIUM,
    DIFFICULTY_PROMPT_INSTRUCTIONS,
    QUESTION_TYPE_INSTRUCTION_GENERAL,
    QUESTION_TYPE_PROMPT_INSTRUCTIONS,
)


def _resolve_difficulty_instruction(difficulty: Optional[str]) -> str:
    """يحوّل قيمة الصعوبة المخزّنة إلى نص التعليمة المحقونة بالبرومبت، مع افتراض
    'متوسط' الآمن لأي قيمة غير معروفة أو غير محددة."""
    return DIFFICULTY_PROMPT_INSTRUCTIONS.get(difficulty or DIFFICULTY_MEDIUM, DIFFICULTY_PROMPT_INSTRUCTIONS[DIFFICULTY_MEDIUM])


def _resolve_question_type_instruction(question_type: Optional[str], custom_question_type_text: Optional[str]) -> str:
    """يحوّل نوع الأسئلة المختار إلى نص التعليمة المحقونة بالبرومبت:
    - نوع مخصص (custom): يُحقن نص الطالب الحر كما هو (بعد تنظيف بسيط).
    - نوع ثابت من القوائم الجاهزة أو من اقتراحات AI: يُستخدم النص الجاهز المطابق إن وجد.
    - غير محدد أو 'general': تعليمة محايدة (تغطية متوازنة لكل الأنواع)."""
    if custom_question_type_text and custom_question_type_text.strip():
        return f"ركّز حصراً على نوع الأسئلة التالي كما حدده الطالب بالضبط: {custom_question_type_text.strip()}"
    if question_type and question_type in QUESTION_TYPE_PROMPT_INSTRUCTIONS:
        return QUESTION_TYPE_PROMPT_INSTRUCTIONS[question_type]
    return QUESTION_TYPE_INSTRUCTION_GENERAL
