"""
بوابة الموافقة على شروط الخصوصية وسياسة الخدمة.

المبدأ: أي مستخدم (جديد أو قديم) لم يوافق على النسخة الحالية من الوثيقتين تُعرض له رسالة
الموافقة بدل أي وظيفة أخرى في المحادثة الخاصة. بعد الموافقة لا تظهر الرسالة ثانية إلا إذا
رفعت الإدارة رقم النسخة (app_settings: terms_version / privacy_version).

حماية من إساءة الاستخدام:
- الفحص الأساسي من كاش محلي ثم Redis ثم قاعدة البيانات (لا استعلام لكل رسالة).
- الرسالة تظهر مرة كل _NOTICE_TTL ثانية لكل مستخدم كحد أقصى، وبقية الرسائل تُتجاهل بصمت.
- ضغطات "لا أوافق" المتكررة لا تكتب شيئاً جديداً (RPC idempotent) ولا ترسل رسائل جديدة.
- عند تعطل Redis يُرجَع للقاعدة، وعند تعطل القاعدة يُرفض الطلب (fail closed) برسالة قصيرة.
"""

import re
from typing import Any, Awaitable, Callable, Dict, Tuple

from aiogram import BaseMiddleware
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from cachetools import TTLCache

from logger import get_logger

logger = get_logger(__name__)

PRIVACY_URL = "https://abiad.me/privacy-policy.html"
TERMS_URL = "https://abiad.me/terms-of-service.html"

CONSENT_ACCEPT = "consent:accept"
CONSENT_DECLINE = "consent:decline"

STATE_OK = "ok"              # وافق على النسخة الحالية
STATE_NEW = "new"            # لم يقرر بعد (أو لم تُعرض له الرسالة)
STATE_UPDATE = "update"      # وافق سابقاً على نسخة أقدم
STATE_DECLINED = "declined"  # رفض

_START_PAYLOAD_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")

# Bound stale positive permissions if a Redis delete fails during a later decline.
_OK_CACHE_TTL = 60
_DENIED_CACHE_TTL = 15
_NOTICE_TTL = 10
_REDIS_OK_TTL = 60
_RESUME_TTL = 86400
_PROMPTED_TTL = 30 * 86400

# (user_id, terms_version, privacy_version) -> True / state. رقم النسخة داخل المفتاح، فرفعه
# يُبطل كل الكاش تلقائياً دون أي تنظيف.
_ok_cache: TTLCache = TTLCache(maxsize=50000, ttl=_OK_CACHE_TTL)
_denied_cache: TTLCache = TTLCache(maxsize=50000, ttl=_DENIED_CACHE_TTL)


def _redis():
    from config import redis_client  # استيراد متأخر لتفادي دورة استيراد
    return redis_client


def _as_version(value: Any) -> int:
    try:
        return max(1, int(float(value)))
    except (TypeError, ValueError):
        return 1


async def current_versions() -> Tuple[int, int]:
    """نسختا الوثيقتين الحاليتان من app_settings (كاش 30 ثانية). الافتراضي 1."""
    from helpers.settings_helper import get_app_settings
    settings = await get_app_settings()
    return _as_version(settings.get("terms_version")), _as_version(settings.get("privacy_version"))


def _ok_redis_key(user_id: int, tv: int, pv: int) -> str:
    return f"consent:ok:{user_id}:{tv}:{pv}"


async def remember_accepted(user_id: int, tv: int, pv: int) -> None:
    key = (user_id, tv, pv)
    _ok_cache[key] = True
    _denied_cache.pop(key, None)
    try:
        await _redis().set(_ok_redis_key(user_id, tv, pv), "1", ex=_REDIS_OK_TTL)
    except Exception as exc:
        logger.warning("Could not cache consent in Redis: %s", exc)


async def forget_denied(user_id: int, tv: int, pv: int) -> None:
    """Immediately revoke cached acceptance when the user declines consent."""
    key = (user_id, tv, pv)
    _ok_cache.pop(key, None)
    _denied_cache[key] = STATE_DECLINED
    try:
        await _redis().delete(_ok_redis_key(user_id, tv, pv))
    except Exception as exc:
        logger.warning("Could not revoke cached consent in Redis: %s", exc)


