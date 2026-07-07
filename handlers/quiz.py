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
    MAX_FAVORITE_TITLE_LENGTH,
    MAX_FAVORITE_SECTIONS,
    DEFAULT_FAVORITE_SECTION_TITLE,
    ERROR_FILE_TOO_LARGE, ERROR_INVALID_PDF_PAGES, ERROR_PDF_READ_FAILED,
    ERROR_IMAGE_TOO_LARGE, ERROR_NO_TEXT_EXTRACTED, ERROR_NO_QUESTIONS_GENERATED,
    ERROR_INSUFFICIENT_POINTS, SUCCESS_FILE_UPLOADED, SUCCESS_PHOTO_UPLOADED,
    SUCCESS_QUIZ_COMPLETED, MSG_PROCESSING,
    MSG_FAVORITE_NAME_PROMPT, MSG_FAVORITE_NAME_INVALID, MSG_FAVORITE_SECTION_PROMPT,
    MSG_FAVORITE_SECTION_CREATE, MSG_FAVORITE_SAVED, MSG_FAVORITES_SEARCH_PROMPT,
    MSG_FAVORITES_SEARCH_EMPTY, MSG_QUIZ_STOPPED,
)
from utils import process_file_smart, safe_file_cleanup, ensure_directory_exists
from gemini_helper import get_questions_from_text, extract_text_from_image
from supabase_helper import (
    check_or_add_user,
    update_user_stats,
    create_shared_quiz_id,
    save_shared_quiz,
    get_shared_quiz,
    save_favorite_quiz,
    list_favorite_quizzes,
    list_favorite_sections,
    create_favorite_section,
    can_create_more_favorite_sections,
    get_favorite_quiz,
    remove_favorite_quiz,
)
from keyboards import (
    get_main_menu_keyboard,
    get_quiz_start_keyboard,
    get_quiz_result_keyboard,
    get_favorites_keyboard,
    get_quiz_question_keyboard,
    get_quiz_answered_keyboard,
    get_favorites_actions_keyboard,
    get_favorite_section_keyboard,
)
from validators import validate_file_size, validate_pdf_pages, validate_question_count
from logger import get_logger, log_error, log_info, log_warning

logger = get_logger(__name__)
router = Router()

DOWNLOADS_DIR = "downloads"
processing_users_lock = asyncio.Lock()
processing_users: set[int] = set()


def _build_source_title(state_data: dict, fallback: str = "كويز") -> str:
    title = state_data.get("source_title") or fallback
    return title[:80]


def _build_favorites_text(favorites: list, sort_mode: str, search_query: str) -> str:
    lines = ["⭐ الكويزات المفضلة"]
    lines.append(f"📌 الفرز: {'حسب القسم' if sort_mode == 'section' else 'الأحدث'}")
    if search_query:
        lines.append(f"🔎 البحث: {search_query}")
    lines.append("")

    if not favorites:
        lines.append(MSG_FAVORITES_SEARCH_EMPTY if search_query else "لا توجد كويزات محفوظة بعد.")
        return "\n".join(lines)

    current_section = None
    for index, favorite in enumerate(favorites, start=1):
        section_title = favorite.get("section_title") or DEFAULT_FAVORITE_SECTION_TITLE
        if sort_mode == "section" and section_title != current_section:
            current_section = section_title
            lines.append(f"📁 {section_title}")
        title = favorite.get("title") or "كويز محفوظ"
        if sort_mode == "section":
            lines.append(f"{index}. {title}")
        else:
            lines.append(f"{index}. {title} — {section_title}")

    return "\n".join(lines)


async def _send_main_menu(call_or_message: Union[types.Message, types.CallbackQuery], user_id: int) -> None:
    bot_info = await bot.get_me()
    menu = get_main_menu_keyboard(bot_info.username, user_id)
    text = "🏠 القائمة الرئيسية"
    if isinstance(call_or_message, types.CallbackQuery):
        await call_or_message.message.answer(text, reply_markup=menu)
    else:
        await call_or_message.answer(text, reply_markup=menu)


