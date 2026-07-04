import asyncio
from aiogram import Router, types, F
from aiogram.filters import Command, CommandObject
from config import bot
from keyboards import get_main_menu_keyboard
from supabase_helper import check_or_add_user

router = Router()

@router.message(Command("start"))
async def start(msg: types.Message, command: CommandObject):
    bot_info = await bot.get_me()
    
    # فحص إذا كان المستخدم مسجلاً عبر رابط إحالة طالب آخر
    referrer_id = None
    if command.args and command.args.isdigit():
        referrer_id = int(command.args)
        
    # تم إزالة asyncio.to_thread
    user_info = await check_or_add_user(msg.from_user.id, msg.from_user.username or "Unknown", referrer_id)
    
    points = user_info["points"]
    status = user_info["status"]
    
    welcome_text = "مرحباً بك مجدداً! 👋\n\n"
    if status == "new":
        welcome_text = "مرحباً بك في بوت توليد الكويزات الذكي! 🎉\nلقد حصلت على **20 نقطة ترحيبية** مجانية.\n"
        if user_info["referrer"]:
            welcome_text += "🎁 تم منح زميلك الذي دعاك مكافأة أيضاً!\n"
    elif status == "renewed":
        welcome_text += "☀️ صباح الخير! تم تجديد رصيدك ومنحك **15 نقطة مجانية جديدة** لليوم.\n"
        
    from config import MAX_PDF_PAGES
    welcome_text += f"أرسل ملف PDF (حتى {MAX_PDF_PAGES} صفحة) أو صورة للمحاضرة لإنشاء كويز تفاعلي فوراً.\n\n💰 رصيدك الحالي: {points} نقطة."
    
    await msg.answer(welcome_text, reply_markup=get_main_menu_keyboard(bot_info.username, msg.from_user.id))

@router.callback_query(F.data == "recharge_info")
async def show_recharge_info(call: types.CallbackQuery):
    """عرض معلومات شحن الرصيد"""
    recharge_text = (
        "💳 **نظام شحن النقاط المتقدم**\n\n"
        "إذا نفدت نقاطك المجانية وتريد شحن رصيدك بكميات مخصصة لتوليد اختبارات غير محدودة، "
        "يرجى التواصل مباشرة مع الإدارة عبر المعرف التالي:\n\n"
        "👉 @abiadd\n\n"
        "أرسل له الآيدي الخاص بك مع الكمية المطلوبة وسيتم التفعيل فوراً! 🚀"
    )
    await call.message.answer(recharge_text)
    await call.answer()
