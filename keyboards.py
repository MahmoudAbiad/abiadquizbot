"""
Keyboard layouts and UI button definitions.
Provides reusable button sets for different scenarios with compressed list comprehensions.
"""

from aiogram import types
from logger import get_logger

logger = get_logger(__name__)

def get_main_menu_keyboard(bot_username: str, user_id: int) -> types.InlineKeyboardMarkup:
    try:
        ref_link = f"https://t.me/{bot_username}?start={user_id}"
        kb = [
            [types.InlineKeyboardButton(text="💰 شحن الرصيد (نقاط إضافية)", callback_data="recharge_info")],
            [types.InlineKeyboardButton(text="⭐ قائمتي المفضلة", callback_data="favorites_menu")],
            [types.InlineKeyboardButton(text="🔗 شارك واربح نقاط مجانية", switch_inline_query=f"\nاشترك في بوت الكويزات الرهيب عبر رابطي واربح نقاطاً: {ref_link}")]
        ]
        return types.InlineKeyboardMarkup(inline_keyboard=kb)
    except Exception as e:
        logger.error(f"Error generating main menu keyboard: {e}")
        return types.InlineKeyboardMarkup(inline_keyboard=[])

def get_quiz_result_keyboard() -> types.InlineKeyboardMarkup:
    kb = [
        [types.InlineKeyboardButton(text="🔄 إعادة الاختبار", callback_data="quiz_replay")],
        [types.InlineKeyboardButton(text="🔗 مشاركة الكويز", callback_data="quiz_share")],
        [types.InlineKeyboardButton(text="⭐ حفظ في المفضلة", callback_data="quiz_favorite")],
        [types.InlineKeyboardButton(text="🏠 القائمة الرئيسية", callback_data="quiz_home")]
    ]
    return types.InlineKeyboardMarkup(inline_keyboard=kb)

def get_favorites_keyboard(favorites: list) -> types.InlineKeyboardMarkup:
    kb = []
    for item in favorites:
        title = item.get("title") or "كويز محفوظ"
        favorite_id = item.get("favorite_id")
        kb.append([types.InlineKeyboardButton(text=f"🎯 {title}", callback_data=f"fav_open_{favorite_id}")])
        kb.append([types.InlineKeyboardButton(text="🗑 حذف من المفضلة", callback_data=f"fav_del_{favorite_id}")])
    kb.append([types.InlineKeyboardButton(text="🏠 العودة للقائمة الرئيسية", callback_data="favorites_back")])
    return types.InlineKeyboardMarkup(inline_keyboard=kb)

def get_cache_choice_keyboard(points_cost: int) -> types.InlineKeyboardMarkup:
    kb = [
        [types.InlineKeyboardButton(text=f"🎁 كويز جاهز بـ {points_cost} نقطة (خصم 80%)", callback_data="cache_accept")],
        [types.InlineKeyboardButton(text="🆕 توليد كويز جديد (تكلفة كاملة)", callback_data="cache_reject")]
    ]
    return types.InlineKeyboardMarkup(inline_keyboard=kb)

def get_quiz_question_keyboard(options: list) -> types.InlineKeyboardMarkup:
    kb = [[types.InlineKeyboardButton(text=opt, callback_data=f"ans_{i}")] for i, opt in enumerate(options)]
    kb.append([types.InlineKeyboardButton(text="💡 طلب تلميح", callback_data="get_hint")])
    return types.InlineKeyboardMarkup(inline_keyboard=kb)

def get_quiz_answered_keyboard(options: list, correct_opt: int, selected_opt: int) -> types.InlineKeyboardMarkup:
    kb = []
    for i, opt in enumerate(options):
        prefix = "🟢 " if i == correct_opt else "🔴 " if i == selected_opt else ""
        kb.append([types.InlineKeyboardButton(text=f"{prefix}{opt}", callback_data="ignored")])
    kb.append([types.InlineKeyboardButton(text="➡️ السؤال التالي", callback_data="next_question")])
    return types.InlineKeyboardMarkup(inline_keyboard=kb)