async def _save_pending_favorite(
    target: Union[types.Message, types.CallbackQuery],
    state: FSMContext,
    section_id: Optional[str] = None,
) -> bool:
    data = await state.get_data()
    questions = data.get("questions", [])
    favorite_name = data.get("pending_favorite_name")
    source_title = data.get("source_title") or "كويز"

    if not questions or not favorite_name:
        if isinstance(target, types.CallbackQuery):
            await target.answer("❌ لا يمكن حفظ هذا الكويز حالياً", show_alert=True)
        else:
            await target.answer("❌ لا يمكن حفظ هذا الكويز حالياً")
        return False

    favorite_id = await asyncio.to_thread(
        save_favorite_quiz,
        target.from_user.id,
        favorite_name,
        questions,
        section_id,
        source_title,
    )

    if not favorite_id:
        if isinstance(target, types.CallbackQuery):
            await target.answer("❌ تعذر حفظ الكويز في المفضلة", show_alert=True)
        else:
            await target.answer("❌ تعذر حفظ الكويز في المفضلة")
        return False

    await state.update_data(pending_favorite_name=None)
    await state.set_state(QuizState.answering_quiz)
    if isinstance(target, types.CallbackQuery):
        await target.message.answer(MSG_FAVORITE_SAVED)
    else:
        await target.answer(MSG_FAVORITE_SAVED)
    return True


async def _start_loaded_quiz(
    msg_or_call: Union[types.Message, types.CallbackQuery],
    state: FSMContext,
    quiz_data: list,
    source_title: str,
    origin: str = "shared",
) -> None:
    await state.update_data(
        questions=quiz_data,
        current_index=0,
        score=0,
        total_count=len(quiz_data),
        source_title=source_title,
        quiz_origin=origin,
        quiz_completed=False,
    )
    await state.set_state(QuizState.answering_quiz)
    if isinstance(msg_or_call, types.CallbackQuery):
        try:
            await msg_or_call.message.delete()
        except Exception:
            pass
    await send_question(msg_or_call, state)


async def _send_favorites_menu(target: Union[types.Message, types.CallbackQuery], state: FSMContext) -> None:
    data = await state.get_data()
    sort_mode = data.get("favorites_sort_mode", "latest")
    search_query = data.get("favorites_search_query", "")
    favorites = await asyncio.to_thread(list_favorite_quizzes, target.from_user.id, search_query or None, sort_mode)
    text = _build_favorites_text(favorites, sort_mode, search_query)
    keyboard = get_favorites_keyboard(favorites, sort_mode=sort_mode, search_query=search_query)

    if isinstance(target, types.CallbackQuery):
        await target.message.answer(text, reply_markup=keyboard)
    else:
        await target.answer(text, reply_markup=keyboard)

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
        await state.update_data(file_path=file_path, is_photo=False, source_title=msg.document.file_name)
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
        await state.update_data(file_path=file_path, is_photo=True, source_title=f"صورة {photo.file_id[:6]}")
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

        async with processing_users_lock:
            if msg.from_user.id in processing_users or (await state.get_data()).get("quiz_processing"):
                await msg.answer("⏳ جاري معالجة الملف بالفعل، انتظر حتى ينتهي ثم جرّب مرة أخرى.")
                return
            processing_users.add(msg.from_user.id)
        try:
            await state.update_data(quiz_processing=True)

            # Start quiz generation
            processing_msg = await msg.answer(MSG_PROCESSING)
            asyncio.create_task(
                _generate_and_start_quiz_background(
                    msg,
                    file_path,
                    is_photo,
                    count,
                    state,
                    processing_msg,
                )
            )
        except Exception:
            await state.update_data(quiz_processing=False)
            async with processing_users_lock:
                processing_users.discard(msg.from_user.id)
            raise
        
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
        # Extract text from file
        full_text = await _extract_text_from_file(file_path, is_photo)
        
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
        await asyncio.to_thread(update_user_stats, msg.from_user.id, actual_count, actual_count)
        
        log_info(logger, f"Generated {actual_count} questions for user {msg.from_user.id}")
        
        # Update state with quiz data
        await state.update_data(
            questions=quiz_data,
            current_index=0,
            score=0,
            total_count=actual_count,
            quiz_completed=False,
            quiz_origin="generated"
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


async def _generate_and_start_quiz_background(
    msg: types.Message,
    file_path: str,
    is_photo: bool,
    count: int,
    state: FSMContext,
    processing_msg: types.Message,
) -> None:
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
                reply_markup=get_quiz_result_keyboard()
            )
            
            log_info(logger, f"Quiz completed for user {chat_id}: {score}/{total}")
            await state.update_data(quiz_completed=True)
            return
        
        # Send current question
        q = questions[idx]
        text = f"📝 **السؤال {idx + 1} من {len(questions)}:**\n\n{q['question']}"
        keyboard = get_quiz_question_keyboard(q['options'])

        if isinstance(msg_or_call, types.Message):
            await msg_or_call.answer(text, reply_markup=keyboard)
        else:
            await msg_or_call.message.answer(text, reply_markup=keyboard)
            
    except Exception as e:
        log_error(logger, f"Error in send_question: {e}", exception=e)