async def get_consent_state(user_id: int) -> str:
    """يرجع إحدى STATE_*. يرمي استثناء لو تعذّرت القاعدة (المستدعي يرفض الطلب)."""
    tv, pv = await current_versions()
    key = (user_id, tv, pv)
    # A just-recorded decline must override an older positive Redis result,
    # even if deleting that Redis key temporarily failed.
    if key in _denied_cache:
        return _denied_cache[key]
    if key in _ok_cache:
        return STATE_OK
    try:
        if await _redis().exists(_ok_redis_key(user_id, tv, pv)):
            _ok_cache[key] = True
            return STATE_OK
    except Exception as exc:
        logger.warning("Consent Redis check failed, falling back to DB: %s", exc)

    from helpers.supabase_helper import supabase
    result = await (
        supabase.table("user_consents")
        .select("status, terms_version, privacy_version")
        .eq("user_id", user_id)
        .limit(1)
        .execute()
    )
    rows = result.data or []
    if not rows:
        state = STATE_NEW
    else:
        row = rows[0]
        if row.get("status") == "accepted":
            if int(row.get("terms_version") or 0) >= tv and int(row.get("privacy_version") or 0) >= pv:
                state = STATE_OK
            else:
                state = STATE_UPDATE
        elif row.get("status") == "declined":
            state = STATE_DECLINED
        else:
            state = STATE_NEW

    if state == STATE_OK:
        await remember_accepted(user_id, tv, pv)
    else:
        _denied_cache[key] = state
    return state


# ==================== واجهة الرسائل ====================

def build_consent_text(state: str) -> str:
    if state == STATE_UPDATE:
        return (
            "📜 <b>تحديث على شروطنا</b>\n\n"
            "حدّثنا شروط الخصوصية وسياسة الخدمة.\n"
            "اطّلع عليهما من الزرّين أدناه، ثم اضغط «أوافق» لمتابعة استخدام البوت."
        )
    return (
        "📜 <b>قبل أن نبدأ</b>\n\n"
        "لاستخدام <b>أبيض منشئ الكويزات</b> يجب الموافقة على شروط الخصوصية وسياسة الخدمة.\n"
        "اطّلع عليهما من الزرّين أدناه، ثم اضغط «أوافق» للمتابعة."
    )


def build_declined_text() -> str:
    return (
        "تم تسجيل اختيارك.\n\n"
        "لا يمكننا تشغيل البوت دون الموافقة على شروط الخصوصية وسياسة الخدمة. "
        "إذا غيّرت رأيك اضغط «أوافق» في أي وقت."
    )


def build_accepted_text() -> str:
    return (
        "✅ <b>شكراً لك، تمت الموافقة.</b>\n\n"
        "أرسل ملفك أو نصك أو صورك لإنشاء كويز، أو اكتب /start لعرض القائمة الرئيسية."
    )


def build_consent_keyboard(include_decline: bool = True) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text="🔒 شروط الخصوصية", url=PRIVACY_URL)],
        [InlineKeyboardButton(text="📄 سياسة الخدمة", url=TERMS_URL)],
    ]
    decision_row = [InlineKeyboardButton(text="✅ أوافق", callback_data=CONSENT_ACCEPT)]
    if include_decline:
        decision_row.append(InlineKeyboardButton(text="❌ لا أوافق", callback_data=CONSENT_DECLINE))
    rows.append(decision_row)
    return InlineKeyboardMarkup(inline_keyboard=rows)


# ==================== استئناف الرابط العميق ====================

def _resume_key(user_id: int) -> str:
    return f"consent:resume:{user_id}"


async def remember_start_payload(message: Message) -> None:
    """لو كانت الرسالة /start <payload> (رابط تحدّي/مشاركة/إحالة) نحفظ الـ payload مؤقتاً
    لنُعيد المستخدم إليه بعد الموافقة بدل أن يضيع الرابط."""
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) != 2 or parts[0].split("@")[0] != "/start":
        return
    payload = parts[1].strip()
    if not _START_PAYLOAD_RE.match(payload):
        return
    try:
        await _redis().set(_resume_key(message.from_user.id), payload, ex=_RESUME_TTL)
    except Exception as exc:
        logger.warning("Could not store consent resume payload: %s", exc)


