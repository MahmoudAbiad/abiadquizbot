"""Bot-scoped maintenance settings in the existing shared Redis store.

The setting has no expiry and survives application restarts. No schema changes.
"""
import asyncio
import json
from dataclasses import dataclass

from logger import get_logger

logger = get_logger(__name__)
DEFAULT_MESSAGE = "🛠 البوت في وضع الصيانة حالياً. يرجى المحاولة لاحقاً، شكراً لتفهّمكم."
MAX_MESSAGE_LENGTH = 2000
_last_known = None
_save_lock = asyncio.Lock()


@dataclass(frozen=True)
class MaintenanceSettings:
    enabled: bool = False
    message: str = DEFAULT_MESSAGE


def validate_message(text: str) -> str:
    text = text.strip()
    if not text or len(text) > MAX_MESSAGE_LENGTH:
        raise ValueError(f"أرسل رسالة نصية من 1 إلى {MAX_MESSAGE_LENGTH} حرف.")
    return text


def _key():
    from config import bot
    return f"maintenance:{bot.id}"


async def get_maintenance(*, fresh=False):
    global _last_known
    from config import redis_client
    try:
        raw = await redis_client.get(_key())
        payload = json.loads(raw) if raw is not None else {}
        settings = MaintenanceSettings(bool(payload.get("enabled", False)),
            validate_message(payload.get("message") or DEFAULT_MESSAGE))
        _last_known = settings
        return settings
    except Exception:
        # Never silently open a previously closed bot during a Redis outage.
        if not fresh and _last_known is not None:
            logger.warning("Maintenance storage unavailable; retaining last known setting")
            return _last_known
        raise


async def save_maintenance(*, enabled=None, message=None):
    global _last_known
    from config import redis_client
    async with _save_lock:
        previous = await get_maintenance(fresh=True)
        settings = MaintenanceSettings(previous.enabled if enabled is None else bool(enabled),
            previous.message if message is None else validate_message(message))
        saved = await redis_client.set(_key(), json.dumps({
            "enabled": settings.enabled, "message": settings.message,
        }, ensure_ascii=False))
        if not saved:
            raise RuntimeError("Maintenance setting was not saved")
        _last_known = settings
        logger.info("Maintenance settings saved (enabled=%s)", settings.enabled)
        return settings


async def maintenance_notice(user_id):
    from config import ADMIN_ID
    if user_id == ADMIN_ID:
        return None
    settings = await get_maintenance()
    return settings.message if settings.enabled else None
