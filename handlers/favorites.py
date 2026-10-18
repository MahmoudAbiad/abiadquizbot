import asyncio
from html import escape
from task_utils import spawn
from aiogram import Router, types, F
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from typing import Union, Optional

from config import QuizState
from constants import (
    DEFAULT_FAVORITE_SECTION_TITLE,
    MSG_FAVORITES_SEARCH_EMPTY, MSG_FAVORITES_SEARCH_PROMPT
)
from helpers.supabase_helper import (
    list_favorite_quizzes, list_favorite_sections,
    get_favorite_quiz, remove_favorite_quiz, log_usage_event,
    supabase # 🆕 تم استيراد كائن سوبابيس لعمل الاستعلام المباشر للـ UUID
)
from keyboards import (
    get_favorites_list_keyboard, get_sections_list_keyboard, 
    get_favorite_details_keyboard, get_delete_quiz_button_row
)
from logger import get_logger, log_error

# استيراد الوظائف المشتركة من ملف تشغيل الكويز لمنع التعارض الدائري
from handlers.quiz_runner import send_main_menu, start_loaded_quiz

logger = get_logger(__name__)
router = Router()
# 🩹 FIX (تسريب خصوصية): /favorites بيرد بلائحة كويزات المستخدم المحفوظة وأقسامه
# بنفس المحادثة اللي انكتب فيها - بلا هالفلتر أي عضو بغروب مضاف فيه البوت كان
# يقدر يكتبها ويكشف قائمته الخاصة لكل الغروب. راجع نفس الملاحظة بـ handlers/start.py.
router.message.filter(F.chat.type == "private")
router.callback_query.filter(F.message.chat.type == "private")

# ==================== بناء واجهات النصوص المصنفة ====================

