"""Blocks private bot interactions until the current legal documents are accepted."""
from aiogram import BaseMiddleware

from logger import get_logger
from services.legal_consent import consent_prompt, has_current_consent

logger = get_logger(__name__)


class LegalConsentMiddleware(BaseMiddleware):
    async def __call__(self, handler, event, data):
        user = getattr(event, "from_user", None)
        if not user:
            return await handler(event, data)

        message = event if hasattr(event, "text") else None
        callback_message = getattr(event, "message", None)
        chat = getattr(event, "chat", None) or getattr(callback_message, "chat", None) or getattr(message, "chat", None)
        if chat is not None and getattr(chat, "type", None) != "private":
            return await handler(event, data)

        if message:
            parts = (message.text or "").split(maxsplit=1)
            if parts and parts[0].split("@")[0] == "/start":
                return await handler(event, data)

        callback_data = getattr(event, "data", None)
        if callback_data and callback_data.startswith("legal_consent:"):
            return await handler(event, data)

        try:
            if await has_current_consent(user.id):
                return await handler(event, data)
        except Exception:
            logger.exception("Could not verify legal consent for user %s", user.id)
            if message:
                await message.answer("تعذر التحقق من الموافقة حالياً. أرسل /start وحاول مجدداً بعد قليل.")
            elif hasattr(event, "answer"):
                await event.answer("تعذر التحقق من الموافقة حالياً. حاول مجدداً بعد قليل.", show_alert=True)
            return None

        try:
            body, keyboard = await consent_prompt(is_new_user=False)
            if message:
                await message.answer(body, parse_mode="HTML", reply_markup=keyboard)
            elif hasattr(event, "answer"):
                await event.answer("لمتابعة استخدام البوت، يرجى الموافقة على الشروط والخصوصية.", show_alert=True)
        except Exception:
            logger.exception("Could not send legal consent prompt to user %s", user.id)
        return None
