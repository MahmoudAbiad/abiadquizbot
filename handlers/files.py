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
    MAX_FILE_QUIZZES_LIMIT, MIN_QUIZZES_PER_FILE, MSG_MAX_QUIZZES_REACHED,
    MSG_ENGLISH_CONTENT_DETECTED, SUBJECT_ENGLISH, SUBJECT_OTHER,
    QUESTION_TYPE_GENERAL, DIFFICULTY_MEDIUM, MSG_QUIZ_OPTIONS_PROMPT,
)
from gemini_helper import get_pdf_page_count_sync
from helpers.points_calculator import calculate_cached_points_cost, calculate_quiz_points_cost
from keyboards import (
    get_generation_confirm_keyboard, get_multiple_quizzes_keyboard, get_question_count_quick_keyboard,
    get_translation_choice_keyboard, get_quiz_options_keyboard
)
from logger import get_logger, log_error
from supabase_helper import (
    check_or_add_user, get_file_quizzes, update_user_stats, log_usage_event, mark_quiz_attempt_stopped
)
from utils import calculate_file_hash, ensure_directory_exists, safe_file_cleanup
from validators import validate_file_size, validate_question_count

# استيراد الخدمات الجديدة
from services.file_service import compute_combined_hash, download_photos_service, extract_office_text_if_needed
from services.subject_classifier import classify_subject
from services.quiz_service import (
    determine_execution_mode, build_transparency_text, refund_user_on_failure, execute_quiz_generation_workflow,
    combo_quiz_count,
)

logger = get_logger(__name__)
router = Router()
DOWNLOADS_DIR = "downloads"
processing_users_lock = asyncio.Lock()
processing_users: set[int] = set()

