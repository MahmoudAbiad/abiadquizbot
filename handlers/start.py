from aiogram import Router, types, F
from aiogram.filters import Command, CommandObject
from config import bot
from keyboards import get_main_menu_keyboard
from supabase_helper import check_or_add_user

router = Router()

def _format_date_ar(dt_value):
    if not dt_value:
        return "غير محدد"
    return dt_value.strftime("%Y-%m-%d")

def _format_duration_ar(start_dt, end_dt):
    if not start_dt or not end_dt:
        return "غير محددة"

    delta = end_dt - start_dt
    days = max(0, delta.days)

    if days == 0:
        return "أقل من يوم"
    if days == 1:
        return "يوم واحد"
    return f"{days} أيام"

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
    
    welcome_text = "مرحباً بك مجدداً في **ابيض منشئ الكويزات**! 👋\n\n"
    if status == "new":
        welcome_text = "مرحباً بك في **ابيض منشئ الكويزات**! 🎉\nلقد حصلت على **20 نقطة ترحيبية** مجانية.\n"
        if user_info["referrer"]:
            welcome_text += "🎁 تم منح زميلك الذي دعاك مكافأة أيضاً!\n"
    elif status == "renewed":
        welcome_text += "☀️ صباح الخير! تم تجديد رصيدك ومنحك **15 نقطة مجانية جديدة** لليوم.\n"
        
    from config import MAX_PDF_PAGES
    welcome_text += f"أرسل ملف PDF (حتى {MAX_PDF_PAGES} صفحة) أو صورة للمحاضرة لإنشاء كويز تفاعلي فوراً.\n\n💰 الرصيد العام: {points} نقطة."
    
    await msg.answer(welcome_text, reply_markup=get_main_menu_keyboard(bot_info.username, msg.from_user.id))

@router.callback_query(F.data == "my_balance_gifts")
async def show_balance_and_gifts(call: types.CallbackQuery):
    user_info = await check_or_add_user(call.from_user.id, call.from_user.username or "Unknown")
    points = user_info["points"]
    gift_info = user_info.get("gift_info", {})

    response_lines = [
        "💳 **رصيدي و هداياي**",
        "",
        f"💰 رصيدك الحالي: **{points} نقطة**",
    ]

    if gift_info.get("has_gift"):
        start_dt = gift_info.get("start")
        end_dt = gift_info.get("end")
        gift_points = gift_info.get("points")
        duration = _format_duration_ar(start_dt, end_dt)

        response_lines.extend([
            "",
            "🎁 **تفاصيل الهدية**",
            f"• تاريخ البدء: {_format_date_ar(start_dt)}",
            f"• تاريخ الانتهاء: {_format_date_ar(end_dt)}",
            f"• المدة التقديرية: {duration}",
        ])

        if gift_points is not None:
            response_lines.append(f"• نقاط الهدية: {gift_points} نقطة")
    else:
        response_lines.extend([
            "",
            "🎁 لا توجد لديك هدايا مفعّلة حالياً.",
        ])

    details_keyboard = types.InlineKeyboardMarkup(
        inline_keyboard=[
            [types.InlineKeyboardButton(text="💰 شحن الرصيد (نقاط إضافية)", callback_data="recharge_info")]
        ]
    )

    await call.message.answer("\n".join(response_lines), reply_markup=details_keyboard)
    await call.answer()

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
