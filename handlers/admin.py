import asyncio
import os
import io
import csv
from typing import Optional, Dict
from aiogram import Router, types, F
from aiogram.filters import Command, CommandObject, BaseFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import StatesGroup, State
from aiogram.types import BufferedInputFile
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError, TelegramRetryAfter
from supabase import create_client

# 🚀 تم تعديل الاستيراد لاستجلاب كائن supabase المشترك لمنع تسريب الاتصالات
from config import bot, ADMIN_ID
from supabase_helper import (
    supabase, 
    admin_add_points, 
    admin_get_global_stats, 
    admin_search_user,
    admin_get_feedbacks_page,      # 🆕 تصفح ملاحظات الكويزات (صفحة مع معلومات الكويز والطالب)
    admin_get_feedback_by_id,      # 🆕 تفاصيل ملاحظة واحدة
    admin_get_quiz_board_position, # 🆕 رقم الكويز ضمن لوحة كويزات نفس الملف
    admin_get_quiz_by_id,          # 🆕 لجلب بيانات الكويز عند تجربته
    admin_delete_quiz,             # 🆕 حذف كويز من لوحة الإدارة
    admin_get_usage_overview,      # 🆕 ملخص تحليلات الاستخدام
    admin_get_daily_active_users,  # 🆕 نشاط المستخدمين اليومي
    admin_get_user_activity,       # 🆕 نشاط تفصيلي لطالب محدد
    admin_get_all_usage_events,    # 🆕 تصدير سجل الأحداث كـ CSV
    admin_get_users_question_totals, # 🆕 إجمالي الأسئلة المحلولة لكل مستخدم (يشمل المشاركة والمفضلة)
    admin_get_active_users,        # 🆕 الطلاب النشطون خلال نافذة زمنية معينة (ساعة/3/6/12/24)
    log_usage_event,               # 🆕 تسجيل حدث شحن النقاط من الإدارة لأغراض التحليلات
)
from logger import get_logger
from handlers.execution import _start_loaded_quiz  # 🆕 لتشغيل الكويز مباشرة كتجربة من لوحة الإدارة

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
    waiting_for_charge_custom_amount = State()   # 🆕 شحن مع رسالة: انتظار الكمية
    waiting_for_charge_custom_message = State()  # 🆕 شحن مع رسالة: انتظار نص الرسالة
    waiting_for_broadcast_text = State()
    waiting_for_broadcast_confirm = State()

def _is_admin(user_id: int) -> bool:
    return str(user_id) == str(ADMIN_ID)

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
# 🆕 أصبحت تدعم إرفاق رسالة مخصصة من الإدارة داخل نفس إشعار الشحن (custom_message اختياري)
async def send_points_notification(target_id: int, amount: int, new_balance: int, custom_message: Optional[str] = None):
    try:
        user_text = (
            "🎉 <b>أخبار سارة! تم تحديث رصيدك</b>\n\n"
            f"عزيزي الطالب، تم إضافة <code>{amount}</code> نقاط جديدة إلى حسابك بنجاح. ✨\n\n"
            f"🟢 <b>الكمية المضافة:</b> <code>+{amount}</code>\n"
            f"💰 <b>رصيدك الحالي أصبح:</b> <code>{new_balance}</code> نقطة\n"
        )
        if custom_message:
            user_text += f"\n💬 <b>رسالة من الإدارة:</b>\n{custom_message}\n"
        user_text += "\nنتمنى لك رحلة تعليمية ممتعة ومليئة بالتوفيق والنجاح! 🚀"

        await bot.send_message(chat_id=target_id, text=user_text, parse_mode="HTML")
        logger.info(f"Notification sent successfully to user {target_id}")
    except Exception as e:
        logger.error(f"Could not send notification to user {target_id}: {e}")

# ==================== لوحات الأزرار (Keyboards) المدمجة ====================

