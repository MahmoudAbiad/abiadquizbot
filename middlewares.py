"""
Middlewares for Telegram Bot.
Handles anti-spam (Rate Limiting) to protect the bot and API keys.
"""

import asyncio
from task_utils import spawn
from typing import Any, Awaitable, Callable, Dict, Optional, Set
from aiogram import BaseMiddleware
from aiogram.types import Update, Message, CallbackQuery
from cachetools import TTLCache
from logger import get_logger
from config import ADMIN_ID
from error_context import set_error_context, reset_error_context

logger = get_logger(__name__)



class GroupQuizOnlyMiddleware(BaseMiddleware):
    """Ignore unrelated group traffic before maintenance, FSM and quiz handlers.

    Group quiz launch commands and group-quiz buttons still work; poll votes,
    poll closures and membership events pass through unchanged.
    """

    _GROUP_TYPES = {"group", "supergroup"}
    _GROUP_QUIZ_CALLBACK_PREFIXES = (
        "gql:", "gqa:", "gqn:", "gqk:", "gqt:", "gqp:", "gqc:", "gqe:",
        "gqg:", "gqgr:", "gqchl:", "gqchr:", "gqchp:",
    )

    @staticmethod
    def _is_group_quiz_launch(message: Message) -> bool:
        text = (getattr(message, "text", None) or "").strip()
        command, separator, payload = text.partition(" ")
        if not separator or not payload.strip():
            return False
        command = command.split("@", 1)[0].lower()
        payload = payload.strip()
        if command == "/groupquiz":
            return True
        return command == "/start" and payload.startswith("gq_") and bool(payload[3:].strip())

    async def __call__(self, handler, event: Update, data: Dict[str, Any]) -> Any:
        # Telegram group messages must not enter generic student handlers.
        # Keep only the two explicit group-quiz launch entry points.
        message = getattr(event, "message", None) or getattr(event, "edited_message", None)
        if message is not None and getattr(getattr(message, "chat", None), "type", None) in self._GROUP_TYPES:
            if getattr(event, "edited_message", None) is not None:
                return None
            if not self._is_group_quiz_launch(message):
                return None

        # Stale unrelated inline buttons can otherwise trigger personal menus
        # and error messages inside a group. Answer silently to clear spinner.
        callback = getattr(event, "callback_query", None)
        if callback is not None:
            callback_chat = getattr(getattr(callback, "message", None), "chat", None)
            if getattr(callback_chat, "type", None) in self._GROUP_TYPES:
                callback_data = getattr(callback, "data", None) or ""
                if not callback_data.startswith(self._GROUP_QUIZ_CALLBACK_PREFIXES):
                    try:
                        await callback.answer()
                    except Exception:
                        logger.warning("Could not acknowledge ignored group callback")
                    return None

        return await handler(event, data)


