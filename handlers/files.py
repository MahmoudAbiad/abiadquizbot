"""Media ingestion, pricing transparency, and generation orchestration."""

import asyncio
import hashlib
import json
import os
import uuid
from typing import Any, Dict, List

from aiogram import F, Router, types
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext

from config import QuizState, bot, redis_client
from constants import (
    ADMIN_CONTACT,
    BTN_CANCEL_REQUEST,
    DAILY_RENEWAL_POINTS,
    ERROR_ALBUM_TOO_LARGE,
    MAX_ALBUM_IMAGES,
    MAX_LIMIT_PAGES,
    MAX_LIMIT_QUESTIONS,
    MAX_STANDARD_PAGES,
    MAX_STANDARD_QUESTIONS,
    MAX_SUPER_PAGES,
    MAX_TEXT_INPUT_SIZE,
    MSG_NOTHING_TO_CANCEL,
    MSG_PREVIOUS_REQUEST_REPLACED,
    MSG_PROCESSING,
    MSG_REQUEST_CANCELLED,
    MSG_SUPER_PROCESSING_ALERT,
    SUCCESS_MEDIA_RECEIVED,
)
from gemini_helper import generate_quiz_smart, get_pdf_page_count_sync
from helpers.points_calculator import calculate_cached_points_cost, calculate_quiz_points_cost
from logger import get_logger, log_error
from supabase_helper import (
    check_or_add_user,
    get_cached_quiz,
    refund_user_points,
    save_shared_quiz,
    update_user_stats,
)
from utils import calculate_file_hash, ensure_directory_exists, safe_file_cleanup
from validators import validate_file_size, validate_question_count

logger = get_logger(__name__)
router = Router()
DOWNLOADS_DIR = "downloads"
processing_users_lock = asyncio.Lock()
processing_users: set[int] = set()

# الحالات التي يوجد فيها "طلب معلّق" (ملف/صورة/نص بانتظار قرار المستخدم)
# يمكن إلغاؤه أو استبداله بشكل نظيف.
PENDING_REQUEST_STATES = (QuizState.waiting_for_count, QuizState.waiting_for_cache_decision)


def _combined_hash(paths: List[str]) -> str:
    """Hash ordered file payloads without using their names or Telegram IDs."""
    digest = hashlib.sha256()
    for path in paths:
        digest.update(calculate_file_hash(path).encode("ascii"))
    return digest.hexdigest()


def _cancel_keyboard() -> types.InlineKeyboardMarkup:
    """Inline keyboard offering a clean way to back out of a pending request."""
    return types.InlineKeyboardMarkup(
        inline_keyboard=[[types.InlineKeyboardButton(text=BTN_CANCEL_REQUEST, callback_data="cancel_upload_request")]]
    )


async def _discard_pending_upload(state: FSMContext) -> int:
    """Delete any files tied to the currently pending (not-yet-actioned) request.

    Returns the number of files removed from disk. Safe to call even when
    there is nothing pending (e.g. a pure-text request has no files).
    """
    data = await state.get_data()
    file_paths = data.get("file_paths", []) or []
    removed = 0
    for path in file_paths:
        if safe_file_cleanup(path):
            removed += 1
    return removed


async def collect_album_photos_redis(message: types.Message) -> List[Dict[str, Any]]:
    """Coalesce Telegram media-group messages across workers using Redis."""
    photo = message.photo[-1]
    current = {
        "file_id": photo.file_id,
        "file_unique_id": photo.file_unique_id,
        "file_size": photo.file_size,
    }
    if not message.media_group_id:
        return [current]

    group_id = message.media_group_id
    list_key = f"album_list:{group_id}"
    lock_key = f"album_lock:{group_id}"
    await redis_client.rpush(list_key, json.dumps(current))
    # ضبط TTL على قائمة الألبوم نفسها (وليس فقط القفل) لمنع تراكم بيانات يتيمة
    # في Redis في حال انهارت العملية قبل اكتمال التجميع.
    await redis_client.expire(list_key, 30)
    coordinator = await redis_client.set(lock_key, "1", nx=True, ex=15)
    if not coordinator:
        return []

    await asyncio.sleep(1.2)
    raw_photos = await redis_client.lrange(list_key, 0, -1)
    await redis_client.delete(list_key)
    await redis_client.delete(lock_key)

    # تصفية أي تكرار محتمل لنفس الصورة (بحسب معرّفها الفريد) قبل المتابعة.
    seen: set = set()
    unique_photos: List[Dict[str, Any]] = []
    for raw_photo in raw_photos:
        item = json.loads(raw_photo)
        unique_id = item.get("file_unique_id")
        if unique_id and unique_id in seen:
            continue
        if unique_id:
            seen.add(unique_id)
        unique_photos.append(item)
    return unique_photos


