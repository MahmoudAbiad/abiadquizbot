# handlers/admin/users.py
import asyncio
import io
import csv
import html
import uuid
from constants import USER_QUIZZES_PAGE_SIZE, format_syria_time as _format_syria_time
from helpers.supabase_helper import admin_get_user_quizzes
from .admin_utils import sanitize_csv_value
from aiogram import Router, types, F
from aiogram.filters import Command, CommandObject
from aiogram.fsm.context import FSMContext
from aiogram.types import BufferedInputFile
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError, TelegramRetryAfter

from config import bot, ADMIN_ID
from helpers.supabase_helper import (
    supabase, 
    admin_add_points,
    admin_grant_direct_gift,
    admin_get_global_stats, 
    admin_find_exact_user,
    admin_search_user,
    admin_get_users_page,
    admin_get_user_deletion_summary,
    admin_get_user_generated_quizzes,
    admin_delete_user_action,
    admin_delete_quiz,
)
from keyboards import (
    DIRECT_GIFT_PRESET_TITLES,
    get_admin_dashboard_keyboard,
    get_admin_communication_keyboard,
    get_admin_users_menu_keyboard,
    get_admin_user_actions_keyboard,
    get_admin_user_delete_menu_keyboard,
    get_admin_user_delete_confirm_keyboard,
    get_admin_charge_options_keyboard,
    get_admin_direct_message_charge_keyboard,
    get_admin_direct_gift_mode_keyboard,
    get_admin_direct_gift_title_keyboard,
    get_admin_direct_gift_amount_keyboard,
    get_admin_direct_gift_days_keyboard,
    get_admin_direct_message_mode_keyboard,
    get_cancel_keyboard,
    get_analytics_keyboard,
)
from services.flow_state import get_user_redis_active_summary, reset_user_redis_operations
from services.consent import purge_user_consent_cache
from logger import get_logger
from helpers.supabase_helper.gifts import enrich_balances
from services.gift_presentation import expiry_text, remaining_expiry_text
from .dashboard import AdminState, IsAdminFilter, safe_edit_text

logger = get_logger(__name__)
router = Router()

# تطبيق فلتر حماية الإدارة على الراوتر
router.message.filter(IsAdminFilter())
router.callback_query.filter(IsAdminFilter())
# 🩹 FIX (تسريب خصوصية): محمي أصلاً بهوية الأدمن (IsAdminFilter) بس بلا فلتر مكان -
# لو الأدمن كتب الأمر بالغلط جوا أي غروب هو عضو فيه، اللوحة/العملية كانت رح تنفّذ
# وتظهر علناً هناك. راجع نفس الملاحظة بـ handlers/start.py وhandlers/favorites.py.
router.message.filter(F.chat.type == "private")
router.callback_query.filter(F.message.chat.type == "private")


@router.callback_query(F.data == "admin_renewal_report")
async def show_renewal_report(call: types.CallbackQuery):
    from services.daily_renewal import report_text
    response = await supabase.table("daily_renewal_runs").select("day,report_status").order("day", desc=True).limit(1).execute()
    text = "📭 لم يحن موعد أول تجديد مجدول بعد."
    if response.data:
        run = response.data[0]
        summary = await supabase.rpc("daily_renewal_summary", {"p_day": run["day"]}).execute()
        text = report_text(summary.data) + f"\n⏳ بانتظار الإرسال: {summary.data['pending']}"
        if run["report_status"] == "uncertain":
            text += "\n❔ تعذر تأكيد وصول نسخة التقرير للأدمن؛ هذه النسخة محفوظة."
    await safe_edit_text(call.message, text, reply_markup=get_admin_users_menu_keyboard())
    await call.answer()


@router.callback_query(F.data.startswith("admin_renewal_failures:"))
async def show_renewal_failures(call: types.CallbackQuery):
    _, mode, raw_page = call.data.split(":")
    if mode not in {"all", "blocked"} or not raw_page.isdigit():
        await call.answer("طلب غير صالح", show_alert=True)
        return
    page = int(raw_page)
    size = 2
    if mode == "blocked":
        result = await supabase.table("users").select("*", count="exact").eq(
            "bot_delivery_state", "blocked").order("user_id").range(page*size, page*size+size-1).execute()
        entries = [{"user_snapshot": u, "error": "المستخدم حظر البوت — مستثنى من الإرسال القادم",
                    "day": "", "status": "blocked"} for u in result.data or []]
    else:
        result = await supabase.table("daily_renewal_deliveries").select("*", count="exact").in_(
            "status", ["blocked", "unavailable", "failed", "uncertain", "expired"]).order(
            "day", desc=True).order("user_id").range(page*size, page*size+size-1).execute()
        entries = result.data or []
    await enrich_balances([entry["user_snapshot"] for entry in entries])
    total = result.count or 0
    text = ("🚫 المستخدمون الذين حظروا البوت" if mode == "blocked" else "📭 سجل تعذر وصول إشعارات التجديد")
    text += f"\nالإجمالي: {total} — صفحة {page+1}\n"
    if mode == "all":
        text += "الهوية والأرصدة المجانية والمدفوعة محفوظة وقت المحاولة؛ رصيد الهدايا المعروض حالي.\n"
    kb = []
    for entry in entries:
        u = entry["user_snapshot"]
        text += "\n" + _format_user_profile_report(u)
        text += (
            f"\n📅 تاريخ التسجيل: {html.escape(str(u.get('joined_at') or 'غير مسجل'))}"
            f"\n🔗 مصدر الانضمام: {html.escape(str(u.get('join_source') or 'غير مسجل'))}"
            f" · المُحيل: {html.escape(str(u.get('referred_by') or '—'))}"
            f"\n🔄 آخر تجديد: {html.escape(str(u.get('last_renewal') or '—'))}"
            f"\n🔔 إشعارات التجديد: {'مفعّلة' if u.get('renewal_notifications', True) else 'متوقفة'}"
            f"\n📨 تاريخ المحاولة: {html.escape(str(entry.get('day') or '—'))}"
            f"\n❌ السبب: {html.escape(str(entry['error'])[:220])}\n"
        )
        kb.append([types.InlineKeyboardButton(
            text=f"{'🚫 حظر البوت' if entry['status']=='blocked' else '👤 بيانات المستخدم'} · {u['user_id']}",
            callback_data=f"admin_user_profile_{u['user_id']}")])
    if not entries:
        text += "\nلا توجد حالات مسجلة."
    nav = []
    if page:
        nav.append(types.InlineKeyboardButton(text="⬅️ السابق", callback_data=f"admin_renewal_failures:{mode}:{page-1}"))
    if (page+1)*size < total:
        nav.append(types.InlineKeyboardButton(text="التالي ➡️", callback_data=f"admin_renewal_failures:{mode}:{page+1}"))
    if nav:
        kb.append(nav)
    kb.append([types.InlineKeyboardButton(text="🔙 إدارة المستخدمين", callback_data="admin_users_menu")])
    await safe_edit_text(call.message, text, reply_markup=types.InlineKeyboardMarkup(inline_keyboard=kb))
    await call.answer()


def user_total_points(user: dict) -> float:
    """حساب إجمالي رصيد الطالب من الخانتين المجانية والمدفوعة."""
    return float(user.get("free_points") or 0) + float(user.get("paid_points") or 0) + float(user.get("gift_points") or 0)


def _telegram_profile_link(user_id: int, label: str = "فتح ملف الطالب على تيليغرام") -> str:
    """رابط HTML لملف الطالب عبر المعرّف الرقمي، حتى لو لم يكن لديه username.

    تيليغرام قد يمنع فتحه بحسب إعدادات خصوصية الطالب؛ لهذا يبقى زر المراسلة
    عبر البوت متاحاً بصورة مستقلة داخل لوحة الإجراءات.
    """
    try:
        numeric_id = int(user_id)
    except (TypeError, ValueError):
        return html.escape(label)
    if numeric_id <= 0:
        return html.escape(label)
    return f'<a href="tg://user?id={numeric_id}">{html.escape(label)}</a>'


async def fetch_users_async():
    """Fetch all users in bounded requests for export and confirmed broadcasts."""
    users = []
    last_id = None
    while True:
        request = supabase.table("users").select("*").order("user_id").limit(500)
        if last_id is not None:
            request = request.gt("user_id", last_id)
        response = await request.execute()
        batch = response.data or []
        if not batch:
            return users
        await enrich_balances(batch)
        users.extend(batch)
        last_id = batch[-1]["user_id"]


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


