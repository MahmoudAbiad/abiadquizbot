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

import asyncio
import json
import re
import time
from typing import Any, Awaitable, Callable, Dict, List, Tuple

from aiogram import BaseMiddleware
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from cachetools import TTLCache

from logger import get_logger

logger = get_logger(__name__)

PRIVACY_URL = "https://abiad.me/privacy-policy.html?v=10"
TERMS_URL = "https://abiad.me/terms-of-service.html?v=10"

CONSENT_ACCEPT = "consent:accept"
CONSENT_DECLINE = "consent:decline"
CONSENT_LANG_PICK_PREFIX = "consent:lang:pick:"
CONSENT_LANG_CONFIRM_PREFIX = "consent:lang:confirm:"
LANGUAGE_STEP_ENABLED = True

STATE_OK = "ok"              # وافق على النسخة الحالية
STATE_NEW = "new"            # مستخدم جديد لم يوافق بعد
STATE_RETURNING = "returning"  # مستخدم عائد لم يوافق بعد
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
_PENDING_TTL = 900
_PENDING_MAX_ALBUM = 10
RESUME_FLAG = "consent_resumed"

# (user_id, terms_version, privacy_version) -> True / state. رقم النسخة داخل المفتاح، فرفعه
# يُبطل كل الكاش تلقائياً دون أي تنظيف.
_ok_cache: TTLCache = TTLCache(maxsize=50000, ttl=_OK_CACHE_TTL)
_denied_cache: TTLCache = TTLCache(maxsize=50000, ttl=_DENIED_CACHE_TTL)
_active_middlewares: list = []


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


async def purge_user_consent_cache(user_id: int) -> int:
    """يمسح كافة سجلات كاش الموافقة المحلية وفي Redis لمستخدم محدد فوراً (عند حذف موافقته أو حسابه)."""
    uid = int(user_id)
    for cache in (_ok_cache, _denied_cache):
        for k in [k for k in list(cache.keys()) if isinstance(k, tuple) and len(k) >= 1 and k[0] == uid]:
            cache.pop(k, None)
    for mw in list(_active_middlewares):
        try:
            mw._notices.pop(uid, None)
        except Exception:
            pass

    deleted = 0
    try:
        redis = _redis()
        tv, pv = await current_versions()
        keys_to_delete = [
            _ok_redis_key(uid, tv, pv),
            f"consent:prompted:{uid}",
            _resume_key(uid),
            _pending_key(uid),
            _pending_cb_key(uid),
            f"consent:lock:{uid}",
        ]
        try:
            async for k in redis.scan_iter(match=f"consent:ok:{uid}:*", count=50):
                k_str = k.decode() if isinstance(k, bytes) else str(k)
                if k_str not in keys_to_delete:
                    keys_to_delete.append(k_str)
        except Exception:
            pass
        if keys_to_delete:
            deleted = int(await redis.delete(*keys_to_delete) or 0)
    except Exception as exc:
        logger.warning("Could not purge consent Redis keys for user %s: %s", uid, exc)
    try:
        from services.i18n import purge_user_language_cache
        await purge_user_language_cache(uid)
    except Exception:
        pass
    return deleted


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
    row = rows[0] if rows else None
    saved_terms = int((row or {}).get("terms_version") or 0)
    saved_privacy = int((row or {}).get("privacy_version") or 0)
    if (0 < saved_terms < tv) or (0 < saved_privacy < pv):
        state = STATE_UPDATE
    elif row and row.get("status") == "accepted":
        if saved_terms >= tv and saved_privacy >= pv:
            state = STATE_OK
        else:
            state = STATE_UPDATE
    elif row and row.get("status") == "declined":
        state = STATE_DECLINED
    else:
        # Consent is checked before user creation. Distinguish returning
        # accounts from people starting the bot for the first time.
        existing = await supabase.table("users").select("user_id").eq(
            "user_id", user_id
        ).limit(1).execute()
        state = STATE_RETURNING if existing.data else STATE_NEW

    if state == STATE_OK:
        await remember_accepted(user_id, tv, pv)
    else:
        _denied_cache[key] = state
    return state


# ==================== واجهة الرسائل ====================