def _build_favorites_text(favorites: list, sort_mode: str, search_query: str, page: int = 1, section_title: str = "") -> str:
    lines = ["⭐ <b>اختباراتي المحفوظة</b>"]
    if section_title:
        lines.append(f"📁 {escape(section_title[:120])}")
    if search_query:
        lines.append(f'🔎 البحث عن: «{escape(search_query[:120])}»')
    if not favorites:
        if search_query:
            lines.append("\nلا توجد اختبارات تطابق بحثك. جرّب كلمة أخرى أو امسح البحث.")
        elif section_title:
            lines.append("\nهذا القسم فارغ حالياً. يمكنك تصفّح بقية الأقسام أو عرض كل اختباراتك.")
        else:
            lines.append("\nاختباراتك المفضلة بتنتظرك هون ⭐\nبعد إنهاء أي اختبار، اضغط «حفظ بالمفضلة» لتعود إليه وقت المراجعة.")
    else:
        pages = max(1, (len(favorites) + 4) // 5)
        page = max(1, min(page, pages))
        lines.append(f"\n{len(favorites)} اختبار · صفحة {page} من {pages}")
        lines.append("اضغط اسم الاختبار لبدء الحل أو تحميل نسخة منه.")
    return "\n".join(lines)


def _build_sections_text(sections: list, quiz_counts: dict[str, int], page: int = 1) -> str:
    pages = max(1, (len(sections) + 5) // 6)
    page = max(1, min(page, pages))
    if not sections:
        return "📁 <b>أقسام اختباراتي</b>\n\nلا توجد أقسام بعد. عند حفظ اختبار جديد، يمكنك اختيار قسم أو إنشاء قسم."
    start = (page - 1) * 6
    # Only the current page is rendered, keeping long libraries within Telegram limits.
    names = " · ".join(escape((s.get("title") or DEFAULT_FAVORITE_SECTION_TITLE)[:35]) for s in sections[start:start + 6])
    return (f"📁 <b>أقسام اختباراتي</b>\n\n{len(sections)} قسم · صفحة {page} من {pages}\n"
            f"{names}\n\nاختر قسمك. الرقم بجانب كل قسم هو عدد اختباراته.")


async def _show_favorites_screen(target, text, keyboard):
    if isinstance(target, types.CallbackQuery):
        try:
            await target.message.edit_text(text, reply_markup=keyboard, parse_mode="HTML")
            return
        except TelegramBadRequest as exc:
            if "message is not modified" in str(exc).lower():
                return
        except (AttributeError, TypeError):
            pass
        await target.message.answer(text, reply_markup=keyboard, parse_mode="HTML")
    else:
        await target.answer(text, reply_markup=keyboard, parse_mode="HTML")


async def _end_favorites_search(state):
    if await state.get_state() == QuizState.searching_favorites.state:
        await state.set_state(None)


async def _send_favorites_menu(target: Union[types.Message, types.CallbackQuery], state: FSMContext, page: int = 1, section_filter: Optional[str] = None) -> None:
    await _end_favorites_search(state)
    data = await state.get_data()
    sort_mode = data.get("favorites_sort_mode", "latest")
    search_query = data.get("favorites_search_query", "")
    if section_filter is None:
        section_filter = data.get("favorites_section_filter", "")
    section_title = data.get("favorites_section_title", "") if section_filter else ""
    favorites = await list_favorite_quizzes(target.from_user.id, search_query or None, sort_mode)
    if section_filter:
        favorites = [f for f in favorites if
                     (not f.get("section_id") if section_filter == "general" else str(f.get("section_id")) == section_filter)]
    pages = max(1, (len(favorites) + 4) // 5)
    page = max(1, min(page, pages))
    await state.update_data(favorites_page=page, favorites_section_filter=section_filter)
    text = _build_favorites_text(favorites, sort_mode, search_query, page, section_title)
    keyboard = get_favorites_list_keyboard(favorites, current_page=page, page_size=5,
        sort_mode=sort_mode, search_query=search_query, section_filter=section_filter)
    await _show_favorites_screen(target, text, keyboard)


async def _send_sections_menu(target: Union[types.Message, types.CallbackQuery], state: FSMContext, page: int = 1) -> None:
    await _end_favorites_search(state)
    sections = await list_favorite_sections(target.from_user.id)
    favorites = await list_favorite_quizzes(target.from_user.id, None, "latest")
    quiz_counts = {}
    for item in favorites:
        sid = str(item.get("section_id") or "general")
        quiz_counts[sid] = quiz_counts.get(sid, 0) + 1
    if quiz_counts.get("general"):
        sections = [{"section_id": "general", "title": DEFAULT_FAVORITE_SECTION_TITLE}] + sections
    text = _build_sections_text(sections, quiz_counts, page)
    keyboard = get_sections_list_keyboard(sections, quiz_counts, page)
    await _show_favorites_screen(target, text, keyboard)


# ملاحظة: مسار حفظ الكويز إلى المفضلة (زر "quiz_favorite" / "save_quiz") أصبح
# مُدارًا بالكامل من handlers/quiz_runner.py (الويزارد بخطوتين: تسمية ثم اختيار قسم).
# كان هنا سابقاً مسار مواز مكرر لنفس الغرض لم يعد أي مستخدم يصله فعلياً بسبب
# ترتيب تسجيل الراوترات (quiz_runner_router مسجّل قبل favorites_router)، وكان
# استدعاؤه لـ save_favorite_quiz متأخراً عن آخر تعديل لتوقيع الدالة (بدون quiz_id).
# تم حذفه هنا لتفادي ازدواجية مصدر الحقيقة.

# ==================== معالجات التصفح والقوائم الذكية ====================

@router.message(Command("favorites"))
async def favorites_command(msg: types.Message, state: FSMContext):
    """🆕 نقطة دخول مباشرة من قائمة الأوامر الجانبية (زر Menu بجانب صندوق الكتابة)"""
    try:
        await state.update_data(favorites_section_filter="", favorites_section_title="", favorites_search_query="")
        await _send_favorites_menu(msg, state)
    except Exception as e:
        log_error(logger, f"Error in favorites_command: {e}")
        await msg.answer("❌ تعذر عرض القائمة المفضلة")

@router.callback_query(F.data.in_({"favorites_menu", "favorites_all"}))
async def show_favorites_menu(call: types.CallbackQuery, state: FSMContext):
    try:
        await state.update_data(favorites_section_filter="", favorites_section_title="", favorites_search_query="")
        await _send_favorites_menu(call, state)
    except Exception as e: 
        log_error(logger, f"Error in show_favorites_menu: {e}")
        await call.answer("❌ تعذر عرض القائمة المفضلة", show_alert=True)
    finally: 
        await call.answer()

@router.callback_query(F.data == "sections_menu")
async def show_sections_menu(call: types.CallbackQuery, state: FSMContext):
    try: 
        await _send_sections_menu(call, state)
    except Exception as e: 
        log_error(logger, f"Error in show_sections_menu: {e}")
        await call.answer("❌ تعذر استعراض الدليل المصنف للأقسام", show_alert=True)
    finally: 
        await call.answer()

@router.callback_query(F.data.startswith("fav_page_"))
async def favorites_page(call: types.CallbackQuery, state: FSMContext):
    try:
        page = int(call.data.replace("fav_page_", ""))
        await _send_favorites_menu(call, state, page=page)
    except Exception as e:
        log_error(logger, f"Error in pagination: {e}")
        await call.answer("❌ خطأ أثناء الانتقال بين الصفحات", show_alert=True)
    finally:
        await call.answer()

@router.callback_query(F.data.startswith("fav_sec_view_"))
async def view_section_favorites(call: types.CallbackQuery, state: FSMContext):
    try:
        section_id = call.data.replace("fav_sec_view_", "", 1)
        sections = await list_favorite_sections(call.from_user.id)
        title = DEFAULT_FAVORITE_SECTION_TITLE if section_id == "general" else next(
            (s["title"] for s in sections if str(s["section_id"]) == section_id), None)
        if title is None:
            await call.answer("هذا القسم لم يعد موجوداً. افتح الأقسام مجدداً.", show_alert=True)
            return
        await state.update_data(favorites_section_filter=section_id, favorites_section_title=title,
                                favorites_search_query="", favorites_sort_mode="latest")
        await _send_favorites_menu(call, state, page=1, section_filter=section_id)
    except Exception as e:
        log_error(logger, f"Error in view_section_favorites: {e}")
        await call.answer("❌ تعذر تصفح محتويات هذا القسم", show_alert=True)
    finally:
        await call.answer()

@router.callback_query(F.data == "favorites_resume")
@router.callback_query(F.data == "favorites_search_cancel")
async def resume_favorites(call: types.CallbackQuery, state: FSMContext):
    try:
        data = await state.get_data()
        await _send_favorites_menu(call, state, page=data.get("favorites_page", 1))
    finally:
        await call.answer()


@router.callback_query(F.data.startswith("fav_sections_"))
async def sections_page(call: types.CallbackQuery, state: FSMContext):
    try:
        await _send_sections_menu(call, state, page=int(call.data.replace("fav_sections_", "", 1)))
    finally:
        await call.answer()


@router.callback_query(F.data == "favorites_search")
async def favorites_search(call: types.CallbackQuery, state: FSMContext):
    current = await state.get_state()
    if current not in (None, QuizState.searching_favorites.state):
        await call.answer("أنهِ العملية الحالية أو ألغِها أولاً، ثم ابحث عن اختبار.", show_alert=True)
        return
    keyboard = types.InlineKeyboardMarkup(inline_keyboard=[[
        types.InlineKeyboardButton(text="🔙 إلغاء البحث", callback_data="favorites_search_cancel")]])
    await _show_favorites_screen(call,
        "🔎 <b>ابحث عن اختبار محفوظ</b>\n\nأرسل كلمة من اسم الاختبار أو المادة.\nسيظهر البحث ضمن القائمة التي تتصفّحها.",
        keyboard)
    await state.set_state(QuizState.searching_favorites)
    await call.answer()


@router.message(QuizState.searching_favorites, F.text, ~F.text.startswith("/"))
async def process_favorites_search(msg: types.Message, state: FSMContext):
    try:
        query = msg.text.strip()
        if not query or len(query) > 120:
            await msg.answer("أرسل كلمة أو عبارة قصيرة للبحث (حتى 120 حرفاً).")
            return
        await state.update_data(favorites_search_query=query)
        await _end_favorites_search(state)
        await _send_favorites_menu(msg, state)
    except Exception as e:
        log_error(logger, f"Error in process_favorites_search: {e}")
        await msg.answer("❌ تعذر البحث. حاول مجدداً.")


@router.callback_query(F.data == "favorites_clear_search")
async def favorites_clear_search(call: types.CallbackQuery, state: FSMContext):
    try: 
        await state.update_data(favorites_search_query="")
        await _send_favorites_menu(call, state)
    finally: 
        await call.answer()

@router.callback_query(F.data == "favorites_sort_latest")
async def favorites_sort_latest(call: types.CallbackQuery, state: FSMContext):
    try: 
        await state.update_data(favorites_sort_mode="latest")
        await _send_favorites_menu(call, state)
    finally: 
        await call.answer()

@router.callback_query(F.data == "favorites_sort_section")
async def favorites_sort_section(call: types.CallbackQuery, state: FSMContext):
    try: 
        await state.update_data(favorites_sort_mode="section")
        await _send_favorites_menu(call, state)
    finally: 
        await call.answer()

@router.callback_query(F.data == "favorites_back")
async def favorites_back(call: types.CallbackQuery):
    try: 
        await send_main_menu(call, call.from_user.id)
    finally: 
        await call.answer()

# ==================== إدارة وعرض تفاصيل الكويز داخل المفضلة ====================

@router.callback_query(F.data.startswith("fav_details_"))
async def show_favorite_details(call: types.CallbackQuery, state: FSMContext):
    try:
        fid = call.data.replace("fav_details_", "", 1)
        favorite = await get_favorite_quiz(call.from_user.id, fid)
        
        if not favorite:
            await call.answer("❌ الكويز غير موجود أو تم حذفه مسبقاً", show_alert=True)
            return
            
        title = favorite.get("title") or favorite.get("source_title") or "كويز محفوظ"
        section_id = favorite.get("section_id")
        sections = await list_favorite_sections(call.from_user.id)
        section_title = next((s["title"] for s in sections if str(s["section_id"]) == str(section_id)),
                             DEFAULT_FAVORITE_SECTION_TITLE)
        questions_count = len(favorite.get("quiz_data") or [])
        
        details_text = (
            f"📘 <b>{escape(title[:180])}</b>\n\n"
            f"📁 {escape(section_title[:120])}\n"
            f"📝 {questions_count} سؤال\n\n"
            "جاهز للمراجعة؟ ابدأ الحل، أو حمّل نسخة للدراسة."
        )

        # تمرير الـ section_id لزر الرجوع الذكي ليعود الطالب لنفس المجلد بدلاً من تشتيته
        keyboard = get_favorite_details_keyboard(
            fid, section_id=section_id,
            quiz_id=favorite.get("quiz_id"), viewer_id=call.from_user.id,
            creator_id=favorite.get("creator_id"),
        )
        await _show_favorites_screen(call, details_text, keyboard)
        
    except Exception as e:
        log_error(logger, f"Error in show_favorite_details: {e}")
        await call.answer("❌ حدث خطأ أثناء جلب تفاصيل السجل", show_alert=True)
    finally:
        await call.answer()

@router.callback_query(F.data.startswith("fav_open_"))
async def open_favorite_quiz(call: types.CallbackQuery, state: FSMContext):
    try:
        fid = call.data.replace("fav_open_", "", 1)
        favorite = await get_favorite_quiz(call.from_user.id, fid)
        if not favorite:
            await call.answer("❌ لم يتم العثور على ملفات هذا الاختبار", show_alert=True)
            return
            
        # 🆕 استخراج الـ UUID المركزي الحقيقي المرتبط بجدول المفضلة لضمان عمل القيود وجدول النتائج بسلاسة
        actual_quiz_id = None
        fav_res = await supabase.table("favorite_quizzes").select("quiz_id").eq("favorite_id", fid).execute()
        if fav_res.data:
            actual_quiz_id = fav_res.data[0]["quiz_id"]
            
        await start_loaded_quiz(
            call, state, favorite["quiz_data"], 
            favorite.get("title") or "كويز محفوظ", 
            origin="favorite",
            quiz_id=str(actual_quiz_id) if actual_quiz_id else "" # تمرير المعرف الفريد للمركزية
        )
        spawn(log_usage_event(call.from_user.id, "favorite_opened", {"favorite_id": fid}))
    except Exception as e: 
        log_error(logger, f"Error in open_favorite_quiz: {e}")
        await call.answer("❌ تعذر تشغيل واختبار هذا الكويز حالياً", show_alert=True)
    finally: 
        await call.answer()

@router.callback_query(F.data.startswith("fav_manage_"))
async def manage_favorite(call: types.CallbackQuery):
    try:
        fid = call.data.replace("fav_manage_", "", 1)
        favorite = await get_favorite_quiz(call.from_user.id, fid)
        if not favorite:
            await call.answer("الاختبار غير موجود في قائمتك.", show_alert=True)
            return
        rows = [[types.InlineKeyboardButton(text="🗑 إزالة من اختباراتي", callback_data=f"fav_remove_{fid}", style="danger")]]
        rows.extend(get_delete_quiz_button_row(favorite.get("quiz_id"), call.from_user.id, favorite.get("creator_id")))
        for row in rows:
            for button in row:
                button.style = "danger"
        rows.append([types.InlineKeyboardButton(text="🔙 رجوع للاختبار", callback_data=f"fav_details_{fid}")])
        text = (f"⚙️ <b>إدارة الاختبار</b>\n\n{escape((favorite.get('title') or 'اختبار محفوظ')[:180])}\n\n"
                "«إزالة من اختباراتي» تزيله من قائمتك الشخصية فقط.")
        await _show_favorites_screen(call, text, types.InlineKeyboardMarkup(inline_keyboard=rows))
    finally:
        await call.answer()


@router.callback_query(F.data.startswith("fav_remove_"))
async def confirm_remove_favorite(call: types.CallbackQuery):
    try:
        fid = call.data.replace("fav_remove_", "", 1)
        favorite = await get_favorite_quiz(call.from_user.id, fid)
        if not favorite:
            await call.answer("الاختبار غير موجود في قائمتك.", show_alert=True)
            return
        keyboard = types.InlineKeyboardMarkup(inline_keyboard=[
            [types.InlineKeyboardButton(text="🗑 نعم، إزالة من قائمتي", callback_data=f"fav_del_{fid}", style="danger")],
            [types.InlineKeyboardButton(text="🔙 الاحتفاظ بالاختبار", callback_data=f"fav_details_{fid}")],
        ])
        await _show_favorites_screen(call,
            f"🗑 <b>إزالة هذا الاختبار؟</b>\n\n{escape((favorite.get('title') or 'اختبار محفوظ')[:180])}\n\n"
            "ستُزال نسخته من قائمتك الشخصية. لن يُحذف من حسابات الآخرين.", keyboard)
    finally:
        await call.answer()


@router.callback_query(F.data.startswith("fav_del_"))
async def delete_favorite_quiz(call: types.CallbackQuery, state: FSMContext):
    try:
        fid = call.data.replace("fav_del_", "", 1)
        if await remove_favorite_quiz(call.from_user.id, fid):
            await call.answer("تمت إزالة الاختبار من قائمتك.")
            spawn(log_usage_event(call.from_user.id, "favorite_deleted", {"favorite_id": fid}))
            data = await state.get_data()
            await _send_favorites_menu(call, state, page=data.get("favorites_page", 1))
        else: 
            await call.answer("❌ تعذر إتمام طلب الحذف", show_alert=True)
    except Exception as e: 
        log_error(logger, f"Error in delete_favorite_quiz: {e}")
        await call.answer("❌ تعذر حذف الملف حالياً", show_alert=True)
    finally: 
        await call.answer()

favorites_router = router
