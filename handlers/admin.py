"""
Admin-specific command handlers.
Provides commands for user management, statistics, and points administration.
"""

import asyncio
from typing import Optional
from aiogram import Router, types
from aiogram.filters import Command, CommandObject
from config import bot, ADMIN_ID
from supabase_helper import admin_add_points, admin_get_global_stats, admin_search_user
from validators import safe_parse_command_args, validate_user_id, validate_points_amount
from logger import get_logger, log_warning, log_info
from supabase import create_client
import os
from constants import SUCCESS_POINTS_CHARGED, ADMIN_CONTACT

logger = get_logger(__name__)
router = Router()

def _is_admin(user_id: int) -> bool:
    return user_id == ADMIN_ID

# ==================== Admin Commands ====================

@router.message(Command("charge"))
async def admin_charge_points(msg: types.Message, command: CommandObject):
    if not _is_admin(msg.from_user.id):
        return
    
    try:
        if not command.args:
            await msg.answer("⚠️ الصيغة:\n`/charge <آيدي_المستخدم> <كمية_النقاط>`")
            return
        
        success, parsed = safe_parse_command_args(command.args, expected_count=2)
        if not success:
            await msg.answer("❌ خطأ في تحليل المعاملات.")
            return
        
        try:
            target_id = int(parsed[0])
            amount = int(parsed[1])
        except ValueError:
            await msg.answer("❌ الآيدي والكمية يجب أن تكون أرقاماً.")
            return
        
        new_balance = await asyncio.to_thread(admin_add_points, target_id, amount)
        
        if new_balance is not None:
            await msg.answer(SUCCESS_POINTS_CHARGED.format(amount=amount, user_id=target_id, balance=new_balance))
            try:
                await bot.send_message(
                    target_id,
                    f"🎉 بشرى سارة! قامت الإدارة بشحن حسابك بـ **{amount}** نقطة إضافية.\n"
                    f"💰 رصيدك الإجمالي الحالي: **{new_balance}** نقطة.\nاستمتع بالاختبارات! 🚀"
                )
            except Exception:
                pass
        else:
            await msg.answer("❌ فشل الشحن. تأكد من أن رقم الآيدي صحيح ومسجل.")
            
    except Exception as e:
        logger.error(f"Error in admin_charge_points: {e}")

@router.message(Command("dbstats"))
async def admin_db_stats(msg: types.Message):
    if not _is_admin(msg.from_user.id):
        return
    
    try:
        stats = await asyncio.to_thread(admin_get_global_stats)
        report = (
            "📊 **إحصائيات قاعدة البيانات (Supabase) الحالية:**\n\n"
            f"👥 إجمالي الطلاب: **{stats['total_users']}** طالب/طالبة\n"
            f"📝 إجمالي الأسئلة: **{stats['total_questions']}** سؤال\n\n"
            f"📈 متوسط الأسئلة لكل طالب: "
            f"**{stats['total_questions'] / max(1, stats['total_users']):.1f}** أسئلة"
        )
        await msg.answer(report)
    except Exception as e:
        logger.error(f"Error in admin_db_stats: {e}")

@router.message(Command("searchuser"))
async def admin_get_user(msg: types.Message, command: CommandObject):
    if not _is_admin(msg.from_user.id):
        return
    
    try:
        if not command.args:
            await msg.answer("الرجاء إدخال اليوزرنيم (أو جزء منه) أو الآيدي.\nمثال: `/searchuser ahmad`")
            return
        
        query = command.args.strip()
        users_data = await asyncio.to_thread(admin_search_user, query)
        
        if users_data:
            report = f"🔍 **نتائج البحث عن ({query}):**\n\n"
            for u in users_data:
                referred_by = u.get('referred_by') or "لم يتم الإحالة"
                report += (
                    f"🆔 الآيدي: `{u['user_id']}`\n"
                    f"👤 اليوزر: @{u['username']}\n"
                    f"💰 النقاط: **{u['points']}**\n"
                    f"📈 الأسئلة المستهلكة: **{u['total_questions']}**\n"
                    f"🔗 مستدعى بواسطة: `{referred_by}`\n"
                    "───────────────\n"
                )
            
            # تقطيع الرسالة إذا كانت طويلة
            if len(report) > 4000:
                parts = [report[i:i+4000] for i in range(0, len(report), 4000)]
                for part in parts:
                    await msg.answer(part)
            else:
                await msg.answer(report)
        else:
            await msg.answer("❌ لم يتم العثور على أي نتائج مطابقة.")
            
    except Exception as e:
        logger.error(f"Error in admin_get_user: {e}")

@router.message(Command("fetchall"))
async def admin_fetch_all_users(msg: types.Message):
    if not _is_admin(msg.from_user.id):
        return
    
    try:
        supabase = create_client(os.getenv("SUPABASE_URL"), os.getenv("SUPABASE_KEY"))
        res = supabase.table("users").select("*").order("created_at", desc=True).execute()
        users = res.data
        
        if not users:
            await msg.answer("📭 لا يوجد أي طلاب مسجلين في قاعدة البيانات حتى الآن.")
            return
        
        report = "👥 **سجل الطلاب المسجلين بالكامل في البوت:**\n\n"
        for idx, u in enumerate(users, 1):
            username_str = f"@{u['username']}" if u['username'] and u['username'] != "Unknown" else "بلا يوزر"
            report += f"{idx}. 🆔 `{u['user_id']}` | 👤 {username_str} | 💰 {u['points']} ن | 📈 {u['total_questions']} س\n"
            
            if len(report) > 3500:
                await msg.answer(report)
                report = ""
                
        if report:
            await msg.answer(report)
            
    except Exception as e:
        logger.error(f"Error fetching all users: {e}")
        await msg.answer("❌ حدث خطأ أثناء جلب سجلات الطلاب.")