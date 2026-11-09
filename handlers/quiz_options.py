# handlers/quiz_options.py
"""
==============================================================================
MODULE: Quiz Options Screen (لوحة خيارات الكويز الموحدة)
==============================================================================
🆕 يدير هذا الراوتر خيارات الكويز (نوع الأسئلة، الصعوبة، والترجمة) في إطار
شاشة لوحة الكويز الموحدة (Unified Dashboard).
تعديل أي خيار (نوع / صعوبة / ترجمة) يُحدّث نفس الشاشة فورياً عبر (edit_message_text)
دون الحاجة للتنقل بين مراحل متتالية.
==============================================================================
"""

from aiogram import F, Router, types
from aiogram.fsm.context import FSMContext
from html import escape

from config import QuizState
from constants import (
    BTN_CANCEL_REQUEST, DIFFICULTY_ADVANCED, DIFFICULTY_EASY, DIFFICULTY_MEDIUM, DIFFICULTY_PROGRESSIVE,
    ERROR_CUSTOM_QUESTION_TYPE_TOO_LONG, MAX_CUSTOM_QUESTION_TYPE_LENGTH,
    MSG_CUSTOM_QUESTION_TYPE_PROMPT, MSG_QUIZ_DIFFICULTY_PROMPT, MSG_QUIZ_TYPE_PROMPT,
    QUESTION_TYPE_CUSTOM, QUESTION_TYPE_GENERAL, SUBJECT_OTHER,
    QUESTION_TYPE_SIMILAR_MATH, MSG_SIMILAR_MATH_NOTICE, SUBJECT_MATH,
    SUBJECT_QUIZ_SOLVED, SUBJECT_QUIZ_UNSOLVED,
)
from keyboards import get_quiz_difficulty_keyboard, get_quiz_type_keyboard
from logger import get_logger, log_error

logger = get_logger(__name__)
router = Router()


def _type_display_label(data: dict) -> str:
    """يبني نص عرض النوع المختار لعنوان مرحلة الصعوبة (✅ النوع: ...)."""
    question_type = data.get("question_type", QUESTION_TYPE_GENERAL)
    if question_type == QUESTION_TYPE_CUSTOM:
        return data.get("custom_question_type_text") or "تفضيل خاص"
    if question_type.startswith("other_"):
        suggested = data.get("suggested_question_types", [])
        try:
            idx = int(question_type.split("_", 1)[1])
            if 0 <= idx < len(suggested):
                return suggested[idx]
        except (ValueError, IndexError):
            pass
        return "مخصص"
    if question_type == QUESTION_TYPE_GENERAL:
        return "📚 أسئلة شاملة عن الدرس"
    from constants import QUESTION_TYPE_OPTIONS
    for subj_options in QUESTION_TYPE_OPTIONS.values():
        for value, label in subj_options:
            if value == question_type:
                return label
    return "📚 أسئلة شاملة عن الدرس"


async def _render_type_screen(target: types.Message, state: FSMContext, edit: bool) -> None:
    from handlers.files import _render_question_count_screen
    await state.set_state(QuizState.waiting_for_count)
    msg_id = target.message_id if edit else None
    await _render_question_count_screen(target.bot, target.chat.id, msg_id, state, target_for_send=target)


async def _render_difficulty_screen(target: types.Message, state: FSMContext, edit: bool) -> None:
    from handlers.files import _render_question_count_screen
    await state.set_state(QuizState.waiting_for_count)
    msg_id = target.message_id if edit else None
    await _render_question_count_screen(target.bot, target.chat.id, msg_id, state, target_for_send=target)


# ==================== معالجات لوحة الكويز الموحدة ====================

