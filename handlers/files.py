"""
Files handling module - handles document and photo uploads,
and background quiz generation using the Hybrid API flow.
"""

import os
import asyncio
from aiogram import Router, types, F
from aiogram.fsm.context import FSMContext

from config import bot, QuizState
from constants import (
    SUCCESS_FILE_UPLOADED, SUCCESS_PHOTO_UPLOADED, MSG_PROCESSING,
    ERROR_INSUFFICIENT_POINTS
)
from utils import safe_file_cleanup, ensure_directory_exists
from gemini_helper import generate_quiz_smart  # الدالة الموحدة الجديدة
from supabase_helper import check_or_add_user, update_user_stats
from keyboards import get_main_menu_keyboard
from validators import validate_file_size, validate_question_count
from logger import get_logger, log_error, log_info, log_warning

logger = get_logger(__name__)
router = Router()

DOWNLOADS_DIR = "downloads"
processing_users_lock = asyncio.Lock()
processing_users: set[int] = set()

# ==================== File Handlers ====================

@router.message(F.document | F.photo)
async def handle_media(msg: types.Message, state: FSMContext):
    """
    Handle both document and photo uploads in one place.
    """
    try:
        # تحديد المسار ونوع الملف
        ensure_directory_exists(DOWNLOADS_DIR)
        user_id = msg.from_user.id
        
        if msg.document:
            file_path = os.path.join(DOWNLOADS_DIR, f"{user_id}_{msg.document.file_name}")
            await bot.download(msg.document, destination=file_path)
            is_valid, error = validate_file_size(msg.document.file_size, "document")
        else:
            photo = msg.photo[-1]
            file_path = os.path.join(DOWNLOADS_DIR, f"{user_id}_{photo.file_id}.jpg")
            await bot.download_file((await bot.get_file(photo.file_id)).file_path, file_path)
            is_valid, error = validate_file_size(photo.file_size, "photo")

        if not is_valid:
            await msg.answer(error)
            safe_file_cleanup(file_path)
            return

        # تخزين البيانات والطلب من المستخدم تحديد عدد الأسئلة
        await state.update_data(file_path=file_path, source_title=f"ملف_{user_id}")
        await state.set_state(QuizState.waiting_for_count)
        await msg.answer("✅ تم رفع الملف! كم سؤال تريد استخراجه؟ (مثلاً: 10)")

    except Exception as e:
        log_error(logger, f"Error in handle_media: {e}", exception=e)
        await msg.answer("❌ حدث خطأ أثناء رفع الملف.")

# ==================== Question Count Handler ====================

@router.message(QuizState.waiting_for_count, F.text.isdigit())
async def process_count(msg: types.Message, state: FSMContext):
    count = int(msg.text)
    is_valid, error = validate_question_count(count)
    if not is_valid:
        await msg.answer(f"❌ {error}")
        return

    data = await state.get_data()
    file_path = data.get('file_path')
    
    # التحقق من الرصيد
    user_info = await asyncio.to_thread(check_or_add_user, msg.from_user.id, msg.from_user.username or "Unknown",msg.from_user.first_name or "Unknown", msg.from_user.last_name or "Unknown" )
    if user_info["points"] < count:
        await msg.answer(ERROR_INSUFFICIENT_POINTS.format(current=user_info["points"], required=count))
        safe_file_cleanup(file_path)
        await state.clear()
        return

    # بدء المعالجة
    async with processing_users_lock:
        if msg.from_user.id in processing_users:
            await msg.answer("⏳ جاري المعالجة، انتظر قليلاً...")
            return
        processing_users.add(msg.from_user.id)

    processing_msg = await msg.answer(MSG_PROCESSING)
    asyncio.create_task(_run_quiz_flow(msg, file_path, count, state, processing_msg))

async def _run_quiz_flow(msg, file_path, count, state, processing_msg):
    """
    سير العمل الموحد: توليد الأسئلة ثم البدء.
    """
    try:
        # استدعاء الدالة الذكية (Gemini) مباشرة
        quiz_data = await generate_quiz_smart(file_path=file_path, count=count)
        
        if not quiz_data:
            await processing_msg.edit_text("❌ تعذر توليد الأسئلة. قد يكون الملف غير واضح أو يحتاج لتنسيق أفضل.")
            return

        # تحديث الإحصائيات وبدء الكويز
        await asyncio.to_thread(update_user_stats, msg.from_user.id, len(quiz_data), len(quiz_data))
        
        from handlers.execution import _start_loaded_quiz
        await _start_loaded_quiz(msg, state, quiz_data, "كويز من ملف", origin="file")
        await processing_msg.delete()

    except Exception as e:
        log_error(logger, f"Error in quiz flow: {e}", exception=e)
        await processing_msg.edit_text("⚠️ حدث خطأ تقني.")
    finally:
        safe_file_cleanup(file_path)
        async with processing_users_lock:
            processing_users.discard(msg.from_user.id)
            await state.update_data(quiz_processing=False)

files_router = router