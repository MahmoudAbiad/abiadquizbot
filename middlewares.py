"""
Middlewares for Telegram Bot.
Handles anti-spam (Rate Limiting) to protect the bot and API keys.
"""

from typing import Any, Awaitable, Callable, Dict
from aiogram import BaseMiddleware
from aiogram.types import Message, CallbackQuery
from cachetools import TTLCache
from logger import get_logger

logger = get_logger(__name__)

class ThrottlingMiddleware(BaseMiddleware):
    """
    ميدل وير يمنع المستخدمين من إرسال طلبات متتالية سريعة جداً.
    """
    def __init__(self, limit: int = 4):
        # السماح بطلب واحد فقط كل (limit) ثوانٍ لكل مستخدم
        self.cache = TTLCache(maxsize=10000, ttl=limit)

    async def __call__(
        self,
        handler: Callable[[Any, Dict[str, Any]], Awaitable[Any]],
        event: Any,
        data: Dict[str, Any]
    ) -> Any:
        # التحقق مما إذا كان الحدث رسالة أو ضغطة زر
        if isinstance(event, (Message, CallbackQuery)):
            user_id = event.from_user.id
            
            # إذا كان المستخدم في الكاش، فهذا يعني أنه أرسل طلباً قبل انتهاء المهلة
            if user_id in self.cache:
                if isinstance(event, Message):
                    await event.answer("⚠️ **مهلاً!** الرجاء الانتظار بضع ثوانٍ قبل إرسال طلب جديد لتجنب الضغط على البوت.")
                else:
                    await event.answer("⚠️ الرجاء الانتظار...", show_alert=True)
                
                logger.warning(f"Spam detected and blocked for user: {user_id}")
                return # ننهي العملية هنا ولا نمررها للبوت
            
            # إضافة المستخدم للكاش
            self.cache[user_id] = True
            
        return await handler(event, data)