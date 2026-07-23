# Handlers/files.py
import asyncio
import json
import os
import uuid
from typing import Any, Dict, List

from aiogram import F, Router, types
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext

from config import QuizState, bot, redis_client
from constants import (
    ADMIN_CONTACT, BTN_CANCEL_REQUEST, DAILY_RENEWAL_POINTS, ERROR_ALBUM_TOO_LARGE,
    MAX_ALBUM_IMAGES, MAX_SUPER_PAGES, MAX_TEXT_INPUT_SIZE, MSG_NOTHING_TO_CANCEL,
    MSG_PREVIOUS_REQUEST_REPLACED, MSG_PROCESSING, MSG_REQUEST_CANCELLED,
    MSG_SUPER_PROCESSING_ALERT, SUCCESS_MEDIA_RECEIVED, PAGES_PER_QUIZ_RATIO,
    MAX_FILE_QUIZZES_LIMIT, MIN_QUIZZES_PER_FILE, MSG_MAX_QUIZZES_REACHED
)
from gemini_helper import get_pdf_page_count_sync
from helpers.points_calculator import calculate_cached_points_cost, calculate_quiz_points_cost
from keyboards import get_generation_confirm_keyboard, get_multiple_quizzes_keyboard
from logger import get_logger, log_error
from supabase_helper import (
    check_or_add_user, get_file_quizzes, update_user_stats, log_usage_event, mark_quiz_attempt_stopped
)
from utils import calculate_file_hash, ensure_directory_exists, safe_file_cleanup
from validators import validate_file_size, validate_question_count

# استيراد الخدمات الجديدة
from services.file_service import compute_combined_hash, download_photos_service
from services.quiz_service import (
    determine_execution_mode, build_transparency_text, refund_user_on_failure, execute_quiz_generation_workflow
)

logger = get_logger(__name__)
router = Router()
DOWNLOADS_DIR = "downloads"
processing_users_lock = asyncio.Lock()
processing_users: set[int] = set()

PENDING_REQUEST_STATES = (
    QuizState.waiting_for_count, 
    QuizState.waiting_for_cache_decision, 
    QuizState.waiting_for_generation_confirm
)

def _cancel_keyboard() -> types.InlineKeyboardMarkup:
    return types.InlineKeyboardMarkup(inline_keyboard=[[types.InlineKeyboardButton(text=BTN_CANCEL_REQUEST, callback_data="cancel_upload_request")]])

async def _discard_pending_upload(state: FSMContext) -> int:
    data = await state.get_data()
    file_paths = data.get("file_paths", []) or []
    removed = sum(1 for path in file_paths if safe_file_cleanup(path))
    return removed

async def _renewal_notice(message: types.Message, user_info: Dict[str, Any]) -> None:
    if user_info.get("status") == "renewed":
        await message.answer(f"☀️ تم تجديد رصيدك اليومي إلى <b>{DAILY_RENEWAL_POINTS} نقطة مجانية</b>.", parse_mode="HTML")

async def _insufficient_balance(message: types.Message, user_info: Dict[str, Any], required: float) -> None:
    balance = float(user_info.get("points") or 0)
    deficit = max(0.0, required - balance)
    contact = ADMIN_CONTACT.lstrip("@")
    keyboard = types.InlineKeyboardMarkup(inline_keyboard=[[types.InlineKeyboardButton(text="💳 شحن الرصيد الآن", url=f"https://t.me/{contact}")]])
    await message.answer(
        "❌ <b>رصيدك الحالي لا يكفي لإتمام هذه العملية.</b>\n\n"
        f"🎁 المجاني: <code>{float(user_info.get('free_points') or 0):.2f}</code>\n"
        f"💳 المدفوع: <code>{float(user_info.get('paid_points') or 0):.2f}</code>\n"
        f"💰 الإجمالي الحالي: <code>{balance:.2f}</code> / المطلوب: <code>{required:.2f}</code>\n"
        f"⚠️ العجز المطلوب شحنه: <b>{deficit:.2f} نقطة</b>",
        reply_markup=keyboard, parse_mode="HTML"
    )