def _execution_mode(items: int, questions: int, cached: bool = False) -> str:
    if cached:
        return "Cached"
    if items > MAX_LIMIT_PAGES or questions > MAX_LIMIT_QUESTIONS:
        return "Super-Processing"
    if items > MAX_STANDARD_PAGES or questions > MAX_STANDARD_QUESTIONS:
        return "Over-Limit"
    return "Standard"


def _transparency_text(items: int, questions: int, mode: str, cost: float) -> str:
    return (
        "📋 <b>تفاصيل التنفيذ</b>\n"
        f"• العناصر/الصفحات: <code>{items}</code>\n"
        f"• الأسئلة المطلوبة: <code>{questions}</code>\n"
        f"• وضع التنفيذ: <code>{mode}</code>\n"
        f"• النقاط المطلوب خصمها: <code>{cost:.2f}</code>"
    )


async def _renewal_notice(message: types.Message, user_info: Dict[str, Any]) -> None:
    if user_info.get("status") == "renewed":
        await message.answer(
            f"☀️ تم تجديد رصيدك اليومي إلى <b>{DAILY_RENEWAL_POINTS} نقطة مجانية</b>.",
            parse_mode="HTML",
        )


async def _insufficient_balance(
    message: types.Message, user_info: Dict[str, Any], required: float
) -> None:
    balance = float(user_info.get("points") or 0)
    deficit = max(0.0, required - balance)
    contact = ADMIN_CONTACT.lstrip("@")
    keyboard = types.InlineKeyboardMarkup(
        inline_keyboard=[[types.InlineKeyboardButton(text="💳 شحن الرصيد", url=f"https://t.me/{contact}")]]
    )
    await message.answer(
        "❌ <b>رصيدك لا يكفي لإتمام الطلب.</b>\n"
        f"المجاني: <code>{float(user_info.get('free_points') or 0):.2f}</code>\n"
        f"المدفوع: <code>{float(user_info.get('paid_points') or 0):.2f}</code>\n"
        f"الإجمالي: <code>{balance:.2f}</code> / المطلوب: <code>{required:.2f}</code>\n"
        f"العجز: <code>{deficit:.2f}</code>",
        reply_markup=keyboard,
        parse_mode="HTML",
    )


async def _current_user(message: types.Message, user: Any = None) -> Dict[str, Any]:
    user = user or message.from_user
    return await check_or_add_user(
        user.id,
        user.username or "Unknown",
        user.first_name or "Unknown",
        user.last_name or "Unknown",
    )


async def _download_photos(message: types.Message, photos: List[Dict[str, Any]]) -> List[str]:
    paths: List[str] = []
    try:
        for index, photo in enumerate(photos, start=1):
            valid, error = validate_file_size(photo.get("file_size") or 0, "photo")
            if not valid:
                await message.answer(f"❌ الصورة رقم {index}: {error}")
                return []
            path = os.path.join(DOWNLOADS_DIR, f"{message.from_user.id}_{uuid.uuid4().hex}.jpg")
            await bot.download(photo["file_id"], destination=path)
            paths.append(path)
        return paths
    except Exception:
        for path in paths:
            safe_file_cleanup(path)
        raise


