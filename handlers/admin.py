import asyncio
import os
import io
import csv
from aiogram import Router, types, F
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import StatesGroup, State
from aiogram.types import BufferedInputFile
from aiogram.exceptions import TelegramBadRequest
from supabase import create_client

from config import bot, ADMIN_ID
from supabase_helper import admin_add_points, admin_get_global_stats, admin_search_user
from keyboards import (
    get_admin_dashboard_keyboard, 
    get_admin_user_actions_keyboard, 
    get_admin_charge_options_keyboard,
    get_cancel_keyboard
)
from logger import get_logger

logger = get_logger(__name__)
router = Router()

# تعريف حالات الـ FSM للإدارة
class AdminState(StatesGroup):
    waiting_for_search_query = State()
    waiting_for_charge_amount = State()

def _is_admin(user_id: int) -> bool:
    # تحويل القيم لنصوص لضمان مطابقتها في حال كان الآيدي محفوظ كـ String في بيئة التشغيل
    return str(user_id) == str(ADMIN_ID)

# دالة مساعدة آمنة لتعديل الرسائل لتجنب خطأ (Message is not modified)
async def safe_edit_text(message: types.Message, text: str, reply_markup=None):
    try:
        await message.edit_text(text, reply_markup=reply_markup, parse_mode="HTML")
    except TelegramBadRequest:
        pass # تجاهل الخطأ بصمت إذا كان النص مطابقاً للنص الحالي

# ==================== Main Dashboard ====================

@router.message(Command("admin"))
async def admin_dashboard(msg: types.Message, state: FSMContext):
    if not _is_admin(msg.from_user.id):
        return
    await state.clear()
    await msg.answer(
        "⚙️ <b>لوحة تحكم الإدارة</b>\n\nأهلاً بك، اختر الإجراء الذي تود القيام به من القائمة أدناه:",
        reply_markup=get_admin_dashboard_keyboard(),
        parse_mode="HTML"
    )

# ==================== Cancel Action ====================

@router.callback_query(F.data == "admin_cancel")
async def admin_cancel_action(call: types.CallbackQuery, state: FSMContext):
    await state.clear()
    await safe_edit_text(
        call.message,
        "❌ تم إغلاق القائمة.\n\n⚙️ <b>لوحة تحكم الإدارة</b>",
        reply_markup=get_admin_dashboard_keyboard()
    )
    await call.answer()

# ==================== Search User Flow ====================

@router.callback_query(F.data == "admin_search_prompt")
async def prompt_search_user(call: types.CallbackQuery, state: FSMContext):
    await state.set_state(AdminState.waiting_for_search_query)
    await safe_edit_text(
        call.message,
        "🔍 <b>بحث عن مستخدم</b>\n\nأرسل الآن (الآيدي ID) أو (معرف المستخدم @Username):",
        reply_markup=get_cancel_keyboard()
    )
    await call.answer()

@router.message(AdminState.waiting_for_search_query)
async def process_search_user(msg: types.Message, state: FSMContext):
    query = msg.text.strip()
    users_data = await asyncio.to_thread(admin_search_user, query)
    
    if users_data:
        u = users_data[0] 
        username_str = f"@{u['username']}" if u['username'] and u['username'] != "Unknown" else "بدون يوزر"
        
        report = (
            "👤 <b>معلومات المستخدم:</b>\n"
            f"┣ 🆔 الآيدي: <code>{u['user_id']}</code>\n"
            f"┣ 👤 اليوزر: {username_str}\n"
            f"┣ 💰 النقاط الحالية: <code>{u['points']}</code>\n"
            f"┗ 📊 إجمالي الأسئلة المُولدة: <code>{u.get('total_questions', 0)}</code>"
        )
        await msg.answer(report, reply_markup=get_admin_user_actions_keyboard(u['user_id']), parse_mode="HTML")
    else:
        await msg.answer("❌ لم يتم العثور على أي مستخدم بهذا البحث.", reply_markup=get_cancel_keyboard())
    
    await state.clear()

# ==================== Charge Points Flow ====================

@router.callback_query(F.data.startswith("admin_charge_menu_"))
async def show_charge_menu(call: types.CallbackQuery):
    target_id = call.data.split("_")[3]
    await safe_edit_text(
        call.message,
        f"💰 <b>شحن رصيد للمستخدم</b> <code>{target_id}</code>\n\nاختر كمية الشحن السريعة أو اختر إدخالاً يدوياً:",
        reply_markup=get_admin_charge_options_keyboard(target_id)
    )
    await call.answer()