async def _current_user(message: types.Message, user: Any = None) -> Dict[str, Any]:
    user = user or message.from_user
    return await check_or_add_user(user.id, user.username or "Unknown", user.first_name or "Unknown", user.last_name or "Unknown")

# ==================== Background Album Processor ====================

async def process_album_background(message: types.Message, state: FSMContext):
    try:
        await asyncio.sleep(1.5)
        group_id = message.media_group_id
        list_key = f"album_list:{group_id}"
        
        raw_photos = await redis_client.lrange(list_key, 0, -1)
        await redis_client.delete(list_key)
        
        seen, photos = set(), []
        for raw_photo in raw_photos:
            item = json.loads(raw_photo)
            uid = item.get("file_unique_id")
            if uid and uid not in seen:
                seen.add(uid)
                photos.append(item)
                
        if not photos: return
        if len(photos) > MAX_ALBUM_IMAGES:
            await message.answer(ERROR_ALBUM_TOO_LARGE)
            return
            
        file_paths, err = await download_photos_service(message.from_user.id, photos)
        if err:
            await message.answer(err)
            return
        if not file_paths: return
        
        is_album = len(file_paths) > 1
        title = f"كويز من ألبوم صور ({len(file_paths)} صور)" if is_album else "كويز من صورة"
        file_hash = await asyncio.to_thread(compute_combined_hash, file_paths)
        
        await _finalize_media_processing(message, state, file_paths, title, len(file_paths), is_album, file_hash)
    except Exception as exc:
        log_error(logger, f"Album background processing failed: {exc}", exception=exc)
        await message.answer("❌ حدث خطأ غير متوقع أثناء تجميع الألبوم.")