@router.callback_query(QuizState.answering_quiz, F.data == "quiz_stop")
async def stop_quiz(call: types.CallbackQuery, state: FSMContext):
    try:
        await state.clear()
        try:
            await call.message.delete()
        except Exception:
            pass
        await _send_main_menu(call, call.from_user.id)
        await call.message.answer(MSG_QUIZ_STOPPED)
    except Exception as e:
        log_error(logger, f"Error in stop_quiz: {e}", exception=e)
        await call.answer("❌ تعذر إيقاف الكويز", show_alert=True)
    finally:
        await call.answer()


@router.callback_query(F.data == "quiz_replay")
async def replay_quiz(call: types.CallbackQuery, state: FSMContext):
    try:
        data = await state.get_data()
        questions = data.get("questions", [])
        if not questions:
            await call.answer("❌ لا يوجد كويز محفوظ لإعادة تشغيله", show_alert=True)
            return

        await state.update_data(current_index=0, score=0, quiz_completed=False)
        try:
            await call.message.delete()
        except Exception:
            pass
        await send_question(call, state)
    except Exception as e:
        log_error(logger, f"Error in replay_quiz: {e}", exception=e)
        await call.answer("❌ تعذر إعادة تشغيل الكويز", show_alert=True)
    finally:
        await call.answer()


@router.callback_query(F.data == "quiz_share")
async def share_quiz(call: types.CallbackQuery, state: FSMContext):
    try:
        data = await state.get_data()
        questions = data.get("questions", [])
        if not questions:
            await call.answer("❌ لا يوجد كويز لمشاركته", show_alert=True)
            return

        share_id = data.get("share_id") or create_shared_quiz_id()
        title = _build_source_title(data)
        saved = await asyncio.to_thread(save_shared_quiz, share_id, call.from_user.id, title, questions)
        if not saved:
            await call.answer("❌ تعذر حفظ رابط المشاركة حالياً", show_alert=True)
            return

        await state.update_data(share_id=share_id)
        bot_info = await bot.get_me()
        share_link = f"https://t.me/{bot_info.username}?start=share_{share_id}"
        await call.message.answer(
            f"🔗 تم إنشاء رابط مشاركة الكويز:\n\n{share_link}\n\nأرسله لأي شخص ليفتح الكويز مباشرة.",
            disable_web_page_preview=True,
            reply_markup=types.InlineKeyboardMarkup(
                inline_keyboard=[[types.InlineKeyboardButton(text="فتح رابط المشاركة", url=share_link)]]
            )
        )
    except Exception as e:
        log_error(logger, f"Error in share_quiz: {e}", exception=e)
        await call.answer("❌ حدث خطأ أثناء إنشاء رابط المشاركة", show_alert=True)
    finally:
        await call.answer()


@router.callback_query(F.data == "quiz_favorite")
async def favorite_quiz(call: types.CallbackQuery, state: FSMContext):
    try:
        data = await state.get_data()
        questions = data.get("questions", [])
        if not questions:
            await call.answer("❌ لا يوجد كويز لحفظه", show_alert=True)
            return

        await state.update_data(pending_favorite_name=None)
        await state.set_state(QuizState.saving_favorite_name)
        await call.message.answer(MSG_FAVORITE_NAME_PROMPT)
    except Exception as e:
        log_error(logger, f"Error in favorite_quiz: {e}", exception=e)
        await call.answer("❌ حدث خطأ أثناء حفظ الكويز", show_alert=True)
    finally:
        await call.answer()


