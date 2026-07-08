"""
Admin-specific command handlers.
Updated with FSM for interactive user charging and existing management tools.
"""

import asyncio
import os
from aiogram import Router, types, F
from aiogram.filters import Command, CommandObject
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import StatesGroup, State
from supabase import create_client

# الاستيرادات الخاصة بك
from config import bot, ADMIN_ID
from supabase_helper import admin_add_points, admin_get_global_stats, admin_search_user
from keyboards import get_admin_charge_keyboard  # تأكد من إضافة هذه الدالة في ملف keyboards.py
from logger import get_logger
from constants import SUCCESS_POINTS_CHARGED

logger = get_logger(__name__)
router = Router()

# تعريف حالات الـ FSM للشحن
class AdminChargeState(StatesGroup):
    waiting_for_amount = State()

def _is_admin(user_id: int) -> bool:
    return user_id == ADMIN_ID

# ==================== Admin Commands ====================

@router.message(Command("searchuser"))
async def admin_get_user(msg: types.Message, command: CommandObject):
    if not _is_admin(msg.from_user.id):
        return
    
    if not command.args:
        await msg.answer("🔍 الرجاء إدخال اليوزر أو الآيدي للبحث. مثال: `/searchuser ahmad`")
        return
    
    query = command.args.strip()
    users_data = await asyncio.to_thread(admin_search_user, query)
    
    if users_data:
        for u in users_data:
            report = (f"🆔 الآيدي: `{u['user_id']}`\n👤 اليوزر: @{u['username']}\n💰 النقاط: {u['points']}")
            # استخدام زر الشحن التفاعلي الجديد
            await msg.answer(report, reply_markup=get_admin_charge_keyboard(u['user_id']))
    else:
        await msg.answer("❌ لم يتم العثور على نتائج.")

# --- نظام الشحن التفاعلي ---

@router.callback_query(F.data.startswith("admin_charge_"))
async def start_charge_process(callback: types.CallbackQuery, state: FSMContext):
    target_id = callback.data.split("_")[2]
    # تخزين الآيدي في الحالة المؤقتة
    await state.update_data(target_id=target_id)
    await state.set_state(AdminChargeState.waiting_for_amount)
    
    await callback.message.answer(f"✅ تم اختيار المستخدم `{target_id}`.\nأرسل الآن كمية النقاط (رقم فقط):")
    await callback.answer()

@router.message(AdminChargeState.waiting_for_amount)
async def process_amount(msg: types.Message, state: FSMContext):
    if not msg.text.isdigit():
        return await msg.answer("❌ خطأ: يرجى إرسال أرقام فقط.")
    
    amount = int(msg.text)
    data = await state.get_data()
    target_id = data.get('target_id')
    
    # تنفيذ الشحن
    new_balance = await asyncio.to_thread(admin_add_points, int(target_id), amount)
    
    if new_balance is not None:
        await msg.answer(f"✅ تمت العملية!\nالمستخدم `{target_id}` حصل على {amount} نقطة.\n💰 رصيده الجديد: {new_balance}")
    else:
        await msg.answer("❌ فشل الشحن.")
    
    # مسح الحالة لإنهاء عملية الشحن
    await state.clear()

# --- باقي الأوامر الإدارية الأصلية ---

@router.message(Command("dbstats"))
async def admin_db_stats(msg: types.Message):
    if not _is_admin(msg.from_user.id): return
    stats = await asyncio.to_thread(admin_get_global_stats)
    await msg.answer(f"📊 إحصائيات النظام:\n- إجمالي الطلاب: {stats['total_users']}\n- إجمالي الأسئلة: {stats['total_questions']}")

@router.message(Command("fetchall"))
async def admin_fetch_all_users(msg: types.Message):
    if not _is_admin(msg.from_user.id): return
    
    try:
        supabase = create_client(os.getenv("SUPABASE_URL"), os.getenv("SUPABASE_KEY"))
        res = supabase.table("users").select("*").order("joined_at", desc=True).execute()
        users = res.data
        
        if not users:
            return await msg.answer("📭 لا يوجد أي طلاب مسجلين.")
        
        report = "👥 **سجل الطلاب المسجلين بالكامل:**\n\n"
        for idx, u in enumerate(users, 1):
            username_str = f"@{u['username']}" if u['username'] and u['username'] != "Unknown" else "بلا يوزر"
            report += f"{idx}. 🆔 `{u['user_id']}` | 👤 {username_str} | 💰 {u['points']} ن\n"
            
            if len(report) > 3500: # تجنب تجاوز حد رسالة التليجرام
                await msg.answer(report)
                report = ""
        
        if report:
            await msg.answer(report)
            
    except Exception as e:
        logger.error(f"Error in admin_fetch_all_users: {e}")