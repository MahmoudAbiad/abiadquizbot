"""Explicit, expiring transitions out of a private quiz; never silently replace it."""
import asyncio
import json
import logging
import time
import uuid

from aiogram import Router, F, types
from config import bot, redis_client, QuizState
from services.redis_reliability import user_locked, UserLock

router = Router(name="session_navigation")
router.callback_query.filter(F.message.chat.type == "private")
PENDING_SECONDS = 1800
EXPIRY_KEY = "session_navigation:expiry"
logger = logging.getLogger(__name__)


def pending_key(user_id):
    return f"session_navigation:pending:{user_id}"


async def active_quiz(state):
    data = await state.get_data()
    current = await state.get_state()
    # Includes save/edit subflows, but not completed quizzes retained for replay.
    quiz_states = {getattr(QuizState, name).state for name in (
        "answering_quiz", "waiting_for_custom_name", "waiting_for_new_section_title",
        "waiting_for_question_edit_choice", "waiting_for_question_edit_text",
        "waiting_for_answer_edit_choice", "waiting_for_answer_edit_text", "searching_favorites")}
    quiz_states.add(None)  # Favorites search can return to the menu without ending the run.
    return bool(current in quiz_states and data.get("questions") and not data.get("quiz_completed"))


async def session_id(state):
    data = await state.get_data()
    identity = data.get("attempt_id") or data.get("navigation_session_id")
    if not identity:
        identity = uuid.uuid4().hex
        await state.update_data(navigation_session_id=identity)
    return str(identity)


async def read_pending(user_id):
    raw = await redis_client.get(pending_key(user_id))
    return json.loads(raw) if raw else None


async def cleanup_action(action):
    kind = action["kind"]
    if kind.startswith("web_"):
        from helpers.r2_helper import delete_audio_temp, delete_file_temp, delete_file_temp_batch
        if kind == "web_audio":
            await delete_audio_temp(action["object_path"])
        elif kind == "web_file":
            await delete_file_temp(action["object_path"])
        elif kind == "web_images":
            await delete_file_temp_batch(action["object_paths"])


async def discard_pending(user_id, *, cleanup=True):
    pending = await read_pending(user_id)
    if pending and cleanup:
        await cleanup_action(pending["action"])
    await redis_client.delete(pending_key(user_id))
    await redis_client.zrem(EXPIRY_KEY, str(user_id))


async def offer_transition(state, user_id, chat_id, action):
    """Called under the same per-user lock as intake. True means intake must stop."""
    if not await active_quiz(state):
        return False
    identity = await session_id(state)
    previous = await read_pending(user_id)
    # Telegram albums arrive as separate messages: retain every photo, one prompt.
    group = action.get("album")
    if (group and previous and previous["expires"] > time.time()
            and previous["session"] == identity
            and previous["action"].get("album") == group):
        messages = previous["action"]["messages"]
        known = {m["message_id"] for m in messages}
        messages.extend(m for m in action["messages"] if m["message_id"] not in known)
        await redis_client.set(pending_key(user_id), json.dumps(previous))
        return True
    await discard_pending(user_id)
    token = uuid.uuid4().hex[:16]
    pending = {"token": token, "session": identity, "expires": time.time() + PENDING_SECONDS,
               "chat_id": chat_id, "action": action}
    # Keep the cleanup record durable until the sweeper has removed temporary objects.
    await redis_client.zadd(EXPIRY_KEY, {str(user_id): pending["expires"]})
    await redis_client.set(pending_key(user_id), json.dumps(pending))
    data = await state.get_data()
    index = min(data.get("current_index", 0) + 1, len(data["questions"]))
    text = (f"⚠️ لديك اختبار جارٍ: {data.get('source_title') or 'اختبار'}\n"
            f"السؤال {index} من {len(data['questions'])}.\n\n"
            "يمكنك متابعة الحل، أو إنهاء المحاولة الحالية والانتقال للطلب الجديد. "
            "الإنهاء يُسجّل كتوقف مبكر ولا يمكن التراجع عنه.")
    if action["kind"] == "stop":
        text = "⚠️ لديك اختبار جارٍ. يمكنك العودة للسؤال أو إنهاء الاختبار من هنا."
    if action["kind"].startswith("web_"):
        text += "\n📤 احتفظنا برفعك لمدة ٣٠ دقيقة بانتظار اختيارك."
    rows = [
        [types.InlineKeyboardButton(text="▶️ متابعة الاختبار الحالي", callback_data=f"sn:resume:{token}")],
        [types.InlineKeyboardButton(text="⏹ إنهاء الحالي" if action["kind"] == "stop" else "⏹ إنهاء الحالي والمتابعة للطلب الجديد", callback_data=f"sn:replace:{token}")],
        [types.InlineKeyboardButton(text="إلغاء الطلب الجديد", callback_data=f"sn:cancel:{token}")],
    ]
    await bot.send_message(chat_id, text, reply_markup=types.InlineKeyboardMarkup(inline_keyboard=rows), parse_mode=None)
    return True


def message_action(message, kind):
    return {"kind": kind, "album": message.media_group_id,
            "messages": [message.model_dump(mode="json", exclude_none=True)]}


async def show_upload(chat_id, kind="all"):
    from constants import WEBAPP_PUBLIC_BASE_URL
    if not WEBAPP_PUBLIC_BASE_URL:
        await bot.send_message(chat_id, "صفحة الرفع غير متاحة الآن.")
        return
    suffix = "" if kind == "all" else f"?type={kind}"
    keyboard = types.InlineKeyboardMarkup(inline_keyboard=[[
        types.InlineKeyboardButton(text="📤 فتح صفحة الرفع", web_app=types.WebAppInfo(
            url=f"{WEBAPP_PUBLIC_BASE_URL}/webapp/upload_hub.html{suffix}"))]])
    await bot.send_message(chat_id, "📤 افتح صفحة الرفع واختر المحتوى المطلوب.", reply_markup=keyboard)


