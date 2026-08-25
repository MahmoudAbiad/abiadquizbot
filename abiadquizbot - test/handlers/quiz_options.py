# handlers/quiz_options.py
"""
==============================================================================
MODULE: Quiz Options Screen (شاشة اختيار نوع الأسئلة + الصعوبة)
==============================================================================
🆕 يدير هذا الراوتر شاشة الخيارات - مرحلتان متتاليتان بنفس الرسالة (edit_text) بدل
شاشة واحدة مزدحمة بكل الأزرار معاً:

  المرحلة 1 (نوع الأسئلة) → المرحلة 2 (الصعوبة) → شاشة عدد الأسئلة

اختيار أي زر بأي مرحلة ينقل تلقائياً للمرحلة التالية مباشرة - بدون زر "متابعة"
منفصل - لتقليل عدد الضغطات وعدد الأزرار الظاهرة بنفس الوقت.

⚠️ ملاحظة تصميم مهمة (سبب خلل سابق تم إصلاحه): كل أزرار "qtype_*" (بما فيها
"qtype_custom" و"qtype_general") تُعالَج بمعالج واحد فقط (handle_type_screen
أدناه) عبر فحص القيمة داخلياً بـ if/elif، بدل معالجات متعددة مسجّلة بفلاتر
متداخلة (F.data.startswith("qtype_") و F.data == "qtype_custom" معاً) - لأن
aiogram ينفّذ أول معالج مطابق بترتيب التسجيل فقط ويتوقف، فأي تداخل بين فلترين
كان يعني أن المعالج الأعم (startswith) يبتلع ضغطات المعالج الأخص (== الدقيق)
قبل ما توصله أبداً، بغض النظر عن ترتيب كتابتها بالملف.
==============================================================================
"""

from aiogram import F, Router, types
from aiogram.fsm.context import FSMContext

