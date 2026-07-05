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
from constants import (
    SUCCESS_POINTS_CHARGED, ADMIN_CONTACT
)

logger = get_logger(__name__)
router = Router()

def _is_admin(user_id: int) -> bool:
    """Check if user is the configured admin"""
    return user_id == ADMIN_ID

def _send_admin_warning() -> str:
    """Return warning message for non-admin users"""
    return ""  # Silently ignore non-admin commands

# ==================== Admin Commands ====================

@router.message(Command("charge"))
async def admin_charge_points(msg: types.Message, command: CommandObject):
    """
    Admin command to charge points to a user.
    Usage: /charge <user_id> <amount>
    
    Args:
        msg: Message object
        command: Command object with arguments
    """
    if not _is_admin(msg.from_user.id):
        log_warning(logger, f"Non-admin {msg.from_user.id} tried to use /charge")
        return
    
    try:
        if not command.args:
            await msg.answer(
                "⚠️ طريقة الاستخدام الخاطئة!\n"
                "الرجاء كتابة الأمر كالتالي:\n"
                "`/charge <آيدي_المستخدم> <كمية_النقاط>`\n\n"
                "مثال: `/charge 123456789 50`"
            )
            return
        
        # Parse arguments
        success, parsed = safe_parse_command_args(command.args, expected_count=2)
        if not success:
            await msg.answer("❌ خطأ في تحليل المعاملات. تحقق من الصيغة.")
            return
        
        try:
            target_id = int(parsed[0])
            amount = int(parsed[1])
        except ValueError:
            await msg.answer("❌ الرجاء التأكد من أن آيدي المستخدم والكمية أرقام صحيحة.")
            return
        
        # Validate inputs
        is_valid, error = validate_user_id(target_id)
        if not is_valid:
            await msg.answer(f"❌ آيدي المستخدم غير صحيح: {error}")
            return
        
        is_valid, error = validate_points_amount(amount)
        if not is_valid:
            await msg.answer(f"❌ كمية النقاط غير صحيحة: {error}")
            return
        
        # Perform charge
        new_balance = await asyncio.to_thread(admin_add_points, target_id, amount)
        
        if new_balance is not None:
            # Notify admin
            await msg.answer(
                SUCCESS_POINTS_CHARGED.format(
                    amount=amount,
                    user_id=target_id,
                    balance=new_balance
                )
            )
            
            # Try to notify user
            try:
                await bot.send_message(
                    target_id,
                    f"🎉 بشرى سارة! قامت الإدارة بشحن حسابك بـ **{amount}** نقطة إضافية.\n"
                    f"💰 رصيدك الإجمالي الحالي: **{new_balance}** نقطة.\n"
                    f"استمتع بالاختبارات! 🚀"
                )
                log_info(logger, f"Notified user {target_id} about charge")
            except Exception as e:
                log_warning(logger, f"Could not notify user {target_id}: {e}")
        else:
            await msg.answer(
                "❌ فشل الشحن. تأكد من أن رقم الآيدي صحيح ومسجل في قاعدة البيانات."
            )
            
    except Exception as e:
        logger.error(f"Error in admin_charge_points: {e}")
        await msg.answer("❌ حدث خطأ أثناء معالجة الأمر.")

@router.message(Command("dbstats"))
async def admin_db_stats(msg: types.Message):
    """
    Admin command to view database statistics.
    Usage: /dbstats
    
    Args:
        msg: Message object
    """
    if not _is_admin(msg.from_user.id):
        log_warning(logger, f"Non-admin {msg.from_user.id} tried to use /dbstats")
        return
    
    try:
        stats = await asyncio.to_thread(admin_get_global_stats)
        
        report = (
            "📊 **إحصائيات قاعدة البيانات (Supabase) الحالية:**\n\n"
            f"👥 إجمالي الطلاب المسجلين: **{stats['total_users']}** طالب/طالبة\n"
            f"📝 إجمالي الأسئلة المولدة بالذكاء الاصطناعي: **{stats['total_questions']}** سؤال\n\n"
            f"📈 متوسط الأسئلة لكل طالب: "
            f"**{stats['total_questions'] / max(1, stats['total_users']):.1f}** أسئلة"
        )
        await msg.answer(report)
        log_info(logger, f"Admin {msg.from_user.id} viewed database stats")
        
    except Exception as e:
        logger.error(f"Error in admin_db_stats: {e}")
        await msg.answer("❌ حدث خطأ أثناء جلب الإحصائيات.")

@router.message(Command("searchuser"))
async def admin_get_user(msg: types.Message, command: CommandObject):
    """
    Admin command to search for and view user information.
    Usage: /searchuser <user_id or username>
    
    Args:
        msg: Message object
        command: Command object with search query
    """
    if not _is_admin(msg.from_user.id):
        log_warning(logger, f"Non-admin {msg.from_user.id} tried to use /searchuser")
        return
    
    try:
        if not command.args:
            await msg.answer(
                "الرجاء إدخال اليوزرنيم أو الآيدي بعد الأمر.\n\n"
                "مثال:\n"
                "`/searchuser 123456789`\n"
                "`/searchuser @username`"
            )
            return
        
        query = command.args.strip()
        user_data = await asyncio.to_thread(admin_search_user, query)
        
        if user_data:
            referred_by = user_data.get('referred_by') or "لم يتم الإحالة"
            info = (
                "🔍 **بيانات الطالب المستخرجة:**\n\n"
                f"🆔 الآيدي الرقمي: `{user_data['user_id']}`\n"
                f"👤 اسم المستخدم: @{user_data['username']}\n"
                f"💰 النقاط المتاحة: **{user_data['points']}** نقطة\n"
                f"📈 الأسئلة المستهلكة: **{user_data['total_questions']}** سؤال\n"
                f"🔗 مستدعى بواسطة: `{referred_by}`\n"
                f"📅 تاريخ آخر تجديد: {user_data.get('last_renewal', 'لا يوجد')}"
            )
            await msg.answer(info)
            log_info(logger, f"Admin {msg.from_user.id} searched for user: {query}")
        else:
            await msg.answer(
                "❌ لم يتم العثور على أي بيانات مطابقة لهذا الطالب في قاعدة البيانات."
            )
            
    except Exception as e:
        logger.error(f"Error in admin_get_user: {e}")
        await msg.answer("❌ حدث خطأ أثناء البحث عن المستخدم.")
