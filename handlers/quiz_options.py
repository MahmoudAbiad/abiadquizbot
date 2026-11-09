# handlers/quiz_options.py
"""
==============================================================================
MODULE: Quiz Settings & Options Screens (قائمة إعدادات الاختبار وشاشاتها الفرعية)
==============================================================================
🆕 بعد توحيد تدفق الإنشاء في شاشة ملخص واحدة ذات إعدادات افتراضية مسبقة و3 أزرار
فقط («🚀 إنشاء الاختبار»، «⚙️ إعدادات الاختبار»، «❌ إلغاء الطلب»)، يدير هذا الراوتر:
- شاشة قائمة «⚙️ إعدادات الاختبار» (qsetup_menu)
- شاشة اختيار عدد الأسئلة (qsetup_open_count)
- شاشة اختيار نوع الأسئلة (qback_to_type / qtype_*) + النص الحر (qtype_custom)
- شاشة اختيار مستوى الصعوبة (qback_to_difficulty / qdiff_*)
- شاشة اختيار لغة الأسئلة / الترجمة (qsetup_open_translation)
- شاشة اختيار مسار الاختبار الجاهز (qsetup_open_extraction)

اختيار أي قيمة من أي شاشة فرعية يحفظ الإعداد ويعيد الطالب مباشرة إلى شاشة الملخص
الموحدة ليرى الإعدادات المحدثة والتكلفة ويبدأ الاختبار بضغطة واحدة.
==============================================================================
"""

from typing import Optional
from aiogram import F, Router, types
from aiogram.filters import StateFilter
from aiogram.fsm.context import FSMContext
from html import escape

from config import QuizState
from constants import (
    BTN_CANCEL_REQUEST, DIFFICULTY_ADVANCED, DIFFICULTY_EASY, DIFFICULTY_MEDIUM, DIFFICULTY_PROGRESSIVE,
    DIFFICULTY_LABELS_AR, ERROR_CUSTOM_QUESTION_TYPE_TOO_LONG, MAX_CUSTOM_QUESTION_TYPE_LENGTH,
    MSG_CUSTOM_QUESTION_TYPE_PROMPT, MSG_QUIZ_DIFFICULTY_PROMPT, MSG_QUIZ_SETTINGS_MENU, MSG_QUIZ_TYPE_PROMPT,
    MSG_ENGLISH_CONTENT_DETECTED, MSG_FRENCH_CONTENT_DETECTED,
    QUESTION_TYPE_CUSTOM, QUESTION_TYPE_GENERAL, SUBJECT_OTHER,
    QUESTION_TYPE_SIMILAR_MATH, MSG_SIMILAR_MATH_NOTICE, SUBJECT_MATH,
    SUBJECT_ENGLISH, SUBJECT_FRENCH,
    SUBJECT_QUIZ_SOLVED, SUBJECT_QUIZ_UNSOLVED,
    QUIZ_EXTRACTION_MODE_AS_IS, QUIZ_EXTRACTION_MODE_AI_SOLVE, QUIZ_EXTRACTION_MODE_LABELS_AR,
)
from keyboards import (
    get_question_count_keyboard, get_quiz_difficulty_keyboard, get_quiz_settings_menu_keyboard,
    get_quiz_type_keyboard, get_translation_choice_keyboard,
)
from logger import get_logger, log_error
from handlers.files import (
    DEFAULT_COUNT_SUGGESTIONS, PENDING_REQUEST_STATES,
    _proceed_to_quiz_solved_screen, _proceed_to_quiz_unsolved_screen,
    show_question_count_screen,
)

logger = get_logger(__name__)
router = Router()


def _effective_subject(data: dict) -> str:
    subject_type = data.get("subject_type", SUBJECT_OTHER)
    content_subject = data.get("content_subject_type")
    if subject_type in (SUBJECT_QUIZ_SOLVED, SUBJECT_QUIZ_UNSOLVED) and content_subject:
        return content_subject
    return subject_type


def _type_display_label(data: dict) -> str:
    """يبني نص عرض النوع المختار لعرضه في قائمة الإعدادات وشاشة الصعوبة."""
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


def _language_display_label(data: dict) -> Optional[str]:
    eff = _effective_subject(data)
    if eff not in (SUBJECT_ENGLISH, SUBJECT_FRENCH):
        return None
    return "🔤 مترجمة للعربية" if data.get("english_mode") == "translated" else "🔠 بدون ترجمة"