async def pop_start_payload(user_id: int):
    try:
        redis = _redis()
        value = await redis.get(_resume_key(user_id))
        if value is None:
            return None
        await redis.delete(_resume_key(user_id))
        value = value.decode() if isinstance(value, bytes) else str(value)
        return value if _START_PAYLOAD_RE.match(value) else None
    except Exception as exc:
        logger.warning("Could not read consent resume payload: %s", exc)
        return None


async def _mark_prompted(user_id: int) -> None:
    try:
        from helpers.supabase_helper import supabase
        await supabase.rpc("mark_consent_prompted", {"p_user_id": user_id}).execute()
    except Exception as exc:
        logger.warning("mark_consent_prompted failed: %s", exc)


# ==================== الـ Middleware ====================

class ConsentMiddleware(BaseMiddleware):
    """يُسجَّل على dp.message و dp.callback_query (بعد ThrottlingMiddleware، بنسخة واحدة مشتركة).
    يتخطى: الأدمن، البوتات، المحادثات غير الخاصة، أزرار الموافقة نفسها، وأي نوع تحديث آخر
    (poll_answer / my_chat_member / ... لا يمر من هنا أصلاً)."""

    def __init__(self) -> None:
        # حجز قبل أول await حتى تتشارك التحديثات المتزامنة نفس الحد.
        self._notices: TTLCache = TTLCache(maxsize=50000, ttl=_NOTICE_TTL)

    async def __call__(
        self,
        handler: Callable[[Any, Dict[str, Any]], Awaitable[Any]],
        event: Any,
        data: Dict[str, Any],
    ) -> Any:
        user = getattr(event, "from_user", None)
        if user is None or user.is_bot:
            return await handler(event, data)

        from config import ADMIN_ID
        if user.id == ADMIN_ID:
            return await handler(event, data)

        if isinstance(event, Message):
            if event.chat.type != "private":
                return await handler(event, data)
        elif isinstance(event, CallbackQuery):
            chat = getattr(event.message, "chat", None)
            if chat is None or chat.type != "private":
                return await handler(event, data)
            if (event.data or "").startswith("consent:"):
                return await handler(event, data)
        else:
            return await handler(event, data)

        if isinstance(event, Message):
            await remember_start_payload(event)

        try:
            state = await get_consent_state(user.id)
        except Exception as exc:
            logger.error("Consent check failed; request blocked (fail closed): %s", exc, exc_info=True)
            await self._notify(event, user.id, "⚠️ تعذّر التحقق من حسابك حالياً، حاول مجدداً بعد قليل.", None)
            return None

        if state == STATE_OK:
            return await handler(event, data)

        if user.id not in self._notices and await self._first_prompt(user.id):
            from task_utils import spawn
            spawn(_mark_prompted(user.id))
        await self._notify(event, user.id, build_consent_text(state), build_consent_keyboard())
        return None

    @staticmethod
    async def _first_prompt(user_id: int) -> bool:
        try:
            return bool(await _redis().set(f"consent:prompted:{user_id}", "1", nx=True, ex=_PROMPTED_TTL))
        except Exception:
            return False

    async def _notify(self, event: Any, user_id: int, text: str, markup) -> None:
        """رسالة واحدة كل _NOTICE_TTL ثانية لكل مستخدم؛ الباقي يُتجاهل بصمت."""
        if user_id in self._notices:
            if isinstance(event, CallbackQuery):
                try:
                    await event.answer()
                except (TelegramBadRequest, TelegramForbiddenError):
                    pass
            return
        self._notices[user_id] = True
        try:
            if isinstance(event, CallbackQuery):
                await event.answer("📜 يلزم الموافقة على الشروط أولاً.")
                await event.message.answer(text, parse_mode="HTML", reply_markup=markup)
            else:
                await event.answer(text, parse_mode="HTML", reply_markup=markup)
        except (TelegramBadRequest, TelegramForbiddenError):
            logger.warning("Could not deliver consent prompt to Telegram user")
