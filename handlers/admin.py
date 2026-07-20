import asyncio
import os
import io
import csv
from aiogram import Router, types, F
from aiogram.filters import Command, CommandObject, BaseFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import StatesGroup, State
from aiogram.types import BufferedInputFile
from aiogram.exceptions import TelegramBadRequest

# 🚀 تم تعديل الاستيراد لاستجلاب كائن supabase المشترك لمنع تسريب الاتصالات
from config import bot, ADMIN_ID
from supabase_helper import (
    supabase, 
    admin_add_points, 
    admin_get_global_stats, 
    admin_search_user,
    admin_get_recent_feedbacks  # 🆕 استيراد دالة جلب الملاحظات الجديدة
)
from logger import get_logger

logger = get_logger(__name__)
router = Router()


def user_total_points(user: dict) -> float:
    """Calculate a display balance from the segregated database columns."""
    return float(user.get("free_points") or 0) + float(user.get("paid_points") or 0)

# ==================== فلتر الحماية المركزي للإدارة ====================
class IsAdminFilter(BaseFilter):
    """
    جدار حماية ذكي يفحص صلاحيات الآدمن للرسائل وضغطات الأزرار تلقائياً
    """
    async def __call__(self, event: types.TelegramObject) -> bool:
        user = getattr(event, "from_user", None)
        if not user:
            return False
        return str(user.id) == str(ADMIN_ID)

# 🛡️ تطبيق الفلتر المركزي على الراوتر بالكامل لجميع الأحداث القادمة
router.message.filter(IsAdminFilter())
router.callback_query.filter(IsAdminFilter())

# تعريف حالات الـ FSM للإدارة
class AdminState(StatesGroup):
    waiting_for_search_query = State()
    waiting_for_charge_amount = State()

# دالة مساعدة آمنة لتعديل الرسائل لتجنب خطأ الـ Telegram التكراري
async def safe_edit_text(message: types.Message, text: str, reply_markup=None):
    try:
        await message.edit_text(text, reply_markup=reply_markup, parse_mode="HTML")
    except TelegramBadRequest:
        pass

# 🚀 تم التعديل: تحويل جلب المستخدمين إلى دالة Async أصيلة ومباشرة
async def fetch_users_async():
    res = await supabase.table("users").select("*").order("joined_at", desc=True).execute()
    return res.data

# دالة مساعدة لإرسال إشعار لبق للمستخدم عند شحن نقاطه
async def send_points_notification(target_id: int, amount: int, new_balance: int):
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

# ==================== لوحات الأزرار (Keyboards) المدمجة ====================

def get_admin_dashboard_keyboard() -> types.InlineKeyboardMarkup:
    kb = [
        [types.InlineKeyboardButton(text="🔍 البحث عن مستخدم", callback_data="admin_search_prompt")],
        [types.InlineKeyboardButton(text="👥 استعراض الطلاب (مصفّح)", callback_data="admin_users_page_1")],
        [types.InlineKeyboardButton(text="📊 الإحصائيات", callback_data="admin_stats"),
         types.InlineKeyboardButton(text="📥 تصدير CSV", callback_data="admin_export_users")],
        [types.InlineKeyboardButton(text="💬 مراجعة الملاحظات والتقييمات", callback_data="admin_view_feedbacks")], # 🆕 الزر الجديد مضاف هنا
        [types.InlineKeyboardButton(text="❌ إغلاق القائمة", callback_data="admin_cancel")]
    ]
    return types.InlineKeyboardMarkup(inline_keyboard=kb)

def get_admin_user_actions_keyboard(target_id: int) -> types.InlineKeyboardMarkup:
    kb = [
        [types.InlineKeyboardButton(text="💰 شحن نقاط للمستخدم", callback_data=f"admin_charge_menu_{target_id}")],
        [types.InlineKeyboardButton(text="⚙️ لوحة التحكم", callback_data="admin_main_menu")]
    ]
    return types.InlineKeyboardMarkup(inline_keyboard=kb)

def get_admin_charge_options_keyboard(target_id: int) -> types.InlineKeyboardMarkup:
    kb = [
        [
            types.InlineKeyboardButton(text="+10", callback_data=f"admin_charge_quick_10_{target_id}"),
            types.InlineKeyboardButton(text="+50", callback_data=f"admin_charge_quick_50_{target_id}"),
            types.InlineKeyboardButton(text="+100", callback_data=f"admin_charge_quick_100_{target_id}")
        ],
        [types.InlineKeyboardButton(text="✍️ إدخال كمية مخصصة (يدوي)", callback_data=f"admin_charge_manual_{target_id}")],
        [types.InlineKeyboardButton(text="🔙 إلغاء والعودة", callback_data="admin_main_menu")]
    ]
    return types.InlineKeyboardMarkup(inline_keyboard=kb)

