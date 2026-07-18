"""
Start command and recharge info handler.
Manages initial user onboarding and account information display.
"""

import asyncio
from typing import Optional
from aiogram import Router, types, F
from aiogram.filters import Command, CommandObject
from aiogram.fsm.context import FSMContext
from config import bot
from keyboards import get_main_menu_keyboard
from supabase_helper import check_or_add_user, get_shared_quiz, get_favorite_quiz_by_global_id
from logger import get_logger, log_warning, log_info
from constants import ADMIN_CONTACT, MAX_PDF_PAGES, DAILY_RENEWAL_POINTS

logger = get_logger(__name__)
router = Router()

@router.message(Command("start"))
async def start(msg: types.Message, command: CommandObject, state: FSMContext):
    """
    Start command handler - greets user and displays welcome info.
    Handles referral system if user was invited by another user.
    
    Args:
        msg: Message object
        command: Command object with optional referrer ID
    """
    try:
        bot_info = await bot.get_me()
        
        # 1. 🔥 تم التحديث: التحقق من وجود رابط مشاركة كويز (يدعم كلا البادئتين لضمان التوافق)
        # 1. 🔥 تم التحديث: التحقق من الرابط والبحث في الجدول المناسب حسب الهيكل
        if command.args and (command.args.startswith("share_") or command.args.startswith("quiz_")):
            if command.args.startswith("share_"):
                share_id = command.args.replace("share_", "", 1)
                # البحث في جدول shared_quizzes مباشرة بشكل غير متزامن
                shared = await get_shared_quiz(share_id)
            else:
                share_id = command.args.replace("quiz_", "", 1)
                
                # 1. البحث في جدول المفضلة أولاً
                shared = await get_favorite_quiz_by_global_id(share_id)
                
                # 2. 🆕 إذا لم يجده في المفضلة، يبحث عنه في الكويزات المشتركة المخفية
                if not shared:
                    shared = await get_shared_quiz(share_id)
                
            if shared:
                # حماية: تسجيل أو التحقق من المستخدم في الداتابيز أولاً بشكل مباشر
                await check_or_add_user(
                    msg.from_user.id,
                    msg.from_user.username or "Unknown",
                    msg.from_user.first_name,
                    msg.from_user.last_name or "Unknown",
                    None
                )
                
                from handlers.execution import _start_loaded_quiz
                await msg.answer(f"🔗 تم فتح كويز مشترك: {shared.get('title') or 'كويز مشترك'}")
                await _start_loaded_quiz(
                    msg, state, shared["quiz_data"], 
                    shared.get('title') or 'كويز مشترك', 
                    origin="shared", 
                    quiz_id=share_id
                )
                return
          

        # Extract referrer ID from command arguments
        referrer_id: Optional[int] = None
        if command.args and command.args.isdigit():
            referrer_id = int(command.args)
            log_info(logger, f"User {msg.from_user.id} joined via referral from {referrer_id}")
        
        # Check or create user in database بشكل مباشر دون استهلاك خيوط
        user_info = await check_or_add_user(
            msg.from_user.id,
            msg.from_user.username or "Unknown",
            msg.from_user.first_name,
            msg.from_user.last_name or "Unknown",
            referrer_id
        )
        
        points = user_info["points"]
        status = user_info["status"]
        
        # Build welcome message based on user status
        welcome_text = ""
        
        if status == "new":
            welcome_text = (
                "👋 <b>أهلاً بك في بوت الكويزات الذكي!</b>\n"
                "مكانك الأول لتحويل المحاضرات لاختبارات تفاعلية بسهولة. 🚀\n\n"
                f"🎁 هدية البداية: ضفنا لحسابك <b>{points} نقطة مجانية</b> لتجرب البوت فوراً!\n"
            )
            if user_info["referrer"]:
                welcome_text += "✨ وجميلك ما ننساه! تم منح زميلك الذي دعاك مكافأة إضافية أيضاً. 🤝\n"
                
        elif status == "renewed":
            welcome_text = (
                "☀️ <b>يا أهلاً، يومك سعيد!</b>\n\n"
                f"دائماً معك في رحلتك الدراسية.. تم تجديد رصيدك اليومي وإضافة <b>{DAILY_RENEWAL_POINTS} نقطة مجانية جديدة</b> لحسابك. 🔄\n"
            )
        else:
            welcome_text = "👋 <b>يا مرحباً بك مجدداً!</b>\n جاهز لاختبار جديد اليوم؟ ✍️\n"
        
        # Add main instructions
        welcome_text += (
            f"\n💡 <b>طريقة الاستخدام في ثوانٍ:</b>\n"
            f"1️⃣ أرسل ملف الـ PDF الخاص بمحاضرتك (بحد أقصى {MAX_PDF_PAGES} صفحة) أو حتى صورة واضحة لها.\n"
            f"2️⃣ حدد عدد الأسئلة التي تفضلها.\n"
            f"3️⃣ ابدأ حل الكويز التفاعلي واختبر معلوماتك! 🔥\n\n"
            f"📊 <b>رصيدك الحالي:</b> <code>{points}</code> نقطة\n\n"
            f"🎯 <b>نصيحة ذكية:</b> شارك البوت مع زملائك عبر رابط الدعوة الخاص بك واكسب نقاطاً إضافية مع كل مشترك جديد! 🎁"
        )
        
        await msg.answer(
            welcome_text,
            parse_mode="HTML",
            reply_markup=get_main_menu_keyboard(bot_info.username, msg.from_user.id)
        )
        
        log_info(logger, f"User {msg.from_user.id} started bot. Status: {status}, Points: {points}")
        
    except Exception as e:
        logger.error(f"Error in start handler: {e}")
        await msg.answer(
            "⚠️ <b>عذراً، واجهنا مشكلة بسيطة أثناء تجهيز حسابك.</b>\n"
            "يرجى محاولة الضغط على /start مرة أخرى بعد قليل.",
            parse_mode="HTML"
        )

