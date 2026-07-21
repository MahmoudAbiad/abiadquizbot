import asyncio
from aiogram import Router, types, F
from aiogram.filters import Command
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
    create_favorite_section, can_create_more_favorite_sections, get_favorite_quiz, 
    remove_favorite_quiz, supabase # 🆕 تم استيراد كائن سوبابيس لعمل الاستعلام المباشر للـ UUID
)
from keyboards import (
    get_favorites_list_keyboard, get_sections_list_keyboard, 
    get_favorite_section_keyboard, get_favorite_details_keyboard
)
from logger import get_logger, log_error

# استيراد الوظائف المشتركة من ملف التنفيذ لمنع التعارض الدائري
from handlers.execution import _send_main_menu, _start_loaded_quiz

logger = get_logger(__name__)
router = Router()

# ==================== بناء واجهات النصوص المصنفة ====================

def _build_favorites_text(favorites: list, sort_mode: str, search_query: str) -> str:
    """تنسيق بVisual Breadcrumbs مريح لعين الطالب أثناء التصفح"""
    lines = ["⭐ <b>قائمتي المفضلة المنظمة</b>\n"]
    lines.append(f"📌 الفرز الحالي: {'حسب القسم' if sort_mode == 'section' else 'الأحدث'}")
    
    if search_query:
        lines.append(f"🔎 تصفية البحث: {search_query}")
    lines.append("")

    if not favorites:
        lines.append(MSG_FAVORITES_SEARCH_EMPTY if search_query else "لا توجد كويزات محفوظة بعد في حسابك.")
    else:
        lines.append("اختر الاختبار المطلوب من القائمة أدناه لعرض تفاصيله أو بدئه:")
        
    return "\n".join(lines)

def _build_sections_text(sections: list, quiz_counts: dict[str, int]) -> str:
    """عرض إحصائيات حية شفافة لعدد ملفات كل قسم أكاديمي"""
    lines = ["📁 <b>الأقسام الأكاديمية المفضلة</b>\n"]
    if not sections:
        lines.append("لا توجد أقسام محفوظة ومخصصة بعد.")
        return "\n".join(lines)
        
    lines.append("📊 إحصائيات الملفات الحالية:")
    for section in sections:
        title = section.get("title") or DEFAULT_FAVORITE_SECTION_TITLE
        section_id = section.get("section_id")
        quiz_count = quiz_counts.get(section_id, 0)
        lines.append(f"🔹 {title} — يحتوي على ({quiz_count}) كويز")
        
    lines.append("\nاضغط على اسم القسم لاستعراض وحل اختباراته:")
    return "\n".join(lines)

# ==================== دوال الإرسال والتحديث الأساسية ====================

async def _save_pending_favorite(target: Union[types.Message, types.CallbackQuery], state: FSMContext, section_id: Optional[str] = None) -> bool:
    data = await state.get_data()
    questions = data.get("questions", [])
    favorite_name = data.get("pending_favorite_name")
    source_title = data.get("source_title") or "كويز"

    if not questions or not favorite_name:
        if isinstance(target, types.CallbackQuery): 
            await target.answer("❌ لا يمكن حفظ هذا الكويز حالياً", show_alert=True)
        else: 
            await target.answer("❌ لا يمكن حفظ هذا الكويز حالياً")
        return False

    favorite_id = await save_favorite_quiz(target.from_user.id, favorite_name, questions, section_id, source_title)
    if not favorite_id:
        if isinstance(target, types.CallbackQuery): 
            await target.answer("❌ تعذر حفظ الكويز في المفضلة", show_alert=True)
        else: 
            await target.answer("❌ تعذر حفظ الكويز في المفضلة")
        return False

    await state.update_data(pending_favorite_name=None)
    await state.set_state(QuizState.answering_quiz)
    if isinstance(target, types.CallbackQuery): 
        await target.message.answer(MSG_FAVORITE_SAVED)
    else: 
        await target.answer(MSG_FAVORITE_SAVED)
    return True