def get_admin_dashboard_keyboard() -> types.InlineKeyboardMarkup:
    kb = [
        [types.InlineKeyboardButton(text="📢 إرسال رسالة جماعية", callback_data="admin_broadcast_prompt")],
        [types.InlineKeyboardButton(text="🔍 البحث عن مستخدم", callback_data="admin_search_prompt")],
        [types.InlineKeyboardButton(text="👥 استعراض الطلاب (مصفّح)", callback_data="admin_users_page_1")],
        [types.InlineKeyboardButton(text="📊 الإحصائيات", callback_data="admin_stats"),
         types.InlineKeyboardButton(text="📥 تصدير CSV", callback_data="admin_export_users")],
        [types.InlineKeyboardButton(text="📈 تحليلات الاستخدام", callback_data="admin_analytics_7")],
        [types.InlineKeyboardButton(text="🟢 الطلاب النشطون الآن", callback_data="admin_active_24")],  # 🆕
        [types.InlineKeyboardButton(text="📋 تصفح ملاحظات الكويزات", callback_data="admin_view_feedbacks")],
        [types.InlineKeyboardButton(text="❌ إغلاق القائمة", callback_data="admin_cancel")]
    ]
    return types.InlineKeyboardMarkup(inline_keyboard=kb)

def get_admin_user_actions_keyboard(target_id: int) -> types.InlineKeyboardMarkup:
    kb = [
        [types.InlineKeyboardButton(text="💰 شحن نقاط للمستخدم", callback_data=f"admin_charge_menu_{target_id}")],
        [types.InlineKeyboardButton(text="📈 نشاط هذا الطالب", callback_data=f"admin_user_activity_{target_id}")],
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
        [types.InlineKeyboardButton(text="💬 شحن نقاط + رسالة مخصصة للطالب", callback_data=f"admin_charge_withmsg_{target_id}")],  # 🆕
        [types.InlineKeyboardButton(text="🔙 إلغاء والعودة", callback_data="admin_main_menu")]
    ]
    return types.InlineKeyboardMarkup(inline_keyboard=kb)

# 🆕 لوحة اختيار النافذة الزمنية لعرض الطلاب النشطين
def get_active_users_keyboard(hours: int) -> types.InlineKeyboardMarkup:
    windows = [(1, "ساعة"), (3, "3 ساعات"), (6, "6 ساعات"), (12, "12 ساعة"), (24, "24 ساعة")]
    buttons = []
    for h, label in windows:
        text = f"✅ {label}" if h == hours else label
        buttons.append(types.InlineKeyboardButton(text=text, callback_data=f"admin_active_{h}"))
    kb = [buttons[:3], buttons[3:]]
    kb.append([types.InlineKeyboardButton(text="⚙️ لوحة التحكم", callback_data="admin_main_menu")])
    return types.InlineKeyboardMarkup(inline_keyboard=kb)

def get_cancel_keyboard() -> types.InlineKeyboardMarkup:
    kb = [[types.InlineKeyboardButton(text="❌ إلغاء العملية", callback_data="admin_main_menu")]]
    return types.InlineKeyboardMarkup(inline_keyboard=kb)