async def _finalize_media_processing(message: types.Message, state: FSMContext, file_paths: List[str], title: str, items: int, is_album: bool, file_hash: str):
    try:
        content_type = "album" if is_album else ("photo" if len(file_paths) == 1 and file_paths[0].lower().endswith((".jpg", ".jpeg", ".png")) else "document")
        asyncio.create_task(log_usage_event(message.from_user.id, "content_uploaded", {
            "content_type": content_type, "items_count": items, "is_album": is_album, "file_hash": file_hash,
        }))

        cached_quizzes = await get_file_quizzes(file_hash)
        common_state = {
            "file_paths": file_paths, "source_title": title, "input_type": "media",
            "file_hash": file_hash, "items_count": items, "is_album": is_album,
        }
        
        if cached_quizzes:
            max_allowed = max(MIN_QUIZZES_PER_FILE, min(MAX_FILE_QUIZZES_LIMIT, items // PAGES_PER_QUIZ_RATIO))
            show_generate_btn = len(cached_quizzes) < max_allowed
            cost = calculate_cached_points_cost(items, len(cached_quizzes[0]["quiz_data"]), is_album)
            
            await state.update_data(**common_state, available_quizzes=cached_quizzes, cache_cost=cost, max_allowed_quizzes=max_allowed)
            await state.set_state(QuizState.waiting_for_cache_decision)
            
            keyboard = get_multiple_quizzes_keyboard(cached_quizzes, cost, show_generate_btn=show_generate_btn)
            msg_text = f"💡 <b>ملاحظة ذكية: تم العثور على ({len(cached_quizzes)}) كويز جاهز مخزن لهذا الملف مسبقاً!</b>\n\n"
            msg_text += f"🛑 <b>تم استنفاد الحد الأقصى ({max_allowed}) كويزات.</b>" if not show_generate_btn else f"يمكنك اختيار كويز جاهز بخصم 90% ({cost:.2f} نقطة)، أو توليد كويز جديد (المتاح: {len(cached_quizzes)} من {max_allowed}):"
            await message.answer(msg_text, parse_mode="HTML", reply_markup=keyboard)
            return

        await state.update_data(**common_state)
        await state.set_state(QuizState.waiting_for_count)
        await message.answer(SUCCESS_MEDIA_RECEIVED, reply_markup=_cancel_keyboard())
    except Exception as exc:
        for path in file_paths: safe_file_cleanup(path)
        log_error(logger, f"Finalize media failed: {exc}", exception=exc)
        await message.answer("❌ حدث خطأ غير متوقع أثناء معالجة الوسائط.")

# ==================== Handlers ====================

@router.message(F.document | F.photo)
async def handle_media(message: types.Message, state: FSMContext) -> None:
    try:
        current_state = await state.get_state()
        if current_state == QuizState.answering_quiz:
            data = await state.get_data()
            if data.get("attempt_id"):
                asyncio.create_task(mark_quiz_attempt_stopped(data["attempt_id"]))
            await _discard_pending_upload(state)
            await state.clear()
            await message.answer("ℹ️ <b>تم إيقاف اختبارك السابق تلقائياً وجاري معالجة المحتوى الجديد...</b>", parse_mode="HTML")

        elif current_state in PENDING_REQUEST_STATES:
            if await _discard_pending_upload(state):
                await message.answer(MSG_PREVIOUS_REQUEST_REPLACED)
            await state.clear()

        ensure_directory_exists(DOWNLOADS_DIR)

        if message.photo:
            photo = message.photo[-1]
            current = {"file_id": photo.file_id, "file_unique_id": photo.file_unique_id, "file_size": photo.file_size}
            if message.media_group_id:
                group_id = message.media_group_id
                await redis_client.rpush(f"album_list:{group_id}", json.dumps(current))
                await redis_client.expire(f"album_list:{group_id}", 30)
                if await redis_client.set(f"album_lock:{group_id}", "1", nx=True, ex=15):
                    await message.answer("📥 جارٍ تجميع الصور ومعالجة الألبوم بالخلفية...")
                    asyncio.create_task(process_album_background(message, state))
                return
            else:
                file_paths, err = await download_photos_service(message.from_user.id, [current])
                if err or not file_paths: return
                file_hash = await asyncio.to_thread(compute_combined_hash, file_paths)
                await _finalize_media_processing(message, state, file_paths, "كويز من صورة", 1, False, file_hash)
        else:
            valid, error = validate_file_size(message.document.file_size, "document")
            if not valid:
                await message.answer(error)
                return
            title, extension = os.path.splitext(message.document.file_name or "document")
            destination = os.path.join(DOWNLOADS_DIR, f"{message.from_user.id}_{uuid.uuid4().hex}{extension}")
            await bot.download(message.document, destination=destination)
            
            items = 1
            if destination.lower().endswith(".pdf"):
                items = await asyncio.to_thread(get_pdf_page_count_sync, destination)
                if items > MAX_SUPER_PAGES:
                    await message.answer(f"❌ الحد الأقصى لمعالجة ملفات PDF هو {MAX_SUPER_PAGES} صفحة.")
                    safe_file_cleanup(destination)
                    return
            file_hash = await asyncio.to_thread(calculate_file_hash, destination)
            await _finalize_media_processing(message, state, [destination], title, items, False, file_hash)

    except Exception as exc:
        log_error(logger, f"Media handling failed: {exc}", exception=exc)
        await message.answer("❌ حدث خطأ غير متوقع أثناء معالجة الوسائط.")

@router.message(StateFilter(None, QuizState.answering_quiz), F.text, ~F.text.startswith("/"))
async def handle_pure_text(message: types.Message, state: FSMContext) -> None:
    text = message.text.strip()
    if await state.get_state() == QuizState.answering_quiz:
        if len(text) >= 30:
            data = await state.get_data()
            if data.get("attempt_id"):
                asyncio.create_task(mark_quiz_attempt_stopped(data["attempt_id"]))
            await _discard_pending_upload(state)
            await state.clear()
            await message.answer("ℹ️ <b>تم إيقاف الاختبار السابق تلقائياً لبدء الكويز النصي الجديد...</b>", parse_mode="HTML")
        else:
            await message.answer("⚠️ لديك اختبار قائم حالياً؛ أتممه أو أوقفه عبر الضغط على (⏹️ إيقاف) أولاً.")
            return

    if len(text) < 30:
        await message.answer("⚠️ النص قصير جداً؛ أرسل 30 حرفاً على الأقل لضمان دقة الأسئلة.")
        return
    if len(text) > MAX_TEXT_INPUT_SIZE:
        await message.answer(f"❌ الحد الأقصى للنص المباشر هو {MAX_TEXT_INPUT_SIZE} حرفاً.")
        return

    await state.update_data(pure_text=text, source_title=text[:20] + "...", input_type="text", items_count=1, is_album=False)
    await state.set_state(QuizState.waiting_for_count)
    await message.answer("✅ تم استقبال النص بنجاح. كم سؤالاً تريد توليده من هذا المحتوى؟", reply_markup=_cancel_keyboard())

# ==================== معالجات قرار الكاش والأزرار المتعددة ====================

@router.callback_query(QuizState.waiting_for_cache_decision, F.data.startswith("use_multi_"))
async def handle_multi_cache_selection(call: types.CallbackQuery, state: FSMContext) -> None:
    """معالج تشغيل أحد الكويزات الجاهزة المخزنة بالجدول المركزي"""
    try:
        quiz_uuid = call.data.replace("use_multi_", "")
        data = await state.get_data()
        cost = float(data.get("cache_cost", 0))
        
        user_info = await _current_user(call.message, call.from_user)
        await _renewal_notice(call.message, user_info)
        
        available_quizzes = data.get("available_quizzes", [])
        selected_quiz = next((q for q in available_quizzes if str(q["id"]) == quiz_uuid), None)
        if not selected_quiz:
            await call.answer("❌ عذراً، لم نتمكن من جلب الكويز المختار.", show_alert=True)
            return
            
        if float(user_info["points"]) < cost:
            await _insufficient_balance(call.message, user_info, cost)
            return
            
        remaining = await update_user_stats(call.from_user.id, cost, len(selected_quiz["quiz_data"]))
        if remaining is None:
            await _insufficient_balance(call.message, await _current_user(call.message, call.from_user), cost)
            return
            
        asyncio.create_task(log_usage_event(call.from_user.id, "cached_quiz_used", {
            "quiz_id": quiz_uuid, "cost": cost,
        }))

        from handlers.quiz_runner import _start_loaded_quiz
        await _start_loaded_quiz(call.message, state, selected_quiz["quiz_data"], data.get("source_title", "كويز"), origin="cached_file", quiz_id=quiz_uuid)
        
        for path in data.get("file_paths", []):
            safe_file_cleanup(path)
    except Exception as exc:
        log_error(logger, f"Multi-cached selection trigger failed: {exc}", exception=exc)
        await call.message.answer("❌ تعذر بدء تشغيل الاختبار المخزّن.")
    finally:
        await call.answer()

@router.callback_query(QuizState.waiting_for_cache_decision, F.data == "cache_action_no")
async def handle_cache_no(call: types.CallbackQuery, state: FSMContext) -> None:
    """في حال رفض الكاش ورغبة الطالب بتوليد كويز جديد كلياً"""
    await state.set_state(QuizState.waiting_for_count)
    await call.message.edit_text("📝 كم سؤالاً تريد استخراجه وتوليده من هذا المحتوى؟", reply_markup=_cancel_keyboard())
    await call.answer()

# ==================== معالجات تحديد الأسئلة والتأكيد ====================

@router.message(QuizState.waiting_for_count, F.text.isdigit())
async def process_count(message: types.Message, state: FSMContext) -> None:
    """معالج إدخال عدد الأسئلة المطلوبة والتحقق من التكلفة وإظهار رسالة التأكيد"""
    count = int(message.text)
    valid, error = validate_question_count(count)
    if not valid:
        await message.answer(f"❌ {error}", reply_markup=_cancel_keyboard())
        return
        
    data = await state.get_data()
    items = int(data.get("items_count") or 1)
    is_album = bool(data.get("is_album"))
    file_hash = data.get("file_hash")
    
    if file_hash:
        current_quizzes = await get_file_quizzes(file_hash)
        max_allowed = max(MIN_QUIZZES_PER_FILE, min(MAX_FILE_QUIZZES_LIMIT, items // PAGES_PER_QUIZ_RATIO))
        if len(current_quizzes) >= max_allowed:
            await message.answer(MSG_MAX_QUIZZES_REACHED, parse_mode="HTML")
            await state.clear()
            return

    cost = calculate_quiz_points_cost(items, count, is_album)
    mode = determine_execution_mode(items, count)

    user_info = await _current_user(message)
    
    if float(user_info["points"]) < cost:
        await _insufficient_balance(message, user_info, cost)
        return

    await state.update_data(calculated_cost=cost, requested_count=count, execution_mode=mode)
    await state.set_state(QuizState.waiting_for_generation_confirm)

    confirm_kb = get_generation_confirm_keyboard()

    confirm_text = (
        f"{build_transparency_text(items, count, mode, cost)}\n\n"
        f"❓ <b>هل تؤكد بدء التوليد وخصم {cost:.2f} نقطة من رصيدك؟</b>"
    )
    if mode == "Super-Processing":
        confirm_text += f"\n\n{MSG_SUPER_PROCESSING_ALERT}"

    await message.answer(confirm_text, reply_markup=confirm_kb, parse_mode="HTML")

@router.message(QuizState.waiting_for_count)
async def process_count_invalid(message: types.Message) -> None:
    """معالج إدخال قيمة غير رقمية لعدد الأسئلة"""
    await message.answer(
        "⚠️ <b>الرجاء إرسال رقم صحيح لعدد الأسئلة!</b>\n\nأو اعمد إلى استخدام زر التراجع أدناه لإلغاء العملية الحالية بشكل نظيف وعادل.",
        reply_markup=_cancel_keyboard(),
        parse_mode="HTML"
    )

@router.callback_query(F.data == "cancel_upload_request")
async def handle_cancel_upload(call: types.CallbackQuery, state: FSMContext) -> None:
    """معالج التراجع والضغط على زر إلغاء الطلب"""
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

@router.callback_query(QuizState.waiting_for_generation_confirm, F.data == "confirm_quiz_generation")
async def handle_confirm_quiz_generation(call: types.CallbackQuery, state: FSMContext) -> None:
    try:
        data = await state.get_data()
        cost, count = float(data.get("calculated_cost") or 0), int(data.get("requested_count") or 0)
        user_info = await _current_user(call.message, call.from_user)
        
        if float(user_info["points"]) < cost or await update_user_stats(call.from_user.id, cost, count) is None:
            await _insufficient_balance(call.message, user_info, cost)
            return

        await state.update_data(debited_cost=cost)
        try: await call.message.delete()
        except Exception: pass

        status_msg = await call.message.answer(MSG_PROCESSING)
        
        # استدعاء خط التوليد من الـ Service
        quiz_data, new_quiz_id, error_code = await execute_quiz_generation_workflow(call.from_user.id, data, count, status_msg)
        
        if error_code == "unreadable_office":
            await refund_user_on_failure(call.from_user.id, data)
            await state.set_state(None)
            await status_msg.edit_text("⚠️ <b>تعذر استخراج نص مفيد من المستند!</b> يرجى التأكد من أنه يحتوي على نصوص وليس صوراً. تم إرجاع نقاطك.", parse_mode="HTML")
            return
        elif error_code == "ai_failed" or not quiz_data:
            await refund_user_on_failure(call.from_user.id, data)
            await state.set_state(None)
            await status_msg.edit_text("⚠️ <b>فشل توليد الأسئلة!</b> رصيدك آمن ولم يتم خصم أي نقاط.", parse_mode="HTML")
            return

        from handlers.quiz_runner import _start_loaded_quiz
        await _start_loaded_quiz(call.message, state, quiz_data, data.get("source_title", "كويز"), origin="file" if data.get("input_type") == "media" else "text", quiz_id=new_quiz_id)
        await status_msg.delete()

    except Exception as exc:
        log_error(logger, f"Confirm quiz generation failed: {exc}", exception=exc)
        await refund_user_on_failure(call.from_user.id, await state.get_data())
        await state.set_state(None)
        await call.answer("❌ حدث خطأ، تم إعادة شحن رصيدك تلقائياً.", show_alert=True)
    finally:
        for path in (await state.get_data()).get("file_paths", []):
            safe_file_cleanup(path)
        await call.answer()

files_router = router