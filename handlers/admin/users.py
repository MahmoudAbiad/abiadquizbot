# handlers/admin/users.py
import asyncio
import io
import csv
import json
from typing import Optional, Dict
from datetime import datetime, timezone, timedelta
from constants import SYRIA_TZ, USER_QUIZZES_PAGE_SIZE
from supabase_helper import admin_get_user_quizzes
from aiogram import Router, types, F
from aiogram.filters import Command, CommandObject
from aiogram.fsm.context import FSMContext
from aiogram.types import BufferedInputFile
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError, TelegramRetryAfter

from config import bot
from supabase_helper import (
    supabase, 
    admin_add_points, 
    admin_get_global_stats, 
    admin_search_user
)
from keyboards import (
    get_admin_dashboard_keyboard,
    get_admin_user_actions_keyboard,
    get_admin_charge_options_keyboard,
    get_cancel_keyboard
)
from logger import get_logger
from .dashboard import AdminState, IsAdminFilter, safe_edit_text

logger = get_logger(__name__)
router = Router()

# تطبيق فلتر حماية الإدارة على الراوتر
router.message.filter(IsAdminFilter())
router.callback_query.filter(IsAdminFilter())


def user_total_points(user: dict) -> float:
    """حساب إجمالي رصيد الطالب من الخانتين المجانية والمدفوعة."""
    return float(user.get("free_points") or 0) + float(user.get("paid_points") or 0)


async def fetch_users_async():
    """جلب سجل الطلاب المسجلين مرتبين من الأحدث بناءً على joined_at مباشرة."""
    try:
        res = await supabase.table("users").select("*").order("joined_at", desc=True).execute()
        return res.data or []
    except Exception as e:
        logger.error(f"Error fetching users: {e}")
        return []


async def send_points_notification(target_id: int, amount: int, new_balance: int):
    """إرسال إشعار تفاعلي للطالب بعد عملية شحن الرصيد."""
    try:
        user_text = (
            "🎉 <b>أخبار سارة! تم تحديث رصيدك</b>\n\n"
            f"عزيزي الطالب، تم إضافة <code>{amount}</code> نقاط جديدة إلى حسابك بنجاح. ✨\n\n"
            f"🟢 <b>الكمية المضافة:</b> <code>+{amount}</code>\n"
            f"💰 <b>رصيدك الحالي أصبح:</b> <code>{new_balance}</code> نقطة\n\n"
            "نتمنى لك رحلة تعليمية ممتعة ومليئة بالتوفيق والنجاح! 🚀"
        )
        await bot.send_message(chat_id=target_id, text=user_text, parse_mode="HTML")
        logger.info(f"Notification sent successfully to user {target_id}")
    except Exception as e:
        logger.error(f"Could not send notification to user {target_id}: {e}")


def sanitize_csv_value(val) -> str:
    """تأمين القيم لتفادي ثغرة CSV Injection عند فتح التقرير في Excel."""
    val_str = str(val) if val is not None else ""
    if val_str.startswith(('=', '+', '-', '@')):
        return f"'{val_str}"
    return val_str