def get_analytics_keyboard(days: int) -> types.InlineKeyboardMarkup:
    period_row = []
    for d, label in [(7, "7 أيام"), (30, "30 يوم"), (90, "90 يوم")]:
        text = f"✅ {label}" if d == days else label
        period_row.append(types.InlineKeyboardButton(text=text, callback_data=f"admin_analytics_{d}"))
    kb = [
        period_row,
        [types.InlineKeyboardButton(text="📅 النشاط اليومي (آخر 14 يوم)", callback_data="admin_analytics_daily")],
        [types.InlineKeyboardButton(text="📥 تصدير سجل الأحداث CSV", callback_data="admin_export_events")],
        [types.InlineKeyboardButton(text="⚙️ لوحة التحكم", callback_data="admin_main_menu")]
    ]
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

    # 🆕 إجمالي الأسئلة "المحلولة فعلياً" لكل طالب بهذه الصفحة، مقسّم حسب المصدر
    # (يشمل تلقائياً الكويزات المشتركة والمحفوظة بالمفضلة وليس فقط المولّدة من ملفات)
    question_totals = await admin_get_users_question_totals([u['user_id'] for u in page_users])

    report = f"👥 <b>سجل الطلاب المسجلين ({page} من {total_pages}):</b>\n\n"
    for idx, u in enumerate(page_users, start=start_idx + 1):
        username_str = f"@{u['username']}" if u['username'] and u['username'] != "Unknown" else "بدون يوزر"
        qt = question_totals.get(u['user_id'], {})
        report += (
            f"<b>{idx}. آيدي:</b> <code>{u['user_id']}</code>\n"
            f"┣ 👤 اليوزر: {username_str}\n"
            f"┣ 📝 الاسم: <b>{u.get('first_name', 'Unknown')} {u.get('last_name', 'Unknown')}</b>\n"
            f"┣ 🎁 المجاني: <code>{float(u.get('free_points') or 0):.2f}</code>\n"
            f"┣ 💳 المدفوع: <code>{float(u.get('paid_points') or 0):.2f}</code>\n"
            f"┣ 💰 الإجمالي: <code>{user_total_points(u):.2f}</code>\n"
            f"┣ 📝 أسئلة مولّدة (مدفوعة من ملفات/نصوص): <code>{u.get('total_questions', 0)}</code>\n"
            f"┣ 🧾 إجمالي الأسئلة التي حلّها (كل المصادر): <code>{qt.get('practiced_total', 0)}</code>\n"
            f"┗ 🗂 منها → ملفات/كاش: <code>{qt.get('generated', 0)}</code> "
            f"| مشتركة: <code>{qt.get('shared', 0)}</code> "
            f"| مفضلة: <code>{qt.get('favorite', 0)}</code>\n"
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
    if not _is_admin(msg.from_user.id): return
    
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
    if not _is_admin(msg.from_user.id): return
    
    # 1. في حال كتب الآدمن النص مباشرة بعد الأمر (/broadcast أهلاً بالجميع)
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

    # 2. في حال ضغط الأمر بدون نص
    await state.set_state(AdminState.waiting_for_broadcast_text)
    await msg.answer(
        "📢 <b>إرسال رسالة جماعية لكافة الطلاب</b>\n\n"
        "أرسل الآن نص الرسالة التي تريد تعميمها على جميع مستخدمي البوت:",
        reply_markup=get_cancel_keyboard(),
        parse_mode="HTML"
    )

@router.callback_query(F.data == "admin_broadcast_prompt")
async def callback_broadcast_prompt(call: types.CallbackQuery, state: FSMContext):
    if not _is_admin(call.from_user.id): return
    await state.set_state(AdminState.waiting_for_broadcast_text)
    await safe_edit_text(
        call.message, 
        "📢 <b>إرسال رسالة جماعية لكافة الطلاب</b>\n\nأرسل الآن نص الرسالة التي تريد تعميمها على جميع مستخدمي البوت:", 
        reply_markup=get_cancel_keyboard()
    )
    await call.answer()

@router.message(AdminState.waiting_for_broadcast_text)
async def process_broadcast_text(msg: types.Message, state: FSMContext):
    if not _is_admin(msg.from_user.id): return
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
    if not _is_admin(call.from_user.id): return
    data = await state.get_data()
    text_to_send = data.get("broadcast_text")
    await state.clear()

    # 🛠️ إصلاح: الدالة القديمة fetch_users_sync لم تكن معرّفة إطلاقاً بالملف (كانت ستكسر البرودكاست عند التنفيذ الفعلي)
    users = await fetch_users_async()
    if not users:
        await safe_edit_text(call.message, "📭 لا يوجد طلاب مسجلين لإرسال الرسالة إليهم.", reply_markup=get_admin_dashboard_keyboard())
        return

    user_ids = [u['user_id'] for u in users if 'user_id' in u]
    total_users = len(user_ids)

    await safe_edit_text(call.message, f"⏳ <b>جاري الإرسال الجماعي...</b>\n\nالعدد الإجمالي المستهدف: <code>{total_users}</code> طالب")

    success_count = 0
    blocked_count = 0
    failed_count = 0

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

        # تأخير أمان (20 رسالة / ثانية)
        await asyncio.sleep(0.05)

        # تحديث التقرير الحي كل 25 مستخدم
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
    await call.answer()


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
        # 🆕 إجمالي الأسئلة المحلولة فعلياً (يشمل المشاركة والمفضلة)، وليس فقط المولّدة من ملفات
        question_totals = await admin_get_users_question_totals([u['user_id']])
        qt = question_totals.get(u['user_id'], {})
        report = (
            "👤 <b>معلومات المستخدم:</b>\n"
            f"┣ 🆔 الآيدي: <code>{u['user_id']}</code>\n"
            f"┣ 👤 اليوزر: {username_str}\n"
            f"┣ 📝 الاسم: <b>{u['first_name']} {u.get('last_name', '')}</b>\n"
            f"┣ 🎁 النقاط المجانية: <code>{float(u.get('free_points') or 0):.2f}</code>\n"
            f"┣ 💳 النقاط المدفوعة: <code>{float(u.get('paid_points') or 0):.2f}</code>\n"
            f"┣ 💰 الإجمالي: <code>{user_total_points(u):.2f}</code>\n"
            f"┣ 📝 أسئلة مولّدة (مدفوعة من ملفات/نصوص): <code>{u.get('total_questions', 0)}</code>\n"
            f"┣ 🧾 إجمالي الأسئلة التي حلّها (كل المصادر): <code>{qt.get('practiced_total', 0)}</code>\n"
            f"┗ 🗂 منها → ملفات/كاش: <code>{qt.get('generated', 0)}</code> "
            f"| مشتركة: <code>{qt.get('shared', 0)}</code> "
            f"| مفضلة: <code>{qt.get('favorite', 0)}</code>"
        )
        await msg.answer(report, reply_markup=get_admin_user_actions_keyboard(u['user_id']), parse_mode="HTML")
    else:
        await msg.answer("❌ لم يتم العثور على أي مستخدم بهذا البحث.", reply_markup=get_cancel_keyboard(), parse_mode="HTML")
    await state.clear()

# 🆕 تصفح ملاحظات الكويزات: قائمة مصفّحة + شاشة تفاصيل غنية لكل ملاحظة
FEEDBACKS_PAGE_SIZE = 5


def _student_label(student: Optional[Dict]) -> str:
    if not student:
        return "طالب غير معروف"
    name = " ".join(filter(None, [student.get("first_name"), student.get("last_name")])).strip()
    return name or (f"@{student['username']}" if student.get("username") else "بدون اسم")


def get_feedbacks_list_keyboard(feedbacks: list, page: int, total: int) -> types.InlineKeyboardMarkup:
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
    kb = []
    if quiz_id:
        kb.append([types.InlineKeyboardButton(text="🎯 خوض الكويز لتجربته", callback_data=f"afb_try_{quiz_id}")])
        kb.append([types.InlineKeyboardButton(text="🗑 حذف الكويز نهائياً", callback_data=f"afb_del_{quiz_id}_{feedback_id}")])
    kb.append([types.InlineKeyboardButton(text="🔙 رجوع للقائمة", callback_data="afb_p_1")])
    return types.InlineKeyboardMarkup(inline_keyboard=kb)


async def _render_feedback_list(page: int):
    offset = (page - 1) * FEEDBACKS_PAGE_SIZE
    feedbacks, total = await admin_get_feedbacks_page(limit=FEEDBACKS_PAGE_SIZE, offset=offset)
    if total == 0:
        return "📭 لا توجد أي ملاحظات أو شكاوى مسجلة من الطلاب حالياً.", get_admin_dashboard_keyboard()
    total_pages = max(1, -(-total // FEEDBACKS_PAGE_SIZE))
    text = f"📋 <b>ملاحظات الكويزات</b> — صفحة {page}/{total_pages} (الإجمالي: {total})\n\nاختر ملاحظة لعرض تفاصيلها الكاملة:"
    return text, get_feedbacks_list_keyboard(feedbacks, page, total)


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
        # 🆕 زر مباشر لمحادثة الطالب - يفتح محادثته الخاصة في تيليجرام مباشرة بدون الحاجة لليوزرنيم
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
    """خطوة تأكيد قبل الحذف النهائي - إجراء حذف فعلي يحتاج تأكيداً صريحاً"""
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
        asyncio.create_task(log_usage_event(target_id, "points_charged_by_admin", {"amount": amount, "method": "quick"}))
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
        asyncio.create_task(log_usage_event(target_id, "points_charged_by_admin", {"amount": amount, "method": "manual"}))
    else:
        await msg.answer("❌ حدث خطأ أثناء الشحن. حاول مجدداً.", reply_markup=get_admin_dashboard_keyboard())
    await state.clear()

# ==================== 🆕 مسار شحن النقاط مع رسالة مخصصة للطالب ====================

@router.callback_query(F.data.startswith("admin_charge_withmsg_"))
async def prompt_custom_charge_amount(call: types.CallbackQuery, state: FSMContext):
    target_id = call.data.split("_")[3]
    await state.update_data(charge_target_id=target_id)
    await state.set_state(AdminState.waiting_for_charge_custom_amount)
    await safe_edit_text(
        call.message,
        f"💬 <b>شحن نقاط مع رسالة مخصصة</b>\n\nأرسل الآن عدد النقاط المراد إضافتها للمستخدم <code>{target_id}</code>:",
        reply_markup=get_cancel_keyboard()
    )
    await call.answer()

@router.message(AdminState.waiting_for_charge_custom_amount)
async def process_custom_charge_amount(msg: types.Message, state: FSMContext):
    if not msg.text.isdigit():
        return await msg.answer("❌ يرجى إرسال أرقام صحيحة فقط.", reply_markup=get_cancel_keyboard(), parse_mode="HTML")

    await state.update_data(charge_amount=int(msg.text))
    await state.set_state(AdminState.waiting_for_charge_custom_message)
    await msg.answer(
        "✍️ <b>الآن أرسل نص الرسالة المخصصة</b> التي تريد إرفاقها مع إشعار الشحن (ستظهر للطالب داخل نفس الرسالة):",
        reply_markup=get_cancel_keyboard(),
        parse_mode="HTML"
    )

@router.message(AdminState.waiting_for_charge_custom_message)
async def process_custom_charge_message(msg: types.Message, state: FSMContext):
    custom_text = (msg.text or "").strip()
    if not custom_text:
        return await msg.answer("❌ الرسالة فارغة، يرجى إرسال نص واضح:", reply_markup=get_cancel_keyboard(), parse_mode="HTML")

    data = await state.get_data()
    target_id = data.get("charge_target_id")
    amount = data.get("charge_amount")
    if not target_id or not amount:
        await msg.answer("❌ انتهت صلاحية هذا الطلب، ابدأ عملية الشحن من جديد.", reply_markup=get_admin_dashboard_keyboard())
        await state.clear()
        return

    target_id = int(target_id)
    amount = int(amount)

    new_balance = await admin_add_points(target_id, amount)
    if new_balance is not None:
        await msg.answer(
            f"✅ <b>تم الشحن مع الرسالة المخصصة بنجاح!</b>\n\n"
            f"المستخدم: <code>{target_id}</code>\n"
            f"الكمية المضافة: <code>+{amount}</code> 🟢\n"
            f"الرصيد الجديد: <code>{new_balance}</code> 💰\n\n"
            f"💬 <b>الرسالة التي أُرسلت للطالب:</b>\n{custom_text}",
            reply_markup=get_admin_dashboard_keyboard(),
            parse_mode="HTML"
        )
        await send_points_notification(target_id, amount, new_balance, custom_message=custom_text)
        asyncio.create_task(log_usage_event(target_id, "points_charged_by_admin", {
            "amount": amount, "method": "custom_message", "message_length": len(custom_text),
        }))
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

# ==================== 🆕 تحليلات الاستخدام (Usage Analytics) ====================

EVENT_LABELS = {
    "bot_start": "▶️ تشغيل البوت",
    "content_uploaded": "📤 رفع محتوى",
    "quiz_generation_requested": "🧮 طلب توليد كويز",
    "quiz_generated": "🆕 كويز تم توليده",
    "cached_quiz_used": "♻️ استخدام كويز مخزن",
    "quiz_started": "🚀 بدء كويز",
    "quiz_completed": "🏁 إكمال كويز",
    "quiz_stopped": "⏹ إيقاف كويز مبكراً",
    "quiz_shared": "🔗 مشاركة كويز",
    "share_link_created": "🔗 إنشاء رابط مشاركة",
    "shared_link_opened": "📬 فتح رابط مشترك",
    "quiz_saved_favorite": "⭐ حفظ بالمفضلة",
    "quiz_rated": "👍👎 تقييم كويز",
    "feedback_submitted": "✍️ إرسال ملاحظة",
    "score_published": "🏆 نشر نتيجة",
    "leaderboard_viewed": "📋 عرض لوحة الشرف",
    "points_charged_by_admin": "💰 شحن نقاط من الإدارة",  # 🆕
}

SOURCE_LABELS = {
    "file": "📄 ملف", "photo": "🖼 صورة", "album": "🖼🖼 ألبوم", "text": "📝 نص مباشر",
    "shared": "🔗 مشترك", "favorite": "⭐ مفضلة", "cached_file": "♻️ كاش", "admin_test": "🛠 تجربة إدارية",
}

def _format_seconds(total_seconds: float) -> str:
    total_seconds = int(total_seconds or 0)
    minutes, seconds = divmod(total_seconds, 60)
    if minutes:
        return f"{minutes} د {seconds} ث"
    return f"{seconds} ث"

async def _render_analytics_overview(call: types.CallbackQuery, days: int):
    overview = await admin_get_usage_overview(days=days)

    top_events = sorted(overview["event_counts"].items(), key=lambda x: x[1], reverse=True)[:8]
    events_lines = "\n".join(
        f"┣ {EVENT_LABELS.get(ev, ev)}: <code>{count}</code>" for ev, count in top_events
    ) or "┣ لا توجد أحداث مسجلة بعد."

    source_lines = "\n".join(
        f"┣ {SOURCE_LABELS.get(src, src)}: <code>{count}</code>"
        for src, count in sorted(overview["source_breakdown"].items(), key=lambda x: x[1], reverse=True)
    ) or "┣ لا توجد بيانات."

    text = (
        f"📈 <b>تحليلات الاستخدام — آخر {days} يوم</b>\n\n"
        f"👥 مستخدمون نشطون: <code>{overview['active_users']}</code>\n"
        f"🎯 محاولات كويز: <code>{overview['total_attempts']}</code> (مكتمل: <code>{overview['completed_attempts']}</code>)\n"
        f"✅ معدل الإكمال: <code>{overview['completion_rate']:.1f}%</code>\n"
        f"⏱ متوسط مدة الحل: <code>{_format_seconds(overview['avg_duration_seconds'])}</code>\n"
        f"🎓 متوسط النتائج: <code>{overview['avg_score_percentage']:.1f}%</code>\n\n"
        f"📊 <b>الأحداث الأكثر تكراراً:</b>\n{events_lines}\n\n"
        f"🗂 <b>مصدر الكويزات:</b>\n{source_lines}"
    )
    await safe_edit_text(call.message, text, reply_markup=get_analytics_keyboard(days))
    await call.answer()

@router.callback_query(F.data.regexp(r"^admin_analytics_(7|30|90)$"))
async def show_usage_analytics(call: types.CallbackQuery):
    days = int(call.data.replace("admin_analytics_", ""))
    try:
        await _render_analytics_overview(call, days)
    except Exception as e:
        logger.error(f"Error rendering analytics overview: {e}")
        await call.answer("❌ تعذر تحميل بيانات التحليلات.", show_alert=True)

@router.callback_query(F.data == "admin_analytics_daily")
async def show_daily_active_users(call: types.CallbackQuery):
    try:
        daily = await admin_get_daily_active_users(days=14)
        if not daily:
            await call.answer("📭 لا توجد بيانات نشاط كافية بعد.", show_alert=True)
            return
        max_active = max(d["active_users"] for d in daily) or 1
        lines = []
        for row in daily:
            bar_len = max(1, round((row["active_users"] / max_active) * 12))
            bar = "█" * bar_len
            lines.append(f"<code>{row['day']}</code> {bar} {row['active_users']}")
        text = "📅 <b>المستخدمون النشطون يومياً (آخر 14 يوم):</b>\n\n" + "\n".join(lines)
        await safe_edit_text(call.message, text, reply_markup=get_analytics_keyboard(7))
        await call.answer()
    except Exception as e:
        logger.error(f"Error rendering daily active users: {e}")
        await call.answer("❌ تعذر تحميل النشاط اليومي.", show_alert=True)

# ==================== 🆕 الطلاب النشطون خلال نافذة زمنية (ساعة/3/6/12/24) ====================

@router.callback_query(F.data.regexp(r"^admin_active_(1|3|6|12|24)$"))
async def show_active_users(call: types.CallbackQuery):
    hours = int(call.data.replace("admin_active_", ""))
    try:
        active_users = await admin_get_active_users(hours=hours, limit=30)

        if not active_users:
            text = f"📭 لا يوجد أي طلاب نشطين خلال آخر {hours} ساعة."
        else:
            blocks = []
            for i, u in enumerate(active_users, start=1):
                username_str = f"@{u['username']}" if u.get('username') and u['username'] != "Unknown" else "بدون يوزر"
                full_name = f"{u['first_name']} {u['last_name']}".strip() or "بدون اسم"
                activity_label = EVENT_LABELS.get(u['last_event_type'], u['last_event_type'])
                last_time = str(u['last_event_at'])[:16].replace('T', ' ')
                blocks.append(
                    f"<b>{i}. {full_name}</b>\n"
                    f"┣ 👤 اليوزر: {username_str}\n"
                    f"┣ 🆔 الآيدي: <code>{u['user_id']}</code>\n"
                    f"┣ 🕒 آخر نشاط: {activity_label} — <code>{last_time}</code>\n"
                    f"┗ 📊 عدد الأحداث بهذه الفترة: <code>{u['events_in_window']}</code>"
                )
            text = f"🟢 <b>الطلاب النشطون خلال آخر {hours} ساعة</b> (الإجمالي: {len(active_users)})\n\n" + "\n\n".join(blocks)

        # 🛡️ حماية من تجاوز الحد الأقصى لطول رسالة تيليجرام (4096 حرف)
        if len(text) > 3900:
            text = text[:3850] + "\n\n… (تم اقتصاص القائمة لطولها، هناك المزيد من الطلاب النشطين)"

        await safe_edit_text(call.message, text, reply_markup=get_active_users_keyboard(hours))
        await call.answer()
    except Exception as e:
        logger.error(f"Error rendering active users window: {e}")
        await call.answer("❌ تعذر تحميل قائمة الطلاب النشطين.", show_alert=True)


@router.callback_query(F.data.startswith("admin_user_activity_"))
async def show_user_activity(call: types.CallbackQuery):
    try:
        target_id = int(call.data.replace("admin_user_activity_", ""))
        activity = await admin_get_user_activity(target_id)

        events_lines = "\n".join(
            f"┣ {EVENT_LABELS.get(e['event_type'], e['event_type'])} — <code>{str(e['created_at'])[:16].replace('T', ' ')}</code>"
            for e in activity["recent_events"][:10]
        ) or "┣ لا يوجد نشاط مسجل بعد."

        attempts_lines = "\n".join(
            f"┣ {SOURCE_LABELS.get(a.get('source_type'), a.get('source_type'))} — "
            f"{'✅' if a.get('is_completed') else '⏹'} {a.get('score', 0)}/{a.get('total_questions', 0)}"
            for a in activity["recent_attempts"]
        ) or "┣ لم يخض أي كويز بعد."

        text = (
            f"📈 <b>نشاط الطالب</b> <code>{target_id}</code>\n\n"
            f"🎯 إجمالي المحاولات: <code>{activity['total_attempts']}</code> "
            f"(مكتملة: <code>{activity['completed_attempts']}</code>)\n"
            f"🎓 متوسط النتائج: <code>{activity['avg_score_percentage']:.1f}%</code>\n\n"
            f"📝 <b>آخر المحاولات:</b>\n{attempts_lines}\n\n"
            f"🕒 <b>آخر الأحداث:</b>\n{events_lines}"
        )
        kb = types.InlineKeyboardMarkup(inline_keyboard=[
            [types.InlineKeyboardButton(text="🔙 رجوع لبيانات الطالب", callback_data="admin_main_menu")]
        ])
        await safe_edit_text(call.message, text, reply_markup=kb)
        await call.answer()
    except Exception as e:
        logger.error(f"Error rendering user activity: {e}")
        await call.answer("❌ تعذر تحميل نشاط هذا الطالب.", show_alert=True)

@router.callback_query(F.data == "admin_export_events")
async def export_usage_events(call: types.CallbackQuery):
    await safe_edit_text(call.message, "⏳ جاري استخراج سجل الأحداث وبناء ملف الـ CSV، يرجى الانتظار...")
    try:
        events = await admin_get_all_usage_events(limit=5000)
        if not events:
            return await safe_edit_text(call.message, "📭 لا توجد أحداث مسجلة لتصديرها.", reply_markup=get_analytics_keyboard(7))

        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerow(["User ID", "Event Type", "Metadata", "Created At"])
        for e in events:
            writer.writerow([
                sanitize_csv_value(e.get("user_id", "")),
                sanitize_csv_value(e.get("event_type", "")),
                sanitize_csv_value(e.get("metadata", {})),
                sanitize_csv_value(e.get("created_at", "")),
            ])

        csv_bytes = output.getvalue().encode("utf-8-sig")
        file = BufferedInputFile(csv_bytes, filename="usage_events.csv")

        try:
            await call.message.delete()
        except TelegramBadRequest:
            pass

        await call.message.answer_document(
            document=file,
            caption="📥 <b>تم استخراج سجل أحداث الاستخدام بنجاح!</b>",
            reply_markup=get_analytics_keyboard(7),
            parse_mode="HTML"
        )
    except Exception as e:
        logger.error(f"Error exporting usage events: {e}")
        try:
            await safe_edit_text(call.message, "❌ حدث خطأ داخلي أثناء استخراج الملف.", reply_markup=get_analytics_keyboard(7))
        except TelegramBadRequest:
            await call.message.answer("❌ حدث خطأ داخلي أثناء استخراج الملف.", reply_markup=get_analytics_keyboard(7))

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
        
        # 🆕 إجمالي الأسئلة المحلولة فعلياً لكل الطلاب دفعة واحدة (يشمل المشاركة والمفضلة)
        question_totals = await admin_get_users_question_totals([u['user_id'] for u in users if 'user_id' in u])

        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerow([
            "User ID", "Username", "First Name", "Last Name", "Free Points", "Paid Points", "Total Points",
            "Generated Questions (Paid)", "Practiced Questions Total", "Practiced From Files/Cache",
            "Practiced From Shared", "Practiced From Favorites", "Joined At"
        ])

        for u in users:
            qt = question_totals.get(u.get('user_id'), {})
            writer.writerow([
                sanitize_csv_value(u.get('user_id', '')),
                sanitize_csv_value(u.get('username', 'Unknown')),
                sanitize_csv_value(u.get('first_name', 'Unknown')),
                sanitize_csv_value(u.get('last_name', 'Unknown')),
                sanitize_csv_value(u.get('free_points', 0)),
                sanitize_csv_value(u.get('paid_points', 0)),
                sanitize_csv_value(user_total_points(u)),
                sanitize_csv_value(u.get('total_questions', 0)),
                sanitize_csv_value(qt.get('practiced_total', 0)),
                sanitize_csv_value(qt.get('generated', 0)),
                sanitize_csv_value(qt.get('shared', 0)),
                sanitize_csv_value(qt.get('favorite', 0)),
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
