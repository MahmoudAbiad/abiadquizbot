"""
Start command and recharge info handler.
Manages initial user onboarding and account information display.
"""

import asyncio
from typing import Optional
from aiogram import Router, types, F
from aiogram.filters import Command, CommandObject
from config import bot
from keyboards import get_main_menu_keyboard
from supabase_helper import check_or_add_user
from logger import get_logger, log_warning, log_info
from constants import ADMIN_CONTACT, MAX_PDF_PAGES

logger = get_logger(__name__)
router = Router()

@router.message(Command("start"))
async def start(msg: types.Message, command: CommandObject):
    """
    Start command handler - greets user and displays welcome info.
    Handles referral system if user was invited by another user.
    
    Args:
        msg: Message object
        command: Command object with optional referrer ID
    """
    try:
        bot_info = await bot.get_me()
        
        # Extract referrer ID from command arguments
        referrer_id: Optional[int] = None
        if command.args and command.args.isdigit():
            referrer_id = int(command.args)
            log_info(logger, f"User {msg.from_user.id} joined via referral from {referrer_id}")
        
        # Check or create user in database
        user_info = await asyncio.to_thread(
            check_or_add_user,
            msg.from_user.id,
            msg.from_user.username or "Unknown",
            referrer_id
        )
        
        points = user_info["points"]
        status = user_info["status"]
        
        # Build welcome message based on user status
        welcome_text = ""
        
        if status == "new":
            welcome_text = (
                "🎉 **مرحباً بك في بوت توليد الكويزات الذكي!**\n\n"
                f"🎁 لقد حصلت على **{points} نقطة ترحيبية** مجانية لبدء رحلتك.\n"
            )
            if user_info["referrer"]:
                welcome_text += "✨ شكراً لتوصيتك! تم منح زميلك الذي دعاك مكافأة أيضاً.\n"
                
        elif status == "renewed":
            welcome_text = (
                "☀️ **صباح/مساء الخير!**\n\n"
                f"✅ تم تجديد رصيدك ومنحك **15 نقطة مجانية جديدة** لليوم.\n"
            )
        else:
            welcome_text = "👋 **مرحباً بك مجدداً!**\n\n"
        
        # Add main instructions
        welcome_text += (
            f"\n📚 **كيفية الاستخدام:**\n"
            f"1. أرسل ملف PDF (حتى {MAX_PDF_PAGES} صفحة) أو صورة لمحاضرتك\n"
            f"2. حدد عدد الأسئلة التي تريدها\n"
            f"3. استمتع بالاختبار التفاعلي الذكي!\n\n"
            f"💰 **رصيدك الحالي: {points} نقطة**\n\n"
            f"💡 **نصيحة:** يمكنك دعوة زملاءك واربح نقاطاً إضافية! 🎯"
        )
        
        await msg.answer(
            welcome_text,
            reply_markup=get_main_menu_keyboard(bot_info.username, msg.from_user.id)
        )
        
        log_info(logger, f"User {msg.from_user.id} started bot. Status: {status}, Points: {points}")
        
    except Exception as e:
        logger.error(f"Error in start handler: {e}")
        await msg.answer(
            "❌ عذراً، حدث خطأ أثناء معالجة طلبك. يرجى محاولة لاحقاً."
        )

@router.callback_query(F.data == "recharge_info")
async def show_recharge_info(call: types.CallbackQuery):
    """
    Show recharge information and contact details.
    
    Args:
        call: Callback query object
    """
    try:
        recharge_text = (
            "💳 **نظام شحن النقاط المتقدم**\n\n"
            "إذا نفدت نقاطك المجانية وتريد شحن رصيدك بكميات مخصصة لتوليد "
            "اختبارات غير محدودة دون قلق، يرجى التواصل مباشرة مع الإدارة:\n\n"
            f"👉 **{ADMIN_CONTACT}**\n\n"
            "📋 **خطوات الشحن:**\n"
            "1. أرسل لنا رسالة مع الكمية المطلوبة\n"
            "2. ستتم معالجة طلبك فوراً\n"
            "3. سيتم شحن حسابك وستتمكن من الاستمرار مباشرة\n\n"
            "🚀 استمتع بالاختبارات!"
        )
        await call.message.answer(recharge_text)
        log_info(logger, f"User {call.from_user.id} requested recharge info")
        
    except Exception as e:
        logger.error(f"Error in show_recharge_info: {e}")
        await call.message.answer(
            "❌ عذراً، حدث خطأ أثناء عرض معلومات الشحن."
        )
    
    finally:
        await call.answer()
