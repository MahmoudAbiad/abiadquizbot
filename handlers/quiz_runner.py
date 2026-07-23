# Handlers/quiz_runner.py
import asyncio
import json
from typing import Union, Optional, List, Dict, Any

from aiogram import Router, types, F
from aiogram.fsm.context import FSMContext
from aiogram.filters import StateFilter

from config import bot, QuizState, redis_client
from constants import (
    MSG_QUIZ_STOPPED, MSG_FEEDBACK_PROMPT, MSG_FEEDBACK_SAVED
)
from keyboards import (
    get_main_menu_keyboard,
    get_quiz_result_keyboard,
    get_quiz_exit_confirmation_keyboard,
    get_rating_keyboard
)
from logger import get_logger, log_error, log_info, log_warning
from supabase_helper import (
    list_favorite_quizzes,
    update_user_stats,
    save_favorite_quiz,
    list_favorite_sections,
    create_favorite_section,
    get_or_update_high_score,
    submit_quiz_vote,
    save_quiz_feedback,
    log_usage_event,
    start_quiz_attempt,
    complete_quiz_attempt,
    mark_quiz_attempt_stopped
)
from services.quiz_engine import send_quiz_poll

logger = get_logger(__name__)
router = Router()

ACTIVE_QUIZ_STATES = (
    QuizState.answering_quiz, 
    QuizState.waiting_for_custom_name, 
    QuizState.waiting_for_new_section_title,
    QuizState.waiting_for_quiz_feedback
)

async def _send_main_menu(call_or_message: Union[types.Message, types.CallbackQuery], user_id: int) -> None:
    bot_info = await bot.get_me()
    menu = get_main_menu_keyboard(bot_info.username, user_id)
    text = "🏠 القائمة الرئيسية"
    if isinstance(call_or_message, types.CallbackQuery):
        await call_or_message.message.answer(text, reply_markup=menu)
    else:
        await call_or_message.answer(text, reply_markup=menu)

