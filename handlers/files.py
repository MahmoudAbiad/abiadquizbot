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
    PAGES_PER_QUIZ_RATIO,
    MAX_FILE_QUIZZES_LIMIT,
    MIN_QUIZZES_PER_FILE,
    MSG_MAX_QUIZZES_REACHED,
)
from gemini_helper import generate_quiz_smart, get_pdf_page_count_sync
from helpers.points_calculator import calculate_cached_points_cost, calculate_quiz_points_cost
from logger import get_logger, log_error
from supabase_helper import (
    check_or_add_user,
    get_file_quizzes,
    refund_user_points,
    save_shared_quiz,
    update_user_stats,
    log_usage_event,
)
# 🟢 تم إضافة استيراد دالة استخراج النص من ملفات Word/PPT/TXT
from utils import calculate_file_hash, ensure_directory_exists, safe_file_cleanup, extract_text_from_file
from validators import validate_file_size, validate_question_count

logger = get_logger(__name__)
router = Router()
DOWNLOADS_DIR = "downloads"
processing_users_lock = asyncio.Lock()
processing_users: set[int] = set()

PENDING_REQUEST_STATES = (QuizState.waiting_for_count, QuizState.waiting_for_cache_decision)

def _combined_hash(paths: List[str]) -> str:
    digest = hashlib.sha256()
    for path in paths:
        digest.update(calculate_file_hash(path).encode("ascii"))
    return digest.hexdigest()

def _cancel_keyboard() -> types.InlineKeyboardMarkup:
    return types.InlineKeyboardMarkup(
        inline_keyboard=[[types.InlineKeyboardButton(text=BTN_CANCEL_REQUEST, callback_data="cancel_upload_request")]]
    )

async def _discard_pending_upload(state: FSMContext) -> int:
    data = await state.get_data()
    file_paths = data.get("file_paths", []) or []
    removed = 0
    for path in file_paths:
        if safe_file_cleanup(path):
            removed += 1
    return removed

# ==================== Core Processing Helpers ====================

def _execution_mode(items: int, questions: int, cached: bool = False) -> str:
    if cached: return "Cached"
    if items > MAX_LIMIT_PAGES or questions > MAX_LIMIT_QUESTIONS: return "Super-Processing"
    if items > MAX_STANDARD_PAGES or questions > MAX_STANDARD_QUESTIONS: return "Over-Limit"
    return "Standard"

def _transparency_text(items: int, questions: int, mode: str, cost: float) -> str:
    return (
        "📋 <b>تفاصيل التنفيذ والشفافية المالية</b>\n\n"
        f"• العناصر/الصفحات: <code>{items}</code>\n"
        f"• الأسئلة المطلوبة: <code>{questions}</code>\n"
        f"• وضع المعالجة: <code>{mode}</code>\n"
        f"• تكلفة العملية: <b>{cost:.2f} نقطة</b>"
    )

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
        for path in paths: safe_file_cleanup(path)
        raise

# ==================== Background Album Processor ====================

async def process_album_background(message: types.Message, state: FSMContext):
    try:
        await asyncio.sleep(1.5)
        
        group_id = message.media_group_id
        list_key = f"album_list:{group_id}"
        
        raw_photos = await redis_client.lrange(list_key, 0, -1)
        await redis_client.delete(list_key)
        
        seen = set()
        photos = []
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
            
        file_paths = await _download_photos(message, photos)
        if not file_paths: return
        
        is_album = len(file_paths) > 1
        title = f"كويز من ألبوم صور ({len(file_paths)} صور)" if is_album else "كويز من صورة"
        items = len(file_paths)
        file_hash = await asyncio.to_thread(_combined_hash, file_paths)
        
        await _finalize_media_processing(message, state, file_paths, title, items, is_album, file_hash)
        
    except Exception as exc:
        log_error(logger, f"Album background processing failed: {exc}", exception=exc)
        await message.answer("❌ حدث خطأ غير متوقع أثناء تجميع الألبوم.")

# ==================== Common Finalization ====================

