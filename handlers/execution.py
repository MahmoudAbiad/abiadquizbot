"""
Quiz Execution Module - Handles quiz questions delivery, answering, 
hints, and completion logic.
"""

from aiogram import Router, types, F
from aiogram.fsm.context import FSMContext
from typing import Union
from config import bot, QuizState
from constants import MSG_QUIZ_STOPPED
from keyboards import (
    get_main_menu_keyboard, get_quiz_result_keyboard, 
    get_quiz_question_keyboard, get_quiz_answered_keyboard
)
from logger import get_logger, log_error, log_info

logger = get_logger(__name__)
router = Router()

async def _send_main_menu(call_or_message: Union[types.Message, types.CallbackQuery], user_id: int) -> None:
    """Helper to send the main menu keyboard."""
    bot_info = await bot.get_me()
    menu = get_main_menu_keyboard(bot_info.username, user_id)
    text = "🏠 القائمة الرئيسية"
    if isinstance(call_or_message, types.CallbackQuery):
        await call_or_message.message.answer(text, reply_markup=menu)
    else:
        await call_or_message.answer(text, reply_markup=menu)

async def start_loaded_quiz(msg_or_call: Union[types.Message, types.CallbackQuery], state: FSMContext, quiz_data: list, source_title: str, origin: str = "shared") -> None:
    """Starts a quiz loaded from favorites or shared links (Made public for external imports)."""
    await state.update_data(
        questions=quiz_data, current_index=0, score=0,
        total_count=len(quiz_data), source_title=source_title,
        quiz_origin=origin, quiz_completed=False
    )
    await state.set_state(QuizState.answering_quiz)
    if isinstance(msg_or_call, types.CallbackQuery):
        try:
            await msg_or_call.message.delete()
        except Exception:
            pass
    await send_question(msg_or_call, state)

async def send_question(msg_or_call: Union[types.Message, types.CallbackQuery], state: FSMContext) -> None:
    """Sends the current question or handles the quiz completion screen."""
    try:
        data = await state.get_data()
        questions = data.get('questions', [])
        idx = data.get('current_index', 0)
        
        # 🏁 Check if quiz is completed
        if idx >= len(questions):
            score = data.get('score', 0)
            total = data.get('total_count', 0)
            chat_id = msg_or_call.chat.id if isinstance(msg_or_call, types.Message) else msg_or_call.message.chat.id
            
            percentage = (score / total * 100) if total > 0 else 0
            result_text = (
                f"🏁 **اكتمل الاختبار بنجاح!**\n\n"
                f"🎯 نتيجتك النهائية: **{score}** من **{total}**\n"
                f"📊 النسبة المئوية: **{percentage:.1f}%**\n\n"
                f"{'🏆 ممتاز!' if percentage >= 80 else '👍 جيد!' if percentage >= 60 else '📚 استمر في الممارسة!'}"
            )
            await bot.send_message(chat_id, result_text, reply_markup=get_quiz_result_keyboard())
            log_info(logger, f"Quiz completed for user {chat_id}: {score}/{total}")
            
            # التحسين: نقوم بتصفير الحالة الفردية حتى لا يعلق حساب المستخدم، مع إبقاء البيانات للـ Replay
            await state.update_data(quiz_completed=True)
            await state.set_state(None)
            return
        
        # 📝 Send Current Question
        q = questions[idx]
        text = f"📝 **السؤال {idx + 1} من {len(questions)}:**\n\n{q['question']}"
        keyboard = get_quiz_question_keyboard(q['options'])

        if isinstance(msg_or_call, types.Message):
            await msg_or_call.answer(text, reply_markup=keyboard)
        else:
            await msg_or_call.message.answer(text, reply_markup=keyboard)
    except Exception as e:
        log_error(logger, f"Error in send_question: {e}", exception=e)

@router.callback_query(QuizState.answering_quiz, F.data == "start_first_question")
async def start_quiz_after_warning(call: types.CallbackQuery, state: FSMContext):
    try:
        await call.message.delete()
        await send_question(call, state)
    except Exception as e:
        log_error(logger, f"Error in start_quiz_after_warning: {e}", exception=e)
    finally:
        await call.answer()

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
            
        # التحسين: إعادة تعيين الحالة لتستقبل أزرار الإجابات بشكل صحيح
        await state.set_state(QuizState.answering_quiz)
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

@router.callback_query(F.data == "quiz_home")
async def quiz_home(call: types.CallbackQuery):
    try:
        await _send_main_menu(call, call.from_user.id)
    finally:
        await call.answer()

@router.callback_query(QuizState.answering_quiz, F.data.startswith("ans_"))
async def handle_answer(call: types.CallbackQuery, state: FSMContext):
    try:
        data = await state.get_data()
        questions = data['questions']
        idx = data['current_index']
        q = questions[idx]
        
        selected_opt = int(call.data.split("_")[1])
        correct_opt = q['correct_option_id']
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
    try:
        data = await state.get_data()
        q = data['questions'][data['current_index']]
        await call.answer(f"💡 تلميح: {q['hint']}", show_alert=True)
    except Exception as e:
        log_error(logger, f"Error in handle_hint: {e}", exception=e)
        await call.answer("❌ خطأ في جلب التلميح")

@router.callback_query(QuizState.answering_quiz, F.data == "next_question")
async def handle_next(call: types.CallbackQuery, state: FSMContext):
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
    await call.answer("✅ تم تسجيل إجابتك")