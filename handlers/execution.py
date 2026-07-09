"""
Quiz execution module - handles answering questions, hints, and results.
"""
import asyncio

from supabase_helper import list_favorite_quizzes, update_user_stats, save_favorite_quiz
from aiogram import Router, types, F
from aiogram.fsm.context import FSMContext
from typing import Union

from config import bot, QuizState
from constants import MSG_QUIZ_STOPPED
from keyboards import (
    get_main_menu_keyboard, get_quiz_result_keyboard, 
)
from logger import get_logger, log_error, log_info

logger = get_logger(__name__)

from aiogram.fsm.storage.base import StorageKey
# قاموس عام للربط بين معرف السؤال (poll_id) وبيانات الكويز والمحادثة
poll_to_quiz_map = {}

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

async def _start_loaded_quiz(msg_or_call: Union[types.Message, types.CallbackQuery], state: FSMContext, quiz_data: list, source_title: str, origin: str = "shared", quiz_id: str = "") -> None:
    await state.update_data(
        questions=quiz_data, current_index=0, score=0,
        total_count=len(quiz_data), source_title=source_title,
        quiz_origin=origin, quiz_completed=False, quiz_id=quiz_id
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
        
        # 1. التحقق من انتهاء الاختبار
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
            
            await bot.send_message(chat_id, result_text, reply_markup=get_quiz_result_keyboard(), parse_mode="Markdown")
            log_info(logger, f"Quiz completed for user {chat_id}: {score}/{total}")
            await state.update_data(quiz_completed=True)
            
            # تحرير الحالة للسماح بملفات جديدة مع الحفاظ على البيانات للأزرار الأخرى
            await state.set_state(None)
            return
        
        # 2. جلب بيانات السؤال الحالي
        q = questions[idx]
        chat_id = msg_or_call.chat.id if isinstance(msg_or_call, types.Message) else msg_or_call.message.chat.id

        # 3. إنشاء أزرار التحكم السفلية
        control_kb = types.InlineKeyboardMarkup(inline_keyboard=[
            [types.InlineKeyboardButton(text="💡 طلب تلميح", callback_data="get_hint")],
            [
                types.InlineKeyboardButton(text="⏹ إيقاف", callback_data="quiz_stop"),
                types.InlineKeyboardButton(text="🔗 مشاركة", callback_data="quiz_share"),
                types.InlineKeyboardButton(text="💾 حفظ", callback_data="save_quiz")
            ],
            [types.InlineKeyboardButton(text="التالي ➡️", callback_data="next_question")]
        ])

        # 4. إرسال السؤال كـ Poll رسمي
        poll_msg = await bot.send_poll(
            chat_id=chat_id,
            question=f"📝 السؤال {idx + 1} من {len(questions)}:\n{q['question']}",
            options=q['options'],
            type="quiz",
            correct_option_id=int(q['correct_option_id']),
            explanation=q.get("explanation") or "إجابة صحيحة!",
            reply_markup=control_kb
        )

        poll_to_quiz_map[poll_msg.poll.id] = {
            "chat_id": chat_id,
            "user_id": msg_or_call.from_user.id,
            "correct_option_id": int(q['correct_option_id']),
            "index": idx
        }

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

@router.callback_query(F.data == "quiz_stop")
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
async def quiz_home(call: types.CallbackQuery, state: FSMContext):
    try:
        await state.clear() 
        await _send_main_menu(call, call.from_user.id)
    finally:
        await call.answer()

@router.poll_answer()
async def handle_poll_answer(poll_answer: types.PollAnswer, state: FSMContext):
    try:
        poll_id = poll_answer.poll_id
        if poll_id not in poll_to_quiz_map:
            return

        quiz_info = poll_to_quiz_map[poll_id]
        chat_id = quiz_info["chat_id"]
        user_id = quiz_info["user_id"]
        correct_opt = quiz_info["correct_option_id"]

        # ✅ إصلاح حرج: استخراج المعرف الرياضي للبوت من التوكن مباشرة لتجنب قيم الـ None المطروحة في السيرفر السحابي
        bot_id = int(bot.token.split(":")[0])
        storage_key = StorageKey(bot_id=bot_id, chat_id=chat_id, user_id=user_id)
        correct_state = FSMContext(storage=state.storage, key=storage_key)

        data = await correct_state.get_data()
        if not data or 'questions' not in data:
            return

        selected_opt = poll_answer.option_ids[0]

        if selected_opt == correct_opt:
            score = data.get('score', 0) + 1
            await correct_state.update_data(score=score)
            log_info(logger, f"Correct poll answer: {user_id}, Q{quiz_info['index']+1}")
        else:
            log_info(logger, f"Incorrect poll answer: {user_id}, Q{quiz_info['index']+1}")

    except Exception as e:
        log_error(logger, f"Error in handle_poll_answer: {e}", exception=e)
        
@router.callback_query(QuizState.answering_quiz, F.data == "get_hint")
async def handle_hint(call: types.CallbackQuery, state: FSMContext):
    try:
        data = await state.get_data()
        q = data['questions'][data['current_index']]
        await call.answer(f"💡 تلميح: {q['hint']}", show_alert=False)
    except Exception as e:
        log_error(logger, f"Error in handle_hint: {e}", exception=e)
        await call.answer("❌ خطأ في جلب التلميح", show_alert=True)

@router.callback_query(F.data.in_({"save_quiz", "quiz_favorite"}))
async def handle_save_quiz(call: types.CallbackQuery, state: FSMContext):
    try:
        data = await state.get_data()
        if data.get("is_saved_in_session"):
            await call.answer("✅ هذا الكويز محفوظ بالفعل في قائمتك المفضلة!", show_alert=True)
            return

        questions = data.get("questions")
        title = data.get("source_title") or data.get("title") or "كويز بدون عنوان"
        quiz_id = data.get("quiz_id")  # 🆕 1. جلب معرف الكويز الفريد المخزن في الـ State
        
        if not questions:
            await call.answer("❌ لا يوجد كويز لحفظه!", show_alert=True)
            return

        user_favorites = await asyncio.to_thread(list_favorite_quizzes, call.from_user.id)
        if user_favorites:
            # 🆕 2. تحديث شرط التحقق: يقارن بالـ ID أولاً، أو بالاسم بشرط ألا يكون الاسم التلقائي العام
            is_already_saved = any(
                (quiz_id and fav.get("quiz_id") == quiz_id) or 
                (fav.get("title") == title and title != "كويز بدون عنوان")
                for fav in user_favorites
            )
            if is_already_saved:
                await state.update_data(is_saved_in_session=True)
                await call.answer("💡 هذا الكويز موجود بالفعل في قائمتك المفضلة مسبقاً!", show_alert=True)
                return

        # 🆕 3. تمرير الـ quiz_id هنا كـ Keyword Argument ليتم حفظه في قاعدة البيانات
        await asyncio.to_thread(save_favorite_quiz, call.from_user.id, title, questions, quiz_id=quiz_id)
        await state.update_data(is_saved_in_session=True)
        await call.answer("✅ تم الحفظ بنجاح! تجده في 'قائمتي المفضلة' قسم 'عام'.", show_alert=True)
        
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

@router.callback_query(F.data == "quiz_share")
async def handle_inline_quiz_share(call: types.CallbackQuery, state: FSMContext):
    try:
        bot_info = await bot.get_me()
        data = await state.get_data()
        
        quiz_id = data.get("quiz_id", "")
        title = data.get("source_title", "اختبار مميز")
        
        if quiz_id:
            start_param = f"quiz_{quiz_id}"
            share_text = f"🎯 جرب حل كويز «{title}» وتحدى نفسك معي في الدراسة!"
        else:
            start_param = f"start_{call.from_user.id}"
            share_text = "🔥 جرب حل هذا الكويز الرهيب وتحدى نفسك معي في الدراسة!"
            
        share_url = f"https://t.me/share/url?url=https://t.me/{bot_info.username}?start={start_param}&text={share_text}"
        share_kb = types.InlineKeyboardMarkup(inline_keyboard=[
            [types.InlineKeyboardButton(text="🚀 شارك هذا الكويز الآن", url=share_url)]
        ])
        
        await call.message.answer(
            f"🔗 يمكنك مشاركة كويز «{title}» مع أصدقائك عبر الضغط على الزر أدناه:",
            reply_markup=share_kb
        )
    except Exception as e:
        log_error(logger, f"Error in handle_inline_quiz_share: {e}", exception=e)
    finally:
        await call.answer()