async def _finalize_media_processing(message: types.Message, state: FSMContext, file_paths: List[str], title: str, items: int, is_album: bool, file_hash: str):
    try:
        content_type = "album" if is_album else ("photo" if len(file_paths) == 1 and file_paths[0].lower().endswith((".jpg", ".jpeg", ".png")) else "document")
        asyncio.create_task(log_usage_event(message.from_user.id, "content_uploaded", {
            "content_type": content_type,
            "items_count": items,
            "is_album": is_album,
            "file_hash": file_hash,
        }))

        cached_quizzes = await get_file_quizzes(file_hash)
        
        common_state = {
            "file_paths": file_paths,
            "source_title": title,
            "input_type": "media",
            "file_hash": file_hash,
            "items_count": items,
            "is_album": is_album,
        }
        
        if cached_quizzes:
            max_allowed = max(MIN_QUIZZES_PER_FILE, min(MAX_FILE_QUIZZES_LIMIT, items // PAGES_PER_QUIZ_RATIO))
            show_generate_btn = len(cached_quizzes) < max_allowed
            
            questions_count = len(cached_quizzes[0]["quiz_data"])
            cost = calculate_cached_points_cost(items, questions_count, is_album)
            
            await state.update_data(**common_state, available_quizzes=cached_quizzes, cache_cost=cost, max_allowed_quizzes=max_allowed)
            await state.set_state(QuizState.waiting_for_cache_decision)
            
            from keyboards import get_multiple_quizzes_keyboard
            keyboard = get_multiple_quizzes_keyboard(cached_quizzes, cost, show_generate_btn=show_generate_btn)
            
            msg_text = f"💡 <b>ملاحظة ذكية: تم العثور على ({len(cached_quizzes)}) كويز جاهز مخزن لهذا الملف مسبقاً!</b>\n\n"
            if not show_generate_btn:
                msg_text += f"🛑 <b>تم استنفاد الحد الأقصى للتنوع المسموح به لحجم هذا الملف وهو ({max_allowed}) كويزات.</b>\n"
                msg_text += "يرجى حل أحد الاختبارات الجاهزة المتوفرة في القائمة بالأسفل لتوفير رصيدك حماية للجودة:"
            else:
                msg_text += f"يمكنك اختيار كويز جاهز بخصم 90% (يكلف {cost:.2f} نقطة)، أو توليد كويز جديد تماماً بأفكار مختلفة (المتاح لحجم ملفك: {len(cached_quizzes)} من {max_allowed}):"
                
            await message.answer(msg_text, parse_mode="HTML", reply_markup=keyboard)
            return

        await state.update_data(**common_state)
        await state.set_state(QuizState.waiting_for_count)
        await message.answer(SUCCESS_MEDIA_RECEIVED, reply_markup=_cancel_keyboard())
    except Exception as exc:
        for path in file_paths: safe_file_cleanup(path)
        log_error(logger, f"Finalize media failed: {exc}", exception=exc)
        await message.answer("❌ حدث خطأ غير متوقع أثناء معالجة الوسائط.")

# ==================== Main Handlers ====================

@router.message(F.document | F.photo)
async def handle_media(message: types.Message, state: FSMContext) -> None:
    try:
        current_state = await state.get_state()
        if current_state == QuizState.answering_quiz:
            await message.answer("⚠️ لديك اختبار قائم حالياً؛ أتممه أو أوقفه قبل رفع محتوى جديد.")
            return

        if current_state in PENDING_REQUEST_STATES:
            removed = await _discard_pending_upload(state)
            await state.clear()
            if removed:
                await message.answer(MSG_PREVIOUS_REQUEST_REPLACED)

        ensure_directory_exists(DOWNLOADS_DIR)

        if message.photo:
            photo = message.photo[-1]
            current = {
                "file_id": photo.file_id,
                "file_unique_id": photo.file_unique_id,
                "file_size": photo.file_size,
            }
            
            if message.media_group_id:
                group_id = message.media_group_id
                list_key = f"album_list:{group_id}"
                lock_key = f"album_lock:{group_id}"
                
                await redis_client.rpush(list_key, json.dumps(current))
                await redis_client.expire(list_key, 30)
                
                is_coordinator = await redis_client.set(lock_key, "1", nx=True, ex=15)
                if not is_coordinator:
                    return 
                
                await message.answer("📥 جارٍ تجميع الصور ومعالجة الألبوم سياقياً بالخلفية...")
                asyncio.create_task(process_album_background(message, state))
                return
            else:
                photos = [current]
                file_paths = await _download_photos(message, photos)
                if not file_paths: return
                is_album = False
                title = "كويز من صورة"
                items = 1
                file_hash = await asyncio.to_thread(_combined_hash, file_paths)
                await _finalize_media_processing(message, state, file_paths, title, items, is_album, file_hash)

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
            await _finalize_media_processing(message, state, file_paths, title, items, is_album, file_hash)

    except Exception as exc:
        log_error(logger, f"Media handling failed: {exc}", exception=exc)
        await message.answer("❌ حدث خطأ غير متوقع أثناء معالجة الوسائط.")


@router.message(StateFilter(None), F.text, ~F.text.startswith("/"))
async def handle_pure_text(message: types.Message, state: FSMContext) -> None:
    text = message.text.strip()
    if len(text) < 30:
        await message.answer("⚠️ النص قصير جداً؛ اعمد إلى إرسال 30 حرفاً على الأقل لضمان صياغة أسئلة دقيقة.")
        return
    if len(text) > MAX_TEXT_INPUT_SIZE:
        await message.answer(f"❌ الحد الأقصى للنص المباشر هو {MAX_TEXT_INPUT_SIZE} حرفاً.")
        return
    await state.update_data(pure_text=text, source_title=text[:20] + "...", input_type="text", items_count=1, is_album=False)
    await state.set_state(QuizState.waiting_for_count)
    asyncio.create_task(log_usage_event(message.from_user.id, "content_uploaded", {
        "content_type": "text",
        "text_length": len(text),
    }))
    await message.answer("✅ تم استقبال النص بنجاح. كم سؤالاً تريد توليده من هذا المحتوى؟", reply_markup=_cancel_keyboard())


@router.callback_query(F.data == "cancel_upload_request")
async def handle_cancel_upload(call: types.CallbackQuery, state: FSMContext) -> None:
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
    await _discard_pending_upload(state)
    await state.clear()
    await message.answer(MSG_REQUEST_CANCELLED)


@router.callback_query(QuizState.waiting_for_cache_decision, F.data.startswith("use_multi_"))
async def handle_multi_cache_selection(call: types.CallbackQuery, state: FSMContext) -> None:
    try:
        quiz_uuid = call.data.replace("use_multi_", "")
        data = await state.get_data()
        cost = float(data["cache_cost"])
        
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

        from handlers.execution import _start_loaded_quiz
        await _start_loaded_quiz(call.message, state, selected_quiz["quiz_data"], data["source_title"], origin="cached_file", quiz_id=quiz_uuid)
        
        for path in data.get("file_paths", []):
            safe_file_cleanup(path)
    except Exception as exc:
        log_error(logger, f"Multi-cached selection trigger failed: {exc}")
        await call.message.answer("❌ تعذر بدء تشغيل الاختبار المخزّن.")
    finally:
        await call.answer()


@router.callback_query(QuizState.waiting_for_cache_decision, F.data == "cache_action_no")
async def handle_cache_no(call: types.CallbackQuery, state: FSMContext) -> None:
    await state.set_state(QuizState.waiting_for_count)
    await call.message.edit_text("📝 كم سؤالاً تريد استخراجه وتوليده من هذا المحتوى؟", reply_markup=_cancel_keyboard())
    await call.answer()


@router.message(QuizState.waiting_for_count, F.text.isdigit())
async def process_count(message: types.Message, state: FSMContext) -> None:
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
    asyncio.create_task(log_usage_event(message.from_user.id, "quiz_generation_requested", {
        "items": items, "questions": count, "mode": mode, "cost": cost, "is_album": is_album,
    }))
    if mode == "Super-Processing":
        await message.answer(MSG_SUPER_PROCESSING_ALERT)
    await trigger_quiz_generation(message, message.from_user.id, count, state)


@router.message(QuizState.waiting_for_count)
async def process_count_invalid(message: types.Message) -> None:
    await message.answer(
        "⚠️ <b>الرجاء إرسال رقم صحيح لعدد الأسئلة!</b>\n\nأو اعمد إلى استخدام زر التراجع أدناه لإلغاء العملية الحالية بشكل نظيف وعادل.",
        reply_markup=_cancel_keyboard(),
        parse_mode="HTML"
    )


async def trigger_quiz_generation(message: types.Message, user_id: int, count: int, state: FSMContext) -> None:
    async with processing_users_lock:
        if user_id in processing_users:
            await message.answer("⏳ طلبك قيد المعالجة حالياً بالخلفية.")
            return
        processing_users.add(user_id)
    status_message = await message.answer(MSG_PROCESSING)
    asyncio.create_task(_run_quiz_flow(message, user_id, count, state, status_message))


async def _run_quiz_flow(message: types.Message, user_id: int, count: int, state: FSMContext, status_message: types.Message) -> None:
    data: Dict[str, Any] = {}
    try:
        data = await state.get_data()
        is_media = data.get("input_type") == "media"
        file_hash = data.get("file_hash")
        file_paths = data.get("file_paths", []) or []
        pure_text = data.get("pure_text")

        # 🟢 [تحديث حيوي]: فحص استخراج النص محلياً لملفات Word / PPTX / TXT قبل التوليد
        if is_media and file_paths:
            first_path = file_paths[0]
            ext = os.path.splitext(first_path)[1].lower()
            if ext in [".docx", ".doc", ".pptx", ".ppt", ".txt"]:
                extracted_text = await asyncio.to_thread(extract_text_from_file, first_path)
                if extracted_text and len(extracted_text.strip()) >= 30:
                    pure_text = extracted_text
                    is_media = False  # تحويل المسار لتوليد نصي مباشر لتفادي رفع الملف وتفادي خطأ MIME
                else:
                    await _refund_after_failure(user_id, data)
                    await state.set_state(None)
                    await status_message.edit_text(
                        "⚠️ <b>تعذر استخراج نص مفيد من المستند!</b>\n\nيرجى التأكد من أن ملف Word/PowerPoint يحتوي على نصوص قابلة للقراءة وليس صوراً فقط. تم إرجاع نقاطك بالكامل.",
                        parse_mode="HTML"
                    )
                    return

        # ميزة منع التكرار: سحب الأسئلة السابقة وتمريرها لحقنها في الـ Prompt
        previous_questions = []
        existing_uuids = set()
        if file_hash:
            old_quizzes = await get_file_quizzes(file_hash)
            for qz in old_quizzes:
                existing_uuids.add(str(qz["id"]))
                if "quiz_data" in qz and isinstance(qz["quiz_data"], list):
                    previous_questions.extend(qz["quiz_data"])
                    
        quiz_data = await generate_quiz_smart(
            file_paths=file_paths if is_media else None,
            pure_text=pure_text if not is_media else None,
            count=count,
            skip_cache=True,
            file_hash=file_hash,
            status_message=status_message,
            previous_questions=previous_questions if previous_questions else None,
        )
        if not quiz_data:
            await _refund_after_failure(user_id, data)
            await state.set_state(None)  
            await status_message.edit_text(
                "⚠️ <b>فشل توليد الأسئلة الأكاديمية!</b>\n\nلم يتمكن محرك الذكاء الاصطناعي من قراءة تفاصيل الملف، رصيدك آمن بالكامل ولم يتم خصم أي نقاط منه.",
                parse_mode="HTML"
            )
            return

        new_quiz_id = None
        if file_hash:
            await asyncio.sleep(0.5)
            updated_quizzes = await get_file_quizzes(file_hash)
            for uq in updated_quizzes:
                if str(uq["id"]) not in existing_uuids:
                    new_quiz_id = str(uq["id"])
                    break

        asyncio.create_task(log_usage_event(user_id, "quiz_generated", {
            "quiz_id": new_quiz_id,
            "questions": len(quiz_data),
            "source": "file" if is_media else "text",
        }))

        from handlers.execution import _start_loaded_quiz
        await _start_loaded_quiz(message, state, quiz_data, data.get("source_title", "كويز"), origin="file" if is_media else "text", quiz_id=new_quiz_id)
        await status_message.delete()
    except Exception as exc:
        log_error(logger, f"Quiz flow failed: {exc}", exception=exc)
        await _refund_after_failure(user_id, data)
        await state.set_state(None) 
        await status_message.edit_text(
            "⚠️ <b>المعذرة، واجهنا خطأ تقنياً مفاجئاً أثناء بناء الكويز.</b>\n\nتم إعادة شحن رصيدك تلقائياً دون خصم أي نقاط، يرجى تكرار المحاولة.",
            parse_mode="HTML"
        )
    finally:
        for path in data.get("file_paths", []):
            safe_file_cleanup(path)
        async with processing_users_lock:
            processing_users.discard(user_id)


async def _refund_after_failure(user_id: int, data: Dict[str, Any]) -> None:
    cost = float(data.get("debited_cost") or 0)
    if cost > 0:
        await refund_user_points(user_id, cost)

files_router = router