@router.callback_query(F.data.startswith("admin_charge_quick_"))
async def process_quick_charge(call: types.CallbackQuery):
    parts = call.data.split("_")
    amount = int(parts[3])
    target_id = int(parts[4])
    
    new_balance = await asyncio.to_thread(admin_add_points, target_id, amount)
    
    if new_balance is not None:
        await safe_edit_text(
            call.message,
            f"✅ <b>تم الشحن بنجاح!</b>\n\nالمستخدم: <code>{target_id}</code>\nالكمية المضافة: <code>+{amount}</code> 🟢\nالرصيد الجديد: <code>{new_balance}</code> 💰",
            reply_markup=get_admin_dashboard_keyboard()
        )
    else:
        await call.answer("❌ حدث خطأ أثناء الشحن.", show_alert=True)

@router.callback_query(F.data.startswith("admin_charge_manual_"))
async def prompt_manual_charge(call: types.CallbackQuery, state: FSMContext):
    target_id = call.data.split("_")[3]
    await state.update_data(target_id=target_id)
    await state.set_state(AdminState.waiting_for_charge_amount)
    
    await safe_edit_text(
        call.message,
        f"✍️ <b>شحن يدوي</b>\n\nأرسل عدد النقاط المراد إضافتها للمستخدم <code>{target_id}</code> (أرقام فقط):",
        reply_markup=get_cancel_keyboard()
    )
    await call.answer()

@router.message(AdminState.waiting_for_charge_amount)
async def process_manual_charge(msg: types.Message, state: FSMContext):
    if not msg.text.isdigit():
        return await msg.answer("❌ يرجى إرسال أرقام صحيحة فقط.", reply_markup=get_cancel_keyboard())
    
    amount = int(msg.text)
    data = await state.get_data()
    target_id = int(data.get('target_id'))
    
    new_balance = await asyncio.to_thread(admin_add_points, target_id, amount)
    
    if new_balance is not None:
        await msg.answer(
            f"✅ <b>تم الشحن بنجاح!</b>\n\nالمستخدم: <code>{target_id}</code>\nالكمية المضافة: <code>+{amount}</code> 🟢\nالرصيد الجديد: <code>{new_balance}</code> 💰",
            reply_markup=get_admin_dashboard_keyboard(),
            parse_mode="HTML"
        )
    else:
        await msg.answer("❌ حدث خطأ أثناء الشحن. حاول مجدداً.")
    
    await state.clear()

# ==================== Stats & Exports ====================

@router.callback_query(F.data == "admin_stats")
async def show_db_stats(call: types.CallbackQuery):
    stats = await asyncio.to_thread(admin_get_global_stats)
    text = (
        "📊 <b>إحصائيات النظام الحية:</b>\n\n"
        f"👥 إجمالي الطلاب المسجلين: <code>{stats['total_users']}</code>\n"
        f"📝 إجمالي الأسئلة المُولدة: <code>{stats['total_questions']}</code>\n"
    )
    await safe_edit_text(call.message, text, reply_markup=get_admin_dashboard_keyboard())
    await call.answer()

# دالة مساعدة لتصدير البيانات بشكل غير متزامن
def fetch_users_sync():
    supabase = create_client(os.getenv("SUPABASE_URL"), os.getenv("SUPABASE_KEY"))
    res = supabase.table("users").select("*").order("joined_at", desc=True).execute()
    return res.data

@router.callback_query(F.data == "admin_export_users")
async def export_all_users(call: types.CallbackQuery):
    await safe_edit_text(call.message, "⏳ جاري استخراج البيانات، يرجى الانتظار...")
    
    try:
        users = await asyncio.to_thread(fetch_users_sync)
        
        if not users:
            return await safe_edit_text(call.message, "📭 لا يوجد أي طلاب مسجلين.", reply_markup=get_admin_dashboard_keyboard())
        
        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerow(["User ID", "Username", "Points", "Total Questions", "Joined At"])
        
        for u in users:
            writer.writerow([
                u.get('user_id', ''),
                u.get('username', 'Unknown'),
                u.get('points', 0),
                u.get('total_questions', 0),
                u.get('joined_at', '')
            ])
            
        csv_bytes = output.getvalue().encode('utf-8-sig')
        file = BufferedInputFile(csv_bytes, filename="users_export.csv")
        
        try:
            await call.message.delete()
        except TelegramBadRequest:
            pass # تجاوز الخطأ إذا كانت الرسالة قديمة ولا يمكن حذفها
            
        await call.message.answer_document(
            document=file, 
            caption="📥 <b>تم استخراج قائمة الطلاب بالكامل.</b>",
            reply_markup=get_admin_dashboard_keyboard(),
            parse_mode="HTML"
        )
        
    except Exception as e:
        logger.error(f"Error exporting users: {e}")
        try:
            await safe_edit_text(call.message, "❌ حدث خطأ أثناء استخراج البيانات.", reply_markup=get_admin_dashboard_keyboard())
        except TelegramBadRequest:
            await call.message.answer("❌ حدث خطأ أثناء استخراج البيانات.", reply_markup=get_admin_dashboard_keyboard())