@router.message(F.document | F.photo)
async def handle_media(message: types.Message, state: FSMContext) -> None:
    file_paths: List[str] = []
    try:
        current_state = await state.get_state()
        if current_state == QuizState.answering_quiz:
            await message.answer("⚠️ لديك اختبار قائم حالياً؛ أتممه أو أوقفه قبل رفع محتوى جديد.")
            return

        # إذا كان هناك طلب سابق معلّق (ملف/صورة بانتظار عدد الأسئلة، أو بانتظار
        # قرار الكاش) نقوم بإلغائه وحذف ملفاته تلقائياً قبل قبول المحتوى الجديد،
        # حتى لا تتراكم ملفات يتيمة في مجلد downloads.
        if current_state in PENDING_REQUEST_STATES:
            removed = await _discard_pending_upload(state)
            await state.clear()
            if removed:
                await message.answer(MSG_PREVIOUS_REQUEST_REPLACED)

        ensure_directory_exists(DOWNLOADS_DIR)

        if message.photo:
            photos = await collect_album_photos_redis(message)
            if not photos:
                return
            if len(photos) > MAX_ALBUM_IMAGES:
                await message.answer(ERROR_ALBUM_TOO_LARGE)
                return
            file_paths = await _download_photos(message, photos)
            if not file_paths:
                return
            is_album = len(file_paths) > 1
            title = f"كويز من ألبوم صور ({len(file_paths)} صور)" if is_album else "كويز من صورة"
            items = len(file_paths)
            file_hash = await asyncio.to_thread(_combined_hash, file_paths)
        else:
            valid, error = validate_file_size(message.document.file_size, "document")
            if not valid:
                await message.answer(error)
                return
            original_name = message.document.file_name or "document"
            title, extension = os.path.splitext(original_name)
            destination = os.path.join(DOWNLOADS_DIR, f"{message.from_user.id}_{uuid.uuid4().hex}{extension}")
            await bot.download(message.document, destination=destination)
            file_paths = [destination]
            is_album = False
            items = 1
            if destination.lower().endswith(".pdf"):
                items = await asyncio.to_thread(get_pdf_page_count_sync, destination)
                if items > MAX_SUPER_PAGES:
                    await message.answer(f"❌ الحد الأقصى لمعالجة ملفات PDF هو {MAX_SUPER_PAGES} صفحة.")
                    safe_file_cleanup(destination)
                    return
            file_hash = await asyncio.to_thread(calculate_file_hash, destination)

        cached = await get_cached_quiz(file_hash)
        common_state = {
            "file_paths": file_paths,
            "source_title": title,
            "input_type": "media",
            "file_hash": file_hash,
            "items_count": items,
            "is_album": is_album,
        }
        if cached and cached.get("questions_data"):
            questions = cached["questions_data"]
            cost = calculate_cached_points_cost(items, len(questions), is_album)
            await state.update_data(**common_state, cached_questions=questions, cache_cost=cost)
            await state.set_state(QuizState.waiting_for_cache_decision)
            keyboard = types.InlineKeyboardMarkup(inline_keyboard=[
                [types.InlineKeyboardButton(text=f"⚡ استخدام الكويز الجاهز ({cost:.2f} نقطة)", callback_data="cache_action_yes")],
                [types.InlineKeyboardButton(text="🧠 توليد أسئلة جديدة", callback_data="cache_action_no")],
                [types.InlineKeyboardButton(text=BTN_CANCEL_REQUEST, callback_data="cancel_upload_request")],
            ])
            await message.answer(
                "💡 تم العثور على هذا المحتوى في الكاش.\n\n"
                + _transparency_text(items, len(questions), "Cached", cost),
                parse_mode="HTML",
                reply_markup=keyboard,
            )
            return

        await state.update_data(**common_state)
        await state.set_state(QuizState.waiting_for_count)
        await message.answer(SUCCESS_MEDIA_RECEIVED, reply_markup=_cancel_keyboard())
    except Exception as exc:
        for path in file_paths:
            safe_file_cleanup(path)
        log_error(logger, f"Media handling failed: {exc}", exception=exc)
        await message.answer("❌ حدث خطأ غير متوقع أثناء معالجة الوسائط.")


