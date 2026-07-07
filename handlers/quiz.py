"""
Quiz handling module - main quiz flow, caching logic, and token tracking.
Supports structural usage-metadata deduction and 80% discount for cached files.
"""

import os
import asyncio
from typing import Union
from aiogram import Router, types, F
from aiogram.fsm.context import FSMContext
from pypdf import PdfReader

from config import bot, QuizState
from constants import (
    ERROR_INSUFFICIENT_POINTS, ERROR_NO_QUESTIONS_GENERATED, ERROR_API_KEYS_NOT_CONFIGURED,
    ERROR_PDF_READ_FAILED, SUCCESS_FILE_UPLOADED, SUCCESS_PHOTO_UPLOADED, MSG_PROCESSING,
    DISCOUNT_RATE_FOR_CACHED
)
from utils import safe_file_cleanup, ensure_directory_exists, calculate_file_hash
from gemini_helper import generate_quiz_from_file, has_gemini_api_keys
from supabase_helper import check_or_add_user, update_user_stats, get_cached_quiz, save_quiz_to_cache
from keyboards import (
    get_main_menu_keyboard, get_cache_choice_keyboard, 
    get_quiz_question_keyboard, get_quiz_answered_keyboard
)
from validators import validate_file_size, validate_question_count, validate_pdf_pages
from logger import get_logger, log_error, log_info

logger = get_logger(__name__)
router = Router()
DOWNLOADS_DIR = "downloads"

# ==================== File Handlers & Validation ====================

async def _process_uploaded_file(msg: types.Message, state: FSMContext, file_id: str, file_name: str, file_size: int, is_photo: bool, mime_type: str = None):
    file_path = ""
    try:
        is_valid, error = validate_file_size(file_size, "photo" if is_photo else "document")
        if not is_valid:
            await msg.answer(error)
            return

        ensure_directory_exists(DOWNLOADS_DIR)
        file_path = os.path.join(DOWNLOADS_DIR, file_name)
        processing_msg = await msg.answer("⏳ جاري استلام الملف وفحصه...")
        
        file = await bot.get_file(file_id)
        await bot.download_file(file.file_path, file_path)
        
        if not is_photo and file_name.lower().endswith('.pdf'):
            try:
                reader = PdfReader(file_path)
                page_count = len(reader.pages)
                is_valid_pdf, pdf_error = validate_pdf_pages(page_count)
                if not is_valid_pdf:
                    await processing_msg.delete()
                    await msg.answer(pdf_error)
                    safe_file_cleanup(file_path)
                    return
            except Exception:
                await processing_msg.delete()
                await msg.answer(ERROR_PDF_READ_FAILED)
                safe_file_cleanup(file_path)
                return

        # فحص التكرار وجلب التوكينات المخزنة للكويز السابق
        file_hash = calculate_file_hash(file_path)
        cached_data = await asyncio.to_thread(get_cached_quiz, file_hash)
        
        await processing_msg.delete()
        
        if cached_data:
            cached_quiz = cached_data['questions_data']
            original_tokens = cached_data.get('total_tokens') or 15000
            
            await state.update_data(file_path=file_path, file_hash=file_hash, is_photo=is_photo, mime_type=mime_type, cached_quiz=cached_quiz, original_tokens=original_tokens)
            
            # حساب تكلفة الخصم (20% من تكلفة النقاط الأصلية للتوكينات)
            base_points = max(1, round(original_tokens / 1000))
            points_cost = max(1, int(base_points * DISCOUNT_RATE_FOR_CACHED))
            
            await msg.answer(
                "💡 **هذا الملف تمت معالجته مسبقاً!**\n\n"
                f"يوجد اختبار جاهز مكون من {len(cached_quiz)} سؤال لهذا الملف.\n"
                "اختر ما يناسبك:", 
                reply_markup=get_cache_choice_keyboard(points_cost)
            )
        else:
            await state.update_data(file_path=file_path, file_hash=file_hash, is_photo=is_photo, mime_type=mime_type)
            await state.set_state(QuizState.waiting_for_count)
            await msg.answer(SUCCESS_PHOTO_UPLOADED if is_photo else SUCCESS_FILE_UPLOADED)
            
    except Exception as e:
        log_error(logger, f"Error processing file: {e}")
        await msg.answer("❌ حدث خطأ أثناء رفع الملف.")
        if file_path: safe_file_cleanup(file_path)