@router.message(QuizState.saving_favorite_name, F.text)
async def process_favorite_name(msg: types.Message, state: FSMContext):
    try:
        favorite_name = msg.text.strip()
        if len(favorite_name) < 2 or len(favorite_name) > MAX_FAVORITE_TITLE_LENGTH:
            await msg.answer(MSG_FAVORITE_NAME_INVALID.format(max_len=MAX_FAVORITE_TITLE_LENGTH))
            return

        await state.update_data(pending_favorite_name=favorite_name)
        sections = await asyncio.to_thread(list_favorite_sections, msg.from_user.id)
        allow_new = can_create_more_favorite_sections(msg.from_user.id)
        await msg.answer(
            MSG_FAVORITE_SECTION_PROMPT,
            reply_markup=get_favorite_section_keyboard(sections, allow_new=allow_new, allow_default=True),
        )
    except Exception as e:
        log_error(logger, f"Error in process_favorite_name: {e}", exception=e)
        await msg.answer("❌ تعذر متابعة حفظ الكويز")


@router.message(QuizState.saving_favorite_section_name, F.text)
async def process_favorite_section_name(msg: types.Message, state: FSMContext):
    try:
        section_name = msg.text.strip()
        if not section_name:
            await msg.answer("❌ اسم القسم لا يمكن أن يكون فارغًا")
            return

        if not can_create_more_favorite_sections(msg.from_user.id):
            await msg.answer(f"❌ وصلت للحد الأقصى وهو {MAX_FAVORITE_SECTIONS} قسمًا")
            await state.set_state(QuizState.answering_quiz)
            return

        section_id = await asyncio.to_thread(create_favorite_section, msg.from_user.id, section_name)
        if not section_id:
            await msg.answer("❌ تعذر إنشاء القسم")
            return

        saved = await _save_pending_favorite(msg, state, section_id=section_id)
        if saved:
            await msg.answer(f"📁 تم إنشاء القسم وحفظ الكويز داخله: {section_name}")
    except Exception as e:
        log_error(logger, f"Error in process_favorite_section_name: {e}", exception=e)
        await msg.answer("❌ تعذر إنشاء القسم")


@router.callback_query(F.data.startswith("fav_section_") )
async def favorite_section_existing(call: types.CallbackQuery, state: FSMContext):
    try:
        section_id = call.data.replace("fav_section_", "", 1)
        if section_id == "new":
            if not can_create_more_favorite_sections(call.from_user.id):
                await call.answer(f"❌ وصلت للحد الأقصى وهو {MAX_FAVORITE_SECTIONS} قسمًا", show_alert=True)
                return

            await state.set_state(QuizState.saving_favorite_section_name)
            await call.message.answer(MSG_FAVORITE_SECTION_CREATE)
            return

        if section_id == "default":
            await _save_pending_favorite(call, state, section_id=None)
            return

        await _save_pending_favorite(call, state, section_id=section_id)
    except Exception as e:
        log_error(logger, f"Error in favorite_section_existing: {e}", exception=e)
        await call.answer("❌ تعذر حفظ الكويز", show_alert=True)
    finally:
        await call.answer()


@router.callback_query(F.data == "favorites_menu")
async def show_favorites_menu(call: types.CallbackQuery, state: FSMContext):
    try:
        await _send_favorites_menu(call, state)
    except Exception as e:
        log_error(logger, f"Error in show_favorites_menu: {e}", exception=e)
        await call.answer("❌ تعذر عرض المفضلة", show_alert=True)
    finally:
        await call.answer()


@router.callback_query(F.data == "favorites_search")
async def favorites_search(call: types.CallbackQuery, state: FSMContext):
    try:
        await state.set_state(QuizState.searching_favorites)
        await call.message.answer(MSG_FAVORITES_SEARCH_PROMPT)
    finally:
        await call.answer()


@router.message(QuizState.searching_favorites, F.text)
async def process_favorites_search(msg: types.Message, state: FSMContext):
    try:
        query = msg.text.strip()
        await state.update_data(favorites_search_query=query)
        await state.set_state(None)
        await _send_favorites_menu(msg, state)
    except Exception as e:
        log_error(logger, f"Error in process_favorites_search: {e}", exception=e)
        await msg.answer("❌ تعذر البحث داخل المفضلة")


@router.callback_query(F.data == "favorites_clear_search")
async def favorites_clear_search(call: types.CallbackQuery, state: FSMContext):
    try:
        await state.update_data(favorites_search_query="")
        await _send_favorites_menu(call, state)
    finally:
        await call.answer()


