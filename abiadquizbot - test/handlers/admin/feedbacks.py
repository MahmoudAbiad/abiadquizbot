# handlers/admin/feedbacks.py
from typing import Optional, Dict
from aiogram import Router, types, F
from aiogram.fsm.context import FSMContext

from supabase_helper import (
    admin_get_feedbacks_page,
    admin_get_feedback_by_id,
    admin_get_quiz_board_position,
    admin_get_quiz_by_id,
    admin_delete_quiz
)
from keyboards import get_admin_dashboard_keyboard
from logger import get_logger
from handlers.quiz_runner import _start_loaded_quiz
from .dashboard import IsAdminFilter, safe_edit_text

logger = get_logger(__name__)
router = Router()

# تطبيق فلتر حماية الإدارة على الراوتر
router.message.filter(IsAdminFilter())
router.callback_query.filter(IsAdminFilter())

FEEDBACKS_PAGE_SIZE = 5


def _student_label(student: Optional[Dict]) -> str:
    """تنسيق اسم الطالب المشتكي."""
    if not student:
        return "طالب غير معروف"
    name = " ".join(filter(None, [student.get("first_name"), student.get("last_name")])).strip()
    return name or (f"@{student['username']}" if student.get("username") else "بدون اسم")


def get_feedbacks_list_keyboard(feedbacks: list, page: int, total: int) -> types.InlineKeyboardMarkup:
    """بناء لوحة أزرار الملاحظات المصفحة."""
    kb = []
    for fb in feedbacks:
        quiz = fb.get("quizzes") or {}
        source = (quiz.get("source_title") or "ملف غير معروف")[:28]
        student_name = _student_label(fb.get("student"))[:18]
        kb.append([types.InlineKeyboardButton(
            text=f"📝 {source} — {student_name}",
            callback_data=f"afb_v_{fb['id']}"
        )])

    nav_row = []
    if page > 1:
        nav_row.append(types.InlineKeyboardButton(text="◀️ السابق", callback_data=f"afb_p_{page - 1}"))
    total_pages = max(1, -(-total // FEEDBACKS_PAGE_SIZE))
    if page < total_pages:
        nav_row.append(types.InlineKeyboardButton(text="التالي ▶️", callback_data=f"afb_p_{page + 1}"))
    if nav_row:
        kb.append(nav_row)

    kb.append([types.InlineKeyboardButton(text="⚙️ لوحة التحكم", callback_data="admin_main_menu")])
    return types.InlineKeyboardMarkup(inline_keyboard=kb)


def get_feedback_details_keyboard(feedback_id: int, quiz_id) -> types.InlineKeyboardMarkup:
    """أزرار التحكم بالملاحظة (تجربة الكويز / الحذف / الرجوع)."""
    kb = []
    if quiz_id:
        kb.append([types.InlineKeyboardButton(text="🎯 خوض الكويز لتجربته", callback_data=f"afb_try_{quiz_id}")])
        kb.append([types.InlineKeyboardButton(text="🗑 حذف الكويز نهائياً", callback_data=f"afb_del_{quiz_id}_{feedback_id}")])
    kb.append([types.InlineKeyboardButton(text="🔙 رجوع للقائمة", callback_data="afb_p_1")])
    return types.InlineKeyboardMarkup(inline_keyboard=kb)


async def _render_feedback_list(page: int):
    """جلب وتنسيق عرض صفحة الملاحظات."""
    offset = (page - 1) * FEEDBACKS_PAGE_SIZE
    feedbacks, total = await admin_get_feedbacks_page(limit=FEEDBACKS_PAGE_SIZE, offset=offset)
    if total == 0:
        return "📭 لا توجد أي ملاحظات أو شكاوى مسجلة من الطلاب حالياً.", get_admin_dashboard_keyboard()
    total_pages = max(1, -(-total // FEEDBACKS_PAGE_SIZE))
    text = f"📋 <b>ملاحظات الكويزات</b> — صفحة {page}/{total_pages} (الإجمالي: {total})\n\nاختر ملاحظة لعرض تفاصيلها الكاملة:"
    return text, get_feedbacks_list_keyboard(feedbacks, page, total)


# ==================== تفاعلات الأزرار ====================

@router.callback_query(F.data == "admin_view_feedbacks")
async def admin_callback_view_feedbacks(call: types.CallbackQuery):
    try:
        text, keyboard = await _render_feedback_list(1)
        await safe_edit_text(call.message, text, reply_markup=keyboard)
    except Exception as e:
        logger.error(f"Error in admin_view_feedbacks: {e}")
        await call.answer("❌ حدث خطأ داخلي أثناء جلب الملاحظات.", show_alert=True)
    finally:
        await call.answer()


@router.callback_query(F.data.startswith("afb_p_"))
async def admin_callback_feedbacks_page(call: types.CallbackQuery):
    try:
        page = int(call.data.replace("afb_p_", "", 1))
        text, keyboard = await _render_feedback_list(page)
        await safe_edit_text(call.message, text, reply_markup=keyboard)
    except Exception as e:
        logger.error(f"Error paginating feedbacks: {e}")
        await call.answer("❌ حدث خطأ أثناء التنقل بين الصفحات.", show_alert=True)
    finally:
        await call.answer()


@router.callback_query(F.data.startswith("afb_v_"))
async def admin_callback_feedback_details(call: types.CallbackQuery):
    try:
        feedback_id = int(call.data.replace("afb_v_", "", 1))
        fb = await admin_get_feedback_by_id(feedback_id)
        if not fb:
            await call.answer("❌ هذه الملاحظة لم تعد موجودة (ربما حُذف الكويز المرتبط بها).", show_alert=True)
            return

        quiz = fb.get("quizzes") or {}
        quiz_id = quiz.get("id") or fb.get("quiz_id")
        source_title = quiz.get("source_title") or "غير معروف"
        file_hash = quiz.get("file_hash")
        student = fb.get("student") or {}
        student_id = fb.get("user_id")
        student_name = _student_label(student)

        board_no, board_total = (0, 0)
        if quiz_id and file_hash:
            board_no, board_total = await admin_get_quiz_board_position(file_hash, quiz_id)
        board_line = f"#{board_no} من أصل {board_total}" if board_no else "غير متاح (الكويز محذوف أو منفرد)"

        details = (
            "📋 <b>تفاصيل الملاحظة</b>\n\n"
            f"📄 <b>الملف المصدر:</b> <code>{source_title}</code>\n"
            f"🗂 <b>ترتيب الكويز في لوحة هذا الملف:</b> {board_line}\n"
            f"👤 <b>الطالب:</b> {student_name}\n"
            f"🆔 <b>معرف الطالب:</b> <code>{student_id}</code> (اضغط الزر أدناه لمراسلته مباشرة)\n\n"
            f"📝 <b>نص الملاحظة:</b>\n<i>{fb['comment']}</i>"
        )

        kb = get_feedback_details_keyboard(feedback_id, quiz_id)
        # إدراج زر مباشر لمحادثة الطالب في تيليجرام
        kb.inline_keyboard.insert(0, [types.InlineKeyboardButton(
            text=f"💬 مراسلة {student_name}",
            url=f"tg://user?id={student_id}"
        )])

        await safe_edit_text(call.message, details, reply_markup=kb)
    except Exception as e:
        logger.error(f"Error showing feedback details: {e}")
        await call.answer("❌ حدث خطأ أثناء جلب تفاصيل الملاحظة.", show_alert=True)
    finally:
        await call.answer()


@router.callback_query(F.data.startswith("afb_try_"))
async def admin_callback_try_quiz(call: types.CallbackQuery, state: FSMContext):
    try:
        quiz_id = call.data.replace("afb_try_", "", 1)
        quiz = await admin_get_quiz_by_id(quiz_id)
        if not quiz or not quiz.get("quiz_data"):
            await call.answer("❌ تعذر تحميل بيانات هذا الكويز، قد يكون محذوفاً.", show_alert=True)
            return
        await call.answer("🎯 جاري تشغيل الكويز لتجربته...")
        await _start_loaded_quiz(
            call, state, quiz["quiz_data"],
            quiz.get("source_title") or "كويز (تجربة إدارية)",
            origin="admin_test",
            quiz_id=str(quiz_id)
        )
    except Exception as e:
        logger.error(f"Error starting admin quiz trial: {e}")
        await call.answer("❌ تعذر بدء تجربة الكويز.", show_alert=True)


@router.callback_query(F.data.startswith("afb_del_"))
async def admin_callback_delete_quiz_confirm(call: types.CallbackQuery):
    try:
        raw = call.data.replace("afb_del_", "", 1)
        quiz_id, feedback_id = raw.rsplit("_", 1)
        kb = types.InlineKeyboardMarkup(inline_keyboard=[
            [types.InlineKeyboardButton(text="✅ نعم، احذف نهائياً", callback_data=f"afb_delok_{quiz_id}_{feedback_id}")],
            [types.InlineKeyboardButton(text="◀️ تراجع", callback_data=f"afb_v_{feedback_id}")]
        ])
        await safe_edit_text(
            call.message,
            "⚠️ <b>تأكيد الحذف</b>\n\nسيتم حذف هذا الكويز نهائياً من قاعدة البيانات، بما في ذلك كل التصويتات والنقاط وعناصر المفضلة المرتبطة به لكل الطلاب. هذا الإجراء لا يمكن التراجع عنه.\n\nهل أنت متأكد؟",
            reply_markup=kb
        )
    except Exception as e:
        logger.error(f"Error preparing delete confirmation: {e}")
        await call.answer("❌ حدث خطأ.", show_alert=True)
    finally:
        await call.answer()


@router.callback_query(F.data.startswith("afb_delok_"))
async def admin_callback_delete_quiz_execute(call: types.CallbackQuery):
    try:
        raw = call.data.replace("afb_delok_", "", 1)
        quiz_id, _feedback_id = raw.rsplit("_", 1)
        success = await admin_delete_quiz(quiz_id)
        if success:
            await call.answer("🗑️ تم حذف الكويز نهائياً.", show_alert=True)
            text, keyboard = await _render_feedback_list(1)
            await safe_edit_text(call.message, text, reply_markup=keyboard)
        else:
            await call.answer("❌ تعذر حذف الكويز، حاول مجدداً.", show_alert=True)
    except Exception as e:
        logger.error(f"Error deleting quiz from feedback panel: {e}")
        await call.answer("❌ حدث خطأ أثناء الحذف.", show_alert=True)