@router.callback_query(F.data == "recharge_info")
async def show_recharge_info(call: types.CallbackQuery):
    """
    Show recharge information and contact details.
    """
    try:
        recharge_text = (
            "🔋 <b>شحن النقاط وزيادة الرصيد</b>\n\n"
            "هل استهلكت نقاطك المجانية وتحتاج للمزيد؟ لا تقلق! "
            "يمكنك شحن رصيدك بكميات مخصصة لتوليد اختبارات بلا حدود والتحضير للامتحانات بكل راحة. 📚\n\n"
            "لطلب الشحن, كل ما عليك هو التواصل مباشرة مع الدعم والإدارة عبر الرابط التالي:\n"
            f"👉 <b>{ADMIN_CONTACT}</b>\n\n"
            "📝 <b>طريقة الشحن:</b>\n"
            "1️⃣ تواصل معنا وحدد عدد النقاط التي تحتاجها.\n"
            "2️⃣ سيتم تأكيد وتفعيل النقاط في حسابك فوراً.\n"
            "3️⃣ يمكنك العودة ومتابعة المذاكرة وتوليد الكويزات دون أي انقطاع! 🚀"
        )
        await call.message.answer(recharge_text, parse_mode="HTML")
        log_info(logger, f"User {call.from_user.id} requested recharge info")
        
    except Exception as e:
        logger.error(f"Error in show_recharge_info: {e}")
        await call.message.answer(
            "⚠️ <b>عذراً، لم نتمكن من تحميل معلومات الشحن حالياً.</b>\n"
            "يرجى المحاولة مرة أخرى.",
            parse_mode="HTML"
        )
    finally:
        await call.answer()

async def set_bot_commands(bot):
    commands = [
        types.BotCommand(command="start", description="تشغيل البوت والتحقق من الرصيد"),
    ]
    await bot.set_my_commands(commands)