@router.message(StateFilter(None), F.text, ~F.text.startswith("/"))
async def handle_pure_text(message: types.Message, state: FSMContext) -> None:
    text = message.text.strip()
    if len(text) < 30:
        await message.answer("⚠️ النص قصير جداً؛ أرسل 30 حرفاً على الأقل.")
        return
    if len(text) > MAX_TEXT_INPUT_SIZE:
        await message.answer(f"❌ الحد الأقصى للنص المباشر هو {MAX_TEXT_INPUT_SIZE} حرفاً.")
        return
    await state.update_data(pure_text=text, source_title=text[:20] + "...", input_type="text", items_count=1, is_album=False)
    await state.set_state(QuizState.waiting_for_count)
    await message.answer("✅ تم استقبال النص. كم سؤالاً تريد توليده؟", reply_markup=_cancel_keyboard())


@router.callback_query(F.data == "cancel_upload_request")
async def handle_cancel_upload(call: types.CallbackQuery, state: FSMContext) -> None:
    """إلغاء نظيف لطلب معلّق (ملف/صورة/نص) وحذف أي ملفات مرتبطة به من القرص."""
    try:
        current_state = await state.get_state()
        if current_state not in PENDING_REQUEST_STATES:
            await call.answer(MSG_NOTHING_TO_CANCEL, show_alert=True)
            return

        await _discard_pending_upload(state)
        await state.clear()

        try:
            await call.message.edit_text(MSG_REQUEST_CANCELLED)
        except Exception:
            await call.message.answer(MSG_REQUEST_CANCELLED)
    except Exception as exc:
        log_error(logger, f"Cancel request failed: {exc}", exception=exc)
        await call.answer("❌ تعذر إلغاء الطلب، حاول مجدداً.", show_alert=True)
    finally:
        await call.answer()


@router.message(StateFilter(*PENDING_REQUEST_STATES), Command("cancel"))
async def handle_cancel_command(message: types.Message, state: FSMContext) -> None:
    """أمر /cancel كبديل نصي لزر الإلغاء (لمن لا يستخدم الأزرار)."""
    await _discard_pending_upload(state)
    await state.clear()
    await message.answer(MSG_REQUEST_CANCELLED)


@router.callback_query(QuizState.waiting_for_cache_decision, F.data == "cache_action_yes")
async def handle_cache_yes(call: types.CallbackQuery, state: FSMContext) -> None:
    try:
        data = await state.get_data()
        cost = float(data["cache_cost"])
        user_info = await _current_user(call.message, call.from_user)
        await _renewal_notice(call.message, user_info)
        await call.message.answer(
            _transparency_text(data["items_count"], len(data["cached_questions"]), "Cached", cost),
            parse_mode="HTML",
        )
        if float(user_info["points"]) < cost:
            await _insufficient_balance(call.message, user_info, cost)
            return
        remaining = await update_user_stats(call.from_user.id, cost, len(data["cached_questions"]))
        if remaining is None:
            await _insufficient_balance(call.message, await _current_user(call.message, call.from_user), cost)
            return
        quiz_id = uuid.uuid4().hex[:12]
        await save_shared_quiz(quiz_id, call.from_user.id, data["source_title"], data["cached_questions"])
        from handlers.execution import _start_loaded_quiz
        await _start_loaded_quiz(call.message, state, data["cached_questions"], data["source_title"], origin="cached_file", quiz_id=quiz_id)
        for path in data.get("file_paths", []):
            safe_file_cleanup(path)
    except Exception as exc:
        log_error(logger, f"Cached quiz start failed: {exc}", exception=exc)
        await call.message.answer("❌ تعذر بدء الكويز المخزّن.")
    finally:
        await call.answer()


@router.callback_query(QuizState.waiting_for_cache_decision, F.data == "cache_action_no")
async def handle_cache_no(call: types.CallbackQuery, state: FSMContext) -> None:
    await state.set_state(QuizState.waiting_for_count)
    await call.message.edit_text("📝 كم سؤالاً تريد استخراجه من هذا المحتوى؟", reply_markup=_cancel_keyboard())
    await call.answer()


