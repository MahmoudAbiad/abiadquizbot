import os
import asyncio
from aiogram import Router, types, F
from aiogram.fsm.context import FSMContext
from pypdf import PdfReader

from config import bot, QuizState
from constants import (
    ERROR_FILE_TOO_LARGE, ERROR_INVALID_PDF_PAGES, ERROR_PDF_READ_FAILED,
    ERROR_IMAGE_TOO_LARGE, ERROR_NO_TEXT_EXTRACTED, ERROR_NO_QUESTIONS_GENERATED,
    ERROR_INSUFFICIENT_POINTS, SUCCESS_FILE_UPLOADED, SUCCESS_PHOTO_UPLOADED,
    MSG_PROCESSING
)
from utils import process_file_smart, safe_file_cleanup, ensure_directory_exists
from gemini_helper import get_questions_from_text, extract_text_from_image
from supabase_helper import check_or_add_user, update_user_stats
from keyboards import get_main_menu_keyboard, get_quiz_start_keyboard
from validators import validate_file_size, validate_pdf_pages, validate_question_count
from logger import get_logger, log_error, log_info, log_warning

# استيراد دالة إرسال السؤال الأول عند جاهزية الكويز
from handlers.execution import send_question

logger = get_get_logger(__name__)
router = Router()

DOWNLOADS_DIR = "downloads"
processing_users_lock = asyncio.Lock()
processing_users: set[int] = set()

@router.message(F.document)
async def handle_document(msg: types.Message, state: FSMContext):
    try:
        is_valid, error = validate_file_size(msg.document.file_size, "document")
        if not is_valid:
            await msg.answer(error)
            return
        
        ensure_directory_exists(DOWNLOADS_DIR)
        file_path = os.path.join(DOWNLOADS_DIR, msg.document.file_name)
        file = await bot.get_file(msg.document.file_id)
        await bot.download_file(file.file_path, file_path)
        
        log_info(logger, f"Document downloaded: {msg.document.file_name}")
        
        try:
            reader = PdfReader(file_path)
            page_count = len(reader.pages)
            is_valid, error = validate_pdf_pages(page_count)
            if not is_valid:
                await msg.answer(error)
                safe_file_cleanup(file_path)
                return
        except Exception as e:
            log_error(logger, f"Error reading PDF: {e}", exception=e)
            await msg.answer(ERROR_PDF_READ_FAILED)
            safe_file_cleanup(file_path)
            return
        
        await state.update_data(file_path=file_path, is_photo=False, source_title=msg.document.file_name)
        await state.set_state(QuizState.waiting_for_count)
        await msg.answer(SUCCESS_FILE_UPLOADED)
        
    except Exception as e:
        log_error(logger, f"Error in handle_document: {e}", exception=e)
        await msg.answer("❌ حدث خطأ أثناء رفع الملف. يرجى المحاولة لاحقاً.")

@router.message(F.photo)
async def handle_photo(msg: types.Message, state: FSMContext):
    try:
        photo = msg.photo[-1]
        is_valid, error = validate_file_size(photo.file_size, "photo")
        if not is_valid:
            await msg.answer(error)
            return
        
        ensure_directory_exists(DOWNLOADS_DIR)
        file_path = os.path.join(DOWNLOADS_DIR, f"{photo.file_id}.png")
        file = await bot.get_file(photo.file_id)
        await bot.download_file(file.file_path, file_path)
        
        log_info(logger, f"Photo downloaded: {photo.file_id}")
        
        await state.update_data(file_path=file_path, is_photo=True, source_title=f"صورة {photo.file_id[:6]}")
        await state.set_state(QuizState.waiting_for_count)
        await msg.answer(SUCCESS_PHOTO_UPLOADED)
        
    except Exception as e:
        log_error(logger, f"Error in handle_photo: {e}", exception=e)
        await msg.answer("❌ حدث خطأ أثناء رفع الصورة. يرجى المحاولة لاحقاً.")

