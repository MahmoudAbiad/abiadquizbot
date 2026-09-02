# handlers/admin/admin_utils.py
"""
دوال مشتركة بين وحدات لوحة الأدمن (كانت مكررة حرفياً بأكثر من ملف قبل هذا التوحيد).
"""

from aiogram import types
from aiogram.exceptions import TelegramBadRequest


async def safe_edit_text(message: types.Message, text: str, reply_markup=None):
    """تعديل النص بشكل آمن يتفادى أخطاء التكرار في تيليجرام (Message is not modified)."""
    try:
        await message.edit_text(text, reply_markup=reply_markup, parse_mode="HTML")
    except TelegramBadRequest:
        pass


def sanitize_csv_value(val) -> str:
    """تأمين القيم لتفادي ثغرة CSV Injection عند فتح التقرير في Excel."""
    val_str = str(val) if val is not None else ""
    if val_str.startswith(('=', '+', '-', '@')):
        return f"'{val_str}"
    return val_str
