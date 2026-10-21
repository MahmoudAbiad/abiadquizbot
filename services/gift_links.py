"""Validated Telegram /start payloads for gift-code redemption links."""
import re

GIFT_PREFIX = "gift_"
# Telegram allows at most 64 ASCII characters in a start parameter.
_CODE = re.compile(r"[A-Za-z0-9-]{12,59}\Z")
_USERNAME = re.compile(r"[A-Za-z0-9_]{5,32}\Z")


def gift_code_from_start(payload):
    """Return a normalized code, never a raw/unchecked database lookup value."""
    if not isinstance(payload, str) or not payload.startswith(GIFT_PREFIX):
        return None
    code = payload[len(GIFT_PREFIX):]
    if not _CODE.fullmatch(code):
        return None
    return code.upper()


def build_gift_link(username, code):
    """Build a Telegram link with the same constraints as the /start parser."""
    clean_username = (username or "").lstrip("@")
    if not _USERNAME.fullmatch(clean_username):
        raise ValueError("Invalid Telegram bot username")
    if not isinstance(code, str) or not _CODE.fullmatch(code):
        raise ValueError("Gift code cannot fit a Telegram start link")
    return f"https://t.me/{clean_username}?start={GIFT_PREFIX}{code}"