@router.message(F.document)
async def handle_document(msg: types.Message, state: FSMContext):
    await _process_uploaded_file(msg, state, msg.document.file_id, msg.document.file_name, msg.document.file_size, False, msg.document.mime_type)

@router.message(F.photo)
async def handle_photo(msg: types.Message, state: FSMContext):
    photo = msg.photo[-1]
    await _process_uploaded_file(msg, state, photo.file_id, f"{photo.file_id}.png", photo.file_size, True, "image/png")

# ==================== Cache Choice Handlers ====================

@router.callback_query(F.data == "cache_accept")
async def handle_cache_accept(call: types.CallbackQuery, state: FSMContext):
    try:
        data = await state.get_data()
        cached_quiz = data.get('cached_quiz')
        original_tokens = data.get('original_tokens', 15000)
        file_path = data.get('file_path')
        
        user_info = await asyncio.to_thread(check_or_add_user, call.from_user.id, call.from_user.username or "Unknown")
        
        base_points = max(1, round(original_tokens / 1000))
        points_to_deduct = max(1, int(base_points * DISCOUNT_RATE_FOR_CACHED))
        
        if user_info["points"] < points_to_deduct:
            await call.message.edit_text(ERROR_INSUFFICIENT_POINTS.format(current=user_info['points'], required=points_to_deduct))
            await state.clear()
            safe_file_cleanup(file_path)
            return
            
        await asyncio.to_thread(update_user_stats, call.from_user.id, points_to_deduct, len(cached_quiz))
        
        await state.update_data(questions=cached_quiz, current_index=0, score=0, total_count=len(cached_quiz))
        await state.set_state(QuizState.answering_quiz)
        
        await call.message.delete()
        await send_question(call, state)
        safe_file_cleanup(file_path)
    except Exception as e:
        log_error(logger, f"Error in cache accept: {e}")
    finally:
        await call.answer()

@router.callback_query(F.data == "cache_reject")
async def handle_cache_reject(call: types.CallbackQuery, state: FSMContext):
    await state.set_state(QuizState.waiting_for_count)
    await call.message.edit_text("✅ حسناً، كم سؤالاً تريد توليده بشكل جديد؟ (أرسل رقماً فقط)")
    await call.answer()

# ==================== Question Generation ====================

@router.message(QuizState.waiting_for_count, F.text.isdigit())
async def process_count(msg: types.Message, state: FSMContext):
    file_path = ""
    try:
        count = int(msg.text)
        is_valid, error = validate_question_count(count)
        if not is_valid:
            await msg.answer(f"❌ {error}")
            return
        
        data = await state.get_data()
        file_path, file_hash, mime_type = data.get('file_path'), data.get('file_hash'), data.get('mime_type')
        
        user_info = await asyncio.to_thread(check_or_add_user, msg.from_user.id, msg.from_user.username or "Unknown")
        if user_info["points"] < 2:  # حد أمان
            await msg.answer("❌ رصيد نقاطك منخفض جداً لإجراء عمليات التوليد بالذكاء الاصطناعي.")
            safe_file_cleanup(file_path)
            await state.clear()
            return
        
        processing_msg = await msg.answer(MSG_PROCESSING)
        
        if not has_gemini_api_keys():
            await processing_msg.edit_text(f"❌ {ERROR_API_KEYS_NOT_CONFIGURED}")
            safe_file_cleanup(file_path)
            await state.clear()
            return

        gemini_result = await asyncio.to_thread(generate_quiz_from_file, file_path, count, mime_type)
        
        if not gemini_result:
            await processing_msg.edit_text(f"❌ {ERROR_NO_QUESTIONS_GENERATED}")
            safe_file_cleanup(file_path)
            await state.clear()
            return
            
        quiz_data, total_tokens = gemini_result
        actual_count = len(quiz_data)
        
        # 1000 توكن = 1 نقطة فعلياً
        points_to_deduct = max(1, round(total_tokens / 1000))
        
        if user_info["points"] < points_to_deduct:
            bot_info = await bot.get_me()
            await processing_msg.edit_text(
                f"❌ تكلفة معالجة المستند بلغت ({total_tokens:,} توكن) أي ما يعادل **{points_to_deduct}** نقطة.\n"
                f"💰 رصيدك المتاح هو ({user_info['points']}) نقطة فقط ولا يكفي لإتمام الطلب.",
                reply_markup=get_main_menu_keyboard(bot_info.username, msg.from_user.id)
            )
            safe_file_cleanup(file_path)
            await state.clear()
            return
        
        await asyncio.to_thread(update_user_stats, msg.from_user.id, points_to_deduct, actual_count)
        await asyncio.to_thread(save_quiz_to_cache, file_hash, quiz_data, total_tokens)
        
        await state.update_data(questions=quiz_data, current_index=0, score=0, total_count=actual_count)
        await state.set_state(QuizState.answering_quiz)
        
        await processing_msg.delete()
        safe_file_cleanup(file_path)
        await send_question(msg, state)
    except Exception as e:
        log_error(logger, f"Error in process_count: {e}")
        await msg.answer("❌ حدث خطأ أثناء توليد الكويز.")
        if file_path: safe_file_cleanup(file_path)