def _extraction_display_label(data: dict) -> str:
    subject_type = data.get("subject_type", SUBJECT_OTHER)
    if data.get("question_type") == QUESTION_TYPE_SIMILAR_MATH:
        from constants import BTN_SIMILAR_MATH
        return BTN_SIMILAR_MATH
    if subject_type not in (SUBJECT_QUIZ_SOLVED, SUBJECT_QUIZ_UNSOLVED):
        return "✨ توليد أسئلة جديدة"
    default_mode = QUIZ_EXTRACTION_MODE_AS_IS if subject_type == SUBJECT_QUIZ_SOLVED else QUIZ_EXTRACTION_MODE_AI_SOLVE
    mode = data.get("quiz_extraction_mode") or default_mode
    return QUIZ_EXTRACTION_MODE_LABELS_AR.get(mode, QUIZ_EXTRACTION_MODE_LABELS_AR[default_mode])


async def _render_quiz_settings_menu(target: types.Message, state: FSMContext, edit: bool = True) -> None:
    """يعرض قائمة «⚙️ إعدادات الاختبار» بجميع الخيارات المتاحة للمحتوى الحالي."""
    data = await state.get_data()
    subject_type = data.get("subject_type", SUBJECT_OTHER)
    detected_subject = data.get("detected_subject_type") or (
        subject_type if subject_type in (SUBJECT_QUIZ_SOLVED, SUBJECT_QUIZ_UNSOLVED) else None
    )
    ready_exam = (
        subject_type in (SUBJECT_QUIZ_SOLVED, SUBJECT_QUIZ_UNSOLVED)
        and data.get("question_type") != QUESTION_TYPE_SIMILAR_MATH
    )
    has_ready_exam_origin = detected_subject in (SUBJECT_QUIZ_SOLVED, SUBJECT_QUIZ_UNSOLVED)
    selected_count = int(data.get("selected_question_count") or (0 if ready_exam else DEFAULT_COUNT_SUGGESTIONS[1]))
    difficulty = data.get("difficulty", DIFFICULTY_MEDIUM)
    diff_label = DIFFICULTY_LABELS_AR.get(difficulty, difficulty)

    keyboard = get_quiz_settings_menu_keyboard(
        ready_exam=ready_exam,
        has_ready_exam_origin=has_ready_exam_origin,
        selected_count=selected_count,
        type_label=_type_display_label(data),
        difficulty_label=diff_label,
        extraction_label=_extraction_display_label(data),
        language_label=_language_display_label(data),
    )
    await state.set_state(QuizState.waiting_for_count)
    if edit:
        try:
            await target.edit_text(MSG_QUIZ_SETTINGS_MENU, parse_mode="HTML", reply_markup=keyboard)
            return
        except Exception as exc:
            if "message is not modified" in str(exc).lower():
                return
    await target.answer(MSG_QUIZ_SETTINGS_MENU, parse_mode="HTML", reply_markup=keyboard)


async def _render_count_picker_screen(target: types.Message, state: FSMContext, edit: bool = True) -> None:
    """يعرض شاشة اختيار عدد الأسئلة الفرعية (5 / 10 / 15 / 20 / عدد مخصص)."""
    data = await state.get_data()
    items = int(data.get("items_count") or 1)
    is_album = bool(data.get("is_album"))
    selected_count = int(data.get("selected_question_count") or DEFAULT_COUNT_SUGGESTIONS[1])
    prompt_text = data.get("count_prompt_text") or "📝 اختر عدد الأسئلة المطلوب أو اكتب عدداً مخصصاً:"
    text = f"🔢 <b>عدد الأسئلة</b>\n\n{prompt_text}"
    keyboard = get_question_count_keyboard(items, is_album, selected_count, DEFAULT_COUNT_SUGGESTIONS)
    await state.set_state(QuizState.waiting_for_count)
    if edit:
        await target.edit_text(text, parse_mode="HTML", reply_markup=keyboard)
    else:
        await target.answer(text, parse_mode="HTML", reply_markup=keyboard)


async def _render_type_screen(target: types.Message, state: FSMContext, edit: bool) -> None:
    data = await state.get_data()
    if data.get("subject_type") in (SUBJECT_QUIZ_SOLVED, SUBJECT_QUIZ_UNSOLVED) and data.get("content_subject_type") == SUBJECT_MATH and data.get("question_type") != QUESTION_TYPE_SIMILAR_MATH:
        show = _proceed_to_quiz_solved_screen if data["subject_type"] == SUBJECT_QUIZ_SOLVED else _proceed_to_quiz_unsolved_screen
        await show(target, state, edit=edit)
        return
    eff_subject = _effective_subject(data) if data.get("subject_type") in (SUBJECT_QUIZ_SOLVED, SUBJECT_QUIZ_UNSOLVED) else data.get("subject_type", SUBJECT_OTHER)
    keyboard = get_quiz_type_keyboard(
        subject_type=eff_subject,
        suggested_types=data.get("suggested_question_types", []),
        selected_type=data.get("question_type", QUESTION_TYPE_GENERAL),
    )
    if edit:
        await target.edit_text(MSG_QUIZ_TYPE_PROMPT, parse_mode="HTML", reply_markup=keyboard)
    else:
        await target.answer(MSG_QUIZ_TYPE_PROMPT, parse_mode="HTML", reply_markup=keyboard)