@router.callback_query(F.data.startswith("qtype_"))
async def handle_type_screen(call: types.CallbackQuery, state: FSMContext) -> None:
    """
    معالج موحّد لخيارات نوع الأسئلة: يحدّث النوع فورياً على نفس لوحة الكويز (Dashboard)
    دون نقل الطالب لشاشات مستقلة.
    """
    value = call.data.replace("qtype_", "", 1)
    try:
        if value == QUESTION_TYPE_CUSTOM:
            await state.set_state(QuizState.waiting_for_custom_question_type)
            await call.message.edit_text(
                MSG_CUSTOM_QUESTION_TYPE_PROMPT,
                reply_markup=types.InlineKeyboardMarkup(inline_keyboard=[
                    [types.InlineKeyboardButton(text="🔙 الرجوع للوحة الكويز", callback_data="qcount_review")],
                    [types.InlineKeyboardButton(text=BTN_CANCEL_REQUEST, callback_data="cancel_upload_request")],
                ]),
            )
            return

        if value == QUESTION_TYPE_SIMILAR_MATH:
            data = await state.get_data()
            if (data.get("content_subject_type") or data.get("subject_type")) != SUBJECT_MATH:
                await call.answer("هذا الخيار مخصص لمحتوى الرياضيات.", show_alert=True)
                return
            await state.update_data(
                question_type=QUESTION_TYPE_SIMILAR_MATH,
                custom_question_type_text=None,
                quiz_extraction_mode=None,
                quiz_detection_notice=None,
                count_prompt_text="🧩 كم مسألة جديدة تريد توليدها بنفس النمط؟",
            )
        else:
            await state.update_data(
                question_type=value,
                custom_question_type_text=None,
                count_prompt_text="📝 كم سؤالاً تريد توليده من هذا المحتوى؟",
            )

        from handlers.files import _render_question_count_screen
        await state.set_state(QuizState.waiting_for_count)
        await _render_question_count_screen(
            call.bot, call.message.chat.id, call.message.message_id, state, target_for_send=call.message
        )
    except Exception as exc:
        log_error(logger, f"Quiz type screen handling failed: {exc}", exception=exc)
    finally:
        await call.answer()


@router.callback_query(F.data == "qback_to_type")
async def handle_back_to_type_screen(call: types.CallbackQuery, state: FSMContext) -> None:
    """رجوع للوحة الكويز الموحدة."""
    await state.set_state(QuizState.waiting_for_count)
    from handlers.files import _render_question_count_screen
    await _render_question_count_screen(
        call.bot, call.message.chat.id, call.message.message_id, state, target_for_send=call.message
    )
    await call.answer()


# ==================== خطوة النص الحر (تفضيل خاص) ====================

@router.message(QuizState.waiting_for_custom_question_type, F.text)
async def handle_custom_question_type_text(message: types.Message, state: FSMContext) -> None:
    """استقبال نص تفضيل نوع الأسئلة الحر، ثم العودة مباشرة للوحة الكويز الموحدة."""
    text = (message.text or "").strip()
    if not text:
        await message.answer(MSG_CUSTOM_QUESTION_TYPE_PROMPT)
        return
    if len(text) > MAX_CUSTOM_QUESTION_TYPE_LENGTH:
        await message.answer(ERROR_CUSTOM_QUESTION_TYPE_TOO_LONG.format(max_len=MAX_CUSTOM_QUESTION_TYPE_LENGTH))
        return

    await state.update_data(question_type=QUESTION_TYPE_CUSTOM, custom_question_type_text=text)
    await state.set_state(QuizState.waiting_for_count)
    data = await state.get_data()
    try:
        await message.delete()
    except Exception:
        pass
    from handlers.files import _render_question_count_screen
    await _render_question_count_screen(
        message.bot,
        data.get("count_screen_chat_id", message.chat.id),
        data.get("count_screen_message_id"),
        state,
        target_for_send=message,
    )


# ==================== اختيار الصعوبة ====================

@router.callback_query(F.data == "qback_to_difficulty")
async def back_to_difficulty(call: types.CallbackQuery, state: FSMContext):
    data = await state.get_data()
    if data.get("subject_type") in (SUBJECT_QUIZ_SOLVED, SUBJECT_QUIZ_UNSOLVED) and data.get("question_type") != QUESTION_TYPE_SIMILAR_MATH:
        await call.answer("هذا الاختبار لا يحتاج اختيار الصعوبة.", show_alert=True)
        return
    await state.update_data(review_count_to_restore=data.get("selected_question_count"))
    from handlers.files import _render_question_count_screen
    await state.set_state(QuizState.waiting_for_count)
    await _render_question_count_screen(
        call.bot, call.message.chat.id, call.message.message_id, state, target_for_send=call.message
    )
    await call.answer()


@router.callback_query(F.data.startswith("qdiff_"))
async def handle_difficulty_screen(call: types.CallbackQuery, state: FSMContext) -> None:
    """اختيار الصعوبة: يحدّث الصعوبة فورياً على نفس لوحة الكويز الموحدة (Dashboard)."""
    value = call.data.replace("qdiff_", "", 1)
    if value not in (DIFFICULTY_EASY, DIFFICULTY_MEDIUM, DIFFICULTY_ADVANCED, DIFFICULTY_PROGRESSIVE):
        await call.answer()
        return
    await state.update_data(difficulty=value)
    from handlers.files import _render_question_count_screen
    await state.set_state(QuizState.waiting_for_count)
    try:
        await _render_question_count_screen(
            call.bot, call.message.chat.id, call.message.message_id, state, target_for_send=call.message
        )
    except Exception as exc:
        log_error(logger, f"Failed to update difficulty on count screen: {exc}", exception=exc)
    finally:
        await call.answer()


