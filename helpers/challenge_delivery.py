"""Shared delivery for live/land challenges. Reuses source photos; no rendering/AI."""
from aiogram import types
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError


async def remove_old_buttons(bot, chat_id, message_id):
    if not message_id:
        return
    try:
        await bot.edit_message_reply_markup(chat_id=chat_id, message_id=message_id, reply_markup=None)
    except Exception:
        # Cosmetic cleanup after successful delivery. Never hides the new message.
        pass


async def deliver_screen(bot, chat_id, message_id, text, markup):
    """Return the actual message id, None for blocked users; transient failures propagate.

    Text -> photo and photo -> photo use editMessageMedia. Photo -> text falls back
    to a fresh text message. Only retire old buttons AFTER the replacement succeeds.
    An unavailable image is an error, never a silent conversion to a text question.
    """
    image_url = getattr(text, "image_url", None)
    if image_url and len(text) > 1024:
        raise ValueError("challenge_caption_too_long")
    if message_id:
        try:
            if image_url:
                await bot.edit_message_media(chat_id=chat_id, message_id=message_id,
                    media=types.InputMediaPhoto(media=image_url, caption=str(text), parse_mode=None),
                    reply_markup=markup)
            else:
                await bot.edit_message_text(str(text), chat_id=chat_id, message_id=message_id,
                                           reply_markup=markup, parse_mode=None)
            return message_id
        except TelegramBadRequest as exc:
            if "message is not modified" in str(exc).lower():
                return message_id
        except TelegramForbiddenError:
            return None
    try:
        if image_url:
            sent = await bot.send_photo(chat_id=chat_id, photo=image_url, caption=str(text),
                                        reply_markup=markup, parse_mode=None)
        else:
            sent = await bot.send_message(chat_id, str(text), reply_markup=markup, parse_mode=None)
    except TelegramForbiddenError:
        return None
    await remove_old_buttons(bot, chat_id, message_id)
    return sent.message_id
