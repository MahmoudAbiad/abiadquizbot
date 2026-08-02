# services/quiz_service.py
import asyncio
import os
import random
from typing import Any, Dict, Tuple, Optional, List

from constants import MAX_LIMIT_PAGES, MAX_LIMIT_QUESTIONS, MAX_STANDARD_PAGES, MAX_STANDARD_QUESTIONS
from gemini_helper import generate_quiz_smart, detect_content_type
from logger import get_logger, log_error
from services.file_service import extract_office_text_if_needed
from supabase_helper import (
    get_file_quizzes,
    refund_user_points,
    save_file_quiz_multiple,
    log_usage_event,
)

logger = get_logger(__name__)

def determine_execution_mode(items: int, questions: int, cached: bool = False) -> str:
    """تحديد وضع التنفيذ (عادي، متقدم، أو كاش)"""
    if cached: return "Cached"
    if items > MAX_LIMIT_PAGES or questions > MAX_LIMIT_QUESTIONS: return "Super-Processing"
    if items > MAX_STANDARD_PAGES or questions > MAX_STANDARD_QUESTIONS: return "Over-Limit"
    return "Standard"

MODE_LABELS_AR = {
    "Standard": "⚡ عادي",
    "Over-Limit": "📈 موسّع",
    "Super-Processing": "🚀 معالجة فائقة (ملف كبير)",
    "Cached": "🗃️ من الكاش",
}

def build_transparency_text(items: int, questions: int, mode: str, cost: float) -> str:
    """رسالة الشفافية المالية لعرض تفاصيل الخصم"""
    mode_label = MODE_LABELS_AR.get(mode, mode)
    return (
        "📋 <b>تفاصيل التنفيذ والشفافية المالية</b>\n\n"
        f"• العناصر/الصفحات: <code>{items}</code>\n"
        f"• الأسئلة المطلوبة: <code>{questions}</code>\n"
        f"• وضع المعالجة: <code>{mode_label}</code>\n"
        f"• تكلفة العملية: <b>{cost:.2f} نقطة</b>"
    )

def _shuffle_question_options(question: Dict[str, Any]) -> Dict[str, Any]:
    """
    يخلط ترتيب الخيارات عشوائياً ويحدّث correct_option_id ليطابق الموضع الجديد.
    هذا ضروري لأن Gemini غالباً ما ينحاز لوضع الإجابة الصحيحة في نفس الترتيب
    (عادة الخيار الأول) بشكل متكرر، فبدون خلط تصبح كل الاختبارات المولّدة
    إجابتها الصحيحة رقم 1 دائماً.
    """
    options = question.get("options") or []
    try:
        correct_id = int(question.get("correct_option_id", 0))
    except (TypeError, ValueError):
        return question

    if not options or not (0 <= correct_id < len(options)):
        return question

    indices = list(range(len(options)))
    random.shuffle(indices)

    question["options"] = [options[i] for i in indices]
    question["correct_option_id"] = indices.index(correct_id)
    return question


def shuffle_quiz_options(quiz_data: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """يطبّق خلط الخيارات على كل أسئلة الكويز المولّد."""
    return [_shuffle_question_options(q) for q in quiz_data]


async def refund_user_on_failure(user_id: int, data: Dict[str, Any]) -> None:
    """إعادة النقاط تلقائياً في حال فشل التوليد"""
    cost = float(data.get("debited_cost") or 0)
    if cost > 0:
        await refund_user_points(user_id, cost)

async def execute_quiz_generation_workflow(
    user_id: int,
    data: Dict[str, Any],
    count: int,
    status_message: Any
) -> Tuple[Optional[List[Dict[str, Any]]], Optional[str], str]:
    """
    التدفق التنفيذي المركزي لتوليد الكويز:
    يستخرج النص، يفحص نوع المحتوى (رياضيات/نص)، يجلب الكويزات السابقة لمنع التكرار،
    يستدعي الذكاء الاصطناعي، ويحفظ الكويز في الكاش.
    """
    is_media = data.get("input_type") == "media"
    file_hash = data.get("file_hash")
    file_paths = data.get("file_paths", []) or []
    pure_text = data.get("pure_text")

    # 1. معالجة مستندات أوفيس (استخراج النص إن وجد لتقليل استهلاك الـ Vision API)
    if is_media and file_paths:
        first_path = file_paths[0]
        extracted_text, is_valid = await extract_office_text_if_needed(first_path)
        ext = os.path.splitext(first_path)[1].lower()
        if ext in [".docx", ".doc", ".pptx", ".ppt", ".txt"]:
            if is_valid and extracted_text:
                pure_text = extracted_text
                is_media = False
            else:
                return None, None, "unreadable_office"

    # 2. التصنيف الذكي للمحتوى (فحص ما إذا كان المحتوى يتضمن رياضيات أو قوانين)
    content_type = await detect_content_type(
        file_paths=file_paths if is_media else None,
        pure_text=pure_text if not is_media else None
    )
    data["content_type"] = content_type

    # 3. جلب الأسئلة السابقة لمنع التكرار
    previous_questions = []
    existing_uuids = set()
    if file_hash:
        old_quizzes = await get_file_quizzes(file_hash)
        for qz in old_quizzes:
            existing_uuids.add(str(qz["id"]))
            if "quiz_data" in qz and isinstance(qz["quiz_data"], list):
                previous_questions.extend(qz["quiz_data"])

    # 4. استدعاء محرك الذكاء الاصطناعي لتوليد الأسئلة
    quiz_data = await generate_quiz_smart(
        file_paths=file_paths if is_media else None,
        pure_text=pure_text if not is_media else None,
        count=count,
        skip_cache=True,
        file_hash=file_hash,
        status_message=status_message,
        previous_questions=previous_questions if previous_questions else None,
        is_math=(content_type == "MATH"),
    )

    if not quiz_data:
        return None, None, "ai_failed"

    # 5. خلط ترتيب الخيارات لتفادي انحياز الذكاء الاصطناعي لوضع
    #    الإجابة الصحيحة دائماً في نفس الموضع (غالباً الخيار الأول)
    quiz_data = shuffle_quiz_options(quiz_data)

    # 6. حفظ الكاش لملفات أوفيس والنصوص
    if file_hash and quiz_data:
        await save_file_quiz_multiple(
            file_hash=file_hash,
            creator_id=user_id,
            source_title=data.get("source_title", "كويز من مستند"),
            quiz_data=quiz_data,
            total_tokens=0
        )

    # 7. استخراج الـ UUID للكويز الجديد
    new_quiz_id = None
    if file_hash:
        await asyncio.sleep(0.5)
        updated_quizzes = await get_file_quizzes(file_hash)
        for uq in updated_quizzes:
            if str(uq["id"]) not in existing_uuids:
                new_quiz_id = str(uq["id"])
                break

    return quiz_data, new_quiz_id, ""