async def render_users_page(event, page: int = 1):
    """عرض قائمة الطلاب المصفحة مع معالجة آمنة لـ Callbacks."""
    if isinstance(event, types.CallbackQuery):
        try:
            await event.answer()
        except TelegramBadRequest:
            pass

    try:
        page_users, total_users, page, total_pages = await admin_get_users_page(page)
    except Exception:
        logger.exception("Error fetching admin user page")
        message = event.message if isinstance(event, types.CallbackQuery) else event
        await message.answer("❌ تعذر تحميل المستخدمين، حاول مجدداً.")
        return
    if not total_users:
        text = "📭 لا يوجد أي طلاب مسجلين حالياً."
        if isinstance(event, types.Message):
            await event.answer(text, reply_markup=get_admin_dashboard_keyboard(), parse_mode="HTML")
        elif isinstance(event, types.CallbackQuery):
            await safe_edit_text(event.message, text, reply_markup=get_admin_dashboard_keyboard())
        return

    start_idx = (page - 1) * 5

    report = f"👥 <b>سجل الطلاب المسجلين ({page} من {total_pages}):</b>\n\n"
    for idx, u in enumerate(page_users, start=start_idx + 1):
        username_str = f"@{html.escape(str(u['username']))}" if u.get('username') and u['username'] != "Unknown" else "بدون يوزر"
        joined_time = _short_syria_time(u.get('joined_at'))
        report += (
            f"<b>{idx}. آيدي:</b> <code>{u['user_id']}</code>\n"
            f"┣ 👤 اليوزر: {username_str}\n"
            f"┣ 🔗 {_telegram_profile_link(u['user_id'])}\n"
            f"┣ 📝 الاسم: <b>{html.escape(str(u.get('first_name') or '')[:100])} {html.escape(str(u.get('last_name') or '')[:100])}</b>\n"
            f"┣ 🕒 انضم: <code>{joined_time}</code>\n"
            f"┣ 🔄 المجاني: <code>{float(u.get('free_points') or 0):.2f}</code>\n"
            f"┣ 🎁 الهدايا: <code>{float(u.get('gift_points') or 0):.2f}</code>\n"
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
    kb.append([types.InlineKeyboardButton(text="🔙 رجوع لإدارة المستخدمين", callback_data="admin_users_menu")])
    reply_markup = types.InlineKeyboardMarkup(inline_keyboard=kb)
    
    if isinstance(event, types.Message):
        await event.answer(report, reply_markup=reply_markup, parse_mode="HTML")
    elif isinstance(event, types.CallbackQuery):
        await safe_edit_text(event.message, report, reply_markup=reply_markup)


# ==================== الأوامر النصية ====================

@router.message(Command("searchuser"))
async def admin_cmd_search(msg: types.Message, state: FSMContext):
    await state.set_state(AdminState.waiting_for_search_query)
    await msg.answer("🔍 <b>بحث عن مستخدم</b>\n\nأرسل الآن (الآيدي ID) أو (معرف المستخدم @Username أو جزء منه):", reply_markup=get_cancel_keyboard(), parse_mode="HTML")


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


# ==================== مسار الرسالة المخصصة لمستخدم واحد ====================

@router.callback_query(F.data == "admin_direct_message_prompt")
async def callback_direct_message_prompt(call: types.CallbackQuery, state: FSMContext):
    await state.set_state(AdminState.waiting_for_direct_message_target)
    await safe_edit_text(
        call.message,
        "✉️ <b>إرسال رسالة مخصصة</b>\n\nأرسل الآن آيدي المستخدم أو معرفه مثل <code>@username</code>:",
        reply_markup=get_cancel_keyboard(),
    )
    await call.answer()


async def _present_direct_message_target(answer, state: FSMContext, user: dict):
    """يجهّز شاشة (اختيار نوع العملية) لمستخدم مستهدف مُحدَّد مسبقاً.
    answer: دالة إرسال (msg.answer أو call.message.answer)."""
    target_id = int(user["user_id"])
    username = user.get("username") if user.get("username") and user.get("username") != "Unknown" else "بدون يوزر"
    full_name = " ".join(
        part for part in (user.get("first_name"), user.get("last_name")) if part
    ).strip() or "بدون اسم"
    free_points = float(user.get("free_points") or 0)
    paid_points = float(user.get("paid_points") or 0)
    gift_points = float(user.get("gift_points") or 0)
    await state.update_data(
        direct_message_target_id=target_id,
        direct_message_balance_before=user_total_points(user),
        direct_message_target_name=(
            f"@{user['username']}" if user.get("username") and user["username"] != "Unknown"
            else str(target_id)
        ),
    )
    await state.set_state(AdminState.waiting_for_direct_message_mode)
    await answer(
        "👤 <b>المستخدم المستهدف</b>\n"
        "━━━━━━━━━━━━━━━━━━\n"
        f"┣ الاسم: <b>{html.escape(full_name)}</b>\n"
        f"┣ اليوزر: <code>{html.escape(username)}</code>\n"
        f"┣ الآيدي: <code>{target_id}</code>\n"
        f"┣ 🔗 {_telegram_profile_link(target_id)}\n"
        f"┣ إجمالي النقاط: <code>{free_points + paid_points + gift_points:.2f}</code>\n"
        f"┣ النقاط المجانية: <code>{free_points:.2f}</code>\n"
        f"┣ نقاط الهدايا: <code>{gift_points:.2f}</code>\n"
        f"┗ النقاط المدفوعة: <code>{paid_points:.2f}</code>\n\n"
        "اختر نوع العملية:",
        reply_markup=get_admin_direct_message_mode_keyboard(),
        parse_mode="HTML",
    )


@router.message(AdminState.waiting_for_direct_message_target)
async def process_direct_message_target(msg: types.Message, state: FSMContext):
    query = (msg.text or "").strip().lstrip("@").strip()
    if not query:
        return await msg.answer("⚠️ أرسل آيدي المستخدم أو معرفه.", reply_markup=get_cancel_keyboard())

    exact_user = await admin_find_exact_user(query)
    if exact_user:
        return await _present_direct_message_target(msg.answer, state, exact_user)

    users_data = await admin_search_user(query)
    if not users_data:
        return await msg.answer(
            "❌ لم يتم العثور على مستخدم بهذا الآيدي أو المعرف. حاول مجدداً:",
            reply_markup=get_cancel_keyboard(),
        )

    report = (
        f"🔍 <b>نتائج البحث للرسالة المخصصة: {html.escape(query[:100])}</b>\n"
        f"👥 تم العثور على <code>{len(users_data)}</code> مستخدم مطابق — اختر الطالب المطلوب:\n\n"
    )
    keyboard = []
    for idx, user in enumerate(users_data[:10], start=1):
        raw_uname = str(user.get("username") or "").strip()
        has_uname = bool(raw_uname and raw_uname != "Unknown")
        uname_str = f"@{raw_uname[:64]}" if has_uname else "بدون يوزر"
        name_parts = [
            str(user.get(field) or "").strip()
            for field in ("first_name", "last_name")
            if user.get(field) and str(user.get(field)).strip() != "Unknown"
        ]
        name = " ".join(name_parts)[:100] or "بدون اسم"
        report += (
            f"<b>{idx}. {html.escape(name)}</b>\n"
            f"👤 {html.escape(uname_str)} — 🆔 <code>{user['user_id']}</code>\n\n"
        )
        btn_label = f"✉️ {idx}. {uname_str if has_uname else name[:24]} ({user['user_id']})"
        keyboard.append([types.InlineKeyboardButton(
            text=btn_label[:64], callback_data=f"admin_direct_start_{user['user_id']}"
        )])
    keyboard.append([types.InlineKeyboardButton(text="🏠 الرئيسية", callback_data="admin_main_menu")])
    await msg.answer(report, reply_markup=types.InlineKeyboardMarkup(inline_keyboard=keyboard), parse_mode="HTML")


@router.callback_query(F.data.startswith("admin_direct_start_"))
async def callback_direct_start_from_button(call: types.CallbackQuery, state: FSMContext):
    """زر (تواصل/مراسلة) على بطاقة مستخدم بدون يوزر عام — يدخل مباشرة في تدفق
    الرسالة المباشرة عبر البوت بدل رابط tg://user?id= الذي قد يُرفض حسب خصوصية المستخدم."""
    try:
        target_id = call.data.replace("admin_direct_start_", "", 1)
        users_data = await admin_search_user(target_id)
        if not users_data:
            return await call.answer("❌ لم يتم العثور على هذا المستخدم.", show_alert=True)

        await _present_direct_message_target(call.message.answer, state, users_data[0])
        await call.answer()
    except Exception as e:
        logger.error(f"Error in callback_direct_start_from_button: {e}")
        await call.answer("❌ حدث خطأ أثناء تجهيز الرسالة.", show_alert=True)


@router.callback_query(F.data.startswith("admin_direct_mode_"), AdminState.waiting_for_direct_message_mode)
async def process_direct_message_mode(call: types.CallbackQuery, state: FSMContext):
    mode = call.data.rsplit("_", 1)[1]
    await state.update_data(direct_message_mode=mode)
    await state.set_state(AdminState.waiting_for_direct_message_text)
    await call.answer()
    await safe_edit_text(
        call.message,
        "📝 أرسل نص الرسالة التي تريد إرسالها:",
        reply_markup=get_cancel_keyboard(),
    )


@router.message(AdminState.waiting_for_direct_message_text)
async def process_direct_message_text(msg: types.Message, state: FSMContext):
    message_text = msg.text or ""
    if not message_text.strip():
        return await msg.answer("⚠️ يرجى إرسال نص للرسالة.", reply_markup=get_cancel_keyboard())
    if len(message_text) > 4000:
        return await msg.answer("❌ الرسالة طويلة جداً. الحد الأقصى هو 4000 حرفاً لتوفير مساحة لتفاصيل الرصيد.", reply_markup=get_cancel_keyboard())

    await state.update_data(direct_message_text=message_text, direct_message_charge_amount=None, direct_message_charge_type=None)
    data = await state.get_data()
    if data.get("direct_message_mode") == "message":
        await state.update_data(direct_message_charge_amount=0, direct_message_charge_type="none")
        return await show_direct_message_confirmation(msg, state)

    await state.set_state(AdminState.waiting_for_direct_message_charge_type)
    await msg.answer(
        f"💰 اختر نوع الشحن المرفق مع الرسالة للمستخدم <code>{data['direct_message_target_id']}</code>:",
        reply_markup=get_admin_direct_message_charge_keyboard(data["direct_message_target_id"]),
        parse_mode="HTML",
    )


@router.message(AdminState.waiting_for_direct_message_charge_amount)
async def process_direct_message_charge_amount(msg: types.Message, state: FSMContext):
    amount_text = (msg.text or "").strip()
    if not amount_text.isdigit():
        return await msg.answer("❌ أرسل عدد نقاط صحيحاً (صفر أو أكثر).", reply_markup=get_cancel_keyboard())

    amount = int(amount_text)
    if amount > 1000000:
        return await msg.answer("❌ العدد كبير جداً. الحد الأقصى هو 1,000,000 نقطة.", reply_markup=get_cancel_keyboard())

    data = await state.get_data()
    await state.update_data(direct_message_charge_amount=amount)
    if amount == 0:
        await state.update_data(direct_message_charge_type="none")
        return await show_direct_message_confirmation(msg, state)

    if data.get("direct_message_charge_type") in ("free", "paid"):
        return await show_direct_message_confirmation(msg, state)

    await state.set_state(AdminState.waiting_for_direct_message_charge_type)
    await msg.answer(
        f"اختر وجهة <code>{amount}</code> نقطة للمستخدم <code>{data['direct_message_target_id']}</code>:",
        reply_markup=get_admin_direct_message_charge_keyboard(data["direct_message_target_id"]),
        parse_mode="HTML",
    )


def _format_gift_validity_days(days: int) -> str:
    if not days or int(days) <= 0:
        return "بدون انتهاء"
    d = int(days)
    if d == 1:
        return "يوم واحد"
    if d == 2:
        return "يومان"
    if d == 7:
        return "أسبوع (7 أيام)"
    if d == 14:
        return "أسبوعان (14 يوماً)"
    if d == 30:
        return "شهر (30 يوماً)"
    return f"{d} يوم"


async def show_direct_message_confirmation(message: types.Message, state: FSMContext):
    data = await state.get_data()
    charge_type = data.get("direct_message_charge_type")
    amount = int(data.get("direct_message_charge_amount") or 0)
    balance_before = float(data.get("direct_message_balance_before") or 0)
    include_balance = bool(data.get("direct_message_include_balance"))
    charge_label = {
        "free": "مجاني",
        "gift": "شحن هدايا",
        "paid": "مدفوع",
        "none": "بدون شحن",
    }.get(charge_type, "غير محدد")
    gift_lines = ""
    if charge_type == "gift" and amount > 0:
        gift_title = data.get("direct_message_gift_title") or "هدية مستخدم جديد"
        gift_days = int(data.get("direct_message_gift_days") or 0)
        gift_lines = (
            f"اسم الهدية: <b>{html.escape(str(gift_title))}</b>\n"
            f"صلاحية الهدية: <code>{html.escape(_format_gift_validity_days(gift_days))}</code>\n"
        )
    preview = (
        "🔍 <b>معاينة العملية</b>\n"
        f"المستخدم: <code>{html.escape(str(data['direct_message_target_id']))}</code>\n"
        f"الشحن: <code>{amount}</code> نقطة ({charge_label})\n"
        f"{gift_lines}"
        f"الرصيد قبل الشحن: <code>{balance_before:.2f}</code> نقطة\n"
        f"إرفاق الرصيد بالرسالة: {'☑️ نعم' if include_balance else '☐ لا'}\n"
        "───────────────────\n"
        f"{html.escape(data['direct_message_text'])}\n"
        "───────────────────\n\n"
        "هل تريد تأكيد إرسال الرسالة وتنفيذ الشحن؟"
    )
    toggle_label = (
        ("☑️ إرفاق تفاصيل الهدية والرصيد" if include_balance else "☐ إرفاق تفاصيل الهدية والرصيد")
        if charge_type == "gift"
        else ("☑️ إرفاق الرصيد قبل/بعد الشحن" if include_balance else "☐ إرفاق الرصيد قبل/بعد الشحن")
    )
    keyboard = types.InlineKeyboardMarkup(inline_keyboard=[[
        types.InlineKeyboardButton(text="✅ تأكيد العملية", callback_data="admin_confirm_direct_message"),
        types.InlineKeyboardButton(text="❌ إلغاء", callback_data="admin_main_menu"),
    ], [
        types.InlineKeyboardButton(
            text=toggle_label,
            callback_data="admin_direct_balance_toggle",
        ),
    ]])
    await message.answer(preview, reply_markup=keyboard, parse_mode="HTML")
    await state.set_state(AdminState.waiting_for_direct_message_confirm)


@router.callback_query(F.data.startswith("admin_direct_charge_"), AdminState.waiting_for_direct_message_charge_type)
async def process_direct_message_charge_type(call: types.CallbackQuery, state: FSMContext):
    charge_type = call.data.split("_")[3]
    data = await state.get_data()
    target_id = data.get("direct_message_target_id")
    await call.answer()

    if charge_type == "none":
        await state.update_data(direct_message_charge_type="none", direct_message_charge_amount=0)
        return await show_direct_message_confirmation(call.message, state)

    if charge_type == "gift":
        await state.update_data(direct_message_charge_type="gift")
        await state.set_state(AdminState.waiting_for_direct_gift_mode)
        return await safe_edit_text(
            call.message,
            f"🎁 <b>شحن هدايا للمستخدم</b> <code>{target_id}</code>\n\n"
            "اختر الهدية الافتراضية الجاهزة أو قم بتخصيص الكمية والمدة:",
            reply_markup=get_admin_direct_gift_mode_keyboard(target_id),
        )

    if charge_type in ("free", "paid"):
        await state.update_data(direct_message_charge_type=charge_type)
        existing_amount = data.get("direct_message_charge_amount")
        if isinstance(existing_amount, int) and existing_amount > 0:
            return await show_direct_message_confirmation(call.message, state)
        type_label = "شحن مجاني" if charge_type == "free" else "شحن مدفوع"
        await state.set_state(AdminState.waiting_for_direct_message_charge_amount)
        return await safe_edit_text(
            call.message,
            f"💰 <b>{type_label}</b>\n\n"
            "أرسل عدد النقاط المراد شحنها مع الرسالة.\n"
            "أرسل <code>0</code> إذا كنت لا تريد شحن رصيد.",
            reply_markup=get_cancel_keyboard(),
        )


@router.callback_query(F.data == "admin_direct_gift_back_types")
async def process_direct_gift_back_types(call: types.CallbackQuery, state: FSMContext):
    data = await state.get_data()
    target_id = data.get("direct_message_target_id")
    await state.set_state(AdminState.waiting_for_direct_message_charge_type)
    await call.answer()
    await safe_edit_text(
        call.message,
        f"💰 اختر نوع الشحن المرفق مع الرسالة للمستخدم <code>{target_id}</code>:",
        reply_markup=get_admin_direct_message_charge_keyboard(target_id),
    )


@router.callback_query(F.data.startswith("admin_direct_gift_preset_"), AdminState.waiting_for_direct_gift_mode)
async def process_direct_gift_preset(call: types.CallbackQuery, state: FSMContext):
    preset = call.data.replace("admin_direct_gift_preset_", "", 1)
    data = await state.get_data()
    await call.answer()

    if preset == "welcome":
        updates = {
            "direct_message_charge_type": "gift",
            "direct_message_charge_amount": 150,
            "direct_message_gift_days": 30,
            "direct_message_gift_title": "هدية مستخدم جديد",
            "direct_message_gift_source_key": f"direct:{uuid.uuid4()}",
        }
        if "direct_message_include_balance" not in data:
            updates["direct_message_include_balance"] = True
        await state.update_data(**updates)
        return await show_direct_message_confirmation(call.message, state)

    if preset == "custom":
        await state.set_state(AdminState.waiting_for_direct_gift_title)
        return await safe_edit_text(
            call.message,
            "🏷️ <b>اسم الهدية المخصصة</b>\n\n"
            "اختر اسماً جاهزاً من الأزرار أدناه، أو أرسل الآن اسم الهدية الذي سيظهر للطالب في محفظته (حتى 100 حرف):",
            reply_markup=get_admin_direct_gift_title_keyboard(),
        )


@router.callback_query(F.data == "admin_direct_gift_back_mode")
async def process_direct_gift_back_mode(call: types.CallbackQuery, state: FSMContext):
    data = await state.get_data()
    target_id = data.get("direct_message_target_id")
    await state.set_state(AdminState.waiting_for_direct_gift_mode)
    await call.answer()
    await safe_edit_text(
        call.message,
        f"🎁 <b>شحن هدايا للمستخدم</b> <code>{target_id}</code>\n\n"
        "اختر الهدية الافتراضية الجاهزة أو قم بتخصيص الكمية والمدة:",
        reply_markup=get_admin_direct_gift_mode_keyboard(target_id),
    )


@router.callback_query(F.data.startswith("admin_direct_gift_title_"), AdminState.waiting_for_direct_gift_title)
async def process_direct_gift_title_button(call: types.CallbackQuery, state: FSMContext):
    key = call.data.replace("admin_direct_gift_title_", "", 1)
    title = DIRECT_GIFT_PRESET_TITLES.get(key, "هدية من الإدارة")
    await state.update_data(direct_message_gift_title=title)
    await state.set_state(AdminState.waiting_for_direct_gift_amount)
    await call.answer()
    await safe_edit_text(
        call.message,
        f"💰 <b>كمية نقاط الهدية</b>\n"
        f"اسم الهدية: <b>{html.escape(title)}</b>\n\n"
        "اختر الكمية من الأزرار الجاهزة أو اضغط «كمية مخصصة» لإدخال الرقم يدوياً:",
        reply_markup=get_admin_direct_gift_amount_keyboard(),
    )


@router.message(AdminState.waiting_for_direct_gift_title)
async def process_direct_gift_title_text(msg: types.Message, state: FSMContext):
    title = (msg.text or "").strip()
    if not 1 <= len(title) <= 100:
        return await msg.answer(
            "⚠️ يرجى إرسال اسم للهدية يتراوح بين حرف واحد و100 حرف، أو اختيار اسم جاهز:",
            reply_markup=get_admin_direct_gift_title_keyboard(),
        )
    await state.update_data(direct_message_gift_title=title)
    await state.set_state(AdminState.waiting_for_direct_gift_amount)
    await msg.answer(
        f"💰 <b>كمية نقاط الهدية</b>\n"
        f"اسم الهدية: <b>{html.escape(title)}</b>\n\n"
        "اختر الكمية من الأزرار الجاهزة أو اضغط «كمية مخصصة» لإدخال الرقم يدوياً:",
        reply_markup=get_admin_direct_gift_amount_keyboard(),
        parse_mode="HTML",
    )


@router.callback_query(F.data == "admin_direct_gift_back_title")
async def process_direct_gift_back_title(call: types.CallbackQuery, state: FSMContext):
    await state.set_state(AdminState.waiting_for_direct_gift_title)
    await call.answer()
    await safe_edit_text(
        call.message,
        "🏷️ <b>اسم الهدية المخصصة</b>\n\n"
        "اختر اسماً جاهزاً من الأزرار أدناه، أو أرسل الآن اسم الهدية الذي سيظهر للطالب في محفظته (حتى 100 حرف):",
        reply_markup=get_admin_direct_gift_title_keyboard(),
    )


@router.callback_query(F.data.startswith("admin_direct_gift_amt_"), AdminState.waiting_for_direct_gift_amount)
async def process_direct_gift_amount_button(call: types.CallbackQuery, state: FSMContext):
    raw = call.data.replace("admin_direct_gift_amt_", "", 1)
    await call.answer()

    if raw == "custom":
        kb = types.InlineKeyboardMarkup(inline_keyboard=[
            [types.InlineKeyboardButton(text="🔙 رجوع لأزرار الكمية", callback_data="admin_direct_gift_back_amount")],
            [types.InlineKeyboardButton(text="🏠 الرئيسية", callback_data="admin_main_menu")],
        ])
        return await safe_edit_text(
            call.message,
            "✍️ <b>كمية مخصصة للهدية</b>\n\n"
            "أرسل الآن عدد النقاط المراد منحها كهدية (من <code>1</code> إلى <code>1,000,000</code>):",
            reply_markup=kb,
        )

    if not raw.isdigit():
        return
    amount = int(raw)
    await state.update_data(direct_message_charge_amount=amount)
    await state.set_state(AdminState.waiting_for_direct_gift_days)
    await safe_edit_text(
        call.message,
        f"⏳ <b>مدة صلاحية الهدية</b>\n"
        f"الكمية المختارة: <code>{amount}</code> نقطة\n\n"
        "اختر مدة الصلاحية من الأزرار الجاهزة أو اضغط «مدة مخصصة»:",
        reply_markup=get_admin_direct_gift_days_keyboard(),
    )


@router.message(AdminState.waiting_for_direct_gift_amount)
async def process_direct_gift_amount_text(msg: types.Message, state: FSMContext):
    amount_text = (msg.text or "").strip()
    if not amount_text.isdigit():
        return await msg.answer("❌ أرسل عدد نقاط صحيحاً (من 1 إلى 1,000,000).", reply_markup=get_admin_direct_gift_amount_keyboard())
    amount = int(amount_text)
    if not 1 <= amount <= 1000000:
        return await msg.answer("❌ يجب أن تكون كمية الهدية بين 1 و 1,000,000 نقطة.", reply_markup=get_admin_direct_gift_amount_keyboard())

    await state.update_data(direct_message_charge_amount=amount)
    await state.set_state(AdminState.waiting_for_direct_gift_days)
    await msg.answer(
        f"⏳ <b>مدة صلاحية الهدية</b>\n"
        f"الكمية المختارة: <code>{amount}</code> نقطة\n\n"
        "اختر مدة الصلاحية من الأزرار الجاهزة أو اضغط «مدة مخصصة»:",
        reply_markup=get_admin_direct_gift_days_keyboard(),
        parse_mode="HTML",
    )


@router.callback_query(F.data == "admin_direct_gift_back_amount")
async def process_direct_gift_back_amount(call: types.CallbackQuery, state: FSMContext):
    data = await state.get_data()
    title = data.get("direct_message_gift_title") or "هدية من الإدارة"
    await state.set_state(AdminState.waiting_for_direct_gift_amount)
    await call.answer()
    await safe_edit_text(
        call.message,
        f"💰 <b>كمية نقاط الهدية</b>\n"
        f"اسم الهدية: <b>{html.escape(title)}</b>\n\n"
        "اختر الكمية من الأزرار الجاهزة أو اضغط «كمية مخصصة» لإدخال الرقم يدوياً:",
        reply_markup=get_admin_direct_gift_amount_keyboard(),
    )


@router.callback_query(F.data.startswith("admin_direct_gift_days_"), AdminState.waiting_for_direct_gift_days)
async def process_direct_gift_days_button(call: types.CallbackQuery, state: FSMContext):
    raw = call.data.replace("admin_direct_gift_days_", "", 1)
    await call.answer()

    if raw == "custom":
        kb = types.InlineKeyboardMarkup(inline_keyboard=[
            [types.InlineKeyboardButton(text="🔙 رجوع لأزرار المدة", callback_data="admin_direct_gift_back_days")],
            [types.InlineKeyboardButton(text="🏠 الرئيسية", callback_data="admin_main_menu")],
        ])
        return await safe_edit_text(
            call.message,
            "✍️ <b>مدة صلاحية مخصصة</b>\n\n"
            "أرسل عدد أيام صلاحية الهدية (من <code>1</code> إلى <code>3650</code> يوماً، أو أرسل <code>0</code> لهدية بدون انتهاء):",
            reply_markup=kb,
        )

    if not raw.isdigit():
        return
    days = int(raw)
    data = await state.get_data()
    updates = {
        "direct_message_charge_type": "gift",
        "direct_message_gift_days": days,
        "direct_message_gift_source_key": f"direct:{uuid.uuid4()}",
    }
    if "direct_message_include_balance" not in data:
        updates["direct_message_include_balance"] = True
    await state.update_data(**updates)
    await show_direct_message_confirmation(call.message, state)


@router.message(AdminState.waiting_for_direct_gift_days)
async def process_direct_gift_days_text(msg: types.Message, state: FSMContext):
    days_text = (msg.text or "").strip()
    if not days_text.isdigit():
        return await msg.answer("❌ أرسل عدد أيام صحيحاً (من 0 إلى 3650).", reply_markup=get_admin_direct_gift_days_keyboard())
    days = int(days_text)
    if not 0 <= days <= 3650:
        return await msg.answer("❌ يجب أن تكون المدة بين 0 (بدون انتهاء) و 3650 يوماً.", reply_markup=get_admin_direct_gift_days_keyboard())

    data = await state.get_data()
    updates = {
        "direct_message_charge_type": "gift",
        "direct_message_gift_days": days,
        "direct_message_gift_source_key": f"direct:{uuid.uuid4()}",
    }
    if "direct_message_include_balance" not in data:
        updates["direct_message_include_balance"] = True
    await state.update_data(**updates)
    await show_direct_message_confirmation(msg, state)


@router.callback_query(F.data == "admin_direct_gift_back_days")
async def process_direct_gift_back_days(call: types.CallbackQuery, state: FSMContext):
    data = await state.get_data()
    amount = int(data.get("direct_message_charge_amount") or 0)
    await state.set_state(AdminState.waiting_for_direct_gift_days)
    await call.answer()
    await safe_edit_text(
        call.message,
        f"⏳ <b>مدة صلاحية الهدية</b>\n"
        f"الكمية المختارة: <code>{amount}</code> نقطة\n\n"
        "اختر مدة الصلاحية من الأزرار الجاهزة أو اضغط «مدة مخصصة»:",
        reply_markup=get_admin_direct_gift_days_keyboard(),
    )


@router.callback_query(F.data == "admin_direct_balance_toggle", AdminState.waiting_for_direct_message_confirm)
async def toggle_direct_message_balance(call: types.CallbackQuery, state: FSMContext):
    data = await state.get_data()
    await state.update_data(direct_message_include_balance=not bool(data.get("direct_message_include_balance")))
    await call.answer()
    await show_direct_message_confirmation(call.message, state)


@router.callback_query(
    F.data == "admin_confirm_direct_message",
    AdminState.waiting_for_direct_message_confirm,
)
async def process_confirm_direct_message(call: types.CallbackQuery, state: FSMContext):
    data = await state.get_data()
    target_id = data.get("direct_message_target_id")
    message_text = data.get("direct_message_text")
    charge_amount = int(data.get("direct_message_charge_amount") or 0)
    charge_type = data.get("direct_message_charge_type", "none")
    balance_before = float(data.get("direct_message_balance_before") or 0)
    include_balance = bool(data.get("direct_message_include_balance"))
    gift_title = data.get("direct_message_gift_title") or "هدية مستخدم جديد"
    raw_gift_days = data.get("direct_message_gift_days")
    gift_days = int(30 if raw_gift_days is None else raw_gift_days)
    gift_source_key = data.get("direct_message_gift_source_key") or f"direct:{uuid.uuid4()}"
    await state.clear()
    await call.answer()

    if not target_id or not message_text:
        return await safe_edit_text(call.message, "❌ انتهت جلسة الرسالة، يرجى بدء العملية مجدداً.", reply_markup=get_admin_dashboard_keyboard())

    gift_expires_at = None
    if charge_amount and charge_type in ("free", "paid"):
        new_balance = await admin_add_points(target_id, charge_amount, balance_type=charge_type)
        if new_balance is None:
            return await safe_edit_text(
                call.message,
                "❌ تعذر شحن الرصيد، لذلك لم يتم إرسال الرسالة.",
                reply_markup=get_admin_dashboard_keyboard(),
            )
    elif charge_amount and charge_type == "gift":
        gift_result = await admin_grant_direct_gift(
            target_id=target_id,
            amount=charge_amount,
            valid_days=gift_days,
            title=gift_title,
            source_key=gift_source_key,
        )
        if not gift_result or gift_result.get("status") != "ok":
            return await safe_edit_text(
                call.message,
                "❌ تعذر شحن الهدية، لذلك لم يتم إرسال الرسالة.",
                reply_markup=get_admin_dashboard_keyboard(),
            )
        new_balance = float(gift_result["total_points"])
        gift_expires_at = gift_result.get("expires_at")
    else:
        new_balance = balance_before

    outgoing_message = html.escape(message_text) if include_balance else message_text
    if include_balance:
        if charge_type == "gift" and charge_amount > 0:
            outgoing_message += (
                "\n\n🎁 <b>تفاصيل الهدية والرصيد</b>\n"
                f"┣ الهدية: <b>{html.escape(str(gift_title))}</b>\n"
                f"┣ النقاط المضافة: <code>+{charge_amount}</code> نقطة هدية\n"
                f"┣ انتهاء الهدية: {expiry_text(gift_expires_at)}\n"
                f"┣ ⏳ {remaining_expiry_text(gift_expires_at)}\n"
                f"┣ الرصيد قبل الشحن: <code>{balance_before:.2f}</code> نقطة\n"
                f"┗ الرصيد بعد الشحن: <code>{new_balance:.2f}</code> نقطة"
            )
        else:
            outgoing_message += (
                "\n\n📊 <b>تفاصيل الرصيد</b>\n"
                f"┣ قبل الشحن: <code>{balance_before:.2f}</code> نقطة\n"
                f"┗ بعد الشحن: <code>{new_balance:.2f}</code> نقطة"
            )

    try:
        await bot.send_message(chat_id=int(target_id), text=outgoing_message, parse_mode="HTML" if include_balance else None)
    except TelegramForbiddenError:
        return await safe_edit_text(
            call.message,
            f"🚫 تعذر إرسال الرسالة. المستخدم <code>{target_id}</code> حظر البوت أو لم يبدأ المحادثة.",
            reply_markup=get_admin_dashboard_keyboard(),
        )
    except Exception as e:
        logger.error(f"Error sending direct message to user {target_id}: {e}")
        return await safe_edit_text(
            call.message,
            "❌ حدث خطأ أثناء إرسال الرسالة. حاول مجدداً.",
            reply_markup=get_admin_dashboard_keyboard(),
        )

    if charge_type == "gift" and charge_amount > 0:
        charge_summary = (
            f"<code>{charge_amount}</code> نقطة (شحن هدايا — {html.escape(str(gift_title))}، "
            f"الصلاحية: {html.escape(_format_gift_validity_days(gift_days))})"
        )
    else:
        charge_summary = (
            f"<code>{charge_amount}</code> نقطة "
            f"({'مجاني' if charge_type == 'free' else 'مدفوع' if charge_type == 'paid' else 'بدون شحن'})"
        )
    await safe_edit_text(
        call.message,
        f"✅ تم إرسال الرسالة بنجاح إلى المستخدم <code>{target_id}</code>\n"
        f"💰 الشحن: {charge_summary}",
        reply_markup=get_admin_dashboard_keyboard(),
    )


@router.callback_query(F.data == "admin_confirm_broadcast", AdminState.waiting_for_broadcast_confirm)
async def process_confirm_broadcast(call: types.CallbackQuery, state: FSMContext):
    try:
        await call.answer()
    except TelegramBadRequest:
        pass

    data = await state.get_data()
    text_to_send = data.get("broadcast_text")
    await state.clear()

    try:
        users = await fetch_users_async()
    except Exception:
        logger.exception("Error fetching broadcast recipients")
        await safe_edit_text(call.message, "❌ تعذر تحميل قائمة المستخدمين. لم تُرسل الرسالة، حاول مجدداً.", reply_markup=get_admin_dashboard_keyboard())
        return
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

@router.callback_query(F.data == "admin_communication_menu")
async def open_communication_menu(call: types.CallbackQuery, state: FSMContext):
    """قسم التواصل: رسالة مخصصة أو جماعية."""
    if state:
        await state.clear()
    text = (
        "💬 <b>التواصل مع الطلاب</b>\n"
        "━━━━━━━━━━━━━━━━━━\n\n"
        "✉️ <b>رسالة مخصصة:</b> لطالب واحد، مع إمكانية شحن رصيد معه\n"
        "📢 <b>رسالة جماعية:</b> تصل لكل الطلاب دفعة وحدة"
    )
    await safe_edit_text(call.message, text, reply_markup=get_admin_communication_keyboard())
    try:
        await call.answer()
    except TelegramBadRequest:
        pass


@router.callback_query(F.data == "admin_users_menu")
async def open_users_menu(call: types.CallbackQuery, state: FSMContext):
    """قسم إدارة المستخدمين: بحث، استعراض، تصدير."""
    if state:
        await state.clear()
    text = (
        "👥 <b>إدارة المستخدمين</b>\n"
        "━━━━━━━━━━━━━━━━━━\n\n"
        "🔍 ابحث عن طالب محدد بالآيدي أو اليوزر\n"
        "👥 أو استعرض سجل كل الطلاب مصفحاً\n"
        "📥 أو صدّر القائمة كاملة كملف CSV"
    )
    await safe_edit_text(call.message, text, reply_markup=get_admin_users_menu_keyboard())
    try:
        await call.answer()
    except TelegramBadRequest:
        pass


@router.callback_query(F.data.startswith("admin_users_page_"))
async def admin_callback_users_page(call: types.CallbackQuery):
    page = int(call.data.split("_")[3])
    await render_users_page(call, page=page)


def _format_user_profile_report(u: dict) -> str:
    username_str = f"@{html.escape(str(u['username']))}" if u.get('username') and u['username'] != "Unknown" else "بدون يوزر"
    return (
        "👤 <b>معلومات المستخدم:</b>\n"
        f"┣ 🆔 الآيدي: <code>{u['user_id']}</code>\n"
        f"┣ 👤 اليوزر: {username_str}\n"
        f"┣ 🔗 {_telegram_profile_link(u['user_id'])}\n"
        f"┣ 📝 الاسم: <b>{html.escape(str(u.get('first_name') or '')[:100])} {html.escape(str(u.get('last_name') or '')[:100])}</b>\n"
        f"┣ 🔄 النقاط المجانية: <code>{float(u.get('free_points') or 0):.2f}</code>\n"
        f"┣ 🎁 الهدايا: <code>{float(u.get('gift_points') or 0):.2f}</code>\n"
        f"┣ 💳 النقاط المدفوعة: <code>{float(u.get('paid_points') or 0):.2f}</code>\n"
        f"┣ 💰 الإجمالي: <code>{user_total_points(u):.2f}</code>\n"
        f"┗ 📊 إجمالي الأسئلة المُولدة: <code>{u.get('total_questions', 0)}</code>"
    )


async def render_user_profile(event, user_id, banner: str = "") -> bool:
    """يرسم بروفايل طالب محدد بآيدي معروف مسبقاً (بدون الحاجة لإعادة البحث).
    قابلة لإعادة الاستدعاء من أي شاشة (بعد الشحن، بعد مراجعة النشاط...) عشان
    يضل زر "رجوع لبيانات الطالب" شغّال فعلياً بدل ما يقفز عالرئيسية.
    banner: نص تأكيد اختياري (مثلاً "تم الشحن بنجاح") يظهر فوق البروفايل.
    ترجع True لو لقيت المستخدم، False لو لأ."""
    users_data = await admin_search_user(str(user_id))
    if not users_data:
        if isinstance(event, types.CallbackQuery):
            await event.answer("❌ لم يعد هذا المستخدم موجوداً.", show_alert=True)
        else:
            await event.answer("❌ لم يعد هذا المستخدم موجوداً.")
        return False

    u = users_data[0]
    report = (f"{banner}\n\n" if banner else "") + _format_user_profile_report(u)
    keyboard = get_admin_user_actions_keyboard(u['user_id'], username=u.get('username'))

    if isinstance(event, types.CallbackQuery):
        await safe_edit_text(event.message, report, reply_markup=keyboard)
        try:
            await event.answer()
        except TelegramBadRequest:
            pass
    else:
        await event.answer(report, reply_markup=keyboard, parse_mode="HTML")
    return True


@router.callback_query(F.data.startswith("admin_user_profile_"))
async def callback_user_profile(call: types.CallbackQuery):
    """رجوع فعلي خطوة وحدة لبيانات طالب محدد (من شاشات الشحن/النشاط...) بدل
    القفز للقائمة الرئيسية وفقدان سياق الطالب."""
    user_id = call.data.replace("admin_user_profile_", "", 1)
    await render_user_profile(call, user_id)


@router.callback_query(F.data == "admin_search_prompt")
async def callback_search_prompt(call: types.CallbackQuery, state: FSMContext):
    await state.set_state(AdminState.waiting_for_search_query)
    await safe_edit_text(call.message, "🔍 <b>بحث عن مستخدم</b>\n\nأرسل الآن (الآيدي ID) أو (معرف المستخدم @Username أو جزء منه):", reply_markup=get_cancel_keyboard())
    try:
        await call.answer()
    except TelegramBadRequest:
        pass


async def render_user_search_page(event, query: str, token: str, page: int = 1):
    try:
        users, total, page, pages = await admin_get_users_page(page, query)
    except Exception:
        logger.exception("Error fetching admin search page")
        message = event.message if isinstance(event, types.CallbackQuery) else event
        await message.answer("❌ تعذر إجراء البحث، حاول مجدداً.")
        return
    if not total:
        report = "❌ لم يتم العثور على أي مستخدم بهذا البحث."
    else:
        report = (f"🔍 <b>نتائج البحث: {html.escape(query[:100])}</b>\n"
                  f"👥 {total} مستخدم — الصفحة {page} من {pages}\n\n")
    keyboard = []
    for index, user in enumerate(users, start=(page - 1) * 5 + 1):
        raw_uname = str(user.get("username") or "").strip()
        has_uname = bool(raw_uname and raw_uname != "Unknown")
        username = f"@{raw_uname[:64]}" if has_uname else "بدون يوزر"
        name_parts = [
            str(user.get(field) or "").strip()
            for field in ("first_name", "last_name")
            if user.get(field) and str(user.get(field)).strip() != "Unknown"
        ]
        name = " ".join(name_parts)[:100] or "بدون اسم"
        report += (f"<b>{index}. {html.escape(name)}</b>\n"
                   f"👤 {html.escape(username)} — 🆔 <code>{user['user_id']}</code>\n"
                   f"🔗 {_telegram_profile_link(user['user_id'])}\n\n")
        btn_label = f"👤 {index}. {raw_uname if has_uname else name[:24]} ({user['user_id']})"
        keyboard.append([types.InlineKeyboardButton(
            text=btn_label[:64], callback_data=f"admin_user_profile_{user['user_id']}")])
    navigation = []
    if page > 1:
        navigation.append(types.InlineKeyboardButton(text="⬅️ السابق", callback_data=f"admin_search_page_{token}_{page-1}"))
    if page < pages:
        navigation.append(types.InlineKeyboardButton(text="التالي ➡️", callback_data=f"admin_search_page_{token}_{page+1}"))
    if navigation:
        keyboard.append(navigation)
    keyboard.append([types.InlineKeyboardButton(text="🔍 بحث جديد", callback_data="admin_search_prompt")])
    keyboard.append([types.InlineKeyboardButton(text="🔙 إدارة المستخدمين", callback_data="admin_users_menu")])
    markup = types.InlineKeyboardMarkup(inline_keyboard=keyboard)
    if isinstance(event, types.CallbackQuery):
        await safe_edit_text(event.message, report, reply_markup=markup)
    else:
        await event.answer(report, reply_markup=markup, parse_mode="HTML")


@router.message(AdminState.waiting_for_search_query)
async def process_search_user(msg: types.Message, state: FSMContext):
    query = (msg.text or "").strip().lstrip("@").strip()
    if not query:
        await msg.answer("⚠️ أرسل الآيدي أو المعرف أو جزءاً من المعرف.")
        return

    # إذا أدخل الأدمن آيدي كاملاً أو معرفاً كاملاً ومطابقاً، نفتح بروفايل الطالب فوراً بدون شاشة نتائج البحث
    exact_user = await admin_find_exact_user(query)
    if exact_user:
        await state.clear()
        await render_user_profile(msg, exact_user["user_id"])
        return

    token = uuid.uuid4().hex[:12]
    await state.clear()
    await state.update_data(admin_user_search=query, admin_user_search_token=token)
    await render_user_search_page(msg, query, token)


@router.callback_query(F.data.startswith("admin_search_page_"))
async def callback_user_search_page(call: types.CallbackQuery, state: FSMContext):
    data = await state.get_data()
    parts = call.data.split("_")
    if len(parts) != 5 or parts[3] != data.get("admin_user_search_token") or not parts[4].isdigit():
        await call.answer("انتهت جلسة البحث، ابدأ بحثاً جديداً.", show_alert=True)
        return
    await call.answer()
    await render_user_search_page(call, data["admin_user_search"], parts[3], int(parts[4]))


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
        await render_user_profile(
            call, target_id,
            banner=f"✅ <b>تم الشحن بنجاح!</b> الكمية المضافة: <code>+{amount}</code> 🟢"
        )
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
        await render_user_profile(
            msg, target_id,
            banner=f"✅ <b>تم الشحن بنجاح!</b> الكمية المضافة: <code>+{amount}</code> 🟢"
        )
        await send_points_notification(target_id, amount, new_balance)
    else:
        await msg.answer("❌ حدث خطأ أثناء الشحن. حاول مجدداً.", reply_markup=get_admin_dashboard_keyboard())
    await state.clear()


@router.callback_query(F.data == "admin_stats")
async def show_db_stats(call: types.CallbackQuery):
    """قسم التحليلات والإحصائيات: يبدأ بلمحة سريعة (الأرقام العامة) وبعدين
    أزرار التحليلات المفصّلة (فترات زمنية، النشطون اليوم، الأخطاء، الإحالات...)
    بنفس الشاشة - بدل ما يكونوا شاشتين منفصلتين متشابهات بالاسم."""
    stats = await admin_get_global_stats()
    text = (
        "📊 <b>التحليلات والإحصائيات</b>\n"
        "━━━━━━━━━━━━━━━━━━\n\n"
        f"👥 إجمالي الطلاب المسجلين: <code>{stats['total_users']}</code>\n"
        f"📝 إجمالي الأسئلة المُولدة: <code>{stats['total_questions']}</code>\n\n"
        "اختر تقريراً مفصلاً من تحت:"
    )
    await safe_edit_text(call.message, text, reply_markup=get_analytics_keyboard(7))
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
        writer.writerow(["User ID", "Username", "First Name", "Last Name", "Free Points", "Gift Points", "Paid Points", "Total Points", "Total Questions", "Joined At"])
        
        for u in users:
            joined_time_syria = _short_syria_time(u.get('joined_at'))
            writer.writerow([
                sanitize_csv_value(u.get('user_id', '')),
                sanitize_csv_value(u.get('username', 'Unknown')),
                sanitize_csv_value(u.get('first_name', 'Unknown')),
                sanitize_csv_value(u.get('last_name', 'Unknown')),
                sanitize_csv_value(u.get('free_points', 0)),
                sanitize_csv_value(u.get('gift_points', 0)),
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


def _short_syria_time(iso_str: str) -> str:
    """تحويل توقيت قاعدة البيانات إلى توقيت سوريا (12 ساعة بتنسيق صباحاً/مساءً).
    🩹 أُعيدت التسمية (كانت format_syria_time) لأنها كانت تُخفي الدالة المستوردة
    بنفس الاسم من constants.py (توقيع مختلف: (value, fmt)) - خطر باغ صامت لأي
    استدعاء مستقبلي بهذا الملف يتوقع توقيع constants.py الأصلي."""
    return _format_syria_time(iso_str)


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
                await call.answer("📭 هذا الطالب لم يُنشئ أو يستخدم من الكاش أي كويز بعد.", show_alert=True)
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
            time_syria = _short_syria_time(q.get("created_at"))
            likes = q.get("likes", 0)
            dislikes = q.get("dislikes", 0)
            # 🆕 تمييز الكويزات المُستخدمة من الكاش المركزي (لم ينشئها الطالب بنفسه)
            is_cached = q.get("is_cached", False)
            cache_tag = " ♻️ <i>(من الكاش)</i>" if is_cached else ""
            time_label = "🕒 تاريخ الاستخدام" if is_cached else "🕒 تاريخ الإنشاء"

            report_lines.append(
                f"<b>{idx}. {html.escape(title)}</b>{cache_tag}\n"
                f" ┣ {time_label} (توقيت سوريا): <code>{time_syria}</code>\n"
                f" ┗ 👍 {likes} | 👎 {dislikes}\n"
                f"───────────────────"
            )

            kb.append([
                types.InlineKeyboardButton(text=f"🎯 تجربة #{idx}: {title[:18]}", callback_data=f"afb_try_{quiz_id}"),
                # 🆕 حذف نهائي مباشر من هنا - الراوتر مقيّد بـ IsAdminFilter أصلاً فالأدمن
                # مصرَّح له دائماً (راجع services/quiz_permissions.py/handlers/quiz_delete.py).
                types.InlineKeyboardButton(text="🗑 حذف", callback_data=f"qdel_{quiz_id}"),
            ])

        nav_row = []
        if page > 1:
            nav_row.append(types.InlineKeyboardButton(text="◀️ السابق", callback_data=f"admin_user_quizzes_{target_id}_p_{page - 1}"))
        if page < total_pages:
            nav_row.append(types.InlineKeyboardButton(text="التالي ▶️", callback_data=f"admin_user_quizzes_{target_id}_p_{page + 1}"))
        if nav_row:
            kb.append(nav_row)

        kb.append([types.InlineKeyboardButton(text="🔙 رجوع لبيانات الطالب", callback_data=f"admin_user_profile_{target_id}")])

        text = (
            f"🎯 <b>كويزات الطالب (<code>{target_id}</code>) — المُنشأة والمُستخدمة من الكاش ♻️</b>\n"
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


# ==================== إدارة حذف المستخدمين وتصفير عمليات Redis ====================

def _format_consent_status_line(consent: dict | None) -> str:
    if not consent:
        return "➖ غير مسجل (لم يُعرض عليه بعد)"
    status = consent.get("status")
    tv = int(consent.get("terms_version") or 0)
    pv = int(consent.get("privacy_version") or 0)
    if status == "accepted":
        return f"✅ موافق (الشروط v{tv} · الخصوصية v{pv})"
    if status == "declined":
        return f"❌ رافض (الشروط v{tv} · الخصوصية v{pv})"
    return "⏳ بانتظار الموافقة (تم عرض الرسالة)"


def _format_redis_status_line(redis_info: dict | None) -> str:
    if not redis_info or not redis_info.get("is_busy"):
        return "🟢 خامل (لا توجد عمليات أو أقفال عالقة)"
    parts = []
    if redis_info.get("fsm_state"):
        parts.append(str(redis_info.get("fsm_label") or redis_info["fsm_state"]))
    if redis_info.get("has_processing_lock"):
        parts.append("🔒 قفل معالجة نشط (UserLock)")
    if redis_info.get("has_pending_navigation"):
        parts.append("🧭 انتقال جلسة معلّق")
    return "🟠 " + " | ".join(parts)


def _build_user_delete_menu_text(
    target_id: int,
    summary: dict,
    redis_info: dict | None = None,
    banner: str = "",
) -> str:
    u = summary.get("user") or {}
    owned_count = int(summary.get("owned_quizzes_count") or 0)
    consent = summary.get("consent")
    username_str = (
        f"@{html.escape(str(u['username']))}"
        if u.get("username") and u["username"] != "Unknown"
        else "بدون يوزر"
    )
    full_name = (
        f"{html.escape(str(u.get('first_name') or '')[:100])} "
        f"{html.escape(str(u.get('last_name') or '')[:100])}"
    ).strip() or "غير مسجل بجدول الطلاب"

    is_test_account = bool(summary.get("is_test_account"))
    account_type_line = (
        "🧪 حساب اختبار معتمد (@Kp7771a) — تصفير الموافقة المنفرد متاح للفحص"
        if is_test_account
        else "🔒 حساب طالب فعلي — سجل الموافقة محمي قانونياً من الحذف المنفرد"
    )
    header = f"{banner}\n\n" if banner else ""
    return (
        f"{header}"
        "🗑 <b>خيارات حذف الطالب وتصفير جلساته</b>\n"
        "━━━━━━━━━━━━━━━━━━\n"
        f"┣ 🆔 الآيدي: <code>{target_id}</code>\n"
        f"┣ 👤 الطالب: <b>{full_name}</b> ({username_str})\n"
        f"┣ 🛡 تصنيف الحساب: {account_type_line}\n"
        f"┣ 📜 حالة الموافقة على البوت: {_format_consent_status_line(consent)}\n"
        f"┣ 🗂 الكويزات المولدة عن طريقه: <code>{owned_count}</code> كويز\n"
        f"┗ ⚡ حالة Redis الحالية: {_format_redis_status_line(redis_info)}\n\n"
        "اختر الإجراء المطلوب بدقة (سيُطلب منك تأكيد إضافي قبل أي حذف):"
    )


async def _render_user_delete_menu(call: types.CallbackQuery, target_id: int, banner: str = "") -> bool:
    summary = await admin_get_user_deletion_summary(target_id)
    if not summary:
        try:
            await call.answer("❌ لم يعد هذا المستخدم موجوداً بقاعدة البيانات.", show_alert=True)
        except TelegramBadRequest:
            pass
        return False
    redis_info = await get_user_redis_active_summary(target_id)
    text = _build_user_delete_menu_text(target_id, summary, redis_info, banner=banner)
    kb = get_admin_user_delete_menu_keyboard(
        target_id,
        int(summary.get("owned_quizzes_count") or 0),
        is_test_account=bool(summary.get("is_test_account")),
    )
    await safe_edit_text(call.message, text, reply_markup=kb)
    try:
        await call.answer()
    except TelegramBadRequest:
        pass
    return True


@router.callback_query(F.data.startswith("admin_user_delete_menu_"))
async def show_user_delete_menu(call: types.CallbackQuery):
    try:
        target_id = int(call.data.replace("admin_user_delete_menu_", "", 1))
        await _render_user_delete_menu(call, target_id)
    except Exception as e:
        logger.error(f"Error opening user delete menu: {e}")
        try:
            await call.answer("❌ تعذر فتح قائمة الحذف.", show_alert=True)
        except TelegramBadRequest:
            pass


@router.callback_query(F.data.startswith("admin_user_redis_reset_"))
async def handle_admin_user_redis_reset(call: types.CallbackQuery):
    try:
        target_id = int(call.data.replace("admin_user_redis_reset_", "", 1))
        res = await reset_user_redis_operations(target_id, include_consent_cache=False)
        prev_label = html.escape(str(res.get("prev_state_label") or "لا توجد حالة معلّقة"))
        lock_str = "نعم (تم فكّه وإلغاء المهمة)" if res.get("had_processing_lock") else "لا"
        nav_str = "نعم (تم مسحه وتنظيف المرفقات)" if res.get("had_pending_navigation") else "لا"
        banner = (
            "⚡ <b>تم تصفير جميع عمليات وجلسات Redis النشطة للطالب!</b>\n"
            f"┣ 📌 الحالة السابقة: <code>{prev_label}</code>\n"
            f"┣ 🔒 قفل المعالجة (UserLock): <b>{lock_str}</b>\n"
            f"┣ 🧭 انتقال جلسة معلّق: <b>{nav_str}</b>\n"
            f"┗ 🧹 المفاتيح الممسوحة: <code>{res.get('deleted_keys', 0)}</code>"
        )
        # إذا كان الأدمن داخل شاشة خيارات الحذف، نبقى فيها مع البانر؛ وإلا نحدّث بروفايل الطالب
        current_text = getattr(call.message, "text", None) or getattr(call.message, "caption", None) or ""
        if "خيارات حذف الطالب" in current_text:
            await _render_user_delete_menu(call, target_id, banner=banner)
        else:
            rendered = await render_user_profile(call, target_id, banner=banner)
            if not rendered:
                await safe_edit_text(
                    call.message,
                    f"{banner}\n\nℹ️ هذا المعرف (<code>{target_id}</code>) غير مسجل حالياً بجدول الطلاب.",
                    reply_markup=get_admin_users_menu_keyboard(),
                )
        try:
            await call.answer("⚡ تم تصفير عمليات Redis بنجاح")
        except TelegramBadRequest:
            pass
    except Exception as e:
        logger.error(f"Error resetting Redis operations for user: {e}")
        try:
            await call.answer("❌ حدث خطأ أثناء تصفير عمليات Redis.", show_alert=True)
        except TelegramBadRequest:
            pass


async def _render_generated_quizzes_preview(call: types.CallbackQuery, target_id: int, page: int = 1, banner: str = "") -> None:
    page = max(1, int(page))
    offset = (page - 1) * USER_QUIZZES_PAGE_SIZE
    quizzes, total = await admin_get_user_generated_quizzes(
        creator_id=target_id, limit=USER_QUIZZES_PAGE_SIZE, offset=offset
    )
    if total == 0:
        if banner:
            await _render_user_delete_menu(call, target_id, banner=banner + "\n📭 لم يتبقَّ أي كويز مولد لهذا الطالب.")
            return
        try:
            await call.answer("📭 هذا الطالب لم يُنشئ أي كويزات مولدة خاصة به.", show_alert=True)
        except TelegramBadRequest:
            pass
        return

    total_pages = max(1, -(-total // USER_QUIZZES_PAGE_SIZE))
    if page > total_pages:
        page = total_pages
        offset = (page - 1) * USER_QUIZZES_PAGE_SIZE
        quizzes, total = await admin_get_user_generated_quizzes(
            creator_id=target_id, limit=USER_QUIZZES_PAGE_SIZE, offset=offset
        )

    report_lines = []
    kb = []
    for idx, q in enumerate(quizzes, start=offset + 1):
        quiz_id = q["id"]
        title = q.get("source_title") or "كويز بدون عنوان"
        time_syria = _short_syria_time(q.get("created_at"))
        likes = q.get("likes", 0)
        dislikes = q.get("dislikes", 0)
        q_len = q.get("questions_len") or 0
        q_len_str = f" · <code>{q_len}</code> سؤال" if q_len else ""

        report_lines.append(
            f"<b>{idx}. {html.escape(title)}</b>{q_len_str}\n"
            f" ┣ 🕒 تاريخ الإنشاء: <code>{time_syria}</code>\n"
            f" ┗ 👍 {likes} | 👎 {dislikes}\n"
            "───────────────────"
        )
        kb.append([
            types.InlineKeyboardButton(text=f"🎯 تجربة #{idx}: {title[:18]}", callback_data=f"afb_try_{quiz_id}"),
            types.InlineKeyboardButton(text="🗑 حذف فردي", callback_data=f"admin_del_single_q_{target_id}_{page}_{quiz_id}"),
        ])

    nav_row = []
    if page > 1:
        nav_row.append(types.InlineKeyboardButton(text="◀️ السابق", callback_data=f"admin_del_preview_q_{target_id}_{page - 1}"))
    if page < total_pages:
        nav_row.append(types.InlineKeyboardButton(text="التالي ▶️", callback_data=f"admin_del_preview_q_{target_id}_{page + 1}"))
    if nav_row:
        kb.append(nav_row)

    kb.append([
        types.InlineKeyboardButton(text=f"🗂 حذف كل كويزاته ({total})", callback_data=f"admin_del_ask_quizzes_{target_id}"),
        types.InlineKeyboardButton(text="👤 حذف الطالب مع الإبقاء عليها", callback_data=f"admin_del_ask_keepq_{target_id}"),
    ])
    kb.append([types.InlineKeyboardButton(text="💥 حذف الطالب مع كويزاته بالكامل", callback_data=f"admin_del_ask_full_{target_id}")])
    kb.append([types.InlineKeyboardButton(text="🔙 رجوع لخيارات حذف الطالب", callback_data=f"admin_user_delete_menu_{target_id}")])

    header = f"{banner}\n\n" if banner else ""
    text = (
        f"{header}"
        f"👁 <b>استعراض الكويزات المولدة عن طريق الطالب (<code>{target_id}</code>)</b>\n"
        f"📄 الصفحة {page} من أصل {total_pages} (الإجمالي: <code>{total}</code> كويز مولد)\n"
        "───────────────────\n\n"
        + "\n".join(report_lines)
    )
    await safe_edit_text(call.message, text, reply_markup=types.InlineKeyboardMarkup(inline_keyboard=kb))
    try:
        await call.answer()
    except TelegramBadRequest:
        pass


@router.callback_query(F.data.startswith("admin_del_preview_q_"))
async def show_user_generated_quizzes_preview(call: types.CallbackQuery):
    try:
        rest = call.data.replace("admin_del_preview_q_", "", 1)
        target_str, page_str = rest.rsplit("_", 1)
        await _render_generated_quizzes_preview(call, int(target_str), int(page_str))
    except Exception as e:
        logger.error(f"Error rendering generated quizzes preview: {e}")
        try:
            await call.answer("❌ تعذر استعراض الكويزات المولدة.", show_alert=True)
        except TelegramBadRequest:
            pass


@router.callback_query(F.data.startswith("admin_del_single_q_"))
async def delete_single_generated_quiz_from_preview(call: types.CallbackQuery):
    try:
        rest = call.data.replace("admin_del_single_q_", "", 1)
        target_str, page_str, quiz_id = rest.split("_", 2)
        target_id = int(target_str)
        page = int(page_str)

        ok = await admin_delete_quiz(quiz_id)
        if ok:
            try:
                from services.runner_cleanup import delete_runner_prefix
                await delete_runner_prefix(quiz_id)
            except Exception:
                pass
            await _render_generated_quizzes_preview(
                call, target_id, page=page, banner="✅ <b>تم حذف الكويز المحدد نهائياً.</b>"
            )
        else:
            try:
                await call.answer("❌ تعذر حذف هذا الكويز.", show_alert=True)
            except TelegramBadRequest:
                pass
    except Exception as e:
        logger.error(f"Error deleting single quiz from preview: {e}")
        try:
            await call.answer("❌ حدث خطأ أثناء حذف الكويز.", show_alert=True)
        except TelegramBadRequest:
            pass


@router.callback_query(F.data.startswith("admin_del_ask_"))
async def ask_user_delete_confirmation(call: types.CallbackQuery):
    try:
        rest = call.data.replace("admin_del_ask_", "", 1)
        mode, target_str = rest.split("_", 1)
        target_id = int(target_str)

        if mode in ("full", "keepq") and ADMIN_ID and int(target_id) == int(ADMIN_ID):
            await call.answer("⚠️ لا يمكن حذف حساب الأدمن الرئيسي من البوت.", show_alert=True)
            return

        summary = await admin_get_user_deletion_summary(target_id)
        if not summary:
            await call.answer("❌ لم يعد هذا المستخدم موجوداً.", show_alert=True)
            return

        owned_count = int(summary.get("owned_quizzes_count") or 0)
        consent_line = _format_consent_status_line(summary.get("consent"))

        if mode == "full":
            desc = (
                "⚠️ <b>تأكيد الحذف النهائي الشامل (حذف المستخدم وكامل أثره)</b>\n"
                "━━━━━━━━━━━━━━━━━━\n"
                f"المستخدم المستهدف: <code>{target_id}</code>\n\n"
                "سيتم تنفيذ الآتي بشكل نهائي لا رجعة فيه:\n"
                "• حذف حساب الطالب ورصيده وهداياه وسجل محفظته بالكامل.\n"
                f"• حذف جميع الكويزات المولدة عن طريقه (<code>{owned_count}</code> كويز) من الجدول المركزي والكاش.\n"
                "• حذف موافقته القانونية على شروط البوت وسياسة الخصوصية.\n"
                "• حذف كافة محاولاته، نقاطه، تقييماته، تحدياته، وسجلات نشاطه.\n"
                "• تصفير كافة عملياته وجلساته النشطة في Redis."
            )
        elif mode == "keepq":
            desc = (
                "⚠️ <b>تأكيد حذف الطالب بالكامل مع الإبقاء على كويزاته</b>\n"
                "━━━━━━━━━━━━━━━━━━\n"
                f"المستخدم المستهدف: <code>{target_id}</code>\n\n"
                "سيتم تنفيذ الآتي بشكل نهائي:\n"
                "• حذف حساب الطالب ورصيده وهداياه وموافقته القانونية وكل سجلاته الشخصية.\n"
                "• تصفير كافة عملياته وجلساته النشطة في Redis.\n"
                f"• ✅ <b>الإبقاء على جميع الكويزات المولدة عن طريقه (<code>{owned_count}</code> كويز)</b> في الكاش المركزي وروابط المشاركة ليستفيد منها باقي الطلاب (مع فك ارتباطها بحسابه)."
            )
        elif mode == "consent":
            if not summary.get("is_test_account"):
                await call.answer(
                    "🔒 تصفير الموافقة المنفرد مقيّد قانونياً بحسابات الاختبار (@Kp7771a) فقط لحماية سجل الموافقات القانونية للطلاب.",
                    show_alert=True,
                )
                return
            desc = (
                "🧪 <b>تأكيد تصفير الموافقة على البوت (مخصص لحساب الاختبار @Kp7771a فقط)</b>\n"
                "━━━━━━━━━━━━━━━━━━\n"
                f"حساب الاختبار المستهدف: <code>{target_id}</code>\n"
                f"الحالة الحالية: {consent_line}\n\n"
                "سيتم تنفيذ الآتي لأغراض اختبار بوابة الشروط (QA):\n"
                "• حذف سجل موافقة حساب الاختبار من قاعدة البيانات ومسح كاش الموافقة في Redis فوراً.\n"
                "• عند أول رسالة أو ضغطة زر قادمة من حساب الاختبار، ستظهر له بوابة الموافقة على الشروط وسياسة الخصوصية من جديد.\n"
                "• ℹ️ ملاحظة قانونية: هذا الخيار محجوب برمجياً وقاعدياً عن جميع حسابات الطلاب الفعليين لحماية حجية سجل الموافقات القانونية (<code>user_consent_events</code>)."
            )
        elif mode == "quizzes":
            desc = (
                "⚠️ <b>تأكيد حذف كويزات الطالب المولدة فقط</b>\n"
                "━━━━━━━━━━━━━━━━━━\n"
                f"المستخدم المستهدف: <code>{target_id}</code>\n"
                f"عدد الكويزات المولدة عن طريقه: <code>{owned_count}</code> كويز\n\n"
                "سيتم تنفيذ الآتي:\n"
                f"• حذف جميع الكويزات التي أنشأها هذا الطالب (<code>{owned_count}</code> كويز) نهائياً من الجدول المركزي والكاش.\n"
                "• ✅ يبقى حساب الطالب، رصيده، هداياه، وموافقته على البوت كما هي دون حذف."
            )
        else:
            await call.answer("خيار غير معروف.", show_alert=True)
            return

        kb = get_admin_user_delete_confirm_keyboard(target_id, mode, owned_count)
        await safe_edit_text(call.message, desc, reply_markup=kb)
        try:
            await call.answer()
        except TelegramBadRequest:
            pass
    except Exception as e:
        logger.error(f"Error asking user delete confirmation: {e}")
        try:
            await call.answer("❌ تعذر تجهيز شاشة التأكيد.", show_alert=True)
        except TelegramBadRequest:
            pass


@router.callback_query(F.data.startswith("admin_del_exec_"))
async def execute_user_delete_action(call: types.CallbackQuery):
    try:
        rest = call.data.replace("admin_del_exec_", "", 1)
        mode, target_str = rest.split("_", 1)
        target_id = int(target_str)

        mode_map = {
            "full": "full_wipe",
            "keepq": "keep_quizzes",
            "consent": "consent_only",
            "quizzes": "quizzes_only",
        }
        db_mode = mode_map.get(mode)
        if not db_mode:
            await call.answer("خيار غير صالح.", show_alert=True)
            return

        if mode in ("full", "keepq") and ADMIN_ID and int(target_id) == int(ADMIN_ID):
            await call.answer("⚠️ لا يمكن حذف حساب الأدمن الرئيسي.", show_alert=True)
            return

        if mode == "consent":
            summary = await admin_get_user_deletion_summary(target_id)
            if not summary or not summary.get("is_test_account"):
                await call.answer(
                    "🔒 تصفير الموافقة المنفرد مقيّد قانونياً بحسابات الاختبار (@Kp7771a) فقط.",
                    show_alert=True,
                )
                return

        result = await admin_delete_user_action(target_id, db_mode)
        if isinstance(result, dict) and result.get("status") == "forbidden_non_test_account":
            await call.answer(
                "🔒 تصفير الموافقة المنفرد مقيّد قانونياً بحسابات الاختبار (@Kp7771a) فقط.",
                show_alert=True,
            )
            return
        if not result or result.get("status") != "ok":
            await call.answer("❌ تعذر تنفيذ عملية الحذف في قاعدة البيانات، حاول مجدداً.", show_alert=True)
            return

        deleted_q = int(result.get("deleted_quizzes_count") or 0)
        kept_q = int(result.get("kept_quizzes_count") or 0)

        if mode == "full":
            redis_res = await reset_user_redis_operations(target_id, include_consent_cache=True)
            text = (
                "✅ <b>تم حذف المستخدم وكامل أثره نهائياً بنجاح!</b>\n"
                "━━━━━━━━━━━━━━━━━━\n"
                f"┣ 🆔 الآيدي المحذوف: <code>{target_id}</code>\n"
                f"┣ 🗂 الكويزات المولدة المحذوفة: <code>{deleted_q}</code> كويز\n"
                "┣ 📜 موافقة الشروط والخصوصية: <b>تم حذفها بالكامل</b>\n"
                f"┗ ⚡ مفاتيح وجلسات Redis المصفّرة: <code>{redis_res.get('deleted_keys', 0)}</code>"
            )
            await safe_edit_text(call.message, text, reply_markup=get_admin_users_menu_keyboard())
            try:
                await call.answer("🗑️ تم الحذف النهائي الشامل")
            except TelegramBadRequest:
                pass
            return

        if mode == "keepq":
            redis_res = await reset_user_redis_operations(target_id, include_consent_cache=True)
            text = (
                "✅ <b>تم حذف المستخدم بالكامل مع الإبقاء على كويزاته بنجاح!</b>\n"
                "━━━━━━━━━━━━━━━━━━\n"
                f"┣ 🆔 الآيدي المحذوف: <code>{target_id}</code>\n"
                f"┣ 🗂 الكويزات المحتفظ بها في الكاش: <code>{kept_q}</code> كويز\n"
                "┣ 📜 موافقة الشروط والخصوصية: <b>تم حذفها بالكامل</b>\n"
                f"┗ ⚡ مفاتيح وجلسات Redis المصفّرة: <code>{redis_res.get('deleted_keys', 0)}</code>"
            )
            await safe_edit_text(call.message, text, reply_markup=get_admin_users_menu_keyboard())
            try:
                await call.answer("✅ تم حذف المستخدم مع الإبقاء على كويزاته")
            except TelegramBadRequest:
                pass
            return

        if mode == "consent":
            await purge_user_consent_cache(target_id)
            banner = (
                "✅ <b>تم حذف حالة موافقة الطالب على البوت ومسح الكاش فوراً!</b>\n"
                "سيُطلب منه الموافقة على الشروط وسياسة الخصوصية مجدداً عند أول تفاعل."
            )
            await _render_user_delete_menu(call, target_id, banner=banner)
            try:
                await call.answer("📜 تم حذف حالة الموافقة")
            except TelegramBadRequest:
                pass
            return

        if mode == "quizzes":
            banner = f"✅ <b>تم حذف جميع الكويزات المولدة عن طريق هذا الطالب (<code>{deleted_q}</code> كويز) مع الإبقاء على حسابه!</b>"
            await _render_user_delete_menu(call, target_id, banner=banner)
            try:
                await call.answer(f"🗂 تم حذف {deleted_q} كويز")
            except TelegramBadRequest:
                pass
            return

    except Exception as e:
        logger.error(f"Error executing user delete action: {e}")
        try:
            await call.answer("❌ حدث خطأ غير متوقع أثناء تنفيذ الحذف.", show_alert=True)
        except TelegramBadRequest:
            pass