from config import QuizState
from constants import (
    BTN_CANCEL_REQUEST, DIFFICULTY_ADVANCED, DIFFICULTY_EASY, DIFFICULTY_MEDIUM, DIFFICULTY_PROGRESSIVE,
    DIFFICULTY_LABELS_AR, ERROR_CUSTOM_QUESTION_TYPE_TOO_LONG, MAX_CUSTOM_QUESTION_TYPE_LENGTH,
    MSG_CUSTOM_QUESTION_TYPE_PROMPT, MSG_QUIZ_DIFFICULTY_PROMPT, MSG_QUIZ_TYPE_PROMPT,
    QUESTION_TYPE_CUSTOM, QUESTION_TYPE_GENERAL, SUBJECT_OTHER,
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
        return "🔀 متنوع"
    from constants import QUESTION_TYPE_OPTIONS
    for subj_options in QUESTION_TYPE_OPTIONS.values():
        for value, label in subj_options:
            if value == question_type:
                return label
    return "🔀 متنوع"


async def _render_type_screen(target: types.Message, state: FSMContext, edit: bool) -> None:
    data = await state.get_data()
    keyboard = get_quiz_type_keyboard(
        subject_type=data.get("subject_type", SUBJECT_OTHER),
        suggested_types=data.get("suggested_question_types", []),
        selected_type=data.get("question_type", QUESTION_TYPE_GENERAL),
    )
    if edit:
        await target.edit_text(MSG_QUIZ_TYPE_PROMPT, parse_mode="HTML", reply_markup=keyboard)
    else:
        await target.answer(MSG_QUIZ_TYPE_PROMPT, parse_mode="HTML", reply_markup=keyboard)


async def _render_difficulty_screen(target: types.Message, state: FSMContext, edit: bool) -> None:
    data = await state.get_data()
    text = MSG_QUIZ_DIFFICULTY_PROMPT.format(type_label=_type_display_label(data))
    keyboard = get_quiz_difficulty_keyboard(selected_difficulty=data.get("difficulty", DIFFICULTY_MEDIUM))
    if edit:
        await target.edit_text(text, parse_mode="HTML", reply_markup=keyboard)
    else:
        await target.answer(text, parse_mode="HTML", reply_markup=keyboard)


# ==================== المرحلة 1: اختيار نوع الأسئلة ====================

@router.callback_query(QuizState.waiting_for_quiz_options, F.data.startswith("qtype_"))
async def handle_type_screen(call: types.CallbackQuery, state: FSMContext) -> None:
    """
    معالج موحّد لكل أزرار مرحلة النوع (قيمة ثابتة / "general" / "custom"). راجع
    ملاحظة التصميم أعلى الملف لسبب توحيدها بمعالج واحد بدل معالجات متعددة.
    """
    value = call.data.replace("qtype_", "", 1)
    try:
        if value == "custom":
            await state.set_state(QuizState.waiting_for_custom_question_type)
            await call.message.edit_text(
                MSG_CUSTOM_QUESTION_TYPE_PROMPT,
                reply_markup=types.InlineKeyboardMarkup(inline_keyboard=[
                    [types.InlineKeyboardButton(text="🔙 رجوع", callback_data="qback_to_type")],
                    [types.InlineKeyboardButton(text=BTN_CANCEL_REQUEST, callback_data="cancel_upload_request")],
                ]),
            )
        else:
            # value == "general" أو قيمة ثابتة (problems/grammar/...) أو "other_<index>"
            await state.update_data(question_type=value, custom_question_type_text=None)
            await _render_difficulty_screen(call.message, state, edit=True)
    except Exception as exc:
        log_error(logger, f"Quiz type screen handling failed: {exc}", exception=exc)
    finally:
        await call.answer()


@router.callback_query(F.data == "qback_to_type")
async def handle_back_to_type_screen(call: types.CallbackQuery, state: FSMContext) -> None:
    """رجوع لمرحلة النوع - سواء من مرحلة الصعوبة أو من خطوة إدخال النص المخصص،
    بدون فقدان أي اختيار سابق (question_type يبقى محفوظاً بالحالة)."""
    await state.set_state(QuizState.waiting_for_quiz_options)
    await _render_type_screen(call.message, state, edit=True)
    await call.answer()


# ==================== خطوة النص الحر (تفضيل خاص) ====================

@router.message(QuizState.waiting_for_custom_question_type, F.text)
async def handle_custom_question_type_text(message: types.Message, state: FSMContext) -> None:
    """استقبال نص تفضيل نوع الأسئلة الحر، ثم الانتقال مباشرة لمرحلة الصعوبة."""
    text = (message.text or "").strip()
    if not text:
        await message.answer(MSG_CUSTOM_QUESTION_TYPE_PROMPT)
        return
    if len(text) > MAX_CUSTOM_QUESTION_TYPE_LENGTH:
        await message.answer(ERROR_CUSTOM_QUESTION_TYPE_TOO_LONG.format(max_len=MAX_CUSTOM_QUESTION_TYPE_LENGTH))
        return

    await state.update_data(question_type=QUESTION_TYPE_CUSTOM, custom_question_type_text=text)
    await state.set_state(QuizState.waiting_for_quiz_options)
    # 🆕 هذا المسار الوحيد الذي يرسل رسالة جديدة بدل تعديل القائمة (لأن مصدر الحدث رسالة
    # نصية من الطالب وليس ضغطة زر على رسالة البوت نفسها) - باقي التدفق كله edit_text.
    await _render_difficulty_screen(message, state, edit=False)


# ==================== المرحلة 2: اختيار الصعوبة ====================

@router.callback_query(QuizState.waiting_for_quiz_options, F.data.startswith("qdiff_"))
async def handle_difficulty_screen(call: types.CallbackQuery, state: FSMContext) -> None:
    """اختيار الصعوبة = الخطوة الأخيرة، تنتقل مباشرة لشاشة عدد الأسئلة."""
    value = call.data.replace("qdiff_", "", 1)
    if value not in (DIFFICULTY_EASY, DIFFICULTY_MEDIUM, DIFFICULTY_ADVANCED, DIFFICULTY_PROGRESSIVE):
        await call.answer()
        return
    await state.update_data(difficulty=value)

    from handlers.files import _show_question_count_screen  # تفادي استيراد دائري (circular import)
    try:
        await _show_question_count_screen(call.message, state, edit=True)
    except Exception as exc:
        log_error(logger, f"Failed to proceed from difficulty screen to count screen: {exc}", exception=exc)
        await call.message.answer("❌ حدث خطأ غير متوقع، حاول مجدداً.")
    finally:
        await call.answer()