class ErrorTrackingMiddleware(BaseMiddleware):
    """
    ميدل وير على مستوى الـ Update (يغطي كل الأنواع: رسائل، ضغطات أزرار، إجابات استفتاء
    quiz_runner...) له مهمتان:

    1) يضبط "سياق الطلب الحالي" (راجع error_context.py) بمعرّف المستخدم صاحب الطلب طوال
       مدة معالجته. هذا يجعل أي استدعاء لـ log_error()/log_critical() بأي مكان بالمشروع —
       بما فيها كل try/except الموجودة سلفاً بكل الـ handlers — يُسجَّل تلقائياً كحدث
       تحليلات (usage_events, event_type='error_occurred') مرتبط بالطالب صاحب المشكلة،
       دون أي تعديل يدوي إضافي على تلك النقاط.

    2) شبكة أمان أخيرة (Safety Net): يلتقط أي استثناء "غير متوقع بالكامل" يهرب من كل
       الـ handlers (كود لم يكن يحتوي على try/except أصلاً)، بدل أن يختفي بصمت (الطالب
       كان يبقى بدون أي رد إطلاقاً). يسجّله بنفس آلية الأخطاء، ويحاول إشعار الطالب برسالة
       عامة بدل ترك طلبه معلّقاً بدون رد.

    مصمم ليكون بلا أي تأثير على أداء الطالب: التسجيل بقاعدة البيانات يحصل بالخلفية
    (asyncio task) دون انتظار، تماماً كباقي دوال التتبع بالمشروع.
    """

    async def __call__(
        self,
        handler: Callable[[Any, Dict[str, Any]], Awaitable[Any]],
        event: Update,
        data: Dict[str, Any]
    ) -> Any:
        tg_event = None
        user_id = None
        context_snippet = None
        update_type = None
        try:
            tg_event = (
                event.message or event.callback_query or event.poll_answer
                or event.edited_message or event.my_chat_member
            )
            user_obj = getattr(tg_event, "from_user", None) or getattr(tg_event, "user", None)
            user_id = user_obj.id if user_obj else None
            if isinstance(tg_event, Message):
                context_snippet = tg_event.text or tg_event.caption or f"[{tg_event.content_type}]"
            elif isinstance(tg_event, CallbackQuery):
                context_snippet = tg_event.data
            update_type = event.event_type  # قد ترمي UpdateTypeLookupError لأنواع Update نادرة غير معروفة، لذا داخل try
        except Exception:
            pass

        token = set_error_context(user_id=user_id, update_type=update_type, context=context_snippet)
        try:
            return await handler(event, data)
        except Exception as e:
            logger.error(f"Unhandled exception while processing update {event.update_id}: {e}", exc_info=True)
            if user_id and user_id != ADMIN_ID:
                try:
                    from helpers.supabase_helper import log_error_event
                    spawn(log_error_event(
                        user_id=user_id,
                        error_message=f"Unhandled exception: {e}",
                        exception=e,
                        update_type=update_type,
                        context=context_snippet,
                        unhandled=True,
                    ))
                except Exception:
                    pass
            # 🩹 محاولة عدم ترك الطالب بدون أي رد إطلاقاً (بدل التجاهل الصامت)
            try:
                if isinstance(tg_event, CallbackQuery):
                    await tg_event.answer("⚠️ حدث خطأ غير متوقع، الرجاء المحاولة مجدداً.", show_alert=True)
                elif isinstance(tg_event, Message):
                    await tg_event.answer("⚠️ عذراً، حدث خطأ غير متوقع أثناء تنفيذ طلبك. الرجاء المحاولة مرة أخرى.")
            except Exception:
                pass
            # The durable queue must see failures instead of acknowledging a lost update.
            raise
        finally:
            reset_error_context(token)

class UsageCallbackMiddleware(BaseMiddleware):
    """Track selected user intents; never claim a callback succeeded or log raw data."""

    async def __call__(self, handler, event, data):
        coroutine = None
        try:
            from services.usage_events import callback_event
            event_type = callback_event(event.data)
            if event_type and event.from_user.id != ADMIN_ID and event.message.chat.type == "private":
                from helpers.supabase_helper import log_usage_event
                coroutine = log_usage_event(event.from_user.id, event_type, {"kind": "button_click"})
                spawn(coroutine)
        except Exception:
            if coroutine is not None:
                coroutine.close()
            logger.warning("Could not schedule callback analytics")
        return await handler(event, data)