# ==================== Quiz Execution Flow ====================

async def send_question(msg_or_call: Union[types.Message, types.CallbackQuery], state: FSMContext) -> None:
    try:
        data = await state.get_data()
        questions, idx = data['questions'], data['current_index']
        
        if idx >= len(questions):
            score, total = data['score'], data['total_count']
            chat_id = msg_or_call.chat.id if isinstance(msg_or_call, types.Message) else msg_or_call.message.chat.id
            bot_info = await bot.get_me()
            percentage = (score / total * 100) if total > 0 else 0
            
            result_text = (
                f"🏁 **اكتمل الاختبار بنجاح!**\n\n"
                f"🎯 نتيجتك النهائية: **{score}** من **{total}** ({percentage:.1f}%)\n\n"
                f"{'🏆 مستوى بطل ممتاز!' if percentage >= 80 else '👍 أداء جيد، استمر في الممارسة!'}"
            )
            await bot.send_message(chat_id, result_text, reply_markup=get_main_menu_keyboard(bot_info.username, chat_id))
            await state.clear()
            return
            
        q = questions[idx]
        text = f"📝 **السؤال {idx + 1} من {len(questions)}:**\n\n{q['question']}"
        
        if isinstance(msg_or_call, types.Message):
            await msg_or_call.answer(text, reply_markup=get_quiz_question_keyboard(q['options']))
        else:
            await msg_or_call.message.answer(text, reply_markup=get_quiz_question_keyboard(q['options']))
    except Exception as e:
        log_error(logger, f"Error sending question: {e}")

@router.callback_query(QuizState.answering_quiz, F.data.startswith("ans_"))
async def handle_answer(call: types.CallbackQuery, state: FSMContext):
    try:
        data = await state.get_data()
        q = data['questions'][data['current_index']]
        selected_opt = int(call.data.split("_")[1])
        correct_opt = q['correct_option_id']
        is_correct = selected_opt == correct_opt
        
        if is_correct:
            await state.update_data(score=data['score'] + 1)
            status_text = "✅ **إجابة صحيحة وممتازة!**"
        else:
            status_text = f"❌ **إجابة خاطئة!**\n💡 الإجابة الصحيحة هي: **{q['options'][correct_opt]}**"
            
        explanation_text = q.get('explanation', '')
        if explanation_text:
            status_text += f"\n\n📚 **الشرح:** {explanation_text}"
        
        updated_text = f"📝 **السؤال {data['current_index'] + 1} من {len(data['questions'])}:**\n\n{q['question']}\n\n{status_text}"
        await call.message.edit_text(updated_text, reply_markup=get_quiz_answered_keyboard(q['options'], correct_opt, selected_opt))
    except Exception as e:
        log_error(logger, f"Error in handle_answer: {e}")
    finally:
        await call.answer()

@router.callback_query(QuizState.answering_quiz, F.data == "get_hint")
async def handle_hint(call: types.CallbackQuery, state: FSMContext):
    try:
        data = await state.get_data()
        await call.answer(f"💡 تلميح: {data['questions'][data['current_index']]['hint']}", show_alert=True)
    except Exception:
        await call.answer("❌ تعذر جلب التلميح")

@router.callback_query(QuizState.answering_quiz, F.data == "next_question")
async def handle_next(call: types.CallbackQuery, state: FSMContext):
    try:
        data = await state.get_data()
        await state.update_data(current_index=data['current_index'] + 1)
        await call.message.delete()
        await send_question(call, state)
    except Exception: pass
    finally: await call.answer()

@router.callback_query(F.data == "ignored")
async def handle_ignored_click(call: types.CallbackQuery):
    await call.answer("✅ تم تسجيل إجابتك بالفعل")