@router.callback_query(F.data.startswith("upload_open:"))
@user_locked()
async def open_upload(call, state):
    kind = call.data.split(":", 1)[1]
    if kind not in {"all", "audio", "document", "images"}:
        return await call.answer("طلب غير صالح.", show_alert=True)
    await call.answer()
    if await offer_transition(state, call.from_user.id, call.message.chat.id, {"kind": "upload", "type": kind}):
        return
    await show_upload(call.message.chat.id, kind)


async def dispatch_action(action, call, state):
    """The caller owns UserLock; bypass only the intake wrapper, never validation."""
    kind = action["kind"]
    if kind in {"media", "text", "audio"}:
        from handlers.files import handle_media, handle_pure_text
        from handlers.audio import handle_audio_message
        handler = {"media": handle_media, "text": handle_pure_text, "audio": handle_audio_message}[kind]
        for raw in action["messages"]:
            msg = types.Message.model_validate(raw, context={"bot": bot})
            await handler.__wrapped__(msg, state)
    elif kind == "loaded":
        from handlers.quiz_runner import start_loaded_quiz
        await start_loaded_quiz.__wrapped__(call, state, **action["kwargs"])
    elif kind == "upload":
        await show_upload(call.message.chat.id, action["type"])
    elif kind.startswith("web_"):
        from handlers.audio import process_web_uploaded_audio
        from handlers.files import process_web_uploaded_file, process_web_uploaded_images
        handler = {"web_audio": process_web_uploaded_audio, "web_file": process_web_uploaded_file,
                   "web_images": process_web_uploaded_images}[kind]
        kwargs = {k: v for k, v in action.items() if k != "kind"}
        from services.redis_reliability import _WEB_PREPARATION_SEMAPHORE
        try:
            async with _WEB_PREPARATION_SEMAPHORE:
                await handler.__wrapped__(**kwargs)
        except BaseException:
            await cleanup_action(action)
            raise
    else:
        await bot.send_message(call.message.chat.id, "✅ تم إنهاء الاختبار. يمكنك بدء طلب جديد.")


@router.callback_query(F.data.startswith("sn:"))
@user_locked()
async def transition_callback(call, state):
    parts = call.data.split(":")
    pending = await read_pending(call.from_user.id)
    if (len(parts) != 3 or not pending or pending["token"] != parts[2]
            or pending["chat_id"] != call.message.chat.id):
        return await call.answer("انتهت صلاحية هذا الزر. أعد فتح الطلب المطلوب.", show_alert=True)
    if pending["expires"] <= time.time():
        await discard_pending(call.from_user.id)
        return await call.answer("انتهت مهلة الطلب الجديد. اختبارك الحالي لم يتغير.", show_alert=True)
    active = await active_quiz(state)
    if active and pending["session"] != await session_id(state):
        await discard_pending(call.from_user.id)
        return await call.answer("هذا التنبيه يخص اختبارًا سابقًا؛ لم نغيّر اختبارك الحالي.", show_alert=True)
    choice = parts[1]
    if choice not in {"resume", "replace", "cancel"}:
        return await call.answer("طلب غير صالح.", show_alert=True)
    await call.answer()
    if choice in {"resume", "cancel"}:
        await discard_pending(call.from_user.id)
        try:
            await call.message.edit_reply_markup(reply_markup=None)
        except Exception:
            pass
        if active:
            from handlers.quiz_runner import send_question_by_ids
            await state.set_state(QuizState.answering_quiz)
            await send_question_by_ids(call.message.chat.id, call.from_user.id, state)
        else:
            await bot.send_message(call.message.chat.id, "الاختبار السابق انتهى بالفعل. تم إلغاء الطلب الجديد.")
        return
    # A newer non-quiz operation must not be cleared by an old confirmation either.
    if not active and await state.get_state():
        await discard_pending(call.from_user.id)
        return await bot.send_message(call.message.chat.id, "لديك طلب أحدث قيد التجهيز؛ أعد المحاولة بعد انتهائه.")
    data = await state.get_data()
    if active and data.get("attempt_id"):
        from helpers.supabase_helper import mark_quiz_attempt_stopped
        await mark_quiz_attempt_stopped(data["attempt_id"])
    await state.clear()
    await discard_pending(call.from_user.id, cleanup=False)
    try:
        await call.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass
    try:
        await dispatch_action(pending["action"], call, state)
    except Exception:
        logger.exception("Session transition failed user=%s", call.from_user.id)
        await bot.send_message(call.message.chat.id, "تعذر تجهيز الطلب الجديد. أعد إرساله من فضلك.")
        # The regular web processors clean up their objects on failure.
        raise


async def pending_cleanup_loop():
    while True:
        try:
            for raw_id in await redis_client.zrangebyscore(EXPIRY_KEY, 0, time.time(), start=0, num=100):
                user_id = int(raw_id)
                lock = UserLock(redis_client, user_id)
                if not await lock.acquire():
                    continue
                try:
                    pending = await read_pending(user_id)
                    if not pending or pending["expires"] <= time.time():
                        await discard_pending(user_id)
                finally:
                    await lock.release()
        except Exception:
            logger.exception("Pending session cleanup failed; will retry")
        await asyncio.sleep(60)
