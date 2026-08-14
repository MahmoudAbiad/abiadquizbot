# handlers/quiz_options.py
"""
==============================================================================
MODULE: Quiz Options Screen (شاشة اختيار نوع الأسئلة + الصعوبة)
==============================================================================
🆕 يدير هذا الراوتر شاشة الخيارات الموحّدة (رسالة واحدة تُحدَّث بالمكان مع كل ضغطة
زر عبر edit_text، بدل رسائل متتالية) التي تظهر بعد التصنيف الموحّد
(services/subject_classifier.py) وقبل شاشة "كم سؤالاً تريد؟":

- أزرار نوع الأسئلة (تختلف حسب المادة: رياضيات/إنجليزي/أخرى - راجع
  constants.QUESTION_TYPE_OPTIONS و keyboards.get_quiz_options_keyboard) + خيار
  "عام" الافتراضي + خيار "تفضيل خاص" (نص حر من الطالب).
- أزرار الصعوبة الثلاثة (سهل/متوسط/متقدم).
- زر "متابعة" للانتقال لشاشة عدد الأسئلة (handlers.files._show_question_count_screen).

كل الاختيارات toggle (تُحدَّث بنفس الرسالة عبر edit_reply_markup/edit_text) لتقليل
الاحتكاك - نفس فلسفة "شاشة واحدة" المعتمدة بباقي شاشات هذا التدفق.
==============================================================================
"""

from aiogram import F, Router, types
from aiogram.fsm.context import FSMContext

from config import QuizState
from constants import (
    BTN_CANCEL_REQUEST, DIFFICULTY_ADVANCED, DIFFICULTY_EASY, DIFFICULTY_MEDIUM,
    ERROR_CUSTOM_QUESTION_TYPE_TOO_LONG, MAX_CUSTOM_QUESTION_TYPE_LENGTH,
    MSG_CUSTOM_QUESTION_TYPE_PROMPT, MSG_QUIZ_OPTIONS_PROMPT, QUESTION_TYPE_CUSTOM,
    QUESTION_TYPE_GENERAL, SUBJECT_MATH, SUBJECT_ENGLISH, SUBJECT_OTHER,
)
from keyboards import get_quiz_options_keyboard
from logger import get_logger, log_error

logger = get_logger(__name__)
router = Router()


async def _refresh_options_screen(call: types.CallbackQuery, state: FSMContext) -> None:
    """يعيد رسم شاشة الخيارات بنفس الرسالة (edit) بعد أي تحديث لاختيار الطالب."""
    data = await state.get_data()
    keyboard = get_quiz_options_keyboard(
        subject_type=data.get("subject_type", SUBJECT_OTHER),
        suggested_types=data.get("suggested_question_types", []),
        selected_type=data.get("question_type", QUESTION_TYPE_GENERAL),
        selected_difficulty=data.get("difficulty", DIFFICULTY_MEDIUM),
    )
    try:
        await call.message.edit_reply_markup(reply_markup=keyboard)
    except Exception:
        # لو الكيبورد نفسه ما تغيّر (نفس الاختيار ضُغط مرتين متتاليتين)، تيليجرام يرفض
        # التعديل بخطأ "message is not modified" - نتجاهله بأمان، لا داعي لأي إجراء آخر.
        pass


@router.callback_query(QuizState.waiting_for_quiz_options, F.data.startswith("qtype_"))
async def handle_question_type_selection(call: types.CallbackQuery, state: FSMContext) -> None:
    """اختيار نوع الأسئلة: قيمة ثابتة من القائمة الجاهزة، أو 'general' الافتراضي.
    ملاحظة: زر 'custom' له معالج منفصل أدناه (يفتح خطوة إدخال نصي)."""
    value = call.data.replace("qtype_", "", 1)
    if value == "custom":
        await call.answer()
        return  # يُعالَج بواسطة handle_question_type_custom أدناه
    await state.update_data(question_type=value, custom_question_type_text=None)
    await _refresh_options_screen(call, state)
    await call.answer()