async def _render_difficulty_screen(target: types.Message, state: FSMContext, edit: bool) -> None:
    data = await state.get_data()
    text = MSG_QUIZ_DIFFICULTY_PROMPT.format(type_label=escape(_type_display_label(data)))
    if data.get("question_type") == QUESTION_TYPE_SIMILAR_MATH:
        text += "\n\n" + MSG_SIMILAR_MATH_NOTICE
    keyboard = get_quiz_difficulty_keyboard(selected_difficulty=data.get("difficulty", DIFFICULTY_MEDIUM))
    if edit:
        await target.edit_text(text, parse_mode="HTML", reply_markup=keyboard)
    else:
        await target.answer(text, parse_mode="HTML", reply_markup=keyboard)


async def _render_translation_screen(target: types.Message, state: FSMContext, edit: bool = True) -> None:
    data = await state.get_data()
    eff_subject = _effective_subject(data)
    detected_msg = MSG_FRENCH_CONTENT_DETECTED if eff_subject == SUBJECT_FRENCH else MSG_ENGLISH_CONTENT_DETECTED
    keyboard = get_translation_choice_keyboard(eff_subject, selected_mode=data.get("english_mode") or "plain")
    await state.set_state(QuizState.waiting_for_translation_choice)
    if edit:
        await target.edit_text(detected_msg, parse_mode="HTML", reply_markup=keyboard)
    else:
        await target.answer(detected_msg, parse_mode="HTML", reply_markup=keyboard)


# ==================== فتح قائمة إعدادات الاختبار والشاشات الفرعية ====================

@router.callback_query(StateFilter(*PENDING_REQUEST_STATES), F.data == "qsetup_menu")
async def handle_open_quiz_settings_menu(call: types.CallbackQuery, state: FSMContext) -> None:
    """فتح قائمة «⚙️ إعدادات الاختبار» من شاشة الملخص أو الرجوع إليها من شاشة فرعية."""
    try:
        await _render_quiz_settings_menu(call.message, state, edit=True)
    except Exception as exc:
        log_error(logger, f"Failed to open quiz settings menu: {exc}", exception=exc)
    finally:
        await call.answer()


@router.callback_query(StateFilter(*PENDING_REQUEST_STATES), F.data == "qsetup_open_count")
async def handle_open_count_picker(call: types.CallbackQuery, state: FSMContext) -> None:
    """فتح شاشة اختيار عدد الأسئلة من قائمة الإعدادات."""
    try:
        await _render_count_picker_screen(call.message, state, edit=True)
    except Exception as exc:
        log_error(logger, f"Failed to open count picker screen: {exc}", exception=exc)
    finally:
        await call.answer()


@router.callback_query(StateFilter(*PENDING_REQUEST_STATES), F.data == "qsetup_open_translation")
async def handle_open_translation_picker(call: types.CallbackQuery, state: FSMContext) -> None:
    """فتح شاشة اختيار لغة الأسئلة / الترجمة من قائمة الإعدادات."""
    try:
        await _render_translation_screen(call.message, state, edit=True)
    except Exception as exc:
        log_error(logger, f"Failed to open translation screen: {exc}", exception=exc)
    finally:
        await call.answer()


@router.callback_query(StateFilter(*PENDING_REQUEST_STATES), F.data == "qsetup_open_extraction")
async def handle_open_extraction_picker(call: types.CallbackQuery, state: FSMContext) -> None:
    """فتح شاشة اختيار مسار الاختبار الجاهز (أجوبة المصدر / حل بالذكاء الاصطناعي / توليد جديد)."""
    try:
        data = await state.get_data()
        detected_subject = data.get("detected_subject_type") or data.get("subject_type")
        if detected_subject == SUBJECT_QUIZ_SOLVED:
            await _proceed_to_quiz_solved_screen(call.message, state, edit=True)
        elif detected_subject == SUBJECT_QUIZ_UNSOLVED:
            await _proceed_to_quiz_unsolved_screen(call.message, state, edit=True)
        else:
            await call.answer("هذا الخيار مخصص للاختبارات الجاهزة فقط.", show_alert=True)
            return
    except Exception as exc:
        log_error(logger, f"Failed to open extraction screen: {exc}", exception=exc)
    finally:
        await call.answer()