async def _send_favorites_menu(target: Union[types.Message, types.CallbackQuery], state: FSMContext, page: int = 1, section_filter: Optional[str] = None) -> None:
    data = await state.get_data()
    sort_mode = data.get("favorites_sort_mode", "latest")
    search_query = data.get("favorites_search_query", "")
    
    favorites = await list_favorite_quizzes(target.from_user.id, search_query or None, sort_mode)
    
    if section_filter:
        favorites = [f for f in favorites if str(f.get("section_id")) == str(section_filter)]
        sort_mode = "section" 
        
    text = _build_favorites_text(favorites, sort_mode, search_query)
    keyboard = get_favorites_list_keyboard(favorites, current_page=page, page_size=5, sort_mode=sort_mode, search_query=search_query)

    if isinstance(target, types.CallbackQuery):
        try:
            await target.message.edit_text(text, reply_markup=keyboard, parse_mode="HTML")
        except Exception:
            await target.message.answer(text, reply_markup=keyboard, parse_mode="HTML")
    else:
        await target.answer(text, reply_markup=keyboard, parse_mode="HTML")

async def _send_sections_menu(target: Union[types.Message, types.CallbackQuery], state: FSMContext) -> None:
    sections = await list_favorite_sections(target.from_user.id)
    favorites = await list_favorite_quizzes(target.from_user.id, None, "latest")
    quiz_counts: dict[str, int] = {}
    for item in favorites:
        sid = item.get("section_id")
        if sid: 
            quiz_counts[sid] = quiz_counts.get(sid, 0) + 1

    text = _build_sections_text(sections, quiz_counts)
    keyboard = get_sections_list_keyboard(sections)
    
    if isinstance(target, types.CallbackQuery):
        try:
            await target.message.edit_text(text, reply_markup=keyboard, parse_mode="HTML")
        except Exception:
            await target.message.answer(text, reply_markup=keyboard, parse_mode="HTML")
    else:
        await target.answer(text, reply_markup=keyboard, parse_mode="HTML")

# ==================== معالجات الحفظ والتصنيف ====================

@router.callback_query(F.data == "quiz_favorite")
async def favorite_quiz(call: types.CallbackQuery, state: FSMContext):
    try:
        data = await state.get_data()
        if not data.get("questions", []):
            await call.answer("❌ لا يوجد كويز نشط لحفظه حالياً", show_alert=True)
            return
        await state.update_data(pending_favorite_name=None)
        await state.set_state(QuizState.saving_favorite_name)
        await call.message.answer(MSG_FAVORITE_NAME_PROMPT)
    except Exception as e:
        log_error(logger, f"Error in favorite_quiz: {e}", exception=e)
        await call.answer("❌ حدث خطأ داخلي أثناء معالجة الحفظ", show_alert=True)
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
        sections = await list_favorite_sections(msg.from_user.id)
        allow_new = await can_create_more_favorite_sections(msg.from_user.id)
        await msg.answer(MSG_FAVORITE_SECTION_PROMPT, reply_markup=get_favorite_section_keyboard(sections, allow_new=allow_new, allow_default=True))
    except Exception as e:
        log_error(logger, f"Error in process_favorite_name: {e}", exception=e)
        await msg.answer("❌ تعذر استكمال خطوات حفظ الكويز")

@router.message(QuizState.saving_favorite_section_name, F.text)
async def process_favorite_section_name(msg: types.Message, state: FSMContext):
    try:
        s_name = msg.text.strip()
        if not s_name:
            await msg.answer("❌ اسم القسم الدراسي لا يمكن أن يكون فارغاً")
            return
        if not await can_create_more_favorite_sections(msg.from_user.id):
            await msg.answer(f"❌ وصلت للحد الأقصى المسموح به وهو {MAX_FAVORITE_SECTIONS} قسماً")
            await state.set_state(QuizState.answering_quiz)
            return
        sid = await create_favorite_section(msg.from_user.id, s_name)
        if not sid:
            await msg.answer("❌ تعذر بناء وإنشاء القسم الدراسي الجديد")
            return
        if await _save_pending_favorite(msg, state, section_id=sid):
            await msg.answer(f"📁 تم إنشاء القسم وحفظ الكويز داخله بنجاح: {s_name}")
    except Exception as e:
        log_error(logger, f"Error in process_favorite_section_name: {e}", exception=e)
        await msg.answer("❌ حدث خطأ أثناء إنشاء القسم")

@router.callback_query(F.data.startswith("fav_section_"))
async def favorite_section_existing(call: types.CallbackQuery, state: FSMContext):
    try:
        sid = call.data.replace("fav_section_", "", 1)
        if sid == "new":
            if not await can_create_more_favorite_sections(call.from_user.id):
                await call.answer(f"❌ وصلت للحد الأقصى وهو {MAX_FAVORITE_SECTIONS} قسماً", show_alert=True)
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
        await call.answer("❌ تعذر إتمام حفظ الكويز", show_alert=True)
    finally:
        await call.answer()