async def render_users_page(event, page: int = 1):
    """عرض قائمة الطلاب المصفحة مع معالجة آمنة لـ Callbacks."""
    if isinstance(event, types.CallbackQuery):
        try:
            await event.answer()
        except TelegramBadRequest:
            pass

    users = await fetch_users_async()
    if not users:
        text = "📭 لا يوجد أي طلاب مسجلين حالياً."
        if isinstance(event, types.Message):
            await event.answer(text, reply_markup=get_admin_dashboard_keyboard(), parse_mode="HTML")
        elif isinstance(event, types.CallbackQuery):
            await safe_edit_text(event.message, text, reply_markup=get_admin_dashboard_keyboard())
        return

    total_users = len(users)
    per_page = 5
    total_pages = max(1, (total_users + per_page - 1) // per_page)
    
    page = max(1, min(page, total_pages))
    start_idx = (page - 1) * per_page
    end_idx = start_idx + per_page
    page_users = users[start_idx:end_idx]
    
    report = f"👥 <b>سجل الطلاب المسجلين ({page} من {total_pages}):</b>\n\n"
    for idx, u in enumerate(page_users, start=start_idx + 1):
        username_str = f"@{u['username']}" if u.get('username') and u['username'] != "Unknown" else "بدون يوزر"
        joined_time = format_syria_time(u.get('joined_at'))
        report += (
            f"<b>{idx}. آيدي:</b> <code>{u['user_id']}</code>\n"
            f"┣ 👤 اليوزر: {username_str}\n"
            f"┣ 📝 الاسم: <b>{u.get('first_name', 'Unknown')} {u.get('last_name', 'Unknown')}</b>\n"
            f"┣ 🕒 انضم: <code>{joined_time}</code>\n"
            f"┣ 🎁 المجاني: <code>{float(u.get('free_points') or 0):.2f}</code>\n"
            f"┣ 💳 المدفوع: <code>{float(u.get('paid_points') or 0):.2f}</code>\n"
            f"┣ 💰 الإجمالي: <code>{user_total_points(u):.2f}</code>\n"
            f"┗ 📊 الأسئلة: <code>{u.get('total_questions', 0)}</code>\n"
            f"──────────────────\n"
        )
        
    kb = []
    nav_buttons = []
    if page > 1:
        nav_buttons.append(types.InlineKeyboardButton(text="⬅️ السابق", callback_data=f"admin_users_page_{page-1}"))
    if page < total_pages:
        nav_buttons.append(types.InlineKeyboardButton(text="التالي ➡️", callback_data=f"admin_users_page_{page+1}"))
    if nav_buttons:
        kb.append(nav_buttons)
        
    kb.append([types.InlineKeyboardButton(text="📥 تصدير هذه القائمة كاملة كـ CSV", callback_data="admin_export_users")])
    kb.append([types.InlineKeyboardButton(text="⚙️ لوحة التحكم الرئيسية", callback_data="admin_main_menu")])
    reply_markup = types.InlineKeyboardMarkup(inline_keyboard=kb)
    
    if isinstance(event, types.Message):
        await event.answer(report, reply_markup=reply_markup, parse_mode="HTML")
    elif isinstance(event, types.CallbackQuery):
        await safe_edit_text(event.message, report, reply_markup=reply_markup)


# ==================== الأوامر النصية ====================

@router.message(Command("searchuser"))
async def admin_cmd_search(msg: types.Message, state: FSMContext):
    await state.set_state(AdminState.waiting_for_search_query)
    await msg.answer("🔍 <b>بحث عن مستخدم</b>\n\nأرسل الآن (الآيدي ID) أو (معرف المستخدم @Username):", reply_markup=get_cancel_keyboard(), parse_mode="HTML")


@router.message(Command("dbstats"))
async def admin_cmd_stats(msg: types.Message):
    stats = await admin_get_global_stats()
    text = f"📊 <b>إحصائيات النظام الحية:</b>\n\n👥 إجمالي الطلاب: <code>{stats['total_users']}</code>\n📝 إجمالي الأسئلة: <code>{stats['total_questions']}</code>\n"
    await msg.answer(text, reply_markup=get_admin_dashboard_keyboard(), parse_mode="HTML")


@router.message(Command("fetchall"))
async def admin_cmd_fetchall(msg: types.Message):
    await render_users_page(msg, page=1)


@router.message(Command("charge"))
async def admin_cmd_charge_direct(msg: types.Message, command: CommandObject, state: FSMContext):
    if command.args:
        target_id = command.args.strip()
        if target_id.isdigit():
            await msg.answer(
                f"💰 <b>شحن رصيد للمستخدم</b> <code>{target_id}</code>\n\nاختر كمية شحن سريعة أو إدخال يدوي:", 
                reply_markup=get_admin_charge_options_keyboard(int(target_id)), 
                parse_mode="HTML"
            )
            return

    await state.set_state(AdminState.waiting_for_search_query)
    await msg.answer(
        "💡 <b>طريقة الشحن السريع للمطور:</b>\n"
        "يمكنك إرسال الأمر متبوعاً بآيدي الطالب مباشرة، مثل:\n"
        "<code>/charge 12345678</code>\n\n"
        "🔍 أو قم بإرسال (الآيدي ID) أو (المعرف @Username) الآن للبحث عن الطالب وشحنه:",
        reply_markup=get_cancel_keyboard(),
        parse_mode="HTML"
    )


# ==================== مسار الإرسال الجماعي (Broadcast) ====================

@router.message(Command("broadcast"))
async def admin_cmd_broadcast(msg: types.Message, command: CommandObject, state: FSMContext):
    if command.args:
        text_to_send = command.args.strip()
        await state.update_data(broadcast_text=text_to_send)
        await state.set_state(AdminState.waiting_for_broadcast_confirm)
        
        kb = [
            [
                types.InlineKeyboardButton(text="🚀 تأكيد الإرسال", callback_data="admin_confirm_broadcast"),
                types.InlineKeyboardButton(text="❌ إلغاء", callback_data="admin_main_menu")
            ]
        ]
        preview = (
            "🔍 <b>معاينة الرسالة الجماعية:</b>\n"
            "───────────────────\n"
            f"{text_to_send}\n"
            "───────────────────\n\n"
            "⚠️ <b>هل أنت متأكد من إرسال هذه الرسالة لكافة الطلاب؟</b>"
        )
        await msg.answer(preview, reply_markup=types.InlineKeyboardMarkup(inline_keyboard=kb), parse_mode="HTML")
        return

    await state.set_state(AdminState.waiting_for_broadcast_text)
    await msg.answer(
        "📢 <b>إرسال رسالة جماعية لكافة الطلاب</b>\n\n"
        "أرسل الآن نص الرسالة التي تريد تعميمها على جميع مستخدمي البوت:",
        reply_markup=get_cancel_keyboard(),
        parse_mode="HTML"
    )


@router.callback_query(F.data == "admin_broadcast_prompt")
async def callback_broadcast_prompt(call: types.CallbackQuery, state: FSMContext):
    await state.set_state(AdminState.waiting_for_broadcast_text)
    await safe_edit_text(
        call.message, 
        "📢 <b>إرسال رسالة جماعية لكافة الطلاب</b>\n\nأرسل الآن نص الرسالة التي تريد تعميمها على جميع مستخدمي البوت:", 
        reply_markup=get_cancel_keyboard()
    )
    try:
        await call.answer()
    except TelegramBadRequest:
        pass


@router.message(AdminState.waiting_for_broadcast_text)
async def process_broadcast_text(msg: types.Message, state: FSMContext):
    text_to_send = msg.text
    if not text_to_send:
        return await msg.answer("⚠️ يرجى إرسال نص للرسالة.", reply_markup=get_cancel_keyboard(), parse_mode="HTML")

    await state.update_data(broadcast_text=text_to_send)
    await state.set_state(AdminState.waiting_for_broadcast_confirm)

    kb = [
        [
            types.InlineKeyboardButton(text="🚀 تأكيد الإرسال", callback_data="admin_confirm_broadcast"),
            types.InlineKeyboardButton(text="❌ إلغاء", callback_data="admin_main_menu")
        ]
    ]
    preview = (
        "🔍 <b>معاينة الرسالة الجماعية:</b>\n"
        "───────────────────\n"
        f"{text_to_send}\n"
        "───────────────────\n\n"
        "⚠️ <b>هل أنت متأكد من إرسال هذه الرسالة لكافة الطلاب؟</b>"
    )
    await msg.answer(preview, reply_markup=types.InlineKeyboardMarkup(inline_keyboard=kb), parse_mode="HTML")


@router.callback_query(F.data == "admin_confirm_broadcast", AdminState.waiting_for_broadcast_confirm)
async def process_confirm_broadcast(call: types.CallbackQuery, state: FSMContext):
    try:
        await call.answer()
    except TelegramBadRequest:
        pass

    data = await state.get_data()
    text_to_send = data.get("broadcast_text")
    await state.clear()

    users = await fetch_users_async()
    if not users:
        await safe_edit_text(call.message, "📭 لا يوجد طلاب مسجلين لإرسال الرسالة إليهم.", reply_markup=get_admin_dashboard_keyboard())
        return

    user_ids = [u['user_id'] for u in users if 'user_id' in u]
    total_users = len(user_ids)

    await safe_edit_text(call.message, f"⏳ <b>جاري الإرسال الجماعي...</b>\n\nالعدد الإجمالي المستهدف: <code>{total_users}</code> طالب")

    success_count, blocked_count, failed_count = 0, 0, 0

    for index, user_id in enumerate(user_ids, start=1):
        try:
            await bot.send_message(chat_id=user_id, text=text_to_send, parse_mode="HTML")
            success_count += 1
        except TelegramForbiddenError:
            blocked_count += 1
        except TelegramRetryAfter as e:
            await asyncio.sleep(e.retry_after)
            try:
                await bot.send_message(chat_id=user_id, text=text_to_send, parse_mode="HTML")
                success_count += 1
            except Exception:
                failed_count += 1
        except Exception:
            failed_count += 1

        await asyncio.sleep(0.05)

        if index % 25 == 0 or index == total_users:
            try:
                progress_text = (
                    f"⏳ <b>جاري الإرسال الجماعي...</b> (<code>{index}/{total_users}</code>)\n\n"
                    f"✅ تم التسليم: <code>{success_count}</code>\n"
                    f"🚫 حظروا البوت: <code>{blocked_count}</code>\n"
                    f"❌ فشل: <code>{failed_count}</code>"
                )
                await safe_edit_text(call.message, progress_text)
            except Exception:
                pass

    final_report = (
        "✅ <b>تم الانتهاء من الإرسال الجماعي بنجاح!</b>\n\n"
        "📊 <b>التقرير النهائي:</b>\n"
        f"┣ 👥 الإجمالي المستهدف: <code>{total_users}</code>\n"
        f"┣ ✅ تم التسليم بنجاح: <code>{success_count}</code>\n"
        f"┣ 🚫 مستخدمين حظروا البوت: <code>{blocked_count}</code>\n"
        f"┗ ❌ أخطاء وفشل إرسال: <code>{failed_count}</code>"
    )
    await call.message.answer(final_report, reply_markup=get_admin_dashboard_keyboard(), parse_mode="HTML")


# ==================== تفاعلات البحث والشحن وتصفح الطلاب ====================

@router.callback_query(F.data.startswith("admin_users_page_"))
async def admin_callback_users_page(call: types.CallbackQuery):
    page = int(call.data.split("_")[3])
    await render_users_page(call, page=page)


@router.callback_query(F.data == "admin_search_prompt")
async def callback_search_prompt(call: types.CallbackQuery, state: FSMContext):
    await state.set_state(AdminState.waiting_for_search_query)
    await safe_edit_text(call.message, "🔍 <b>بحث عن مستخدم</b>\n\nأرسل الآن (الآيدي ID) أو (معرف المستخدم @Username):", reply_markup=get_cancel_keyboard())
    try:
        await call.answer()
    except TelegramBadRequest:
        pass


@router.message(AdminState.waiting_for_search_query)
async def process_search_user(msg: types.Message, state: FSMContext):
    query = msg.text.strip()
    users_data = await admin_search_user(query)
    
    if users_data:
        u = users_data[0] 
        username_str = f"@{u['username']}" if u.get('username') and u['username'] != "Unknown" else "بدون يوزر"
        report = (
            "👤 <b>معلومات المستخدم:</b>\n"
            f"┣ 🆔 الآيدي: <code>{u['user_id']}</code>\n"
            f"┣ 👤 اليوزر: {username_str}\n"
            f"┣ 📝 الاسم: <b>{u.get('first_name', '')} {u.get('last_name', '')}</b>\n"
            f"┣ 🎁 النقاط المجانية: <code>{float(u.get('free_points') or 0):.2f}</code>\n"
            f"┣ 💳 النقاط المدفوعة: <code>{float(u.get('paid_points') or 0):.2f}</code>\n"
            f"┣ 💰 الإجمالي: <code>{user_total_points(u):.2f}</code>\n"
            f"┗ 📊 إجمالي الأسئلة المُولدة: <code>{u.get('total_questions', 0)}</code>"
        )
        await msg.answer(report, reply_markup=get_admin_user_actions_keyboard(u['user_id']), parse_mode="HTML")
    else:
        await msg.answer("❌ لم يتم العثور على أي مستخدم بهذا البحث.", reply_markup=get_cancel_keyboard(), parse_mode="HTML")
    await state.clear()


@router.callback_query(F.data.startswith("admin_charge_menu_"))
async def show_charge_menu(call: types.CallbackQuery):
    target_id = call.data.split("_")[3]
    await safe_edit_text(call.message, f"💰 <b>شحن رصيد للمستخدم</b> <code>{target_id}</code>\n\nاختر كمية شحن سريعة أو إدخال يدوي:", reply_markup=get_admin_charge_options_keyboard(target_id))
    try:
        await call.answer()
    except TelegramBadRequest:
        pass


@router.callback_query(F.data.startswith("admin_charge_quick_"))
async def process_quick_charge(call: types.CallbackQuery):
    parts = call.data.split("_")
    amount = int(parts[3])
    target_id = int(parts[4])
    
    new_balance = await admin_add_points(target_id, amount)
    if new_balance is not None:
        await safe_edit_text(call.message, f"✅ <b>تم الشحن بنجاح!</b>\n\nالمستخدم: <code>{target_id}</code>\nالكمية المضافة: <code>+{amount}</code> 🟢\nالرصيد الجديد: <code>{new_balance}</code> 💰", reply_markup=get_admin_dashboard_keyboard())
        await send_points_notification(target_id, amount, new_balance)
    else:
        try:
            await call.answer("❌ حدث خطأ أثناء الشحن.", show_alert=True)
        except TelegramBadRequest:
            pass


@router.callback_query(F.data.startswith("admin_charge_manual_"))
async def prompt_manual_charge(call: types.CallbackQuery, state: FSMContext):
    target_id = call.data.split("_")[3]
    await state.update_data(target_id=target_id)
    await state.set_state(AdminState.waiting_for_charge_amount)
    await safe_edit_text(call.message, f"✍️ <b>شحن يدوي</b>\n\nأرسل عدد النقاط المراد إضافتها للمستخدم <code>{target_id}</code>:", reply_markup=get_cancel_keyboard())
    try:
        await call.answer()
    except TelegramBadRequest:
        pass


@router.message(AdminState.waiting_for_charge_amount)
async def process_manual_charge(msg: types.Message, state: FSMContext):
    if not msg.text.isdigit():
        return await msg.answer("❌ يرجى إرسال أرقام صحيحة فقط.", reply_markup=get_cancel_keyboard(), parse_mode="HTML")
    
    amount = int(msg.text)
    data = await state.get_data()
    raw_target_id = data.get('target_id')
    
    if not raw_target_id:
        await msg.answer("❌ انتهت جلسة الشحن، يرجى إعادة اختيار الطالب مجدداً.", reply_markup=get_admin_dashboard_keyboard())
        await state.clear()
        return

    target_id = int(raw_target_id)
    
    new_balance = await admin_add_points(target_id, amount)
    if new_balance is not None:
        await msg.answer(f"✅ <b>تم الشحن بنجاح!</b>\n\nالمستخدم: <code>{target_id}</code>\nالكمية المضافة: <code>+{amount}</code> 🟢\nالرصيد الجديد: <code>{new_balance}</code> 💰", reply_markup=get_admin_dashboard_keyboard(), parse_mode="HTML")
        await send_points_notification(target_id, amount, new_balance)
    else:
        await msg.answer("❌ حدث خطأ أثناء الشحن. حاول مجدداً.", reply_markup=get_admin_dashboard_keyboard())
    await state.clear()


@router.callback_query(F.data == "admin_stats")
async def show_db_stats(call: types.CallbackQuery):
    stats = await admin_get_global_stats()
    text = f"📊 <b>إحصائيات النظام الحية:</b>\n\n👥 إجمالي الطلاب المسجلين: <code>{stats['total_users']}</code>\n📝 إجمالي الأسئلة المُولدة: <code>{stats['total_questions']}</code>\n"
    await safe_edit_text(call.message, text, reply_markup=get_admin_dashboard_keyboard())
    try:
        await call.answer()
    except TelegramBadRequest:
        pass


@router.callback_query(F.data == "admin_export_users")
async def export_all_users(call: types.CallbackQuery):
    await safe_edit_text(call.message, "⏳ جاري استخراج البيانات وبناء ملف الـ CSV، يرجى الانتظار...")
    
    try:
        users = await fetch_users_async()
        if not users:
            return await safe_edit_text(call.message, "📭 لا يوجد طلاب لتصديرهم.", reply_markup=get_admin_dashboard_keyboard())
        
        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerow(["User ID", "Username", "First Name", "Last Name", "Free Points", "Paid Points", "Total Points", "Total Questions", "Joined At"])
        
        for u in users:
            joined_time_syria = format_syria_time(u.get('joined_at'))
            writer.writerow([
                sanitize_csv_value(u.get('user_id', '')),
                sanitize_csv_value(u.get('username', 'Unknown')),
                sanitize_csv_value(u.get('first_name', 'Unknown')),
                sanitize_csv_value(u.get('last_name', 'Unknown')),
                sanitize_csv_value(u.get('free_points', 0)),
                sanitize_csv_value(u.get('paid_points', 0)),
                sanitize_csv_value(user_total_points(u)),
                sanitize_csv_value(u.get('total_questions', 0)),
                sanitize_csv_value(joined_time_syria)
            ])
            
        csv_bytes = output.getvalue().encode('utf-8-sig')
        file = BufferedInputFile(csv_bytes, filename="students_report.csv")
        
        try:
            await call.message.delete()
        except TelegramBadRequest:
            pass
            
        await call.message.answer_document(
            document=file, 
            caption="📥 <b>تم استخراج ملف سجل الطلاب بنجاح وبشكل آمن!</b>",
            reply_markup=get_admin_dashboard_keyboard(),
            parse_mode="HTML"
        )
    except Exception as e:
        logger.error(f"Error exporting users: {e}")
        try:
            await safe_edit_text(call.message, "❌ حدث خطأ داخلي أثناء استخراج الملف.", reply_markup=get_admin_dashboard_keyboard())
        except TelegramBadRequest:
            await call.message.answer("❌ حدث خطأ داخلي أثناء استخراج الملف.", reply_markup=get_admin_dashboard_keyboard())


def format_syria_time(iso_str: str) -> str:
    """تحويل توقيت قاعدة البيانات إلى توقيت سوريا (12 ساعة بتنسيق صباحاً/مساءً)."""
    if not iso_str:
        return "غير معروف"
    try:
        dt = datetime.fromisoformat(str(iso_str).replace('Z', '+00:00'))
        dt_syria = dt.astimezone(SYRIA_TZ)
        return dt_syria.strftime("%Y-%m-%d %I:%M %p").replace("AM", "ص").replace("PM", "م")
    except Exception:
        return str(iso_str)[:16].replace("T", " ")


@router.callback_query(F.data.startswith("admin_user_quizzes_"))
async def show_user_quizzes_handler(call: types.CallbackQuery):
    try:
        parts = call.data.split("_")
        target_id = int(parts[3])
        page = int(parts[5])

        offset = (page - 1) * USER_QUIZZES_PAGE_SIZE
        quizzes, total = await admin_get_user_quizzes(creator_id=target_id, limit=USER_QUIZZES_PAGE_SIZE, offset=offset)

        if not quizzes or total == 0:
            try:
                await call.answer("📭 هذا الطالب لم يقم بتوليد أي كويزات بعد.", show_alert=True)
            except TelegramBadRequest:
                pass
            return

        total_pages = max(1, -(-total // USER_QUIZZES_PAGE_SIZE))
        page = max(1, min(page, total_pages))

        report_lines = []
        kb = []

        for idx, q in enumerate(quizzes, start=offset + 1):
            quiz_id = q["id"]
            title = q.get("source_title") or "كويز بدون عنوان"
            time_syria = format_syria_time(q.get("created_at"))
            likes = q.get("likes", 0)
            dislikes = q.get("dislikes", 0)

            report_lines.append(
                f"<b>{idx}. {title}</b>\n"
                f" ┣ 🕒 التاريخ (توقيت سوريا): <code>{time_syria}</code>\n"
                f" ┗ 👍 {likes} | 👎 {dislikes}\n"
                f"───────────────────"
            )

            kb.append([types.InlineKeyboardButton(
                text=f"🎯 تجربة #{idx}: {title[:22]}",
                callback_data=f"afb_try_{quiz_id}"
            )])

        nav_row = []
        if page > 1:
            nav_row.append(types.InlineKeyboardButton(text="◀️ السابق", callback_data=f"admin_user_quizzes_{target_id}_p_{page - 1}"))
        if page < total_pages:
            nav_row.append(types.InlineKeyboardButton(text="التالي ▶️", callback_data=f"admin_user_quizzes_{target_id}_p_{page + 1}"))
        if nav_row:
            kb.append(nav_row)

        kb.append([types.InlineKeyboardButton(text="⚙️ لوحة التحكم الرئيسية", callback_data="admin_main_menu")])

        text = (
            f"🎯 <b>الكويزات المُولدة بواسطة الطالب (<code>{target_id}</code>)</b>\n"
            f"📄 الصفحة {page} من أصل {total_pages} (الإجمالي: <code>{total}</code> كويز)\n"
            f"───────────────────\n\n" +
            "\n".join(report_lines)
        )

        await safe_edit_text(call.message, text, reply_markup=types.InlineKeyboardMarkup(inline_keyboard=kb))
        try:
            await call.answer()
        except TelegramBadRequest:
            pass

    except Exception as e:
        logger.error(f"Error rendering user quizzes: {e}")
        try:
            await call.answer("❌ تعذر جلب كويزات هذا الطالب.", show_alert=True)
        except TelegramBadRequest:
            pass