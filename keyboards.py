"""
Keyboard layouts and UI button definitions.
Provides reusable button sets for different scenarios.
"""

from typing import Optional
from aiogram import types
from logger import get_logger

logger = get_logger(__name__)

def get_main_menu_keyboard(bot_username: str, user_id: int) -> types.InlineKeyboardMarkup:
    """
    Generate main menu keyboard with referral and recharge buttons.
    
    Args:
        bot_username: Bot username from Telegram
        user_id: Current user's ID for referral link
        
    Returns:
        InlineKeyboardMarkup with menu buttons
    """
    try:
        ref_link = f"https://t.me/{bot_username}?start={user_id}"
        kb = [
            [types.InlineKeyboardButton(
                text="💰 شحن الرصيد (نقاط إضافية)",
                callback_data="recharge_info"
            )],
            [types.InlineKeyboardButton(
                text="🔗 شارك واربح نقاط مجانية",
                switch_inline_query=f"\nاشترك في بوت الكويزات الرهيب عبر رابطي واربح نقاطاً: {ref_link}"
            )]
        ]
        return types.InlineKeyboardMarkup(inline_keyboard=kb)
    except Exception as e:
        logger.error(f"Error generating main menu keyboard: {e}")
        # Return empty keyboard as fallback
        return types.InlineKeyboardMarkup(inline_keyboard=[])

def get_quiz_start_keyboard() -> types.InlineKeyboardMarkup:
    """
    Generate keyboard for quiz start button.
    
    Returns:
        InlineKeyboardMarkup with start button
    """
    kb = [
        [types.InlineKeyboardButton(
            text="🚀 ابدأ الاختبار الآن",
            callback_data="start_first_question"
        )]
    ]
    return types.InlineKeyboardMarkup(inline_keyboard=kb)

def get_quiz_answer_keyboard(question_text: str, options: list, hint_available: bool = True) -> types.InlineKeyboardMarkup:
    """
    Generate keyboard for answering a quiz question.
    
    Args:
        question_text: The question text (for reference)
        options: List of answer options
        hint_available: Whether hint is available
        
    Returns:
        InlineKeyboardMarkup with answer buttons and hint button
    """
    kb = []
    for i, opt in enumerate(options):
        kb.append([types.InlineKeyboardButton(
            text=opt,
            callback_data=f"ans_{i}"
        )])
    
    if hint_available:
        kb.append([types.InlineKeyboardButton(
            text="💡 طلب تلميح",
            callback_data="get_hint"
        )])
    
    return types.InlineKeyboardMarkup(inline_keyboard=kb)

def get_quiz_result_keyboard() -> types.InlineKeyboardMarkup:
    """
    Generate keyboard for showing quiz result and continuing.
    
    Returns:
        InlineKeyboardMarkup with next button
    """
    kb = [
        [types.InlineKeyboardButton(
            text="➡️ السؤال التالي",
            callback_data="next_question"
        )]
    ]
    return types.InlineKeyboardMarkup(inline_keyboard=kb)

def get_empty_keyboard() -> types.InlineKeyboardMarkup:
    """
    Generate empty keyboard (removes previous buttons).
    
    Returns:
        Empty InlineKeyboardMarkup
    """
    return types.InlineKeyboardMarkup(inline_keyboard=[])
