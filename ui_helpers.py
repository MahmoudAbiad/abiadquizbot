"""
UI and Messaging helper functions for Telegram Bot interactions.
"""

from aiogram import types
from constants import DAILY_RENEWAL_POINTS
from logger import get_logger, log_info

logger = get_logger(__name__)

async def check_and_notify_daily_renewal(event: types.Message | types.CallbackQuery, user_info: dict) -> bool:
    """
    دالة موحدة ومعزولة تفحص حالة المستخدم وتتولى إرسال تنبيه الشحن اليومي 
    مرة واحدة فقط وبشكل تلقائي بناءً على نوع الحدث القادم.
    """
    if user_info.get("status") == "renewed":
        text = (
            "☀️ <b>يا أهلاً، يومك سعيد!</b>\n\n"
            f"دائماً معك في رحلتك الدراسية.. تم تجديد رصيدك اليومي وإضافة "
            f"<b>{DAILY_RENEWAL_POINTS} نقطة مجانية جديدة</b> لحسابك. 🔄"
        )
        
        try:
            # إذا كان الحدث رسالة نصية عادية أو ملف مرفوع
            if isinstance(event, types.Message):
                await event.answer(text, parse_mode="HTML")
                log_info(logger, f"Sent daily renewal notification via Message to user: {event.from_user.id}")
            
            # إذا كان الحدث ضغطة زر من كيبورد إنلاين (CallbackQuery)
            elif isinstance(event, types.CallbackQuery):
                await event.message.answer(text, parse_mode="HTML")
                log_info(logger, f"Sent daily renewal notification via CallbackQuery to user: {event.from_user.id}")
                
            return True # تم التجديد والإرسال بنجاح
        except Exception as e:
            from logger import log_error
            log_error(logger, f"Failed to send daily renewal notification: {e}")
            
    return False # لم يتم التجديد في هذا الطلب (مستخدم عادي)