@router.message(QuizState.waiting_for_count, F.text.isdigit())
async def process_count(msg: types.Message, state: FSMContext):
    try:
        count = int(msg.text)
        is_valid, error = validate_question_count(count)
        if not is_valid:
            await msg.answer(f"❌ {error}")
            return
        
        data = await state.get_data()
        file_path = data.get('file_path')
        
        user_info = await asyncio.to_thread(check_or_add_user, msg.from_user.id, msg.from_user.username or "Unknown")
        current_points = user_info["points"]
        
        if current_points < count:
            log_warning(logger, f"User {msg.from_user.id} insufficient points ({current_points} < {count})")
            bot_info = await bot.get_me()
            await msg.answer(
                ERROR_INSUFFICIENT_POINTS.format(current=current_points, required=count),
                reply_markup=get_main_menu_keyboard(bot_info.username, msg.from_user.id)
            )
            safe_file_cleanup(file_path)
            await state.clear()
            return

        async with processing_users_lock:
            if msg.from_user.id in processing_users or (await state.get_data()).get("quiz_processing"):
                await msg.answer("⏳ جاري معالجة الملف بالفعل، انتظر حتى ينتهي ثم جرّب مرة أخرى.")
                return
            processing_users.add(msg.from_user.id)
            
        try:
            await state.update_data(quiz_processing=True)
            processing_msg = await msg.answer(MSG_PROCESSING)
            asyncio.create_task(
                _generate_and_start_quiz_background(msg, file_path, data.get('is_photo', False), count, state, processing_msg)
            )
        except Exception:
            await state.update_data(quiz_processing=False)
            async with processing_users_lock:
                processing_users.discard(msg.from_user.id)
            raise
        
    except Exception as e:
        log_error(logger, f"Error in process_count: {e}", exception=e)
        await msg.answer("❌ حدث خطأ أثناء معالجة طلبك.")

async def _generate_and_start_quiz(msg: types.Message, file_path: str, is_photo: bool, count: int, state: FSMContext, processing_msg: types.Message) -> None:
    try:
        full_text = await _extract_text_from_file(file_path, is_photo)
        if not full_text.strip():
            await processing_msg.edit_text(f"❌ {ERROR_NO_TEXT_EXTRACTED}")
            await state.clear()
            return
        
        quiz_data = await asyncio.to_thread(get_questions_from_text, full_text, count)
        if not quiz_data:
            await processing_msg.edit_text(f"❌ {ERROR_NO_QUESTIONS_GENERATED}")
            await state.clear()
            return
        
        actual_count = len(quiz_data)
        await asyncio.to_thread(update_user_stats, msg.from_user.id, actual_count, actual_count)
        log_info(logger, f"Generated {actual_count} questions for user {msg.from_user.id}")
        
        await state.update_data(
            questions=quiz_data, current_index=0, score=0,
            total_count=actual_count, quiz_completed=False, quiz_origin="generated"
        )
        await state.set_state(QuizState.answering_quiz)
        
        try:
            await processing_msg.delete()
        except Exception:
            pass
        
        corrupted_questions = [f"السؤال رقم {i+1}" for i, q in enumerate(quiz_data) if q.get('was_corrupted_text_fixed')]
        if corrupted_questions:
            warning_text = (
                f"⚠️ **تنبيه قبل بداية الاختبار:**\n"
                f"نظراً لوجود كلمات غير واضحة بالورقة، أصلح الذكاء الاصطناعي السياق تلقائياً لـ "
                f"({', '.join(corrupted_questions)}).\n\n"
                f"اضغط أدناه للبدء 👇"
            )
            await msg.answer(warning_text, reply_markup=get_quiz_start_keyboard())
        else:
            await send_question(msg, state)
        
    except Exception as e:
        log_error(logger, f"Error in quiz generation: {e}", exception=e)
        try:
            await processing_msg.edit_text("❌ حدث خطأ أثناء معالجة الملف.")
        except Exception:
            pass
        await state.clear()
    finally:
        safe_file_cleanup(file_path)

async def _generate_and_start_quiz_background(msg: types.Message, file_path: str, is_photo: bool, count: int, state: FSMContext, processing_msg: types.Message) -> None:
    user_id = msg.from_user.id
    try:
        await _generate_and_start_quiz(msg, file_path, is_photo, count, state, processing_msg)
    finally:
        try:
            await state.update_data(quiz_processing=False)
        except Exception:
            pass
        async with processing_users_lock:
            processing_users.discard(user_id)

async def _extract_text_from_file(file_path: str, is_photo: bool) -> str:
    try:
        full_text = ""
        if is_photo:
            with open(file_path, "rb") as f:
                image_bytes = f.read()
            full_text = await asyncio.to_thread(extract_text_from_image, image_bytes)
        else:
            processed_data = await asyncio.to_thread(process_file_smart, file_path)
            for item in processed_data:
                if item["type"] == "text":
                    full_text += item["content"] + "\n"
                else:
                    img_text = await asyncio.to_thread(extract_text_from_image, item["content"])
                    full_text += img_text + "\n"
                    await asyncio.sleep(2)
        return full_text
    except Exception as e:
        log_error(logger, f"Error extracting text: {e}", exception=e)
        return ""