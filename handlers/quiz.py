"""
Quiz handling module - main quiz flow and question answering.
Manages file upload, question generation, quiz execution, and scoring.
"""

import os
import asyncio
from typing import Union, Optional
from aiogram import Router, types, F
from aiogram.fsm.context import FSMContext
from pypdf import PdfReader

from config import bot, QuizState
from constants import (
    MAX_DOC_SIZE, MAX_PHOTO_SIZE, MAX_PDF_PAGES,
    ERROR_FILE_TOO_LARGE, ERROR_INVALID_PDF_PAGES, ERROR_PDF_READ_FAILED,
    ERROR_IMAGE_TOO_LARGE, ERROR_NO_TEXT_EXTRACTED, ERROR_NO_QUESTIONS_GENERATED,
    ERROR_INSUFFICIENT_POINTS, ERROR_API_KEYS_NOT_CONFIGURED,
    SUCCESS_FILE_UPLOADED, SUCCESS_PHOTO_UPLOADED,
    SUCCESS_QUIZ_COMPLETED, MSG_PROCESSING
)
from utils import process_file_smart, safe_file_cleanup, ensure_directory_exists
from gemini_helper import get_questions_from_text, extract_text_from_image, has_gemini_api_keys
from supabase_helper import check_or_add_user, update_user_stats
from keyboards import get_main_menu_keyboard, get_quiz_start_keyboard, get_quiz_result_keyboard
from validators import validate_file_size, validate_pdf_pages, validate_question_count
from logger import get_logger, log_error, log_info, log_warning

logger = get_logger(__name__)
router = Router()

DOWNLOADS_DIR = "downloads"

# ==================== File Handlers ====================

@router.message(F.document)
async def handle_document(msg: types.Message, state: FSMContext):
    """
    Handle PDF document uploads.
    
    Args:
        msg: Message object with document
        state: FSM context
    """
    try:
        # Validate file size
        is_valid, error = validate_file_size(msg.document.file_size, "document")
        if not is_valid:
            await msg.answer(error)
            return
        
        # Ensure download directory exists
        ensure_directory_exists(DOWNLOADS_DIR)
        
        # Download file
        file_path = os.path.join(DOWNLOADS_DIR, msg.document.file_name)
        file = await bot.get_file(msg.document.file_id)
        await bot.download_file(file.file_path, file_path)
        
        log_info(logger, f"Document downloaded: {msg.document.file_name}")
        
        # Validate PDF pages
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
        
        # Store file info and request question count
        await state.update_data(file_path=file_path, is_photo=False)
        await state.set_state(QuizState.waiting_for_count)
        await msg.answer(SUCCESS_FILE_UPLOADED)
        
    except Exception as e:
        log_error(logger, f"Error in handle_document: {e}", exception=e)
        await msg.answer("❌ حدث خطأ أثناء رفع الملف. يرجى المحاولة لاحقاً.")

@router.message(F.photo)
async def handle_photo(msg: types.Message, state: FSMContext):
    """
    Handle photo/image uploads.
    
    Args:
        msg: Message object with photo
        state: FSM context
    """
    try:
        photo = msg.photo[-1]  # Get highest quality version
        
        # Validate photo size
        is_valid, error = validate_file_size(photo.file_size, "photo")
        if not is_valid:
            await msg.answer(error)
            return
        
        # Ensure download directory exists
        ensure_directory_exists(DOWNLOADS_DIR)
        
        # Download photo
        file_path = os.path.join(DOWNLOADS_DIR, f"{photo.file_id}.png")
        file = await bot.get_file(photo.file_id)
        await bot.download_file(file.file_path, file_path)
        
        log_info(logger, f"Photo downloaded: {photo.file_id}")
        
        # Store file info and request question count
        await state.update_data(file_path=file_path, is_photo=True)
        await state.set_state(QuizState.waiting_for_count)
        await msg.answer(SUCCESS_PHOTO_UPLOADED)
        
    except Exception as e:
        log_error(logger, f"Error in handle_photo: {e}", exception=e)
        await msg.answer("❌ حدث خطأ أثناء رفع الصورة. يرجى المحاولة لاحقاً.")

# ==================== Question Count Handler ====================

@router.message(QuizState.waiting_for_count, F.text.isdigit())
async def process_count(msg: types.Message, state: FSMContext):
    """
    Process question count input and start quiz generation.
    
    Args:
        msg: Message with question count
        state: FSM context
    """
    try:
        count = int(msg.text)
        
        # Validate question count
        is_valid, error = validate_question_count(count)
        if not is_valid:
            await msg.answer(f"❌ {error}")
            return
        
        data = await state.get_data()
        file_path = data.get('file_path')
        is_photo = data.get('is_photo', False)
        
        # Check user points
        user_info = await asyncio.to_thread(
            check_or_add_user,
            msg.from_user.id,
            msg.from_user.username or "Unknown"
        )
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
        
        # Start quiz generation
        processing_msg = await msg.answer(MSG_PROCESSING)
        await _generate_and_start_quiz(msg, file_path, is_photo, count, state, processing_msg)
        
    except Exception as e:
        log_error(logger, f"Error in process_count: {e}", exception=e)
        await msg.answer("❌ حدث خطأ أثناء معالجة طلبك.")

