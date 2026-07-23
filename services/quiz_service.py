# services/quiz_service.py
import asyncio
import os
from typing import Any, Dict, Tuple, Optional, List

from constants import MAX_LIMIT_PAGES, MAX_LIMIT_QUESTIONS, MAX_STANDARD_PAGES, MAX_STANDARD_QUESTIONS
from gemini_helper import generate_quiz_smart
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

def build_transparency_text(items: int, questions: int, mode: str, cost: float) -> str:
    """رسالة الشفافية المالية لعرض تفاصيل الخصم"""
    return (
        "📋 <b>تفاصيل التنفيذ والشفافية المالية</b>\n\n"
        f"• العناصر/الصفحات: <code>{items}</code>\n"
        f"• الأسئلة المطلوبة: <code>{questions}</code>\n"
        f"• وضع المعالجة: <code>{mode}</code>\n"
        f"• تكلفة العملية: <b>{cost:.2f} نقطة</b>"
    )

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
    يستخرج النص، يجلب الكويزات السابقة لمنع التكرار، يستدعي الذكاء الاصطناعي، ويحفظ في الكاش.
    """
    is_media = data.get("input_type") == "media"
    file_hash = data.get("file_hash")
    file_paths = data.get("file_paths", []) or []
    pure_text = data.get("pure_text")

    # 1. معالجة مستندات أوفيس
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

    # 2. جلب الأسئلة السابقة لمنع التكرار
    previous_questions = []
    existing_uuids = set()
    if file_hash:
        old_quizzes = await get_file_quizzes(file_hash)
        for qz in old_quizzes:
            existing_uuids.add(str(qz["id"]))
            if "quiz_data" in qz and isinstance(qz["quiz_data"], list):
                previous_questions.extend(qz["quiz_data"])

    # 3. استدعى محرك AI
    quiz_data = await generate_quiz_smart(
        file_paths=file_paths if is_media else None,
        pure_text=pure_text if not is_media else None,
        count=count,
        skip_cache=True,
        file_hash=file_hash,
        status_message=status_message,
        previous_questions=previous_questions if previous_questions else None,
    )

    if not quiz_data:
        return None, None, "ai_failed"

    # 4. حفظ الكاش لملفات أوفيس والنصوص
    if file_hash and quiz_data and not is_media:
        await save_file_quiz_multiple(
            file_hash=file_hash,
            creator_id=user_id,
            source_title=data.get("source_title", "كويز من مستند"),
            quiz_data=quiz_data,
            total_tokens=0
        )

    # 5. استخراج الـ UUID للكويز الجديد
    new_quiz_id = None
    if file_hash:
        await asyncio.sleep(0.5)
        updated_quizzes = await get_file_quizzes(file_hash)
        for uq in updated_quizzes:
            if str(uq["id"]) not in existing_uuids:
                new_quiz_id = str(uq["id"])
                break

    return quiz_data, new_quiz_id, ""