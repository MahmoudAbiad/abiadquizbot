"""
Quiz execution module - handles answering questions, hints, and results.
"""
# تأكد من هذا السطر في أعلى الملف
import asyncio

from supabase_helper import update_user_stats, save_favorite_quiz
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

# 💡 تعريف الراوتر الأساسي للملف
router = Router()

async def _send_main_menu(call_or_message: Union[types.Message, types.CallbackQuery], user_id: int) -> None:
    bot_info = await bot.get_me()
    menu = get_main_menu_keyboard(bot_info.username, user_id)
    text = "🏠 القائمة الرئيسية"
    if isinstance(call_or_message, types.CallbackQuery):
        await call_or_message.message.answer(text, reply_markup=menu)
    else:
        await call_or_message.answer(text, reply_markup=menu)

# 💡 هذه هي الدالة التي تسببت في الخطأ لأنها كانت مفقودة
async def _start_loaded_quiz(msg_or_call: Union[types.Message, types.CallbackQuery], state: FSMContext, quiz_data: list, source_title: str, origin: str = "shared") -> None:
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
    try:
        data = await state.get_data()
        questions = data['questions']
        idx = data['current_index']
        
        # 1. التحقق من انتهاء الاختبار (يبقى كما هو)
        if idx >= len(questions):
            score = data['score']
            total = data['total_count']
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
            await state.update_data(quiz_completed=True)
            return
        
        # 2. جلب بيانات السؤال الحالي
        q = questions[idx]
        chat_id = msg_or_call.chat.id if isinstance(msg_or_call, types.Message) else msg_or_call.message.chat.id

        # 3. إنشاء أزرار التحكم السفلية فقط (بدون خيارات الإجابة لأنها ستكون داخل الـ Poll)
        control_buttons = []
        control_buttons.append(types.InlineKeyboardButton(text="💡 تلميح", callback_data="get_hint"))
        control_buttons.append(types.InlineKeyboardButton(text="💾 حفظ الكويز", callback_data="save_quiz"))
        
        control_kb = types.InlineKeyboardMarkup(inline_keyboard=[
            control_buttons,
            [types.InlineKeyboardButton(text="التالي ➡️", callback_data="next_question")]
        ])

        # 4. إرسال السؤال كـ Poll رسمي يدعم تعدد الأسطر
        await bot.send_poll(
            chat_id=chat_id,
            question=f"📝 السؤال {idx + 1} من {len(questions)}:\n{q['question']}",
            options=q['options'],
            type="quiz",
            correct_option_id=int(q['correct_option_id']),
            explanation=q.get("explanation") or "إجابة صحيحة!",
            reply_markup=control_kb
        )

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

@router.poll_answer()
async def handle_poll_answer(poll_answer: types.PollAnswer, state: FSMContext):
    try:
        data = await state.get_data()
        if not data or 'questions' not in data:
            return

        questions = data['questions']
        idx = data['current_index']
        q = questions[idx]

        # جلب الإجابة التي اختارها المستخدم
        selected_opt = poll_answer.option_ids[0]
        correct_opt = int(q['correct_option_id'])

        # إذا كانت الإجابة صحيحة، نزيد السكور في الـ State
        if selected_opt == correct_opt:
            score = data.get('score', 0) + 1
            await state.update_data(score=score)
            log_info(logger, f"Correct poll answer: {poll_answer.user.id}, Q{idx+1}")
        else:
            log_info(logger, f"Incorrect poll answer: {poll_answer.user.id}, Q{idx+1}")

    except Exception as e:
        log_error(logger, f"Error in handle_poll_answer: {e}", exception=e)

@router.callback_query(QuizState.answering_quiz, F.data == "get_hint")
async def handle_hint(call: types.CallbackQuery, state: FSMContext):
    try:
        data = await state.get_data()
        q = data['questions'][data['current_index']]
        # تغيير show_alert إلى False ليظهر كإشعار صغير غير مزعج
        await call.answer(f"💡 تلميح: {q['hint']}", show_alert=False)
    except Exception as e:
        log_error(logger, f"Error in handle_hint: {e}", exception=e)
        await call.answer("❌ خطأ في جلب التلميح", show_alert=True)

@router.callback_query(QuizState.answering_quiz, F.data == "save_quiz")
async def handle_save_quiz(call: types.CallbackQuery, state: FSMContext):
    try:
        data = await state.get_data()
        # إضافة متغير لمعرفة هل تم حفظه مسبقاً لتجنب التكرار
        if data.get("is_saved_in_session"):
            await call.answer("✅ الكويز محفوظ بالفعل في قسم 'عام'!", show_alert=True)
            return

        questions = data.get("questions")
        title = data.get("source_title", "كويز بدون عنوان")
        
        if not questions:
            await call.answer("❌ لا يوجد كويز لحفظه!", show_alert=True)
            return

        await asyncio.to_thread(save_favorite_quiz, call.from_user.id, title, questions)
        await state.update_data(is_saved_in_session=True) # منع التكرار
        
        await call.answer("✅ تم الحفظ السريع! تجده في 'قائمتي المفضلة' قسم 'عام'.", show_alert=True)
        
    except Exception as e:
        log_error(logger, f"Error saving quiz: {e}", exception=e)
        await call.answer("❌ حدث خطأ أثناء حفظ الكويز.", show_alert=True)

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