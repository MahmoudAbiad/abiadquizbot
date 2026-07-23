# Handlers/quiz_runner.py
import asyncio
import json
from typing import Union, Optional
from aiogram import Router, types, F
from aiogram.fsm.context import FSMContext
from aiogram.filters import StateFilter

from config import bot, QuizState, redis_client
from keyboards import get_quiz_result_keyboard, get_rating_keyboard, get_quiz_exit_confirmation_keyboard
from logger import get_logger, log_error, log_info
from supabase_helper import get_or_update_high_score, log_usage_event, start_quiz_attempt, complete_quiz_attempt, mark_quiz_attempt_stopped
from services.quiz_engine import send_quiz_poll

logger = get_logger(__name__)
router = Router()

ACTIVE_QUIZ_STATES = (
    QuizState.answering_quiz, 
    QuizState.waiting_for_custom_name, 
    QuizState.waiting_for_new_section_title,
    QuizState.waiting_for_quiz_feedback
)

async def send_question(msg_or_call: Union[types.Message, types.CallbackQuery], state: FSMContext) -> None:
    data = await state.get_data()
    questions = data['questions']
    idx = data['current_index']
    chat_id = msg_or_call.chat.id if isinstance(msg_or_call, types.Message) else msg_or_call.message.chat.id
    user_id = state.key.user_id

    # 1. حالة النتيجة النهائية
    if idx >= len(questions):
        await _handle_quiz_completion(chat_id, user_id, state, data)
        return

    # 2. إرسال السؤال الحالي
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

    try:
        await send_quiz_poll(chat_id, user_id, q, idx, len(questions), control_kb)
        await state.update_data(is_switching_question=False)
    except Exception as e:
        log_error(logger, f"Error in send_question: {e}", exception=e)
        await state.update_data(is_switching_question=False)
        await bot.send_message(
            chat_id=chat_id,
            text=f"⚠️ **عذراً، تعذر عرض السؤال رقم ({idx + 1}). يمكنك تخطيه للبدء بالسؤال التالي.**",
            reply_markup=types.InlineKeyboardMarkup(inline_keyboard=[
                [types.InlineKeyboardButton(text="تخطي السؤال ➡️", callback_data="next_question")]
            ])
        )

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

    keyboard = get_rating_keyboard(quiz_id, quiz_id=quiz_id, is_score_public=is_public) if (quiz_id and "-" in str(quiz_id)) else get_quiz_result_keyboard(quiz_id=quiz_id, is_score_public=is_public)
    await bot.send_message(chat_id, result_text, reply_markup=keyboard, parse_mode="HTML")

    asyncio.create_task(complete_quiz_attempt(data.get("attempt_id"), score))
    asyncio.create_task(log_usage_event(user_id, "quiz_completed", {"quiz_id": quiz_id, "score": score, "total": total}))
    await state.update_data(quiz_completed=True, is_switching_question=False)
    await state.set_state(None)

@router.poll_answer()
async def handle_poll_answer(poll_answer: types.PollAnswer, state: FSMContext):
    data_json = await redis_client.get(f"poll:{poll_answer.poll_id}")
    if not data_json or not poll_answer.option_ids:
        return

    quiz_info = json.loads(data_json)
    if poll_answer.user.id != quiz_info["user_id"]:
        return

    if int(poll_answer.option_ids[0]) == int(quiz_info["correct_option_id"]):
        current_data = await state.get_data()
        await state.update_data(score=current_data.get('score', 0) + 1)

@router.callback_query(StateFilter(*ACTIVE_QUIZ_STATES), F.data == "next_question")
async def handle_next(call: types.CallbackQuery, state: FSMContext):
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
    await call.answer()

    # أضف هذه الدالة داخل Handlers/quiz_runner.py
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