async def _start_loaded_quiz(msg_or_call: Union[types.Message, types.CallbackQuery], state: FSMContext, quiz_data: list, source_title: str, origin: str = "shared", quiz_id: Optional[str] = "") -> None:
    user_id = msg_or_call.from_user.id
    attempt_id = start_quiz_attempt(user_id, quiz_id or None, origin, len(quiz_data))
    asyncio.create_task(log_usage_event(user_id, "quiz_started", {
        "origin": origin, "quiz_id": quiz_id, "questions": len(quiz_data),
    }))

    await state.update_data(
        questions=quiz_data, current_index=0, score=0,
        total_count=len(quiz_data), source_title=source_title,
        quiz_origin=origin, quiz_completed=False, quiz_id=quiz_id,
        is_saved_in_session=False, is_switching_question=False,
        attempt_id=attempt_id,
        share_id=None
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
        chat_id = msg_or_call.chat.id if isinstance(msg_or_call, types.Message) else msg_or_call.message.chat.id
        user_id = state.key.user_id

        # 1. حالة انتهاء الاختبار
        if idx >= len(questions):
            await _handle_quiz_completion(chat_id, user_id, state, data)
            return

        # 2. تجهيز وإرسال السؤال الحالي
        q = questions[idx]
        control_kb = types.InlineKeyboardMarkup(inline_keyboard=[
            [types.InlineKeyboardButton(text="💡 طلب تلميح", callback_data="get_hint")],
            [
                types.InlineKeyboardButton(text="⏹ إيقاف", callback_data="quiz_stop"),
                types.InlineKeyboardButton(text="🔗 مشاركة", callback_data="quiz_share"),
                types.InlineKeyboardButton(text="💾 حفظ", callback_data="save_quiz")
            ],
            [types.InlineKeyboardButton(text="التالي ➡️", callback_data="next_question")]
        ])

        await send_quiz_poll(chat_id, user_id, q, idx, len(questions), control_kb)
        await state.update_data(is_switching_question=False)
    except Exception as e:
        log_error(logger, f"Error in send_question: {e}", exception=e)
        await state.update_data(is_switching_question=False)
        chat_id = msg_or_call.chat.id if isinstance(msg_or_call, types.Message) else msg_or_call.message.chat.id
        try:
            await bot.send_message(
                chat_id=chat_id,
                text=f"⚠️ **نعتذر منك، واجه النظام مشكلة تقنية في عرض السؤال رقم ({idx + 1}).**\n\n⏩ يمكنك تخطيه والانتقال للتالي.",
                reply_markup=types.InlineKeyboardMarkup(inline_keyboard=[
                    [types.InlineKeyboardButton(text="تخطي هذا السؤال والانتقال للتالي ➡️", callback_data="next_question")]
                ])
            )
        except Exception:
            pass

async def _handle_quiz_completion(chat_id: int, user_id: int, state: FSMContext, data: dict):
    score, total = data['score'], data['total_count']
    quiz_id = data.get('quiz_id')
    percentage = (score / total * 100) if total > 0 else 0
    previous_score_text = ""
    is_public = False

    if quiz_id and quiz_id.strip():
        score_data = await get_or_update_high_score(user_id, quiz_id, score, total)
        is_public = score_data["is_public"]
        if score_data["previous_score"] is not None:
            prev_score, highest = score_data["previous_score"], score_data["highest_score"]
            previous_score_text = f"\n🕒 نتيجتك السابقة: <b>{prev_score}</b>"
            if score > prev_score:
                previous_score_text += "\n🎉 <b>رقم قياسي جديد لك!</b>"
            previous_score_text += f"\n🏆 أعلى نتيجة مسجلة لك: <b>{highest}</b> من <b>{total}</b>\n"

    result_text = (
        f"🏁 <b>اكتمل الاختبار بنجاح!</b>\n\n"
        f"🎯 نتيجتك الحالية: <b>{score}</b> من <b>{total}</b>\n"
        f"📊 النسبة المئوية: <b>{percentage:.1f}%</b>\n"
        f"{previous_score_text}\n"
        f"{'🏆 ممتاز!' if percentage >= 80 else '👍 جيد!' if percentage >= 60 else '📚 استمر في الممارسة!'}"
    )

    if quiz_id and "-" in str(quiz_id):
        keyboard = get_rating_keyboard(quiz_id, quiz_id=quiz_id, is_score_public=is_public)
        result_text += "\n\n⭐ <b>كيف تقيم هذا الكويز؟</b> تقييمك المباشر يساعد الدفعة على فرز الكويزات الممتازة وتصفية الرديئة تلقائياً!"
    else:
        keyboard = get_quiz_result_keyboard(quiz_id=quiz_id, is_score_public=is_public)

    await bot.send_message(chat_id, result_text, reply_markup=keyboard, parse_mode="HTML")

    asyncio.create_task(complete_quiz_attempt(data.get("attempt_id"), score))
    asyncio.create_task(log_usage_event(user_id, "quiz_completed", {
        "quiz_id": quiz_id, "score": score, "total": total, "percentage": round(percentage, 1),
    }))
    await state.update_data(quiz_completed=True, is_switching_question=False)
    await state.set_state(None)

# ==================== معالجات حركة الكويز والتحكم ====================

@router.poll_answer()
async def handle_poll_answer(poll_answer: types.PollAnswer, state: FSMContext):
    try:
        poll_id = poll_answer.poll_id
        data_json = await redis_client.get(f"poll:{poll_id}")
        if not data_json or not poll_answer.option_ids:
            return

        quiz_info = json.loads(data_json)
        correct_opt = int(quiz_info["correct_option_id"])
        
        if poll_answer.user.id != quiz_info["user_id"]:
            return
        
        selected_opt = int(poll_answer.option_ids[0])
        if selected_opt == correct_opt:
            current_data = await state.get_data()
            await state.update_data(score=current_data.get('score', 0) + 1)
    except Exception as e:
        log_error(logger, f"Error in handle_poll_answer: {e}", exception=e)

@router.callback_query(StateFilter(*ACTIVE_QUIZ_STATES), F.data == "next_question")
async def handle_next(call: types.CallbackQuery, state: FSMContext):
    try:
        data = await state.get_data()
        if data.get("is_switching_question"):
            await call.answer()
            return
            
        await state.update_data(is_switching_question=True, current_index=data['current_index'] + 1)
        try:
            await call.message.delete()
        except Exception:
            pass
        await send_question(call, state)
    except Exception as e:
        log_error(logger, f"Error in handle_next: {e}", exception=e)
        await state.update_data(is_switching_question=False)
    finally:
        await call.answer()

@router.callback_query(StateFilter(*ACTIVE_QUIZ_STATES), F.data == "get_hint")
async def handle_hint(call: types.CallbackQuery, state: FSMContext):
    try:
        data = await state.get_data()
        q = data['questions'][data['current_index']]
        await call.answer(f"💡 تلميح ذكي: {q['hint']}", show_alert=False)
    except Exception as e:
        log_error(logger, f"Error in handle_hint: {e}", exception=e)
        await call.answer("❌ خطأ في جلب التلميح", show_alert=True)

@router.callback_query(QuizState.answering_quiz, F.data == "start_first_question")
async def start_quiz_after_warning(call: types.CallbackQuery, state: FSMContext):
    try:
        await call.message.delete()
        await send_question(call, state)
    except Exception as e:
        log_error(logger, f"Error in start_quiz_after_warning: {e}", exception=e)
    finally:
        await call.answer()

@router.callback_query(StateFilter(*ACTIVE_QUIZ_STATES), F.data == "quiz_stop")
async def request_stop_confirmation(call: types.CallbackQuery, state: FSMContext):
    try:
        confirm_kb = get_quiz_exit_confirmation_keyboard()
        await call.message.answer(
            "🏁 <b>تأكيد إنهاء الاختبار</b>\n\n"
            "هل أنت متأكد من رغبتك في إنهاء الكويز الآن؟ سيتم احتساب نتيجتك الحالية بناءً على الأسئلة التي أجبت عليها حتى هذه اللحظة.",
            reply_markup=confirm_kb,
            parse_mode="HTML"
        )
    except Exception as e:
        log_error(logger, f"Error requesting stop confirmation: {e}")
    finally:
        await call.answer()

@router.callback_query(StateFilter(*ACTIVE_QUIZ_STATES), F.data == "quiz_stop_confirmed")
async def stop_quiz_confirmed(call: types.CallbackQuery, state: FSMContext):
    try:
        data = await state.get_data()
        questions = data.get("questions", [])
        await state.update_data(current_index=len(questions))
        try:
            await call.message.delete()
        except Exception:
            pass
        await send_question(call, state)
    except Exception as e:
        log_error(logger, f"Error in stop_quiz_confirmed: {e}", exception=e)
        await call.answer("❌ تعذر إنهاء الكويز", show_alert=True)
    finally:
        await call.answer()

@router.callback_query(StateFilter(*ACTIVE_QUIZ_STATES), F.data == "quiz_resume_flow")
async def resume_quiz_flow(call: types.CallbackQuery):
    try:
        await call.message.delete()
        await call.answer("🔄 ممتاز! تم إلغاء الإيقاف، يمكنك مواصلة حل أسئلتك الآن بنجاح.", show_alert=True)
    except Exception as e:
        log_error(logger, f"Error in resume_quiz_flow: {e}")
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
        await state.update_data(current_index=0, score=0, quiz_completed=False, is_switching_question=False)
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

@router.callback_query(F.data == "ignored")
async def handle_ignored_click(call: types.CallbackQuery):
    await call.answer("✅ تم تسجيل إجابتك")

@router.callback_query(StateFilter(*ACTIVE_QUIZ_STATES), F.data == "quiz_share")
async def handle_inline_quiz_share(call: types.CallbackQuery, state: FSMContext):
    try:
        bot_info = await bot.get_me()
        data = await state.get_data()
        quiz_id = data.get("quiz_id", "")
        title = data.get("source_title", "اختبار مميز")
        
        start_param = f"quiz_{quiz_id}" if quiz_id else f"start_{call.from_user.id}"
        share_text = f"🎯 جرب حل كويز «{title}» وتحدى نفسك معي في الدراسة!"
        share_url = f"https://t.me/share/url?url=https://t.me/{bot_info.username}?start={start_param}&text={share_text}"
        
        share_kb = types.InlineKeyboardMarkup(inline_keyboard=[
            [types.InlineKeyboardButton(text="🚀 شارك هذا الكويز الآن", url=share_url)]
        ])
        
        asyncio.create_task(log_usage_event(call.from_user.id, "quiz_shared", {"quiz_id": quiz_id}))
        await call.message.answer(f"🔗 يمكنك مشاركة كويز «{title}» مع أصدقائك عبر الضغط على الزر أدناه:", reply_markup=share_kb)
    except Exception as e:
        log_error(logger, f"Error in handle_inline_quiz_share: {e}", exception=e)
    finally:
        await call.answer()

# ==================== معالجات التقييم والملاحظات ====================

@router.callback_query(F.data.startswith("rate_like_"))
async def rate_like_quiz(call: types.CallbackQuery):
    try:
        quiz_id = call.data.replace("rate_like_", "")
        success = await submit_quiz_vote(quiz_id, call.from_user.id, "like")
        if success:
            asyncio.create_task(log_usage_event(call.from_user.id, "quiz_rated", {"quiz_id": quiz_id, "vote": "like"}))
            await call.answer("👍 شكراً لك! تم تسجيل إعجابك وتحديث تقييم الاختبار بنجاح.", show_alert=True)
        else:
            await call.answer("⚠️ لقد قمت بالتصويت على هذا الاختبار مسبقاً!", show_alert=True)
    except Exception as e:
        log_error(logger, f"Error in rate_like_quiz: {e}")
        await call.answer("❌ حدث خطأ أثناء معالجة الإعجاب.", show_alert=True)

@router.callback_query(F.data.startswith("rate_dislike_"))
async def rate_dislike_quiz(call: types.CallbackQuery):
    try:
        quiz_id = call.data.replace("rate_dislike_", "")
        success = await submit_quiz_vote(quiz_id, call.from_user.id, "dislike")
        if success:
            asyncio.create_task(log_usage_event(call.from_user.id, "quiz_rated", {"quiz_id": quiz_id, "vote": "dislike"}))
            await call.answer("👎 تم احتساب تقييمك السلبي. سيتولى النظام تصفية وحذف الاختبارات الرديئة تلقائياً.", show_alert=True)
        else:
            await call.answer("⚠️ لقد قمت بالتصويت على هذا الاختبار مسبقاً!", show_alert=True)
    except Exception as e:
        log_error(logger, f"Error in rate_dislike_quiz: {e}")
        await call.answer("❌ حدث خطأ أثناء تسجيل التقييم السلبي.", show_alert=True)

@router.callback_query(F.data.startswith("rate_feedback_"))
async def prompt_feedback(call: types.CallbackQuery, state: FSMContext):
    try:
        quiz_id = call.data.replace("rate_feedback_", "")
        await state.update_data(feedback_quiz_id=quiz_id)
        await state.set_state(QuizState.waiting_for_quiz_feedback)
        await call.message.answer(MSG_FEEDBACK_PROMPT, parse_mode="HTML")
    except Exception as e:
        log_error(logger, f"Error in prompt_feedback: {e}")
        await call.answer("❌ تعذر فتح واجهة الملاحظات والشكاوى.", show_alert=True)
    finally:
        await call.answer()

@router.message(QuizState.waiting_for_quiz_feedback, F.text)
async def process_quiz_feedback(msg: types.Message, state: FSMContext):
    try:
        comment = msg.text.strip()
        if not comment:
            await msg.answer("❌ الملاحظة فارغة، يرجى كتابة نص شكوى واضح ومفهوم:")
            return
        
        data = await state.get_data()
        quiz_id = data.get("feedback_quiz_id")
        if quiz_id:
            await save_quiz_feedback(quiz_id, msg.from_user.id, comment[:500])
            asyncio.create_task(log_usage_event(msg.from_user.id, "feedback_submitted", {"quiz_id": quiz_id}))
            await msg.answer(MSG_FEEDBACK_SAVED, parse_mode="HTML")
        else:
            await msg.answer("❌ حدث خطأ داخلي، لم يتم العثور على المعرف المركزي لهذا الاختبار.")
            
        await state.set_state(None)
    except Exception as e:
        log_error(logger, f"Error in process_quiz_feedback: {e}")
        await msg.answer("❌ نعتذر منك، حدث خطأ أثناء إرسال ملاحظتك.")

# ==================== معالجات ويزارد حفظ الكويز للمفضلة ====================

@router.callback_query(StateFilter(*ACTIVE_QUIZ_STATES), F.data.in_({"save_quiz", "quiz_favorite"}))
async def handle_save_quiz_start(call: types.CallbackQuery, state: FSMContext):
    try:
        data = await state.get_data()
        if data.get("is_saved_in_session"):
            await call.answer("✅ هذا الكويز محفوظ بالفعل في قائمتك المفضلة!", show_alert=True)
            return

        questions, quiz_id = data.get("questions"), data.get("quiz_id")
        if not questions:
            await call.answer("❌ لا يوجد كويز لحفظه!", show_alert=True)
            return

        user_favorites = await list_favorite_quizzes(call.from_user.id)
        if user_favorites and quiz_id and any(fav.get("quiz_id") == quiz_id for fav in user_favorites):
            await state.update_data(is_saved_in_session=True)
            await call.answer("💡 هذا الكويز موجود بالفعل في قائمتك المفضلة مسبقاً!", show_alert=True)
            return

        kb = types.InlineKeyboardMarkup(inline_keyboard=[
            [types.InlineKeyboardButton(text="📄 حفظ بالاسم الحالي", callback_data="save_name_current")],
            [types.InlineKeyboardButton(text="✏️ حفظ باسم مخصص", callback_data="save_name_custom")]
        ])
        await call.message.answer("📝 **خطوة 1 من 2: تسمية الاختبار**\n\nكيف تود تسمية هذا الكويز في المفضلة؟", reply_markup=kb, parse_mode="Markdown")
    except Exception as e:
        log_error(logger, f"Error starting save wizard: {e}", exception=e)
        await call.answer("❌ حدث خطأ أثناء بدء عملية الحفظ.", show_alert=True)
    finally:
        await call.answer()

@router.callback_query(StateFilter(*ACTIVE_QUIZ_STATES), F.data == "save_name_current")
async def save_name_current_handler(call: types.CallbackQuery, state: FSMContext):
    try:
        data = await state.get_data()
        title = data.get("source_title") or data.get("title") or "كويز بدون عنوان"
        await state.update_data(final_save_title=title)
        await _prompt_section_selection(call.message, state)
        try:
            await call.message.delete()
        except Exception:
            pass
    except Exception as e:
        log_error(logger, f"Error in save_name_current: {e}", exception=e)
    finally:
        await call.answer()

@router.callback_query(StateFilter(*ACTIVE_QUIZ_STATES), F.data == "save_name_custom")
async def save_name_custom_handler(call: types.CallbackQuery, state: FSMContext):
    try:
        await state.set_state(QuizState.waiting_for_custom_name)
        await call.message.edit_text("✏️ **أرسل الآن الاسم المخصص** الذي تريده لهذا الاختبار في رسالة نصية مباشرة:")
    except Exception as e:
        log_error(logger, f"Error in save_name_custom: {e}", exception=e)
    finally:
        await call.answer()

@router.message(QuizState.waiting_for_custom_name, F.text)
async def process_custom_name(msg: types.Message, state: FSMContext):
    try:
        custom_title = msg.text.strip()
        if not custom_title:
            await msg.answer("❌ الاسم المرسل غير صالح، يرجى إرسال اسم نصي واضح:")
            return
        await state.update_data(final_save_title=custom_title)
        await _prompt_section_selection(msg, state)
    except Exception as e:
        log_error(logger, f"Error in process_custom_name: {e}", exception=e)

async def _prompt_section_selection(msg_or_call_msg: types.Message, state: FSMContext):
    kb = types.InlineKeyboardMarkup(inline_keyboard=[
        [types.InlineKeyboardButton(text="🌐 حفظ في قسم عام", callback_data="save_sec_general")],
        [types.InlineKeyboardButton(text="📁 حفظ ضمن قسم مخصص", callback_data="save_sec_choose")]
    ])
    await msg_or_call_msg.answer("📁 **خطوة 2 من 2: تصنيف مكان الحفظ**\n\nأين تريد تصنيف هذا الاختبار في المفضلة؟", reply_markup=kb, parse_mode="Markdown")

@router.callback_query(StateFilter(*ACTIVE_QUIZ_STATES), F.data == "save_sec_general")
async def handle_save_general(call: types.CallbackQuery, state: FSMContext):
    try:
        data = await state.get_data()
        title, questions, quiz_id = data.get("final_save_title") or "كويز بدون عنوان", data.get("questions"), data.get("quiz_id")
        
        fav_id = await save_favorite_quiz(call.from_user.id, title, questions, None, None, quiz_id)
        if not fav_id:
            await call.answer("❌ تعذر حفظ الكويز في المفضلة، حاول مجدداً.", show_alert=True)
            return
        await state.update_data(is_saved_in_session=True)
        asyncio.create_task(log_usage_event(call.from_user.id, "quiz_saved_favorite", {"quiz_id": quiz_id, "section": "عام"}))
        await call.message.edit_text(f"✅ **تم الحفظ بنجاح!**\n\n📦 الاسم: `{title}`\n🗂 القسم: `عام`", parse_mode="Markdown")
        await state.set_state(None if data.get("quiz_completed") else QuizState.answering_quiz)
    except Exception as e:
        log_error(logger, f"Error saving to general: {e}", exception=e)
        await call.answer("❌ حدث خطأ أثناء حفظ الكويز.", show_alert=True)
    finally:
        await call.answer()

@router.callback_query(StateFilter(*ACTIVE_QUIZ_STATES), F.data == "save_sec_choose")
async def handle_save_choose_section(call: types.CallbackQuery, state: FSMContext):
    try:
        sections = await list_favorite_sections(call.from_user.id)
        inline_keyboard = [[types.InlineKeyboardButton(text=f"📁 {sec['title']}", callback_data=f"save_to_sec_{sec['section_id']}")] for sec in (sections or [])]
        inline_keyboard.append([types.InlineKeyboardButton(text="➕ إنشاء قسم جديد واختياره", callback_data="save_sec_create_new")])
        inline_keyboard.append([types.InlineKeyboardButton(text="🌐 إلغاء وحفظ في عام", callback_data="save_sec_general")])
        
        await call.message.edit_text("📂 **اختر أحد أقسامك المفضلة الحالية لتصنيف الاختبار داخله:**", reply_markup=types.InlineKeyboardMarkup(inline_keyboard=inline_keyboard), parse_mode="Markdown")
    except Exception as e:
        log_error(logger, f"Error showing sections: {e}", exception=e)
        await call.answer("❌ تعذر جلب قائمة الأقسام حالياً.", show_alert=True)
    finally:
        await call.answer()

@router.callback_query(StateFilter(*ACTIVE_QUIZ_STATES), F.data.startswith("save_to_sec_"))
async def handle_save_to_existing_section(call: types.CallbackQuery, state: FSMContext):
    try:
        section_id = call.data.replace("save_to_sec_", "")
        data = await state.get_data()
        title, questions, quiz_id = data.get("final_save_title") or "كويز بدون عنوان", data.get("questions"), data.get("quiz_id")
        
        fav_id = await save_favorite_quiz(call.from_user.id, title, questions, section_id, None, quiz_id)
        if not fav_id:
            await call.answer("❌ تعذر حفظ الكويز ضمن هذا القسم، حاول مجدداً.", show_alert=True)
            return
        await state.update_data(is_saved_in_session=True)
        asyncio.create_task(log_usage_event(call.from_user.id, "quiz_saved_favorite", {"quiz_id": quiz_id, "section_id": section_id}))
        await call.message.edit_text(f"✅ **تم حفظ الاختبار بنجاح ضمن القسم المختار!**\n\n📦 الاسم: `{title}`", parse_mode="Markdown")
        await state.set_state(None if data.get("quiz_completed") else QuizState.answering_quiz)
    except Exception as e:
        log_error(logger, f"Error saving to existing section: {e}", exception=e)
    finally:
        await call.answer()

@router.callback_query(StateFilter(*ACTIVE_QUIZ_STATES), F.data == "save_sec_create_new")
async def handle_request_new_section(call: types.CallbackQuery, state: FSMContext):
    try:
        await state.set_state(QuizState.waiting_for_new_section_title)
        await call.message.edit_text("➕ **أرسل الآن اسم القسم الجديد** المراد إنشاؤه لتصنيف الكويز داخله:")
    except Exception as e:
        log_error(logger, f"Error in request new section: {e}", exception=e)
    finally:
        await call.answer()

@router.message(QuizState.waiting_for_new_section_title, F.text)
async def process_new_section_title_and_save(msg: types.Message, state: FSMContext):
    try:
        section_title = msg.text.strip()
        if not section_title:
            await msg.answer("❌ اسم القسم غير صالح، يرجى إدخال نص واضح:")
            return
            
        user_id = msg.from_user.id
        data = await state.get_data()
        title, questions, quiz_id = data.get("final_save_title") or "كويز بدون عنوان", data.get("questions"), data.get("quiz_id")
        
        new_section_id = await create_favorite_section(user_id, section_title)
        fav_id = await save_favorite_quiz(user_id, title, questions, new_section_id, None, quiz_id)
        if not fav_id:
            await msg.answer("❌ تعذر حفظ الاختبار، حاول مجدداً.")
            return
        await state.update_data(is_saved_in_session=True)
        asyncio.create_task(log_usage_event(user_id, "quiz_saved_favorite", {"quiz_id": quiz_id, "new_section": section_title}))
        await msg.answer(f"✅ **تم إنشاء القسم وحفظ الاختبار بنجاح!**\n\n📦 الاسم: `{title}`\n🗂 القسم الجديد: `{section_title}`", parse_mode="Markdown")
        await state.set_state(None if data.get("quiz_completed") else QuizState.answering_quiz)
    except Exception as e:
        log_error(logger, f"Error in creating section and saving: {e}", exception=e)

@router.callback_query(F.data == "force_stop_previous_quiz")
async def force_stop_previous_quiz_handler(call: types.CallbackQuery, state: FSMContext):
    try:
        data = await state.get_data()
        if data.get("attempt_id"):
            asyncio.create_task(mark_quiz_attempt_stopped(data["attempt_id"]))
        await state.clear()
        try:
            await call.message.delete()
        except Exception:
            pass
        await call.message.answer("✅ <b>تم إيقاف الاختبار السابق بنجاح!</b>\n\nيمكنك الآن إرسال محتوى جديد فوراً. 🚀", parse_mode="HTML")
    except Exception as e:
        log_error(logger, f"Error in force_stop_previous_quiz: {e}")
    finally:
        await call.answer()

@router.callback_query(F.data == "delete_warning_msg")
async def delete_warning_msg_handler(call: types.CallbackQuery):
    try:
        await call.message.delete()
    except Exception:
        pass
    finally:
        await call.answer()