@router.callback_query(F.data == "favorites_sort_latest")
async def favorites_sort_latest(call: types.CallbackQuery, state: FSMContext):
    try:
        await state.update_data(favorites_sort_mode="latest")
        await _send_favorites_menu(call, state)
    finally:
        await call.answer()


@router.callback_query(F.data == "favorites_sort_section")
async def favorites_sort_section(call: types.CallbackQuery, state: FSMContext):
    try:
        await state.update_data(favorites_sort_mode="section")
        await _send_favorites_menu(call, state)
    finally:
        await call.answer()


@router.callback_query(F.data == "favorites_back")
async def favorites_back(call: types.CallbackQuery):
    try:
        await _send_main_menu(call, call.from_user.id)
    finally:
        await call.answer()


@router.callback_query(F.data == "quiz_home")
async def quiz_home(call: types.CallbackQuery):
    try:
        await _send_main_menu(call, call.from_user.id)
    finally:
        await call.answer()


@router.callback_query(F.data.startswith("fav_open_"))
async def open_favorite_quiz(call: types.CallbackQuery, state: FSMContext):
    try:
        favorite_id = call.data.replace("fav_open_", "", 1)
        favorite = await asyncio.to_thread(get_favorite_quiz, call.from_user.id, favorite_id)
        if not favorite:
            await call.answer("❌ لم يتم العثور على هذا الكويز", show_alert=True)
            return

        await _start_loaded_quiz(call, state, favorite["quiz_data"], favorite.get("title") or "كويز محفوظ", origin="favorite")
    except Exception as e:
        log_error(logger, f"Error in open_favorite_quiz: {e}", exception=e)
        await call.answer("❌ تعذر فتح الكويز المحفوظ", show_alert=True)
    finally:
        await call.answer()


@router.callback_query(F.data.startswith("fav_del_"))
async def delete_favorite_quiz(call: types.CallbackQuery, state: FSMContext):
    try:
        favorite_id = call.data.replace("fav_del_", "", 1)
        removed = await asyncio.to_thread(remove_favorite_quiz, call.from_user.id, favorite_id)
        if removed:
            await call.message.answer("🗑 تم حذف الكويز من المفضلة.")
            await _send_favorites_menu(call, state)
        else:
            await call.answer("❌ تعذر حذف الكويز", show_alert=True)
    except Exception as e:
        log_error(logger, f"Error in delete_favorite_quiz: {e}", exception=e)
        await call.answer("❌ تعذر حذف الكويز", show_alert=True)
    finally:
        await call.answer()


@router.callback_query(F.data.startswith("share_load_"))
async def open_shared_quiz(call: types.CallbackQuery, state: FSMContext):
    try:
        share_id = call.data.replace("share_load_", "", 1)
        shared = await asyncio.to_thread(get_shared_quiz, share_id)
        if not shared:
            await call.answer("❌ انتهى رابط المشاركة أو غير موجود", show_alert=True)
            return

        await _start_loaded_quiz(call, state, shared["quiz_data"], shared.get("title") or "كويز مشترك", origin="shared")
    except Exception as e:
        log_error(logger, f"Error in open_shared_quiz: {e}", exception=e)
        await call.answer("❌ تعذر فتح الكويز المشترك", show_alert=True)

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
            status_text = "✅ إجابة صحيحة وممتازة!"
            log_info(logger, f"Correct answer: {call.from_user.id}, Q{idx+1}")
        else:
            status_text = f"❌ إجابة خاطئة!\n💡 الإجابة الصحيحة هي: {q['options'][correct_opt]}"
            log_info(logger, f"Incorrect answer: {call.from_user.id}, Q{idx+1}")

        explanation = q.get("explanation") or "لا يوجد شرح إضافي لهذا السؤال."
        
        # Build result keyboard
        new_kb = get_quiz_answered_keyboard(q['options'], correct_opt, selected_opt)
        
        updated_text = (
            f"📝 السؤال {idx + 1} من {len(questions)}:\n\n"
            f"{q['question']}\n\n"
            f"📊 {status_text}\n\n"
            f"📘 الشرح: {explanation}"
        )
        
        await call.message.edit_text(updated_text, reply_markup=new_kb)
        
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