PENDING_REQUEST_STATES = (
    QuizState.waiting_for_count, 
    QuizState.waiting_for_cache_decision, 
    QuizState.waiting_for_generation_confirm,
    QuizState.waiting_for_translation_choice,
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
        f"⚠️ العجز المطلوب شحنه: <b>{deficit:.2f} نقطة</b>\n\n"
        # 🩹 UX: أهم لحظة لذكر التجديد اليومي المجاني — الطالب هنا على وشك اتخاذ قرار
        # الدفع، ومن حقه يعرف أن لديه بديلاً مجانياً إن لم يكن مستعجلاً.
        f"💡 <b>تذكير:</b> نقاطك المجانية تتجدد تلقائياً كل يوم بـ <b>{DAILY_RENEWAL_POINTS} نقطة</b> — "
        "إن لم تكن مستعجلاً يمكنك الانتظار لتجديد الغد بدل الشحن الآن.",
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

async def _show_question_count_screen(reply_target: types.Message, state: FSMContext, edit: bool = False) -> None:
    """
    🆕 الخطوة الأخيرة المشتركة: عرض شاشة "كم سؤالاً تريد؟" باستخدام count_prompt_text
    المخزَّن مسبقاً بالحالة (وُضع هناك من _ask_question_count عند نقطة الدخول الأصلية).
    تُستدعى من نهاية شاشة خيارات نوع/صعوبة الأسئلة (handlers/quiz_options.py) بدل تكرار
    نفس منطق set_state/edit_text في مكانين مختلفين.
    """
    data = await state.get_data()
    count_prompt_text = data.get(
        "count_prompt_text",
        "📝 كم سؤالاً تريد استخراجه وتوليده من هذا المحتوى؟\nاختر من الأزرار أدناه، أو أرسل رقماً مخصصاً مباشرة.",
    )
    await state.set_state(QuizState.waiting_for_count)
    if edit:
        await reply_target.edit_text(count_prompt_text, reply_markup=get_question_count_quick_keyboard())
    else:
        await reply_target.answer(count_prompt_text, reply_markup=get_question_count_quick_keyboard())


async def _show_quiz_options_screen(reply_target: types.Message, state: FSMContext, edit: bool = False) -> None:
    """
    🆕 يعرض شاشة اختيار نوع الأسئلة + الصعوبة (رسالة واحدة تُحدَّث بالمكان لاحقاً مع كل
    ضغطة زر عبر handlers/quiz_options.py). تُستدعى بعد التصنيف مباشرة للمواد غير
    الإنجليزية، أو بعد اختيار الترجمة لمحتوى إنجليزي (subject == "english").
    """
    data = await state.get_data()
    await state.set_state(QuizState.waiting_for_quiz_options)
    keyboard = get_quiz_options_keyboard(
        subject_type=data.get("subject_type", SUBJECT_OTHER),
        suggested_types=data.get("suggested_question_types", []),
        selected_type=data.get("question_type", QUESTION_TYPE_GENERAL),
        selected_difficulty=data.get("difficulty", DIFFICULTY_MEDIUM),
    )
    if edit:
        await reply_target.edit_text(MSG_QUIZ_OPTIONS_PROMPT, parse_mode="HTML", reply_markup=keyboard)
    else:
        await reply_target.answer(MSG_QUIZ_OPTIONS_PROMPT, parse_mode="HTML", reply_markup=keyboard)


async def _ask_question_count(reply_target: types.Message, state: FSMContext, count_prompt_text: str, edit: bool = False) -> None:
    """
    🆕 نقطة موحّدة تبدأ سلسلة الشاشات التالية للاستقبال (استقبال وسائط جديدة، استقبال نص
    مباشر، أو رفض الكاش واختيار توليد كويز جديد): تصنيف موحّد → [ترجمة إن كان إنجليزي] →
    شاشة نوع/صعوبة الأسئلة → شاشة عدد الأسئلة. count_prompt_text يُخزَّن بالحالة ليُستخدم
    بالخطوة الأخيرة (_show_question_count_screen) بعد كل الشاشات الوسيطة.
    """
    await state.update_data(count_prompt_text=count_prompt_text)
    data = await state.get_data()
    is_media = data.get("input_type") == "media"
    file_paths = data.get("file_paths", []) or []
    pure_text = data.get("pure_text")

    # 🆕 مستندات الأوفيس (.docx/.pptx/.txt) بحاجة استخراج نص أولاً قبل أي فحص لغوي، لأن
    # classify_subject يفحص فقط PDF/صور مباشرة أو نصاً صريحاً - وليس ملفات أوفيس خام.
    # نستخرج النص هنا مبكراً (عملية محلية خفيفة، بلا أي استدعاء AI) ونخزّنه بحالة الـ FSM
    # ليُعاد استخدامه لاحقاً في services/quiz_service.py بدل إعادة استخراجه من الصفر.
    detection_text, detection_paths = pure_text, (file_paths if is_media else None)
    if is_media and file_paths:
        ext = os.path.splitext(file_paths[0])[1].lower()
        if ext in [".docx", ".doc", ".pptx", ".ppt", ".txt"]:
            extracted_text, is_valid = await extract_office_text_if_needed(file_paths[0])
            if is_valid and extracted_text:
                await state.update_data(cached_office_text=extracted_text)
                detection_text, detection_paths = extracted_text, None
            else:
                detection_text, detection_paths = None, None  # سيُعاد اكتشاف الخطأ لاحقاً كالمعتاد أثناء التوليد

    # 🆕 استدعاء واحد فقط موحّد (services/subject_classifier.py) يحل محل الاستدعاءين
    # المنفصلين السابقين (فحص إنجليزي هنا + فحص رياضيات لاحقاً بـ quiz_service.py).
    # النتيجة تُخزَّن بحالة الـ FSM وتُعاد قراءتها لاحقاً وقت التوليد الفعلي بدل إعادة
    # الفحص من جديد على نفس المحتوى (راجع execute_quiz_generation_workflow).
    try:
        classification = await classify_subject(detection_paths, detection_text)
    except Exception as exc:
        log_error(logger, f"Subject classification failed, continuing with standard mode: {exc}")
        from services.subject_classifier import SubjectClassification
        classification = SubjectClassification(subject=SUBJECT_OTHER, suggested_types=[])

    # 🆕 نخزّن نتيجة التصنيف + قيماً افتراضية لنوع/صعوبة الأسئلة (تُستبدل لاحقاً باختيار
    # الطالب الفعلي عبر شاشة الخيارات handlers/quiz_options.py؛ الافتراضي "عام/متوسط"
    # يبقى صالحاً لو ضغط الطالب "متابعة" مباشرة بدون أي تخصيص).
    # تحديد النوع الافتراضي: إذا كان إنجليزي نجعله general_test تلقائياً لمنع التكرار
    default_question_type = "general_test" if classification.subject == SUBJECT_ENGLISH else QUESTION_TYPE_GENERAL

    await state.update_data(
        subject_type=classification.subject,
        suggested_question_types=classification.suggested_types,
        question_type=default_question_type,
        custom_question_type_text=None,
        difficulty=DIFFICULTY_MEDIUM,
    )

    if classification.subject == SUBJECT_ENGLISH:
        await state.set_state(QuizState.waiting_for_translation_choice)
        if edit:
            await reply_target.edit_text(MSG_ENGLISH_CONTENT_DETECTED, parse_mode="HTML", reply_markup=get_translation_choice_keyboard())
        else:
            await reply_target.answer(MSG_ENGLISH_CONTENT_DETECTED, parse_mode="HTML", reply_markup=get_translation_choice_keyboard())
        return

    await state.update_data(english_mode=None)
    await _show_quiz_options_screen(reply_target, state, edit=edit)

async def _render_cache_decision_screen(reply_target: types.Message, state: FSMContext, edit: bool = False) -> None:
    """
    🆕 يعرض/يحدّث شاشة "قرار الكاش" (الكويزات المخزّنة + أزرار الفلترة) اعتماداً على
    available_quizzes وقيم الفلتر الحالية (cache_filter_type/cache_filter_difficulty)
    المخزّنة بحالة الـ FSM. تُستدعى مرة أولى من _finalize_media_processing (edit=False)،
    وبعدها من معالجات الفلترة أدناه مع كل ضغطة فلتر (edit=True، بنفس الرسالة).
    """
    data = await state.get_data()
    all_quizzes = data.get("available_quizzes", [])
    filter_type = data.get("cache_filter_type", "all")
    filter_difficulty = data.get("cache_filter_difficulty", "all")

    filtered_quizzes = [
        q for q in all_quizzes
        if (filter_type == "all" or (q.get("question_type") or "general") == filter_type)
        and (filter_difficulty == "all" or (q.get("difficulty") or "medium") == filter_difficulty)
    ]

    max_allowed = data.get("max_allowed_quizzes", MAX_FILE_QUIZZES_LIMIT)
    existing_combo_count = combo_quiz_count(
        all_quizzes, data.get("subject_type", "other"),
        data.get("question_type", "general"), data.get("difficulty", "medium"),
    )
    show_generate_btn = existing_combo_count < max_allowed
    cost = data.get("cache_cost", 0)

    keyboard = get_multiple_quizzes_keyboard(
        all_quizzes, filtered_quizzes, cost, show_generate_btn=show_generate_btn,
        filter_type=filter_type, filter_difficulty=filter_difficulty,
    )
    msg_text = f"💡 <b>ملاحظة ذكية: تم العثور على ({len(all_quizzes)}) كويز جاهز مخزن لهذا الملف مسبقاً!</b>\n\n"
    if filter_type != "all" or filter_difficulty != "all":
        msg_text += f"🔍 يُعرض حالياً: ({len(filtered_quizzes)}) كويز مطابق للفلتر المختار.\n\n"
    msg_text += f"🛑 <b>تم استنفاد الحد الأقصى ({max_allowed}) كويزات لهذا النوع من الأسئلة.</b>" if not show_generate_btn else f"يمكنك اختيار كويز جاهز بخصم 90% ({cost:.2f} نقطة)، أو توليد كويز جديد:"

    if edit:
        await reply_target.edit_text(msg_text, parse_mode="HTML", reply_markup=keyboard)
    else:
        await reply_target.answer(msg_text, parse_mode="HTML", reply_markup=keyboard)


@router.callback_query(QuizState.waiting_for_cache_decision, F.data.startswith("cachefilter_type_"))
async def handle_cache_filter_type(call: types.CallbackQuery, state: FSMContext) -> None:
    """🆕 فلترة الكويزات المعروضة بالكاش حسب نوع الأسئلة (محلياً، بدون أي استعلام إضافي)."""
    value = call.data.replace("cachefilter_type_", "", 1)
    await state.update_data(cache_filter_type=value)
    try:
        await _render_cache_decision_screen(call.message, state, edit=True)
    except Exception:
        pass  # "message is not modified" لو نفس الفلتر ضُغط مرتين متتاليتين - آمن التجاهل
    await call.answer()


@router.callback_query(QuizState.waiting_for_cache_decision, F.data.startswith("cachefilter_diff_"))
async def handle_cache_filter_difficulty(call: types.CallbackQuery, state: FSMContext) -> None:
    """🆕 فلترة الكويزات المعروضة بالكاش حسب الصعوبة (محلياً، بدون أي استعلام إضافي)."""
    value = call.data.replace("cachefilter_diff_", "", 1)
    await state.update_data(cache_filter_difficulty=value)
    try:
        await _render_cache_decision_screen(call.message, state, edit=True)
    except Exception:
        pass
    await call.answer()


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
            # 🆕 تُعرض كل الكويزات المخزّنة دائماً (بغض النظر عن نوعها/صعوبتها)، لكن زر
            # "توليد جديد" يُحجب فقط لو التركيبة الافتراضية الحالية (عام/متوسط، بانتظار
            # شاشة الاختيار المخصصة) وصلت لسقفها المستقل هي تحديداً - وليس بسبب امتلاء
            # الكاش الإجمالي بتركيبات أخرى مختلفة عنها.
            max_allowed = max(MIN_QUIZZES_PER_FILE, min(MAX_FILE_QUIZZES_LIMIT, items // PAGES_PER_QUIZ_RATIO))
            data_now = await state.get_data()
            existing_combo_count = combo_quiz_count(
                cached_quizzes, data_now.get("subject_type", "other"),
                data_now.get("question_type", "general"), data_now.get("difficulty", "medium"),
            )
            show_generate_btn = existing_combo_count < max_allowed
            cost = calculate_cached_points_cost(items, len(cached_quizzes[0]["quiz_data"]), is_album)
            
            await state.update_data(
                **common_state, available_quizzes=cached_quizzes, cache_cost=cost, max_allowed_quizzes=max_allowed,
                cache_filter_type="all", cache_filter_difficulty="all",
            )
            await state.set_state(QuizState.waiting_for_cache_decision)
            await _render_cache_decision_screen(message, state, edit=False)
            return

        await state.update_data(**common_state)
        await _ask_question_count(message, state, SUCCESS_MEDIA_RECEIVED)
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

    # إصلاح التتبع: تسجيل حدث رفع النص المباشر
    asyncio.create_task(log_usage_event(message.from_user.id, "content_uploaded", {
        "content_type": "text", "text_length": len(text)
    }))

    await state.update_data(pure_text=text, source_title=text[:20] + "...", input_type="text", items_count=1, is_album=False)
    await _ask_question_count(
        message, state,
        "✅ تم استقبال النص بنجاح. كم سؤالاً تريد توليده من هذا المحتوى؟\nاختر من الأزرار أدناه، أو أرسل رقماً مخصصاً مباشرة."
    )

# ==================== 🆕 معالجات اختيار "مترجمة/بدون ترجمة" للمحتوى الإنجليزي ====================

async def _apply_translation_choice(reply_target: types.Message, state: FSMContext, english_mode: str, edit: bool) -> None:
    """يحفظ اختيار الطالب (مترجمة/بدون ترجمة) بالحالة، ثم ينتقل لشاشة نوع/صعوبة الأسئلة
    (قوائم "قواعد/قراءة/اختبار عام" الخاصة بمادة الإنجليزي - راجع QUESTION_TYPE_OPTIONS)."""
    await state.update_data(english_mode=english_mode)
    await _show_quiz_options_screen(reply_target, state, edit=edit)

@router.callback_query(QuizState.waiting_for_translation_choice, F.data == "translate_choice_yes")
async def handle_translate_choice_yes(call: types.CallbackQuery, state: FSMContext) -> None:
    """الطالب اختار الأسئلة مترجمة (إنجليزي + عربي)"""
    try:
        await _apply_translation_choice(call.message, state, "translated", edit=True)
    except Exception as exc:
        log_error(logger, f"Translation choice (yes) failed: {exc}", exception=exc)
        await call.message.answer("❌ تعذر تنفيذ الاختيار، حاول مجدداً.", reply_markup=get_translation_choice_keyboard())
    finally:
        await call.answer()

@router.callback_query(QuizState.waiting_for_translation_choice, F.data == "translate_choice_no")
async def handle_translate_choice_no(call: types.CallbackQuery, state: FSMContext) -> None:
    """الطالب اختار الأسئلة إنجليزية فقط بدون ترجمة"""
    try:
        await _apply_translation_choice(call.message, state, "plain", edit=True)
    except Exception as exc:
        log_error(logger, f"Translation choice (no) failed: {exc}", exception=exc)
        await call.message.answer("❌ تعذر تنفيذ الاختيار، حاول مجدداً.", reply_markup=get_translation_choice_keyboard())
    finally:
        await call.answer()

# ==================== معالجات قرار الكاش والأزرار المتعددة ====================

@router.callback_query(QuizState.waiting_for_cache_decision, F.data.startswith("use_multi_"))
async def handle_multi_cache_selection(call: types.CallbackQuery, state: FSMContext) -> None:
    """معالج تشغيل أحد الكويزات الجاهزة المخزنة بالجدول المركزي"""
    await call.answer()
    try:
        quiz_uuid = call.data.replace("use_multi_", "")
        data = await state.get_data()
        cost = float(data.get("cache_cost", 0))
        
        user_info = await _current_user(call.message, call.from_user)
        await _renewal_notice(call.message, user_info)
        
        available_quizzes = data.get("available_quizzes", [])
        selected_quiz = next((q for q in available_quizzes if str(q["id"]) == quiz_uuid), None)
        if not selected_quiz:
            await call.message.answer("❌ عذراً، لم نتمكن من جلب الكويز المختار.")
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
        await _start_loaded_quiz(call, state, selected_quiz["quiz_data"], data.get("source_title", "كويز"), origin="cached_file", quiz_id=quiz_uuid)
        
        for path in data.get("file_paths", []):
            safe_file_cleanup(path)
    except Exception as exc:
        log_error(logger, f"Multi-cached selection trigger failed: {exc}", exception=exc)
        await call.message.answer("❌ تعذر بدء تشغيل الاختبار المخزّن.")

@router.callback_query(QuizState.waiting_for_cache_decision, F.data == "cache_action_no")
async def handle_cache_no(call: types.CallbackQuery, state: FSMContext) -> None:
    """في حال رفض الكاش ورغبة الطالب بتوليد كويز جديد كلياً"""
    await _ask_question_count(
        call.message, state,
        "📝 كم سؤالاً تريد استخراجه وتوليده من هذا المحتوى؟\nاختر من الأزرار أدناه، أو أرسل رقماً مخصصاً مباشرة.",
        edit=True
    )
    await call.answer()

# ==================== معالجات تحديد الأسئلة والتأكيد ====================

async def _process_question_count(reply_target: types.Message, state: FSMContext, user, count: int) -> None:
    """المنطق المشترك للتحقق من عدد الأسئلة وحساب التكلفة وعرض شاشة التأكيد،
    يُستدعى سواء أرسل الطالب رقماً كتابة أو ضغط أحد أزرار الاختيار السريع."""
    valid, error = validate_question_count(count)
    if not valid:
        await reply_target.answer(f"❌ {error}", reply_markup=get_question_count_quick_keyboard())
        return

    data = await state.get_data()
    items = int(data.get("items_count") or 1)
    is_album = bool(data.get("is_album"))
    file_hash = data.get("file_hash")

    if file_hash:
        # 🆕 السقف يُحسب الآن لكل تركيبة (نوع مادة × نوع أسئلة × صعوبة) على حدة بدل
        # سقف مشترك لكل الكويزات المخزنة للملف بغض النظر عن نوعها - راجع combo_quiz_count.
        current_quizzes = await get_file_quizzes(file_hash)
        max_allowed = max(MIN_QUIZZES_PER_FILE, min(MAX_FILE_QUIZZES_LIMIT, items // PAGES_PER_QUIZ_RATIO))
        existing_combo_count = combo_quiz_count(
            current_quizzes, data.get("subject_type", "other"),
            data.get("question_type", "general"), data.get("difficulty", "medium"),
        )
        if existing_combo_count >= max_allowed:
            await reply_target.answer(MSG_MAX_QUIZZES_REACHED, parse_mode="HTML")
            await state.clear()
            return

    cost = calculate_quiz_points_cost(items, count, is_album)
    mode = determine_execution_mode(items, count)

    user_info = await _current_user(reply_target, user)

    if float(user_info["points"]) < cost:
        await _insufficient_balance(reply_target, user_info, cost)
        return

    # إصلاح التتبع: تسجيل حدث طلب التوليد
    asyncio.create_task(log_usage_event(user.id, "quiz_generation_requested", {
        "requested_count": count, "items_count": items, "cost": cost, "mode": mode
    }))

    await state.update_data(calculated_cost=cost, requested_count=count, execution_mode=mode)
    await state.set_state(QuizState.waiting_for_generation_confirm)

    confirm_kb = get_generation_confirm_keyboard()

    confirm_text = (
        f"{build_transparency_text(items, count, mode, cost)}\n\n"
        f"❓ <b>هل تؤكد بدء التوليد وخصم {cost:.2f} نقطة من رصيدك؟</b>"
    )
    if mode == "Super-Processing":
        confirm_text += f"\n\n{MSG_SUPER_PROCESSING_ALERT}"

    await reply_target.answer(confirm_text, reply_markup=confirm_kb, parse_mode="HTML")

@router.message(QuizState.waiting_for_count, F.text.isdigit())
async def process_count(message: types.Message, state: FSMContext) -> None:
    """معالج إدخال عدد الأسئلة المطلوبة كتابةً والتحقق من التكلفة وإظهار رسالة التأكيد"""
    await _process_question_count(message, state, message.from_user, int(message.text))

@router.callback_query(QuizState.waiting_for_count, F.data.startswith("qcount_"))
async def process_count_quick_select(call: types.CallbackQuery, state: FSMContext) -> None:
    """معالج الضغط على أحد أزرار الاختيار السريع لعدد الأسئلة (5/10/15/20)"""
    try:
        count = int(call.data.replace("qcount_", ""))
        try:
            await call.message.delete()
        except Exception:
            pass
        await _process_question_count(call.message, state, call.from_user, count)
    except Exception as exc:
        log_error(logger, f"Quick count selection failed: {exc}", exception=exc)
        await call.message.answer("❌ تعذر تنفيذ الاختيار، أرسل الرقم يدوياً من فضلك.", reply_markup=get_question_count_quick_keyboard())
    finally:
        await call.answer()

@router.message(QuizState.waiting_for_count)
async def process_count_invalid(message: types.Message) -> None:
    """معالج إدخال قيمة غير رقمية لعدد الأسئلة"""
    await message.answer(
        "⚠️ <b>الرجاء إرسال رقم صحيح لعدد الأسئلة!</b>\n\nيمكنك اختيار أحد الأزرار أدناه، أو استخدام زر التراجع لإلغاء العملية الحالية بشكل نظيف وعادل.",
        reply_markup=get_question_count_quick_keyboard(),
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
        
        # إصلاح التتبع: تسجيل حدث إلغاء الطلب
        asyncio.create_task(log_usage_event(call.from_user.id, "request_cancelled", {"cancelled_from_state": str(current_state)}))
        
        await _discard_pending_upload(state)
        await state.clear()
        try:
            await call.message.edit_text(MSG_REQUEST_CANCELLED)
        except Exception:
            await call.message.answer(MSG_REQUEST_CANCELLED)
        await call.answer()
    except Exception as exc:
        log_error(logger, f"Cancel request failed: {exc}", exception=exc)
        await call.answer("❌ تعذر إلغاء الطلب، حاول مجدداً.", show_alert=True)

@router.callback_query(QuizState.waiting_for_generation_confirm, F.data == "confirm_quiz_generation")
async def handle_confirm_quiz_generation(call: types.CallbackQuery, state: FSMContext) -> None:
    await call.answer()
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

        # إصلاح التتبع: تسجيل نجاح توليد الكويز الفعلي
        asyncio.create_task(log_usage_event(call.from_user.id, "quiz_generated", {
            "quiz_id": new_quiz_id, "questions_generated": len(quiz_data), "cost": cost
        }))

        from handlers.quiz_runner import _start_loaded_quiz
        await _start_loaded_quiz(call, state, quiz_data, data.get("source_title", "كويز"), origin="file" if data.get("input_type") == "media" else "text", quiz_id=new_quiz_id)
        await status_msg.delete()

    except Exception as exc:
        log_error(logger, f"Confirm quiz generation failed: {exc}", exception=exc)
        await refund_user_on_failure(call.from_user.id, await state.get_data())
        await state.set_state(None)
        await call.message.answer("❌ حدث خطأ، تم إعادة شحن رصيدك تلقائياً.")
    finally:
        for path in (await state.get_data()).get("file_paths", []):
            safe_file_cleanup(path)

files_router = router