def _policy_links() -> str:
    return f'<a href="{PRIVACY_URL}">سياسة الخصوصية</a> و<a href="{TERMS_URL}">شروط الاستخدام</a>'


async def get_updated_documents(user_id: int):
    """Return documents whose saved consent versions are older than the current versions."""
    tv, pv = await current_versions()
    from helpers.supabase_helper import supabase
    result = await (
        supabase.table("user_consents")
        .select("terms_version,privacy_version")
        .eq("user_id", user_id)
        .limit(1)
        .execute()
    )
    if not result.data:
        return []
    row = result.data[0]
    saved = {
        "terms": int(row.get("terms_version") or 0),
        "privacy": int(row.get("privacy_version") or 0),
    }
    current = {"terms": tv, "privacy": pv}
    return [name for name in ("terms", "privacy") if 0 < saved[name] < current[name]]


async def build_consent_text(state: str, user_id: int = None) -> str:
    policy_links = _policy_links()
    if state == STATE_UPDATE and user_id is not None:
        updated = await get_updated_documents(user_id)
        labels = {"terms": "شروط الاستخدام", "privacy": "سياسة الخصوصية"}
        if updated:
            heading = "تحديث على " + " و".join(labels[name] for name in updated)
            urls = {"terms": TERMS_URL, "privacy": PRIVACY_URL}
            updated_links = " و".join(
                f'<a href="{urls[name]}">{labels[name]}</a>' for name in updated
            )
            return (
                f"📜 <b>{heading}</b>\n\n"
                f"حدّثنا {updated_links}.\n"
                "اطّلع على النسخة المحدّثة، ثم اضغط «أوافق» لمتابعة استخدام البوت."
            )
    if state == STATE_NEW:
        return (
            "📜 <b>قبل أن تبدأ</b>\n\n"
            f"لاستخدام <b>أبيض منشئ اختبارات ذكي</b> يجب الموافقة على {policy_links}.\n"
            "اطّلع عليهما بالضغط على اسميهما، ثم اضغط «أوافق» للمتابعة."
        )
    return (
        "📜 <b>لمتابعة استخدام البوت</b>\n\n"
        f"يرجى الموافقة على {policy_links} لمتابعة استخدام أبيض.\n"
        "اطّلع عليهما بالضغط على اسميهما، ثم اضغط «أوافق» للمتابعة."
    )


def build_declined_text() -> str:
    return (
        "تم تسجيل اختيارك.\n\n"
        f"لا يمكن متابعة استخدام البوت دون الموافقة على {_policy_links()}. "
        "إذا غيّرت رأيك اضغط «أوافق» في أي وقت."
    )


def build_accepted_text(resuming: bool = False, callback_pending: bool = False) -> str:
    if resuming:
        return "✅ <b>شكراً لك، تمت الموافقة.</b>\n\n⏳ جارٍ متابعة طلبك السابق…"
    if callback_pending:
        return "✅ <b>شكراً لك، تمت الموافقة.</b>\n\n↩️ اضغط الزر الذي ضغطته قبل قليل مرة أخرى لمتابعة طلبك."
    return (
        "✅ <b>شكراً لك، تمت الموافقة.</b>\n\n"
        "أرسل ملفك أو نصك أو صورك لإنشاء اختبار، أو اكتب /start لعرض القائمة الرئيسية."
    )

def build_resume_failed_text() -> str:
    return "⚠️ تعذّرت متابعة طلبك السابق تلقائياً.\nتمت الموافقة بنجاح، فقط أعد إرسال ملفك أو رسالتك."


def build_consent_keyboard(include_decline: bool = True) -> InlineKeyboardMarkup:
    decision_row = [InlineKeyboardButton(text="✅ أوافق", callback_data=CONSENT_ACCEPT)]
    if include_decline:
        decision_row.append(InlineKeyboardButton(text="❌ لا أوافق", callback_data=CONSENT_DECLINE))
    return InlineKeyboardMarkup(inline_keyboard=[decision_row])


# ==================== شاشة اختيار اللغة بعد الموافقة ====================