# ==================== Quiz Generation ====================

async def _generate_and_start_quiz(
    msg: types.Message,
    file_path: str,
    is_photo: bool,
    count: int,
    state: FSMContext,
    processing_msg: types.Message
) -> None:
    """
    Generate quiz questions and start quiz flow.
    
    Args:
        msg: Original message
        file_path: Path to uploaded file
        is_photo: Whether file is a photo
        count: Number of questions to generate
        state: FSM context
        processing_msg: Processing status message
    """
    try:
        if not has_gemini_api_keys():
            await processing_msg.edit_text(f"❌ {ERROR_API_KEYS_NOT_CONFIGURED}")
            await state.clear()
            return

        # Extract text from file
        full_text = await _extract_text_from_file(file_path, is_photo)

        if full_text in (ERROR_API_KEYS_NOT_CONFIGURED, "❌ خطأ: لم يتم ضبط مفاتيح GEMINI_API_KEYS في ملف .env"):
            await processing_msg.edit_text(f"❌ {ERROR_API_KEYS_NOT_CONFIGURED}")
            await state.clear()
            return
        
        if not full_text.strip():
            await processing_msg.edit_text(f"❌ {ERROR_NO_TEXT_EXTRACTED}")
            await state.clear()
            return
        
        # Generate questions
        quiz_data = await asyncio.to_thread(get_questions_from_text, full_text, count)
        
        if not quiz_data:
            await processing_msg.edit_text(f"❌ {ERROR_NO_QUESTIONS_GENERATED}")
            await state.clear()
            return
        
        actual_count = len(quiz_data)
        
        # Update user statistics
        await asyncio.to_thread(update_user_stats, msg.from_user.id, actual_count)
        
        log_info(logger, f"Generated {actual_count} questions for user {msg.from_user.id}")
        
        # Update state with quiz data
        await state.update_data(
            questions=quiz_data,
            current_index=0,
            score=0,
            total_count=actual_count
        )
        await state.set_state(QuizState.answering_quiz)
        
        try:
            await processing_msg.delete()
        except Exception:
            pass
        
        # Check for corrupted text fixes
        corrupted_questions = [
            f"السؤال رقم {i+1}"
            for i, q in enumerate(quiz_data)
            if q.get('was_corrupted_text_fixed')
        ]
        
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

async def _extract_text_from_file(file_path: str, is_photo: bool) -> str:
    """
    Extract text from file (photo or PDF).
    
    Args:
        file_path: Path to file
        is_photo: Whether file is a photo
        
    Returns:
        Extracted text
    """
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
                    await asyncio.sleep(2)  # Rate limiting between API calls
        
        return full_text
        
    except Exception as e:
        log_error(logger, f"Error extracting text: {e}", exception=e)
        return ""

# ==================== Quiz Execution ====================

@router.callback_query(QuizState.answering_quiz, F.data == "start_first_question")
async def start_quiz_after_warning(call: types.CallbackQuery, state: FSMContext):
    """
    Start quiz after corruption warning.
    
    Args:
        call: Callback query
        state: FSM context
    """
    try:
        await call.message.delete()
        await send_question(call, state)
    except Exception as e:
        log_error(logger, f"Error in start_quiz_after_warning: {e}", exception=e)
    finally:
        await call.answer()

