import asyncio
from aiogram import Router, types, F
from aiogram.fsm.context import FSMContext
from typing import Union, Optional

from config import QuizState
from constants import (
    MAX_FAVORITE_TITLE_LENGTH, MAX_FAVORITE_SECTIONS, DEFAULT_FAVORITE_SECTION_TITLE,
    MSG_FAVORITES_SEARCH_EMPTY, MSG_FAVORITE_NAME_PROMPT, MSG_FAVORITE_NAME_INVALID,
    MSG_FAVORITE_SECTION_PROMPT, MSG_FAVORITE_SECTION_CREATE, MSG_FAVORITE_SAVED,
    MSG_FAVORITES_SEARCH_PROMPT
)
from supabase_helper import (
    save_favorite_quiz, list_favorite_quizzes, list_favorite_sections,
    create_favorite_section, can_create_more_favorite_sections, get_favorite_quiz, remove_favorite_quiz
)
from keyboards import (
    get_favorites_keyboard, get_sections_actions_keyboard, get_favorite_section_keyboard
)
from logger import get_logger, log_error

# استيراد الوظائف المشتركة من ملف التنفيذ لمنع التعارض الدائري
from handlers.execution import _send_main_menu, send_question, _start_loaded_quiz

logger = get_logger(__name__)
router = Router()

def _build_favorites_text(favorites: list, sort_mode: str, search_query: str) -> str:
    lines = ["⭐ الكويزات المفضلة", f"📌 الفرز: {'حسب القسم' if sort_mode == 'section' else 'الأحدث'}"]
    if search_query:
        lines.append(f"🔎 البحث: {search_query}")
    lines.append("")

    if not favorites:
        lines.append(MSG_FAVORITES_SEARCH_EMPTY if search_query else "لا توجد كويزات محفوظة بعد.")
        return "\n".join(lines)

    current_section = None
    for index, favorite in enumerate(favorites, start=1):
        section_title = favorite.get("section_title") or DEFAULT_FAVORITE_SECTION_TITLE
        if sort_mode == "section" and section_title != current_section:
            current_section = section_title
            lines.append(f"📁 {section_title}")
        title = favorite.get("title") or "كويز محفوظ"
        if sort_mode == "section":
            lines.append(f"{index}. {title}")
        else:
            lines.append(f"{index}. {title} — {section_title}")
    return "\n".join(lines)

def _build_sections_text(sections: list, quiz_counts: dict[str, int]) -> str:
    lines = ["📁 أقسام المفضلة", ""]
    if not sections:
        lines.append("لا توجد أقسام محفوظة بعد.")
        return "\n".join(lines)
    for index, section in enumerate(sections, start=1):
        title = section.get("title") or DEFAULT_FAVORITE_SECTION_TITLE
        section_id = section.get("section_id")
        quiz_count = quiz_counts.get(section_id, 0)
        lines.append(f"{index}. {title} — {quiz_count} كويز")
    return "\n".join(lines)

async def _save_pending_favorite(target: Union[types.Message, types.CallbackQuery], state: FSMContext, section_id: Optional[str] = None) -> bool:
    data = await state.get_data()
    questions = data.get("questions", [])
    favorite_name = data.get("pending_favorite_name")
    source_title = data.get("source_title") or "كويز"

    if not questions or not favorite_name:
        if isinstance(target, types.CallbackQuery): await target.answer("❌ لا يمكن حفظ هذا الكويز حالياً", show_alert=True)
        else: await target.answer("❌ لا يمكن حفظ هذا الكويز حالياً")
        return False

    favorite_id = await asyncio.to_thread(save_favorite_quiz, target.from_user.id, favorite_name, questions, section_id, source_title)
    if not favorite_id:
        if isinstance(target, types.CallbackQuery): await target.answer("❌ تعذر حفظ الكويز في المفضلة", show_alert=True)
        else: await target.answer("❌ تعذر حفظ الكويز في المفضلة")
        return False

    await state.update_data(pending_favorite_name=None)
    await state.set_state(QuizState.answering_quiz)
    if isinstance(target, types.CallbackQuery): await target.message.answer(MSG_FAVORITE_SAVED)
    else: await target.answer(MSG_FAVORITE_SAVED)
    return True

async def _send_favorites_menu(target: Union[types.Message, types.CallbackQuery], state: FSMContext) -> None:
    data = await state.get_data()
    sort_mode = data.get("favorites_sort_mode", "latest")
    search_query = data.get("favorites_search_query", "")
    favorites = await asyncio.to_thread(list_favorite_quizzes, target.from_user.id, search_query or None, sort_mode)
    text = _build_favorites_text(favorites, sort_mode, search_query)
    keyboard = get_favorites_keyboard(favorites, sort_mode=sort_mode, search_query=search_query)

    if isinstance(target, types.CallbackQuery): await target.message.answer(text, reply_markup=keyboard)
    else: await target.answer(text, reply_markup=keyboard)