async def detect_initial_language(user: Any) -> str:
    """التعرف التلقائي على لغة المستخدم من تيليجرام أو استرجاع لغته المحفوظة."""
    from services.i18n import detect_telegram_language, get_user_language
    uid = getattr(user, "id", None)
    if uid is not None:
        return await get_user_language(uid, fallback_user_or_code=user)
    return detect_telegram_language(user)


async def save_user_language(user_id: int, lang_code: str) -> str:
    from services.i18n import set_user_language
    return await set_user_language(user_id, lang_code)


def build_language_prompt_text(lang_code: str = "ar") -> str:
    from services.i18n import build_language_screen_text
    return build_language_screen_text(lang_code)


def build_language_keyboard(selected_lang: str = "ar") -> InlineKeyboardMarkup:
    from services.i18n import build_language_screen_keyboard
    return build_language_screen_keyboard(selected_lang, prefix="consent:lang", is_settings=False)


def build_localized_accepted_message(
    lang_code: str = "ar",
    resuming: bool = False,
    callback_pending: bool = False,
) -> str:
    from services.i18n import build_localized_accepted_text
    return build_localized_accepted_text(lang_code, resuming=resuming, callback_pending=callback_pending)


def get_continue_button_label(lang_code: str = "ar") -> str:
    from services.i18n import DEEP_LINK_CONTINUE_LABELS, normalize_language_code
    lang = normalize_language_code(lang_code)
    return DEEP_LINK_CONTINUE_LABELS.get(lang, "▶️ متابعة")


def get_consent_confirmed_toast(lang_code: str = "ar") -> str:
    from services.i18n import CONSENT_CONFIRMED_TOASTS, normalize_language_code
    lang = normalize_language_code(lang_code)
    return CONSENT_CONFIRMED_TOASTS.get(lang, "✅ تمت الموافقة")


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

def _pending_key(user_id: int) -> str:
    return f"consent:pending:{user_id}"

def _is_start_command(message: Message) -> bool:
    text = (message.text or "").strip()
    return text.startswith("/") and text.split(maxsplit=1)[0].split("@")[0].lower() == "/start"

async def remember_pending_message(message: Message) -> None:
    if message.from_user is None or _is_start_command(message):
        return
    try:
        raw = message.model_dump_json(exclude_none=True)
        redis = _redis(); key = _pending_key(message.from_user.id)
        group_id = message.media_group_id; append = False
        if group_id:
            head = await redis.lindex(key, 0)
            if head is not None:
                head = head.decode() if isinstance(head, bytes) else head
                append = json.loads(head).get("media_group_id") == group_id
            if append and await redis.llen(key) >= _PENDING_MAX_ALBUM:
                return
        if not append:
            await redis.delete(key)
        await redis.rpush(key, raw); await redis.expire(key, _PENDING_TTL)
        await redis.delete(_pending_cb_key(message.from_user.id))
    except Exception as exc:
        logger.warning("Could not store pending message for consent resume: %s", exc)

def _pending_cb_key(user_id: int) -> str:
    return f"consent:pending_cb:{user_id}"

async def remember_pending_callback(call: CallbackQuery) -> None:
    if call.from_user is None or not call.data:
        return
    try:
        message = call.message
        payload = json.dumps({"data": call.data, "message_id": getattr(message, "message_id", None), "chat_id": getattr(getattr(message, "chat", None), "id", None)})
        redis = _redis(); await redis.set(_pending_cb_key(call.from_user.id), payload, ex=_PENDING_TTL)
        await redis.delete(_pending_key(call.from_user.id))
    except Exception as exc:
        logger.warning("Could not store pending consent callback: %s", exc)

async def pop_pending_callback(user_id: int):
    try:
        redis = _redis(); key = _pending_cb_key(user_id)
        async with redis.pipeline(transaction=True) as pipe:
            pipe.get(key); pipe.delete(key); value, _ = await pipe.execute()
        if value is None: return None
        value = value.decode() if isinstance(value, bytes) else value
        parsed = json.loads(value); return parsed if isinstance(parsed, dict) else None
    except Exception as exc:
        logger.warning("Could not read pending consent callback: %s", exc); return None

async def clear_pending_callback(user_id: int) -> None:
    try: await _redis().delete(_pending_cb_key(user_id))
    except Exception as exc: logger.warning("Could not clear pending consent callback: %s", exc)