# ==================== اختيار نوع الأسئلة ====================

@router.callback_query(StateFilter(*PENDING_REQUEST_STATES), F.data.startswith("qtype_"))
async def handle_type_screen(call: types.CallbackQuery, state: FSMContext) -> None:
    """
    معالج موحّد لكل أزرار نوع الأسئلة (قيمة ثابتة / "general" / "custom").
    يعيد الطالب مباشرة إلى شاشة الملخص الموحدة بعد اختيار النوع (أو يفتح إدخال النص الحر لـ custom).
    """
    value = call.data.replace("qtype_", "", 1)
    try:
        if value == QUESTION_TYPE_SIMILAR_MATH:
            data = await state.get_data()
            if (data.get("content_subject_type") or data.get("subject_type")) != SUBJECT_MATH:
                return
            await state.update_data(
                quiz_extraction_mode=None,
                quiz_detection_notice=None,
                selected_question_count=None,
                review_count_to_restore=None,
                count_prompt_text="🧩 كم مسألة جديدة تريد توليدها بنفس النمط؟",
            )
        else:
            await state.update_data(count_prompt_text="📝 كم سؤالاً تريد توليده من هذا المحتوى؟")
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
            await state.update_data(question_type=value, custom_question_type_text=None)
            await show_question_count_screen(call.message, state, edit=True)
    except Exception as exc:
        log_error(logger, f"Quiz type screen handling failed: {exc}", exception=exc)
    finally:
        await call.answer()


@router.callback_query(F.data == "qback_to_type")
async def handle_back_to_type_screen(call: types.CallbackQuery, state: FSMContext) -> None:
    """فتح شاشة نوع الأسئلة من قائمة الإعدادات أو الرجوع إليها من خطوة إدخال النص المخصص."""
    await state.set_state(QuizState.waiting_for_quiz_options)
    await _render_type_screen(call.message, state, edit=True)
    await call.answer()


# ==================== خطوة النص الحر (تفضيل خاص) ====================

@router.message(QuizState.waiting_for_custom_question_type, F.text)
async def handle_custom_question_type_text(message: types.Message, state: FSMContext) -> None:
    """استقبال نص تفضيل نوع الأسئلة الحر، ثم العودة مباشرة لشاشة الملخص الموحدة."""
    text = (message.text or "").strip()
    if not text:
        await message.answer(MSG_CUSTOM_QUESTION_TYPE_PROMPT)
        return
    if len(text) > MAX_CUSTOM_QUESTION_TYPE_LENGTH:
        await message.answer(ERROR_CUSTOM_QUESTION_TYPE_TOO_LONG.format(max_len=MAX_CUSTOM_QUESTION_TYPE_LENGTH))
        return

    await state.update_data(question_type=QUESTION_TYPE_CUSTOM, custom_question_type_text=text)
    await show_question_count_screen(message, state, edit=False)


# ==================== اختيار الصعوبة ====================

@router.callback_query(StateFilter(*PENDING_REQUEST_STATES), F.data == "qback_to_difficulty")
async def back_to_difficulty(call: types.CallbackQuery, state: FSMContext):
    data = await state.get_data()
    if data.get("subject_type") in (SUBJECT_QUIZ_SOLVED, SUBJECT_QUIZ_UNSOLVED) and data.get("question_type") != QUESTION_TYPE_SIMILAR_MATH:
        await call.answer("هذا الاختبار لا يحتاج اختيار الصعوبة.", show_alert=True)
        return
    await state.update_data(review_count_to_restore=data.get("selected_question_count"))
    await state.set_state(QuizState.waiting_for_quiz_options)
    await _render_difficulty_screen(call.message, state, edit=True)
    await call.answer()


@router.callback_query(StateFilter(*PENDING_REQUEST_STATES), F.data.startswith("qdiff_"))
async def handle_difficulty_screen(call: types.CallbackQuery, state: FSMContext) -> None:
    """اختيار الصعوبة وحفظها ثم العودة مباشرة لشاشة الملخص الموحدة."""
    value = call.data.replace("qdiff_", "", 1)
    if value not in (DIFFICULTY_EASY, DIFFICULTY_MEDIUM, DIFFICULTY_ADVANCED, DIFFICULTY_PROGRESSIVE):
        await call.answer()
        return
    await state.update_data(difficulty=value)

    try:
        await show_question_count_screen(call.message, state, edit=True)
    except Exception as exc:
        log_error(logger, f"Failed to proceed from difficulty screen to summary screen: {exc}", exception=exc)
        await call.message.answer("❌ حدث خطأ غير متوقع، حاول مجدداً.")
    finally:
        await call.answer()