def get_cancel_keyboard() -> types.InlineKeyboardMarkup:
    kb = [[types.InlineKeyboardButton(text="❌ إلغاء العملية", callback_data="admin_main_menu")]]
    return types.InlineKeyboardMarkup(inline_keyboard=kb)


# ==================== Central Renderers ====================

async def render_admin_dashboard(event, state: FSMContext = None):
    if state:
        await state.clear()
    text = "⚙️ <b>لوحة تحكم الإدارة</b>\n\nأهلاً بك، اختر الإجراء الذي تود القيام به من القائمة أدناه:"
    reply_markup = get_admin_dashboard_keyboard()
    
    if isinstance(event, types.Message):
        await event.answer(text, reply_markup=reply_markup, parse_mode="HTML")
    elif isinstance(event, types.CallbackQuery):
        await safe_edit_text(event.message, text, reply_markup=reply_markup)
        await event.answer()

async def render_users_page(event, page: int = 1):
    # ✅ تم التعديل: استدعاء مباشر غير متزامن
    users = await fetch_users_async()
    if not users:
        text = "📭 لا يوجد أي طلاب مسجلين حالياً."
        if isinstance(event, types.Message):
            await event.answer(text, reply_markup=get_admin_dashboard_keyboard(), parse_mode="HTML")
        elif isinstance(event, types.CallbackQuery):
            await safe_edit_text(event.message, text, reply_markup=get_admin_dashboard_keyboard())
            await event.answer()
        return

    total_users = len(users)
    per_page = 5
    total_pages = (total_users + per_page - 1) // per_page
    
    if page < 1: page = 1
    if page > total_pages: page = total_pages
    
    start_idx = (page - 1) * per_page
    end_idx = start_idx + per_page
    page_users = users[start_idx:end_idx]
    
    report = f"👥 <b>سجل الطلاب المسجلين ({page} من {total_pages}):</b>\n\n"
    for idx, u in enumerate(page_users, start=start_idx + 1):
        username_str = f"@{u['username']}" if u['username'] and u['username'] != "Unknown" else "بدون يوزر"
        report += (
            f"<b>{idx}. آيدي:</b> <code>{u['user_id']}</code>\n"
            f"┣ 👤 اليوزر: {username_str}\n"
            f"┣ 📝 الاسم: <b>{u.get('first_name', 'Unknown')} {u.get('last_name', 'Unknown')}</b>\n"
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
        await event.answer()


# ==================== الأوامر النصية (دعم القائمة الجانبية للبوت) ====================

@router.message(Command("admin"))
async def admin_cmd_dashboard(msg: types.Message, state: FSMContext):
    await render_admin_dashboard(msg, state)

@router.message(Command("searchuser"))
async def admin_cmd_search(msg: types.Message, state: FSMContext):
    await state.set_state(AdminState.waiting_for_search_query)
    await msg.answer("🔍 <b>بحث عن مستخدم</b>\n\nأرسل الآن (الآيدي ID) أو (معرف المستخدم @Username):", reply_markup=get_cancel_keyboard(), parse_mode="HTML")

@router.message(Command("dbstats"))
async def admin_cmd_stats(msg: types.Message):
    # ✅ تم التعديل: استدعاء مباشر غير متزامن
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

# ==================== تفاعلات الأزرار (Callback Queries) ====================

@router.callback_query(F.data == "admin_main_menu")
async def admin_callback_main_menu(call: types.CallbackQuery, state: FSMContext):
    await render_admin_dashboard(call, state)

@router.callback_query(F.data.startswith("admin_users_page_"))
async def admin_callback_users_page(call: types.CallbackQuery):
    page = int(call.data.split("_")[3])
    await render_users_page(call, page=page)

@router.callback_query(F.data == "admin_cancel")
async def admin_cancel_action(call: types.CallbackQuery, state: FSMContext):
    await state.clear()
    await safe_edit_text(call.message, "❌ تم إغلاق لوحة الإدارة.")
    await call.answer()

@router.callback_query(F.data == "admin_search_prompt")
async def callback_search_prompt(call: types.CallbackQuery, state: FSMContext):
    await state.set_state(AdminState.waiting_for_search_query)
    await safe_edit_text(call.message, "🔍 <b>بحث عن مستخدم</b>\n\nأرسل الآن (الآيدي ID) أو (معرف المستخدم @Username):", reply_markup=get_cancel_keyboard())
    await call.answer()

@router.message(AdminState.waiting_for_search_query)
async def process_search_user(msg: types.Message, state: FSMContext):
    query = msg.text.strip()
    # ✅ تم التعديل: استدعاء مباشر غير متزامن
    users_data = await admin_search_user(query)
    
    if users_data:
        u = users_data[0] 
        username_str = f"@{u['username']}" if u['username'] and u['username'] != "Unknown" else "بدون يوزر"
        report = (
            "👤 <b>معلومات المستخدم:</b>\n"
            f"┣ 🆔 الآيدي: <code>{u['user_id']}</code>\n"
            f"┣ 👤 اليوزر: {username_str}\n"
            f"┣ 📝 الاسم: <b>{u['first_name']} {u.get('last_name', '')}</b>\n"
            f"┣ 🎁 النقاط المجانية: <code>{float(u.get('free_points') or 0):.2f}</code>\n"
            f"┣ 💳 النقاط المدفوعة: <code>{float(u.get('paid_points') or 0):.2f}</code>\n"
            f"┣ 💰 الإجمالي: <code>{user_total_points(u):.2f}</code>\n"
            f"┗ 📊 إجمالي الأسئلة المُولدة: <code>{u.get('total_questions', 0)}</code>"
        )
        await msg.answer(report, reply_markup=get_admin_user_actions_keyboard(u['user_id']), parse_mode="HTML")
    else:
        await msg.answer("❌ لم يتم العثور على أي مستخدم بهذا البحث.", reply_markup=get_cancel_keyboard(), parse_mode="HTML")
    await state.clear()

# 🆕 المعالج الجديد لمراجعة أحدث الملاحظات والشكاوى الواردة من الطلاب على الكويزات
@router.callback_query(F.data == "admin_view_feedbacks")
async def admin_callback_view_feedbacks(call: types.CallbackQuery):
    try:
        feedbacks = await admin_get_recent_feedbacks()
        if not feedbacks:
            await safe_edit_text(call.message, "📭 لا توجد أي ملاحظات أو شكاوى مسجلة من الطلاب حالياً.", reply_markup=get_admin_dashboard_keyboard())
            return

        report = "💬 <b>أحدث ملاحظات وشكاوى الطلاب على الكويزات:</b>\n\n"
        for idx, fb in enumerate(feedbacks, start=1):
            report += (
                f"<b>{idx}. كويز ID:</b> <code>{fb['quiz_id']}</code>\n"
                f"┣ 👤 الطالب الآيدي: <code>{fb['user_id']}</code>\n"
                f"┗ 📝 الملاحظة: <i>{fb['comment']}</i>\n"
                f"──────────────────\n"
            )

        await safe_edit_text(call.message, report, reply_markup=get_admin_dashboard_keyboard())
    except Exception as e:
        logger.error(f"Error in admin_view_feedbacks: {e}")
        await call.answer("❌ حدث خطأ داخلي أثناء جلب الملاحظات.", show_alert=True)
    finally:
        await call.answer()

# ==================== مسار شحن الرصيد ====================

@router.callback_query(F.data.startswith("admin_charge_menu_"))
async def show_charge_menu(call: types.CallbackQuery):
    target_id = call.data.split("_")[3]
    await safe_edit_text(call.message, f"💰 <b>شحن رصيد للمستخدم</b> <code>{target_id}</code>\n\nاختر كمية شحن سريعة أو إدخال يدوي:", reply_markup=get_admin_charge_options_keyboard(target_id))
    await call.answer()

@router.callback_query(F.data.startswith("admin_charge_quick_"))
async def process_quick_charge(call: types.CallbackQuery):
    parts = call.data.split("_")
    amount = int(parts[3])
    target_id = int(parts[4])
    
    # ✅ تم التعديل: استدعاء مباشر غير متزامن
    new_balance = await admin_add_points(target_id, amount)
    if new_balance is not None:
        await safe_edit_text(call.message, f"✅ <b>تم الشحن بنجاح!</b>\n\nالمستخدم: <code>{target_id}</code>\nالكمية المضافة: <code>+{amount}</code> 🟢\nالرصيد الجديد: <code>{new_balance}</code> 💰", reply_markup=get_admin_dashboard_keyboard())
        await send_points_notification(target_id, amount, new_balance)
    else:
        await call.answer("❌ حدث خطأ أثناء الشحن.", show_alert=True)

@router.callback_query(F.data.startswith("admin_charge_manual_"))
async def prompt_manual_charge(call: types.CallbackQuery, state: FSMContext):
    target_id = call.data.split("_")[3]
    await state.update_data(target_id=target_id)
    await state.set_state(AdminState.waiting_for_charge_amount)
    await safe_edit_text(call.message, f"✍️ <b>شحن يدوي</b>\n\nأرسل عدد النقاط المراد إضافتها للمستخدم <code>{target_id}</code>:", reply_markup=get_cancel_keyboard())
    await call.answer()

@router.message(AdminState.waiting_for_charge_amount)
async def process_manual_charge(msg: types.Message, state: FSMContext):
    if not msg.text.isdigit():
        return await msg.answer("❌ يرجى إرسال أرقام صحيحة فقط.", reply_markup=get_cancel_keyboard(), parse_mode="HTML")
    
    amount = int(msg.text)
    data = await state.get_data()
    target_id = int(data.get('target_id'))
    
    # ✅ تم التعديل: استدعاء مباشر غير متزامن
    new_balance = await admin_add_points(target_id, amount)
    if new_balance is not None:
        await msg.answer(f"✅ <b>تم الشحن بنجاح!</b>\n\nالمستخدم: <code>{target_id}</code>\nالكمية المضافة: <code>+{amount}</code> 🟢\nالرصيد الجديد: <code>{new_balance}</code> 💰", reply_markup=get_admin_dashboard_keyboard(), parse_mode="HTML")
        await send_points_notification(target_id, amount, new_balance)
    else:
        await msg.answer("❌ حدث خطأ أثناء الشحن. حاول مجدداً.", reply_markup=get_admin_dashboard_keyboard())
    await state.clear()

@router.callback_query(F.data == "admin_stats")
async def show_db_stats(call: types.CallbackQuery):
    # ✅ تم التعديل: استدعاء مباشر غير متزامن
    stats = await admin_get_global_stats()
    text = f"📊 <b>إحصائيات النظام الحية:</b>\n\n👥 إجمالي الطلاب المسجلين: <code>{stats['total_users']}</code>\n📝 إجمالي الأسئلة المُولدة: <code>{stats['total_questions']}</code>\n"
    await safe_edit_text(call.message, text, reply_markup=get_admin_dashboard_keyboard())
    await call.answer()

# ==================== تصدير البيانات إلى ملف CSV ====================

def sanitize_csv_value(val) -> str:
    """
    تأمين النصوص المدخلة لمنع ثغرة الـ CSV Injection في برامج الجداول الحسابية مثل Excel
    """
    val_str = str(val) if val is not None else ""
    if val_str.startswith(('=', '+', '-', '@')):
        return f"'{val_str}"
    return val_str

@router.callback_query(F.data == "admin_export_users")
async def export_all_users(call: types.CallbackQuery):
    await safe_edit_text(call.message, "⏳ جاري استخراج البيانات وبناء ملف الـ CSV، يرجى الانتظار...")
    
    try:
        # ✅ تم التعديل: استدعاء مباشر غير متزامن
        users = await fetch_users_async()
        if not users:
            return await safe_edit_text(call.message, "📭 لا يوجد طلاب لتصديرهم.", reply_markup=get_admin_dashboard_keyboard())
        
        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerow(["User ID", "Username", "First Name", "Last Name", "Free Points", "Paid Points", "Total Points", "Total Questions", "Joined At"])
        
        for u in users:
            writer.writerow([
                sanitize_csv_value(u.get('user_id', '')),
                sanitize_csv_value(u.get('username', 'Unknown')),
                sanitize_csv_value(u.get('first_name', 'Unknown')),
                sanitize_csv_value(u.get('last_name', 'Unknown')),
                sanitize_csv_value(u.get('free_points', 0)),
                sanitize_csv_value(u.get('paid_points', 0)),
                sanitize_csv_value(user_total_points(u)),
                sanitize_csv_value(u.get('total_questions', 0)),
                sanitize_csv_value(u.get('joined_at', ''))
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