async def clear_pending_messages(user_id: int) -> None:
    try: await _redis().delete(_pending_key(user_id))
    except Exception as exc: logger.warning("Could not clear pending consent messages: %s", exc)

async def pop_pending_messages(user_id: int, bot=None) -> List[Message]:
    try:
        redis = _redis(); key = _pending_key(user_id)
        async with redis.pipeline(transaction=True) as pipe:
            pipe.lrange(key, 0, -1); pipe.delete(key); items, _ = await pipe.execute()
    except Exception as exc:
        logger.warning("Could not read pending consent messages: %s", exc); return []
    messages = []
    for item in items or []:
        try:
            text = item.decode() if isinstance(item, bytes) else item; msg = Message.model_validate_json(text)
            if bot is not None: msg = msg.as_(bot)
            if msg.from_user is not None and msg.from_user.id == user_id: messages.append(msg)
        except Exception as exc: logger.warning("Dropping malformed pending consent message: %s", exc)
    return messages

async def replay_pending_messages(bot, messages: List[Message]) -> int:
    if not messages: return 0
    from aiogram.types import Update
    from config import dp
    replayed = 0; failed_user_id = None
    for msg in messages:
        try:
            update = Update(update_id=int(time.time() * 1000) % 2_000_000_000, message=msg)
            await dp.feed_update(bot, update, **{RESUME_FLAG: True}); replayed += 1
        except Exception as exc:
            logger.error("Failed to resume message after consent: %s", exc, exc_info=True)
            if failed_user_id is None and getattr(msg, "from_user", None) is not None: failed_user_id = msg.from_user.id
        if len(messages) > 1: await asyncio.sleep(0.15)
    if failed_user_id is not None:
        try: await bot.send_message(failed_user_id, build_resume_failed_text())
        except Exception as exc: logger.warning("Could not tell user that consent resume failed: %s", exc)
    return replayed


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
        _active_middlewares.append(self)

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

        if not data.get(RESUME_FLAG):
            if isinstance(event, Message): await remember_pending_message(event)
            elif isinstance(event, CallbackQuery): await remember_pending_callback(event)

        if user.id not in self._notices and await self._first_prompt(user.id):
            from task_utils import spawn
            spawn(_mark_prompted(user.id))
        try:
            prompt_text = await build_consent_text(state, user.id)
        except Exception as exc:
            logger.error("Could not build consent prompt for user %s: %s", user.id, exc, exc_info=True)
            await self._notify(event, user.id, "⚠️ تعذّر تجهيز رسالة الموافقة حالياً، حاول مجدداً بعد قليل.", None)
            return None
        await self._notify(
            event,
            user.id,
            prompt_text,
            build_consent_keyboard(),
            activity_state=state,
        )
        return None

    @staticmethod
    async def _first_prompt(user_id: int) -> bool:
        try:
            return bool(await _redis().set(f"consent:prompted:{user_id}", "1", nx=True, ex=_PROMPTED_TTL))
        except Exception:
            return False

    async def _notify(self, event: Any, user_id: int, text: str, markup, activity_state=None) -> None:
        """رسالة واحدة كل _NOTICE_TTL ثانية لكل مستخدم؛ الباقي يُتجاهل بصمت."""
        if user_id in self._notices:
            if isinstance(event, CallbackQuery):
                try:
                    await event.answer()
                except (TelegramBadRequest, TelegramForbiddenError):
                    pass
            return
        self._notices[user_id] = True
        sent = False
        try:
            if isinstance(event, CallbackQuery):
                await event.answer("📜 يلزم الموافقة على الشروط أولاً.")
                await event.message.answer(text, parse_mode="HTML", reply_markup=markup)
            else:
                await event.answer(text, parse_mode="HTML", reply_markup=markup)
            sent = True
        except (TelegramBadRequest, TelegramForbiddenError):
            logger.warning("Could not deliver consent prompt to Telegram user")

        if sent and activity_state is not None:
            from helpers.supabase_helper.analytics import log_usage_event
            await log_usage_event(user_id, "consent_prompted", {"consent_state": activity_state})