async def send_question(msg_or_call: Union[types.Message, types.CallbackQuery], state: FSMContext) -> None:
    """
    Send current question to user.
    
    Args:
        msg_or_call: Message or callback query object
        state: FSM context
    """
    try:
        data = await state.get_data()
        questions = data['questions']
        idx = data['current_index']
        
        # Check if quiz is completed
        if idx >= len(questions):
            score = data['score']
            total = data['total_count']
            
            if isinstance(msg_or_call, types.Message):
                chat_id = msg_or_call.chat.id
            else:
                chat_id = msg_or_call.message.chat.id
            
            bot_info = await bot.get_me()
            
            # Calculate percentage
            percentage = (score / total * 100) if total > 0 else 0
            
            result_text = (
                f"🏁 **اكتمل الاختبار بنجاح!**\n\n"
                f"🎯 نتيجتك النهائية: **{score}** من **{total}**\n"
                f"📊 النسبة المئوية: **{percentage:.1f}%**\n\n"
                f"{'🏆 ممتاز!' if percentage >= 80 else '👍 جيد!' if percentage >= 60 else '📚 استمر في الممارسة!'}"
            )
            
            await bot.send_message(
                chat_id,
                result_text,
                reply_markup=get_main_menu_keyboard(bot_info.username, chat_id)
            )
            
            log_info(logger, f"Quiz completed for user {chat_id}: {score}/{total}")
            await state.clear()
            return
        
        # Send current question
        q = questions[idx]
        text = f"📝 **السؤال {idx + 1} من {len(questions)}:**\n\n{q['question']}"
        
        # Build keyboard with answer options
        kb = []
        for i, opt in enumerate(q['options']):
            kb.append([types.InlineKeyboardButton(text=opt, callback_data=f"ans_{i}")])
        kb.append([types.InlineKeyboardButton(text="💡 طلب تلميح", callback_data="get_hint")])
        
        if isinstance(msg_or_call, types.Message):
            await msg_or_call.answer(text, reply_markup=types.InlineKeyboardMarkup(inline_keyboard=kb))
        else:
            await msg_or_call.message.answer(text, reply_markup=types.InlineKeyboardMarkup(inline_keyboard=kb))
            
    except Exception as e:
        log_error(logger, f"Error in send_question: {e}", exception=e)

@router.callback_query(QuizState.answering_quiz, F.data.startswith("ans_"))
async def handle_answer(call: types.CallbackQuery, state: FSMContext):
    """
    Handle user's answer to question.
    
    Args:
        call: Callback query with answer
        state: FSM context
    """
    try:
        data = await state.get_data()
        questions = data['questions']
        idx = data['current_index']
        q = questions[idx]
        
        selected_opt = int(call.data.split("_")[1])
        correct_opt = q['correct_option_id']
        
        # Check answer
        score = data['score']
        is_correct = selected_opt == correct_opt
        
        if is_correct:
            score += 1
            await state.update_data(score=score)
            status_text = "✅ **إجابة صحيحة وممتازة!**"
            log_info(logger, f"Correct answer: {call.from_user.id}, Q{idx+1}")
        else:
            status_text = f"❌ **إجابة خاطئة!**\n💡 الإجابة الصحيحة هي: **{q['options'][correct_opt]}**"
            log_info(logger, f"Incorrect answer: {call.from_user.id}, Q{idx+1}")
        
        # Build result keyboard
        new_kb = []
        for i, opt in enumerate(q['options']):
            if i == correct_opt:
                prefix = "🟢 "
            elif i == selected_opt and not is_correct:
                prefix = "🔴 "
            else:
                prefix = ""
            new_kb.append([types.InlineKeyboardButton(text=f"{prefix}{opt}", callback_data="ignored")])
        
        new_kb.append([types.InlineKeyboardButton(text="➡️ السؤال التالي", callback_data="next_question")])
        
        updated_text = (
            f"📝 **السؤال {idx + 1} من {len(questions)}:**\n\n"
            f"{q['question']}\n\n"
            f"📊 {status_text}"
        )
        
        await call.message.edit_text(updated_text, reply_markup=types.InlineKeyboardMarkup(inline_keyboard=new_kb))
        
    except Exception as e:
        log_error(logger, f"Error in handle_answer: {e}", exception=e)
    finally:
        await call.answer()

@router.callback_query(QuizState.answering_quiz, F.data == "get_hint")
async def handle_hint(call: types.CallbackQuery, state: FSMContext):
    """
    Provide hint for current question.
    
    Args:
        call: Callback query
        state: FSM context
    """
    try:
        data = await state.get_data()
        q = data['questions'][data['current_index']]
        await call.answer(f"💡 تلميح: {q['hint']}", show_alert=True)
    except Exception as e:
        log_error(logger, f"Error in handle_hint: {e}", exception=e)
        await call.answer("❌ خطأ في جلب التلميح")

@router.callback_query(QuizState.answering_quiz, F.data == "next_question")
async def handle_next(call: types.CallbackQuery, state: FSMContext):
    """
    Move to next question.
    
    Args:
        call: Callback query
        state: FSM context
    """
    try:
        data = await state.get_data()
        await state.update_data(current_index=data['current_index'] + 1)
        
        try:
            await call.message.delete()
        except Exception:
            pass
        
        await send_question(call, state)
        
    except Exception as e:
        log_error(logger, f"Error in handle_next: {e}", exception=e)
    finally:
        await call.answer()

@router.callback_query(F.data == "ignored")
async def handle_ignored_click(call: types.CallbackQuery):
    """
    Handle clicks on disabled buttons.
    
    Args:
        call: Callback query
    """
    await call.answer("✅ تم تسجيل إجابتك")