@router.callback_query(QuizState.waiting_for_quiz_options, F.data == "qtype_custom")
async def handle_question_type_custom(call: types.CallbackQuery, state: FSMContext) -> None:
    """يفتح خطوة إدخال نصي حر لتفضيل نوع الأسئلة الخاص بالطالب."""
    try:
        await state.set_state(QuizState.waiting_for_custom_question_type)
        await call.message.edit_text(
            MSG_CUSTOM_QUESTION_TYPE_PROMPT,
            reply_markup=types.InlineKeyboardMarkup(inline_keyboard=[
                [types.InlineKeyboardButton(text="🔙 رجوع للخيارات", callback_data="qtype_back_to_options")],
                [types.InlineKeyboardButton(text=BTN_CANCEL_REQUEST, callback_data="cancel_upload_request")],
            ]),
        )
    except Exception as exc:
        log_error(logger, f"Failed to open custom question-type prompt: {exc}", exception=exc)
    finally:
        await call.answer()


@router.callback_query(QuizState.waiting_for_custom_question_type, F.data == "qtype_back_to_options")
async def handle_question_type_custom_back(call: types.CallbackQuery, state: FSMContext) -> None:
    """رجوع من خطوة الإدخال النصي لشاشة الخيارات دون تغيير أي اختيار سابق."""
    await state.set_state(QuizState.waiting_for_quiz_options)
    data = await state.get_data()
    keyboard = get_quiz_options_keyboard(
        subject_type=data.get("subject_type", SUBJECT_OTHER),
        suggested_types=data.get("suggested_question_types", []),
        selected_type=data.get("question_type", QUESTION_TYPE_GENERAL),
        selected_difficulty=data.get("difficulty", DIFFICULTY_MEDIUM),
    )
    await call.message.edit_text(MSG_QUIZ_OPTIONS_PROMPT, parse_mode="HTML", reply_markup=keyboard)
    await call.answer()


@router.message(QuizState.waiting_for_custom_question_type, F.text)
async def handle_custom_question_type_text(message: types.Message, state: FSMContext) -> None:
    """استقبال نص تفضيل نوع الأسئلة الحر من الطالب، ثم الرجوع لشاشة الخيارات مع تفعيل
    الاختيار 'custom' وعرض النص المُدخَل بجانبه."""
    text = (message.text or "").strip()
    if len(text) > MAX_CUSTOM_QUESTION_TYPE_LENGTH:
        await message.answer(ERROR_CUSTOM_QUESTION_TYPE_TOO_LONG.format(max_len=MAX_CUSTOM_QUESTION_TYPE_LENGTH))
        return
    if not text:
        await message.answer(MSG_CUSTOM_QUESTION_TYPE_PROMPT)
        return

    await state.update_data(question_type=QUESTION_TYPE_CUSTOM, custom_question_type_text=text)
    await state.set_state(QuizState.waiting_for_quiz_options)

    data = await state.get_data()
    keyboard = get_quiz_options_keyboard(
        subject_type=data.get("subject_type", SUBJECT_OTHER),
        suggested_types=data.get("suggested_question_types", []),
        selected_type=QUESTION_TYPE_CUSTOM,
        selected_difficulty=data.get("difficulty", DIFFICULTY_MEDIUM),
    )
    await message.answer(
        f"✅ تم حفظ تفضيلك: <i>{text}</i>\n\n{MSG_QUIZ_OPTIONS_PROMPT}",
        parse_mode="HTML", reply_markup=keyboard,
    )


@router.callback_query(QuizState.waiting_for_quiz_options, F.data.startswith("qdiff_"))
async def handle_difficulty_selection(call: types.CallbackQuery, state: FSMContext) -> None:
    """اختيار مستوى الصعوبة (سهل/متوسط/متقدم)."""
    value = call.data.replace("qdiff_", "", 1)
    if value not in (DIFFICULTY_EASY, DIFFICULTY_MEDIUM, DIFFICULTY_ADVANCED):
        await call.answer()
        return
    await state.update_data(difficulty=value)
    await _refresh_options_screen(call, state)
    await call.answer()


@router.callback_query(QuizState.waiting_for_quiz_options, F.data == "quiz_options_continue")
async def handle_quiz_options_continue(call: types.CallbackQuery, state: FSMContext) -> None:
    """زر 'متابعة': ينهي شاشة الخيارات وينتقل لشاشة عدد الأسئلة بالإعدادات المختارة
    (أو الافتراضية 'عام/متوسط' لو الطالب لم يغيّر شيئاً)."""
    from handlers.files import _show_question_count_screen  # تفادي استيراد دائري (circular import)
    try:
        await _show_question_count_screen(call.message, state, edit=True)
    except Exception as exc:
        log_error(logger, f"Failed to proceed from quiz-options screen to count screen: {exc}", exception=exc)
        await call.message.answer("❌ حدث خطأ غير متوقع، حاول مجدداً.")
    finally:
        await call.answer()