@router.message(QuizState.waiting_for_count, F.text.isdigit())
async def process_count(message: types.Message, state: FSMContext) -> None:
    count = int(message.text)
    valid, error = validate_question_count(count)
    if not valid:
        await message.answer(f"❌ {error}")
        return
    data = await state.get_data()
    items = int(data.get("items_count") or 1)
    is_album = bool(data.get("is_album"))
    cost = calculate_quiz_points_cost(items, count, is_album)
    mode = _execution_mode(items, count)

    user_info = await _current_user(message)
    await _renewal_notice(message, user_info)
    await message.answer(_transparency_text(items, count, mode, cost), parse_mode="HTML")
    if float(user_info["points"]) < cost:
        await _insufficient_balance(message, user_info, cost)
        return
    remaining = await update_user_stats(message.from_user.id, cost, count)
    if remaining is None:
        await _insufficient_balance(message, await _current_user(message), cost)
        return
    await state.update_data(debited_cost=cost, requested_count=count)
    if mode == "Super-Processing":
        await message.answer(MSG_SUPER_PROCESSING_ALERT)
    await trigger_quiz_generation(message, message.from_user.id, count, state)


@router.message(QuizState.waiting_for_count)
async def process_count_invalid(message: types.Message) -> None:
    await message.answer("⚠️ الرجاء إرسال رقم صحيح لعدد الأسئلة، أو اضغط زر الإلغاء أعلاه للتراجع عن الطلب.")


async def trigger_quiz_generation(message: types.Message, user_id: int, count: int, state: FSMContext) -> None:
    async with processing_users_lock:
        if user_id in processing_users:
            await message.answer("⏳ الطلب قيد المعالجة بالفعل.")
            return
        processing_users.add(user_id)
    status_message = await message.answer(MSG_PROCESSING)
    asyncio.create_task(_run_quiz_flow(message, user_id, count, state, status_message))


async def _run_quiz_flow(message: types.Message, user_id: int, count: int, state: FSMContext, status_message: types.Message) -> None:
    data: Dict[str, Any] = {}
    try:
        data = await state.get_data()
        is_media = data.get("input_type") == "media"
        quiz_data = await generate_quiz_smart(
            file_paths=data.get("file_paths") if is_media else None,
            pure_text=data.get("pure_text") if not is_media else None,
            count=count,
            skip_cache=True,
            file_hash=data.get("file_hash"),
            status_message=status_message,
        )
        if not quiz_data:
            await _refund_after_failure(user_id, data)
            await status_message.edit_text(
                "⚠️ فشل توليد الأسئلة. لم يتمكن النظام من إكمال الطلب، وقد تم استرجاع النقاط المخصومة إلى رصيدك."
            )
            return
        quiz_id = uuid.uuid4().hex[:12]
        await save_shared_quiz(quiz_id, user_id, data.get("source_title", "كويز"), quiz_data)
        from handlers.execution import _start_loaded_quiz
        await _start_loaded_quiz(message, state, quiz_data, data.get("source_title", "كويز"), origin="file" if is_media else "text", quiz_id=quiz_id)
        await status_message.delete()
    except Exception as exc:
        log_error(logger, f"Quiz flow failed: {exc}", exception=exc)
        await _refund_after_failure(user_id, data)
        await status_message.edit_text(
            "⚠️ حدث خطأ تقني أثناء إعداد الاختبار، وقد تم استرجاع النقاط المخصومة إلى رصيدك."
        )
    finally:
        for path in data.get("file_paths", []):
            safe_file_cleanup(path)
        async with processing_users_lock:
            processing_users.discard(user_id)


async def _refund_after_failure(user_id: int, data: Dict[str, Any]) -> None:
    """Best-effort refund of the points deducted for a request that ultimately failed."""
    cost = float(data.get("debited_cost") or 0)
    if cost > 0:
        await refund_user_points(user_id, cost)


files_router = router
