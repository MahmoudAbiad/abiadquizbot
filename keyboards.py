from aiogram import types

def get_main_menu_keyboard(bot_username, user_id):
    """توليد لوحة الأزرار الرئيسية تشمل رابط الإحالة وشحن الرصيد"""
    ref_link = f"https://t.me/{bot_username}?start={user_id}"
    kb = [
        [types.InlineKeyboardButton(text="💰 شحن الرصيد (نقاط إضافية)", callback_data="recharge_info")],
        [types.InlineKeyboardButton(text="🔗 شارك واربح نقاط مجانية", switch_inline_query=f"\nاشترك في بوت الكويزات الرهيب عبر رابطي واربح نقاطاً: {ref_link}")]
    ]
    return types.InlineKeyboardMarkup(inline_keyboard=kb)