# ==================== معالجات التصفح والقوائم الذكية ====================

@router.message(Command("favorites"))
async def favorites_command(msg: types.Message, state: FSMContext):
    """🆕 نقطة دخول مباشرة من قائمة الأوامر الجانبية (زر Menu بجانب صندوق الكتابة)"""
    try:
        await _send_favorites_menu(msg, state)
    except Exception as e:
        log_error(logger, f"Error in favorites_command: {e}")
        await msg.answer("❌ تعذر عرض القائمة المفضلة")

@router.callback_query(F.data == "favorites_menu")
async def show_favorites_menu(call: types.CallbackQuery, state: FSMContext):
    try: 
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
        section_id = call.data.replace("fav_sec_view_", "")
        await _send_favorites_menu(call, state, page=1, section_filter=section_id)
    except Exception as e:
        log_error(logger, f"Error in view_section_favorites: {e}")
        await call.answer("❌ تعذر تصفح محتويات هذا القسم", show_alert=True)
    finally:
        await call.answer()

@router.callback_query(F.data == "favorites_search")
async def favorites_search(call: types.CallbackQuery, state: FSMContext):
    try: 
        await state.set_state(QuizState.searching_favorites)
        await call.message.answer(MSG_FAVORITES_SEARCH_PROMPT)
    finally: 
        await call.answer()

@router.message(QuizState.searching_favorites, F.text)
async def process_favorites_search(msg: types.Message, state: FSMContext):
    try:
        await state.update_data(favorites_search_query=msg.text.strip())
        await state.set_state(None)
        await _send_favorites_menu(msg, state)
    except Exception as e: 
        log_error(logger, f"Error in process_favorites_search: {e}")
        await msg.answer("❌ تعذر إجراء الفلترة والبحث")

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
        await _send_main_menu(call, call.from_user.id)
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
        section_title = favorite.get("section_title") or DEFAULT_FAVORITE_SECTION_TITLE
        section_id = favorite.get("section_id")  # قراءة معرّف القسم لتغذية زر الرجوع السياقي
        questions_count = len(favorite.get("quiz_data", []))
        
        details_text = (
            f"📑 <b>تفاصيل الكويز المحفوظ:</b>\n\n"
            f"📌 <b>العنوان:</b> {title}\n"
            f"📁 <b>القسم الأكاديمي:</b> {section_title}\n"
            f"🔢 <b>إجمالي الأسئلة:</b> {questions_count} أسئلة تفاعلية\n\n"
            f"ماذا تريد أن تفعل بهذا الكويز حالياً؟"
        )
        
        # تمرير الـ section_id لزر الرجوع الذكي ليعود الطالب لنفس المجلد بدلاً من تشتيته
        keyboard = get_favorite_details_keyboard(fid, section_id=section_id)
        await call.message.edit_text(details_text, reply_markup=keyboard, parse_mode="HTML")
        
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
            
        await _start_loaded_quiz(
            call, state, favorite["quiz_data"], 
            favorite.get("title") or "كويز محفوظ", 
            origin="favorite",
            quiz_id=str(actual_quiz_id) if actual_quiz_id else "" # تمرير المعرف الفريد للمركزية
        )
    except Exception as e: 
        log_error(logger, f"Error in open_favorite_quiz: {e}")
        await call.answer("❌ تعذر تشغيل واختبار هذا الكويز حالياً", show_alert=True)
    finally: 
        await call.answer()

@router.callback_query(F.data.startswith("fav_del_"))
async def delete_favorite_quiz(call: types.CallbackQuery, state: FSMContext):
    try:
        fid = call.data.replace("fav_del_", "", 1)
        if await remove_favorite_quiz(call.from_user.id, fid):
            await call.answer("🗑️ تم مسح الاختبار من قائمتك المفضلة وتفريغ مساحته.", show_alert=True)
            await _send_favorites_menu(call, state)
        else: 
            await call.answer("❌ تعذر إتمام طلب الحذف", show_alert=True)
    except Exception as e: 
        log_error(logger, f"Error in delete_favorite_quiz: {e}")
        await call.answer("❌ تعذر حذف الملف حالياً", show_alert=True)
    finally: 
        await call.answer()

favorites_router = router