class MaintenanceMiddleware(BaseMiddleware):
    """Gate updates before handlers, spending points or generating work."""

    def __init__(self):
        self._notices = TTLCache(maxsize=10000, ttl=10)

    async def __call__(self, handler, event, data):
        from services.maintenance import maintenance_notice
        from aiogram.exceptions import TelegramForbiddenError, TelegramBadRequest
        tg_event = event.event
        user = getattr(tg_event, "from_user", None) or getattr(tg_event, "user", None)
        # Keep Telegram membership/service notifications operational.
        if event.event_type in {"my_chat_member", "chat_member", "poll"}:
            return await handler(event, data)
        notice = await maintenance_notice(user.id if user else None)
        if notice is None:
            return await handler(event, data)
        try:
            key = ("user", user.id) if user else ("chat", getattr(getattr(tg_event, "chat", None), "id", None))
            if key in self._notices:
                # Acknowledge buttons without another chat message or alert.
                # Protocol queries still need their negative/empty answer.
                if isinstance(tg_event, CallbackQuery):
                    await tg_event.answer()
                elif event.event_type == "inline_query":
                    await tg_event.answer([], cache_time=0, is_personal=True)
                elif event.event_type in {"pre_checkout_query", "shipping_query"}:
                    await tg_event.answer(ok=False, error_message=notice[:200])
                return None
            # Reserve before the first await so simultaneous updates share the limit.
            self._notices[key] = True
            if isinstance(tg_event, Message):
                await tg_event.answer(notice, parse_mode=None)
            elif isinstance(tg_event, CallbackQuery):
                await tg_event.answer()
                if isinstance(tg_event.message, Message):
                    await tg_event.message.answer(notice, parse_mode=None)
                else:
                    await data["bot"].send_message(tg_event.from_user.id, notice, parse_mode=None)
            elif event.event_type == "inline_query":
                await tg_event.answer([], cache_time=0, is_personal=True)
            elif event.event_type in {"pre_checkout_query", "shipping_query"}:
                await tg_event.answer(ok=False, error_message=notice[:200])
            elif user:
                await data["bot"].send_message(user.id, notice, parse_mode=None)
        except (TelegramForbiddenError, TelegramBadRequest):
            logger.warning("Could not deliver maintenance notice to Telegram user")
        return None


class ThrottlingMiddleware(BaseMiddleware):
    """
    ميدل وير يمنع المستخدمين من إرسال طلبات متتالية سريعة جداً.
    """
    def __init__(self, limit: float = 4, exempt_user_ids: Optional[Set[int]] = None):
        # السماح بطلب واحد فقط كل (limit) ثوانٍ لكل مستخدم
        self.cache = TTLCache(maxsize=10000, ttl=limit)
        self.exempt_user_ids = exempt_user_ids or set()

    async def __call__(
        self,
        handler: Callable[[Any, Dict[str, Any]], Awaitable[Any]],
        event: Any,
        data: Dict[str, Any]
    ) -> Any:
        # التحقق مما إذا كان الحدث رسالة أو ضغطة زر
        if isinstance(event, (Message, CallbackQuery)):
            # 🚀 استثناء ألبومات الصور: إذا كانت الرسالة جزءاً من ألبوم، نمررها مباشرة لتجميعها عبر Redis
            if isinstance(event, Message) and event.media_group_id:
                return await handler(event, data)

            user_id = event.from_user.id

            if user_id == ADMIN_ID or user_id in self.exempt_user_ids:
                return await handler(event, data)
            
            # إذا كان المستخدم في الكاش، فهذا يعني أنه أرسل طلباً قبل انتهاء المهلة
            if user_id in self.cache:
                # نكتفي بإظهار تنبيه لضغطات الأزرار (CallbackQuery) لأنه يظهر كإشعار مؤقت ولا يغرق المحادثة
                if isinstance(event, CallbackQuery):
                    await event.answer("⚠️ الرجاء الانتظار...", show_alert=True)
                
                # بالنسبة للرسائل العادية (Message)، ننهي العملية بصمت (Silent Return) لحماية البوت من الحظر (429)
                logger.warning(f"Spam detected and blocked silently for user: {user_id}")
                return # ننهي العملية هنا ولا نمررها للبوت
            
            # إضافة المستخدم للكاش
            self.cache[user_id] = True
            
        try:
            return await handler(event, data)
        except BaseException:
            # Failed processing is retried by the durable queue; do not suppress it as spam.
            if isinstance(event, (Message, CallbackQuery)):
                self.cache.pop(event.from_user.id, None)
            raise