async def _send_sections_menu(target: Union[types.Message, types.CallbackQuery], state: FSMContext) -> None:
    sections = await asyncio.to_thread(list_favorite_sections, target.from_user.id)
    favorites = await asyncio.to_thread(list_favorite_quizzes, target.from_user.id, None, "latest")
    quiz_counts: dict[str, int] = {}
    for item in favorites:
        sid = item.get("section_id")
        if sid: quiz_counts[sid] = quiz_counts.get(sid, 0) + 1

    text = _build_sections_text(sections, quiz_counts)
    keyboard = get_sections_actions_keyboard()
    if isinstance(target, types.CallbackQuery): await target.message.answer(text, reply_markup=keyboard)
    else: await target.answer(text, reply_markup=keyboard)

@router.callback_query(F.data == "quiz_favorite")
async def favorite_quiz(call: types.CallbackQuery, state: FSMContext):
    try:
        data = await state.get_data()
        if not data.get("questions", []):
            await call.answer("❌ لا يوجد كويز لحفظه", show_alert=True)
            return
        await state.update_data(pending_favorite_name=None)
        await state.set_state(QuizState.saving_favorite_name)
        await call.message.answer(MSG_FAVORITE_NAME_PROMPT)
    except Exception as e:
        log_error(logger, f"Error in favorite_quiz: {e}", exception=e)
        await call.answer("❌ حدث خطأ أثناء حفظ الكويز", show_alert=True)
    finally:
        await call.answer()

@router.message(QuizState.saving_favorite_name, F.text)
async def process_favorite_name(msg: types.Message, state: FSMContext):
    try:
        name = msg.text.strip()
        if len(name) < 2 or len(name) > MAX_FAVORITE_TITLE_LENGTH:
            await msg.answer(MSG_FAVORITE_NAME_INVALID.format(max_len=MAX_FAVORITE_TITLE_LENGTH))
            return
        await state.update_data(pending_favorite_name=name)
        sections = await asyncio.to_thread(list_favorite_sections, msg.from_user.id)
        allow_new = can_create_more_favorite_sections(msg.from_user.id)
        await msg.answer(MSG_FAVORITE_SECTION_PROMPT, reply_markup=get_favorite_section_keyboard(sections, allow_new=allow_new, allow_default=True))
    except Exception as e:
        log_error(logger, f"Error in process_favorite_name: {e}", exception=e)
        await msg.answer("❌ تعذر متابعة حفظ الكويز")

@router.message(QuizState.saving_favorite_section_name, F.text)
async def process_favorite_section_name(msg: types.Message, state: FSMContext):
    try:
        s_name = msg.text.strip()
        if not s_name:
            await msg.answer("❌ اسم القسم لا يمكن أن يكون فارغًا")
            return
        if not can_create_more_favorite_sections(msg.from_user.id):
            await msg.answer(f"❌ وصلت للحد الأقصى وهو {MAX_FAVORITE_SECTIONS} قسمًا")
            await state.set_state(QuizState.answering_quiz)
            return
        sid = await asyncio.to_thread(create_favorite_section, msg.from_user.id, s_name)
        if not sid:
            await msg.answer("❌ تعذر إنشاء القسم")
            return
        if await _save_pending_favorite(msg, state, section_id=sid):
            await msg.answer(f"📁 تم إنشاء القسم وحفظ الكويز داخله: {s_name}")
    except Exception as e:
        log_error(logger, f"Error in process_favorite_section_name: {e}", exception=e)
        await msg.answer("❌ تعذر إنشاء القسم")

@router.callback_query(F.data.startswith("fav_section_"))
async def favorite_section_existing(call: types.CallbackQuery, state: FSMContext):
    try:
        sid = call.data.replace("fav_section_", "", 1)
        if sid == "new":
            if not can_create_more_favorite_sections(call.from_user.id):
                await call.answer(f"❌ وصلت للحد الأقصى وهو {MAX_FAVORITE_SECTIONS} قسمًا", show_alert=True)
                return
            await state.set_state(QuizState.saving_favorite_section_name)
            await call.message.answer(MSG_FAVORITE_SECTION_CREATE)
            return
        if sid == "default":
            await _save_pending_favorite(call, state, section_id=None)
            return
        await _save_pending_favorite(call, state, section_id=sid)
    except Exception as e:
        log_error(logger, f"Error in favorite_section_existing: {e}", exception=e)
        await call.answer("❌ تعذر حفظ الكويز", show_alert=True)
    finally:
        await call.answer()

@router.callback_query(F.data == "favorites_menu")
async def show_favorites_menu(call: types.CallbackQuery, state: FSMContext):
    try: await _send_favorites_menu(call, state)
    except Exception as e: log_error(logger, f"Error in show_favorites_menu: {e}"); await call.answer("❌ تعذر عرض المفضلة", show_alert=True)
    finally: await call.answer()

