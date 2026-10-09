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


class StreamingQuizMiddleware(BaseMiddleware):
    """Only playback actions may mutate a quiz while its producer is running."""
    async def __call__(self, handler, event, data):
        state = data.get("state")
        if state is None or not isinstance(event, (Message, CallbackQuery)):
            return await handler(event, data)
        snapshot = await state.get_data()
        if not snapshot.get("generation_pending"):
            return await handler(event, data)
        from config import redis_client
        from services.streaming_quiz import producer_key, acquire_wait
        from services.redis_reliability import UserLock
        user_id = event.from_user.id
        if not await redis_client.exists(producer_key(user_id)):
            lock = UserLock(redis_client, user_id)
            await acquire_wait(lock)
            recovered = False
            try:
                current = await state.get_data()
                if current.get("generation_pending") and not await redis_client.exists(producer_key(user_id)):
                    from services.quiz_service import refund_user_on_failure
                    await refund_user_on_failure(user_id, current)
                    await state.clear()
                    recovered = True
            finally:
                await lock.release()
            if not recovered:
                return await handler(event, data)
            message = "⚠️ انقطعت عملية التوليد. أُعيدت نقاط الطلب؛ أرسل الملف للمحاولة مجدداً."
        elif isinstance(event, CallbackQuery) and event.data in {"quiz_stop", "stream_resume"}:
            lock = UserLock(redis_client, user_id)
            await acquire_wait(lock)
            try:
                current = await state.get_data()
                if not current.get("generation_pending"):
                    return await event.answer("اكتمل التوليد. استخدم أزرار الاختبار.")
                paused = event.data == "quiz_stop"
                await state.update_data(stream_paused=paused)
                if not paused and current.get("questions"):
                    from handlers.quiz_runner import send_question_by_ids
                    await send_question_by_ids(event.message.chat.id, user_id, state)
                if paused:
                    from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
                    await event.message.answer(
                        "⏸ توقف الحل. سنُكمل تجهيز الاختبار وحفظه، ثم نعرض نتيجتك الحالية. يمكنك استئناف الحل الآن.",
                        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                            InlineKeyboardButton(text="▶️ استئناف الحل", callback_data="stream_resume")]]))
                return await event.answer()
            finally:
                await lock.release()
        elif (isinstance(event, CallbackQuery) and event.data in {"next_question", "get_hint"}
              and not snapshot.get("stream_paused")):
            return await handler(event, data)
        else:
            message = "⏳ ما زال تجهيز بقية الأسئلة جارياً. يمكنك الحل والتنقّل الآن؛ الحفظ والمشاركة والطلبات الجديدة متاحة عند اكتماله."
        if isinstance(event, CallbackQuery):
            return await event.answer(message, show_alert=True)
        return await event.answer(message)


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