@router.callback_query(F.data == "sections_menu")
async def show_sections_menu(call: types.CallbackQuery, state: FSMContext):
    try: await _send_sections_menu(call, state)
    except Exception as e: log_error(logger, f"Error in show_sections_menu: {e}"); await call.answer("❌ تعذر عرض الأقسام", show_alert=True)
    finally: await call.answer()

@router.callback_query(F.data == "favorites_search")
async def favorites_search(call: types.CallbackQuery, state: FSMContext):
    try: await state.set_state(QuizState.searching_favorites); await call.message.answer(MSG_FAVORITES_SEARCH_PROMPT)
    finally: await call.answer()

@router.message(QuizState.searching_favorites, F.text)
async def process_favorites_search(msg: types.Message, state: FSMContext):
    try:
        await state.update_data(favorites_search_query=msg.text.strip())
        await state.set_state(None)
        await _send_favorites_menu(msg, state)
    except Exception as e: log_error(logger, f"Error in process_favorites_search: {e}"); await msg.answer("❌ تعذر البحث داخل المفضلة")

@router.callback_query(F.data == "favorites_clear_search")
async def favorites_clear_search(call: types.CallbackQuery, state: FSMContext):
    try: await state.update_data(favorites_search_query=""); await _send_favorites_menu(call, state)
    finally: await call.answer()

@router.callback_query(F.data == "favorites_sort_latest")
async def favorites_sort_latest(call: types.CallbackQuery, state: FSMContext):
    try: await state.update_data(favorites_sort_mode="latest"); await _send_favorites_menu(call, state)
    finally: await call.answer()

@router.callback_query(F.data == "favorites_sort_section")
async def favorites_sort_section(call: types.CallbackQuery, state: FSMContext):
    try: await state.update_data(favorites_sort_mode="section"); await _send_favorites_menu(call, state)
    finally: await call.answer()

@router.callback_query(F.data == "favorites_back")
async def favorites_back(call: types.CallbackQuery):
    try: await _send_main_menu(call, call.from_user.id)
    finally: await call.answer()

@router.callback_query(F.data.startswith("fav_open_"))
async def open_favorite_quiz(call: types.CallbackQuery, state: FSMContext):
    try:
        fid = call.data.replace("fav_open_", "", 1)
        favorite = await asyncio.to_thread(get_favorite_quiz, call.from_user.id, fid)
        if not favorite:
            await call.answer("❌ لم يتم العثور على هذا الكويز", show_alert=True)
            return
        await _start_loaded_quiz(call, state, favorite["quiz_data"], favorite.get("title") or "كويز محفوظ", origin="favorite")
    except Exception as e: log_error(logger, f"Error in open_favorite_quiz: {e}"); await call.answer("❌ تعذر فتح الكويز المحفوظ", show_alert=True)
    finally: await call.answer()

@router.callback_query(F.data.startswith("fav_del_"))
async def delete_favorite_quiz(call: types.CallbackQuery, state: FSMContext):
    try:
        fid = call.data.replace("fav_del_", "", 1)
        if await asyncio.to_thread(remove_favorite_quiz, call.from_user.id, fid):
            await call.message.answer("🗑 تم حذف الكويز من المفضلة.")
            await _send_favorites_menu(call, state)
        else: await call.answer("❌ تعذر حذف الكويز", show_alert=True)
    except Exception as e: log_error(logger, f"Error in delete_favorite_quiz: {e}"); await call.answer("❌ تعذر حذف الكويز", show_alert=True)
    finally: await call.answer()

    # في ملف favorites.py

@router.callback_query(F.data.startswith("fav_details_"))
async def show_favorite_details(call: types.CallbackQuery, state: FSMContext):
    try:
        fid = call.data.replace("fav_details_", "", 1)
        favorite = await asyncio.to_thread(get_favorite_quiz, call.from_user.id, fid)
        
        if not favorite:
            await call.answer("❌ الكويز غير موجود أو تم حذفه", show_alert=True)
            return
            
        title = favorite.get("title", "بدون عنوان")
        section = favorite.get("section_title", "عام")
        questions_count = len(favorite.get("quiz_data", []))
        
        details_text = (
            f"📑 **تفاصيل الكويز محفوظ:**\n\n"
            f"📌 **العنوان:** {title}\n"
            f"📁 **القسم:** {section}\n"
            f"🔢 **عدد الأسئلة:** {questions_count} أسئلة\n\n"
            f"ماذا تريد أن تفعل بهذا الكويز؟"
        )
        
        keyboard = get_favorite_details_keyboard(fid)
        await call.message.edit_text(details_text, reply_markup=keyboard)
        
    except Exception as e:
        log_error(logger, f"Error in show_favorite_details: {e}")
        await call.answer("❌ حدث خطأ", show_alert=True)
    finally:
        await call.answer()