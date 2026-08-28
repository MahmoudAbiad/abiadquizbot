# Handlers/files.py
import asyncio
import json
import os
import uuid
from typing import Any, Dict, List, Optional

from aiogram import F, Router, types
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext

from config import QuizState, bot, redis_client
from settings_helper import get_setting
from constants import (
    ADMIN_CONTACT, BTN_CANCEL_REQUEST, ERROR_ALBUM_TOO_LARGE,
    MAX_ALBUM_IMAGES, MAX_SUPER_PAGES, MAX_TEXT_INPUT_SIZE, MSG_NOTHING_TO_CANCEL,
    MSG_PREVIOUS_REQUEST_REPLACED, MSG_PROCESSING, MSG_REQUEST_CANCELLED,
    MSG_SUPER_PROCESSING_ALERT, SUCCESS_MEDIA_RECEIVED, PAGES_PER_QUIZ_RATIO,
    MAX_FILE_QUIZZES_LIMIT, MIN_QUIZZES_PER_FILE, MSG_MAX_QUIZZES_REACHED,
    MSG_ENGLISH_CONTENT_DETECTED, SUBJECT_ENGLISH, SUBJECT_OTHER,
    QUESTION_TYPE_GENERAL, DIFFICULTY_MEDIUM, MSG_QUIZ_TYPE_PROMPT,
    MAX_DOC_SIZE, MAX_FILE_WEB_UPLOAD_SIZE, MAX_FILE_WEB_UPLOAD_PAGES,
    MAX_IMAGE_WEB_UPLOAD_COUNT, BTN_OPEN_UPLOAD_PAGE, MSG_REDIRECT_TO_WEB_UPLOAD,
    WEBAPP_PUBLIC_BASE_URL,
)
from gemini_helper import get_pdf_page_count_sync
from helpers.points_calculator import calculate_cached_points_cost, calculate_quiz_points_cost
from keyboards import (
    get_multiple_quizzes_keyboard, get_question_count_keyboard,
    get_translation_choice_keyboard, get_quiz_type_keyboard,
    get_web_upload_redirect_keyboard,
)
from logger import get_logger, log_error
from supabase_helper import (
    check_or_add_user, get_file_quizzes, update_user_stats, log_usage_event, mark_quiz_attempt_stopped,
    reward_referrer_if_eligible,
)
from r2_helper import delete_file_temp, delete_file_temp_batch, download_file_temp_to_file
from utils import calculate_file_hash, ensure_directory_exists, safe_file_cleanup
from validators import validate_file_size, validate_question_count

# استيراد الخدمات الجديدة
from services.file_service import compute_combined_hash, download_photos_service, extract_office_text_if_needed
from services.subject_classifier import classify_subject
from services.quiz_service import (
    determine_execution_mode, build_transparency_text, refund_user_on_failure, execute_quiz_generation_workflow,
    combo_quiz_count, build_question_type_label,
)
from handlers.audio import _build_state_for_chat  # 🆕 نفس بناء FSMContext اليدوي المستخدم لرفع الصوت عبر الويب

logger = get_logger(__name__)
router = Router()
DOWNLOADS_DIR = "downloads"
processing_users_lock = asyncio.Lock()
processing_users: set[int] = set()

PENDING_REQUEST_STATES = (
    QuizState.waiting_for_count, 
    QuizState.waiting_for_cache_decision, 
    QuizState.waiting_for_translation_choice,
    QuizState.waiting_for_quiz_options,       # 🆕
    QuizState.waiting_for_custom_question_type,  # 🆕
)

def _cancel_keyboard() -> types.InlineKeyboardMarkup:
    return types.InlineKeyboardMarkup(inline_keyboard=[[types.InlineKeyboardButton(text=BTN_CANCEL_REQUEST, callback_data="cancel_upload_request")]])

async def _discard_pending_upload(state: FSMContext) -> int:
    data = await state.get_data()
    file_paths = data.get("file_paths", []) or []
    removed = sum(1 for path in file_paths if safe_file_cleanup(path))
    return removed


async def _run_processing_heartbeat(status_msg: types.Message, interval_seconds: int = 20) -> None:
    """🩹 يحدّث رسالة "جاري المعالجة" دورياً بوقت منقضي واضح طول مدة انتظار Gemini
    (يمكن تطول جداً وقت الازدحام - راجع التعليق بمكان الاستدعاء). يُنهى فوراً عبر
    asyncio.CancelledError بمجرد ما يخلص التوليد (finally بمكان الاستدعاء) - لا داعي
    لأي شرط خروج آخر. أي فشل بالتعديل (رسالة محذوفة، rate limit...) غير حرج وتم تجاهله."""
    elapsed = 0
    try:
        while True:
            await asyncio.sleep(interval_seconds)
            elapsed += interval_seconds
            minutes = elapsed // 60
            seconds = elapsed % 60
            time_label = f"{minutes} دقيقة و{seconds} ثانية" if minutes else f"{seconds} ثانية"
            try:
                await status_msg.edit_text(
                    f"🤖 لسا عم نعالج المستند ونولّد الأسئلة... ({time_label})\n"
                    "الذكاء الاصطناعي مزدحم شوي حالياً، بس طلبك قيد التنفيذ فعلياً - ما في داعي "
                    "لإعادة إرسال نفس الملف."
                )
            except Exception:
                pass
    except asyncio.CancelledError:
        pass

async def _renewal_notice(message: types.Message, user_info: Dict[str, Any]) -> None:
    if user_info.get("status") == "renewed":
        # نستخدم free_points الفعلية المُطبَّقة من الدالة الذرية (وليس إعادة جلب الإعداد)
        # لضمان تطابق الرقم المعروض مع ما تمت إضافته فعلياً لحساب الطالب.
        applied_points = float(user_info.get("free_points") or 0)
        await message.answer(f"☀️ تم تجديد رصيدك اليومي إلى <b>{applied_points:.0f} نقطة مجانية</b>.", parse_mode="HTML")

async def _insufficient_balance(message: types.Message, user_info: Dict[str, Any], required: float) -> None:
    balance = float(user_info.get("points") or 0)
    deficit = max(0.0, required - balance)
    daily_renewal_points = await get_setting("daily_renewal_points")
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
        f"💡 <b>تذكير:</b> نقاطك المجانية تتجدد تلقائياً كل يوم بـ <b>{daily_renewal_points:.0f} نقطة</b> — "
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
            # 🆕 هاد سقف تيليجرام نفسه للألبوم (media group)، مش رقم اخترناه - نوجّه
            # الطالب لصفحة رفع ألبوم كبير (حتى 50 صورة سوا) بدل رفض جاف بلا بديل.
            keyboard = get_web_upload_redirect_keyboard("images")
            if keyboard.inline_keyboard:
                await message.answer(
                    f"{ERROR_ALBUM_TOO_LARGE}\n\nبس فيك ترفع حتى {MAX_IMAGE_WEB_UPLOAD_COUNT} صورة سوا عبر صفحة الويب:",
                    reply_markup=keyboard,
                )
            else:
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

DEFAULT_COUNT_SUGGESTIONS = [5, 10, 15, 20]


async def _render_question_count_screen(bot, chat_id: int, message_id: Optional[int], state: FSMContext, target_for_send: Optional[types.Message] = None) -> None:
    """
    🆕 يبني نص وكيبورد شاشة عدد الأسئلة المدمجة (اختيار العدد + التكلفة + زر البدء
    بنفس الشاشة) ويعرضها. لو message_id متوفر يُعدَّل نفس الرسالة (bot.edit_message_text)
    بغض النظر عن مصدر الحدث (ضغطة زر أو رسالة نصية بعدد مخصص) - هذا ما يسمح بدمج
    مسار "الكتابة اليدوية لعدد مخصص" بنفس شاشة الأزرار السريعة بدل رسالة منفصلة.
    لو مافي message_id بعد (أول عرض)، تُرسَل رسالة جديدة عبر target_for_send وتُخزَّن
    معرّفاتها بالحالة ليُعاد استخدامها بكل التحديثات اللاحقة.
    """
    data = await state.get_data()
    items = int(data.get("items_count") or 1)
    is_album = bool(data.get("is_album"))
    selected_count = int(data.get("selected_question_count") or DEFAULT_COUNT_SUGGESTIONS[1])
    count_prompt_text = data.get(
        "count_prompt_text",
        "📝 كم سؤالاً تريد استخراجه وتوليده من هذا المحتوى؟",
    )

    mode = determine_execution_mode(items, selected_count)
    cost = calculate_quiz_points_cost(items, selected_count, is_album)
    difficulty = data.get("difficulty", DIFFICULTY_MEDIUM)
    question_type_label = build_question_type_label(
        data.get("subject_type", "other"), data.get("question_type", QUESTION_TYPE_GENERAL),
        data.get("custom_question_type_text"), data.get("suggested_question_types", []),
    )

    text = f"{count_prompt_text}\n\n{build_transparency_text(items, selected_count, mode, cost, difficulty, question_type_label)}"
    if mode == "Super-Processing":
        text += f"\n\n{MSG_SUPER_PROCESSING_ALERT}"

    keyboard = get_question_count_keyboard(items, is_album, selected_count, DEFAULT_COUNT_SUGGESTIONS)

    if message_id:
        try:
            await bot.edit_message_text(text, chat_id=chat_id, message_id=message_id, reply_markup=keyboard, parse_mode="HTML")
            await state.update_data(count_screen_chat_id=chat_id, count_screen_message_id=message_id)
            return
        except Exception as exc:
            # 🩹 إصلاح ثغرة: تيليجرام يرفض التعديل بخطأ "message is not modified" لو
            # الطالب ضغط نفس الخيار المحدد مرتين متتاليتين (المحتوى الجديد مطابق حرفياً
            # للقديم) - هذا ليس فشلاً حقيقياً، فنتجاهله بأمان (لا حاجة لأي تحديث أصلاً).
            # فقط أخطاء التعديل الحقيقية (رسالة محذوفة، صلاحيات...) تستدعي إرسال رسالة
            # جديدة بديلة أدناه.
            if "message is not modified" in str(exc).lower():
                await state.update_data(count_screen_chat_id=chat_id, count_screen_message_id=message_id)
                return

    msg = await target_for_send.answer(text, reply_markup=keyboard, parse_mode="HTML")
    await state.update_data(count_screen_chat_id=msg.chat.id, count_screen_message_id=msg.message_id)


async def _show_question_count_screen(reply_target: types.Message, state: FSMContext, edit: bool = False) -> None:
    """
    🆕 الخطوة الأخيرة المشتركة: عرض شاشة عدد الأسئلة المدمجة (اختيار + تكلفة + بدء
    التوليد بضغطة واحدة أخيرة، بدل شاشتين منفصلتين كما كان سابقاً). count_prompt_text
    مخزَّن مسبقاً بالحالة (وُضع هناك من _ask_question_count عند نقطة الدخول الأصلية).
    """
    await state.update_data(selected_question_count=DEFAULT_COUNT_SUGGESTIONS[1], count_screen_message_id=None)
    await state.set_state(QuizState.waiting_for_count)
    if edit:
        await _render_question_count_screen(reply_target.bot, reply_target.chat.id, reply_target.message_id, state, target_for_send=reply_target)
    else:
        await _render_question_count_screen(reply_target.bot, reply_target.chat.id, None, state, target_for_send=reply_target)


async def _show_quiz_options_screen(reply_target: types.Message, state: FSMContext, edit: bool = False) -> None:
    """
    🆕 يعرض المرحلة الأولى (نوع الأسئلة) من شاشة الخيارات - مرحلتان متتاليتان بنفس
    الرسالة (نوع ثم صعوبة، راجع handlers/quiz_options.py للتفاصيل الكاملة). تُستدعى
    بعد التصنيف مباشرة للمواد غير الإنجليزية، أو بعد اختيار الترجمة لمحتوى إنجليزي.
    """
    await state.set_state(QuizState.waiting_for_quiz_options)
    data = await state.get_data()
    keyboard = get_quiz_type_keyboard(
        subject_type=data.get("subject_type", SUBJECT_OTHER),
        suggested_types=data.get("suggested_question_types", []),
        selected_type=data.get("question_type", QUESTION_TYPE_GENERAL),
    )
    if edit:
        await reply_target.edit_text(MSG_QUIZ_TYPE_PROMPT, parse_mode="HTML", reply_markup=keyboard)
    else:
        await reply_target.answer(MSG_QUIZ_TYPE_PROMPT, parse_mode="HTML", reply_markup=keyboard)


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
    await state.update_data(
        subject_type=classification.subject,
        suggested_question_types=classification.suggested_types,
        question_type=QUESTION_TYPE_GENERAL,
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

    # 🩹 إصلاح خلل حقيقي: التصنيف (subject_type/question_type/difficulty) لسا ما صار
    # بهالمرحلة (يصير فقط لو ضغط الطالب "توليد جديد" - قرار متعمّد لتفادي سؤاله عن
    # التخصيص قبل ما يشوف الكاش). فحص سقف تركيبة محددة هون كان عم يفحص دايماً قيماً
    # افتراضية ثابتة ("other/general/medium") مالها علاقة بمادة الملف الحقيقية - نتيجة
    # عشوائية وغير صحيحة. الفحص الصحيح الوحيد يصير لاحقاً (handle_count_start) بعد ما
    # التصنيف الفعلي يصير معروفاً، فهون نسمح دايماً بزر "توليد جديد" بدون حجب مبكر خاطئ.
    show_generate_btn = True

    keyboard = get_multiple_quizzes_keyboard(
        all_quizzes, filtered_quizzes, data.get("items_count", 1), bool(data.get("is_album")),
        show_generate_btn=show_generate_btn, filter_type=filter_type, filter_difficulty=filter_difficulty,
    )
    msg_text = f"💡 <b>ملاحظة ذكية: تم العثور على ({len(all_quizzes)}) كويز جاهز مخزن لهذا الملف مسبقاً!</b>\n\n"
    if filter_type != "all" or filter_difficulty != "all":
        msg_text += f"🔍 يُعرض حالياً: ({len(filtered_quizzes)}) كويز مطابق للفلتر المختار.\n\n"
    msg_text += "اختر كويزاً جاهزاً بخصم 90% (السعر موضّح على كل زر)، أو ولّد كويزاً جديداً:"

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


async def _finalize_media_processing(
    message: types.Message, state: FSMContext, file_paths: List[str], title: str, items: int,
    is_album: bool, file_hash: str, user_id: Optional[int] = None,
):
    """
    🆕 user_id: مُمرَّر صراحة (بدل الاعتماد حصراً على message.from_user.id) لأنه لما
    تُستدعى هاي الدالة من مسار رفع الويب (process_web_uploaded_file/_images أدناه)،
    "message" هون بيكون رسالة حالة أرسلها البوت نفسه (bot.send_message) - يعني
    message.from_user فعلياً هو حساب البوت وليس الطالب الفعلي. بالمسار العادي
    (تيليجرام مباشرة) تُترك القيمة الافتراضية (None) فيُستخدم message.from_user.id
    كالمعتاد بلا أي تغيير بالسلوك الحالي.
    """
    resolved_user_id = user_id if user_id is not None else (message.from_user.id if message.from_user else None)
    try:
        content_type = "album" if is_album else ("photo" if len(file_paths) == 1 and file_paths[0].lower().endswith((".jpg", ".jpeg", ".png")) else "document")
        asyncio.create_task(log_usage_event(resolved_user_id, "content_uploaded", {
            "content_type": content_type, "items_count": items, "is_album": is_album, "file_hash": file_hash,
        }))

        cached_quizzes = await get_file_quizzes(file_hash)
        common_state = {
            "file_paths": file_paths, "source_title": title, "input_type": "media",
            "file_hash": file_hash, "items_count": items, "is_album": is_album,
        }
        
        if cached_quizzes:
            # 🩹 إصلاح خلل حقيقي: التصنيف لسا ما صار بهالمرحلة (يصير فقط بعد ضغط "توليد
            # جديد")، فحساب سقف تركيبة محددة هون كان غلطاً (يفحص قيماً افتراضية ثابتة
            # مالها علاقة بمادة الملف الحقيقية). الفحص الصحيح الوحيد يصير لاحقاً
            # (handle_count_start) بعد ما التصنيف الفعلي يصير معروفاً - راجع
            # _render_cache_decision_screen لنفس الإصلاح والشرح الكامل.
            # 🩹 إصلاح خلل حقيقي إضافي: السعر ما عاد يُحسب مرة وحدة من أول كويز بالقائمة
            # ويُطبَّق على الجميع (كان يحجب طلاب برصيد كافٍ لكويز أرخص فعلياً بالقائمة) -
            # كل كويز ياخد سعره الحقيقي حسب عدد أسئلته، محسوب مباشرة بـ
            # get_multiple_quizzes_keyboard وبـ handle_multi_cache_selection.
            await state.update_data(
                **common_state, available_quizzes=cached_quizzes,
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

        elif current_state == QuizState.processing_file_quiz:
            # 🩹 طلب توليد فعلي قيد التنفيذ عند Gemini حالياً (بعد ضغط "ابدأ التوليد") - لازم
            # نرفض أي ملف جديد بدل حذف/استبدال الطلب الحالي، وإلا بنحذف الملف يلي عم يُرفع
            # فعلياً لـ Gemini بهالّحظة ونفشّل التوليد بالكامل (راجع handle_count_start).
            await message.answer("⏳ في طلب توليد كويز قيد المعالجة حالياً؛ يرجى الانتظار حتى ينتهي قبل إرسال ملف جديد.")
            return

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
                # 🆕 بدل رفض جاف بلا بديل: نوجّه الطالب لصفحة رفع الملفات الكبيرة
                # (حتى 150 صفحة/100MB) لو مُهيّأة، لأن هذا الحجم يتجاوز أصلاً حد
                # تحميل تيليجرام المباشر (Bot API) وليس رقماً اخترناه نحن.
                limit_mb = int(MAX_DOC_SIZE / (1024 * 1024))
                keyboard = get_web_upload_redirect_keyboard("document")
                if keyboard.inline_keyboard:
                    await message.answer(
                        MSG_REDIRECT_TO_WEB_UPLOAD.format(limit_mb=limit_mb),
                        parse_mode="HTML", reply_markup=keyboard,
                    )
                else:
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

@router.message(StateFilter(None, QuizState.answering_quiz), F.text, ~F.text.startswith("/"), F.text != ".")
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
        
        user_info = await _current_user(call.message, call.from_user)
        await _renewal_notice(call.message, user_info)
        
        available_quizzes = data.get("available_quizzes", [])
        selected_quiz = next((q for q in available_quizzes if str(q["id"]) == quiz_uuid), None)
        if not selected_quiz:
            await call.message.answer("❌ عذراً، لم نتمكن من جلب الكويز المختار.")
            return

        # 🩹 إصلاح خلل حقيقي: السعر يُحسب هون من عدد أسئلة *هذا الكويز المختار تحديداً*
        # (بدل قيمة "cache_cost" واحدة محسوبة سابقاً من أول كويز بالقائمة وتُطبَّق على
        # الجميع - كانت تسبب حجب طلاب برصيد كافٍ فعلياً لكويز أرخص من هذا الملف).
        cost = calculate_cached_points_cost(
            int(data.get("items_count", 1)), len(selected_quiz["quiz_data"]), bool(data.get("is_album"))
        )

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

# ==================== معالجات شاشة عدد الأسئلة المدمجة (اختيار + تكلفة + بدء) ====================

@router.callback_query(QuizState.waiting_for_count, F.data.startswith("qcount_pick_"))
async def handle_count_pick(call: types.CallbackQuery, state: FSMContext) -> None:
    """اختيار عدد من الأزرار السريعة (toggle) - يحدّث نفس الشاشة (سعر + زر البدء) فوراً."""
    try:
        count = int(call.data.replace("qcount_pick_", "", 1))
        valid, error = validate_question_count(count)
        if not valid:
            await call.answer(f"❌ {error}", show_alert=True)
            return
        await state.update_data(selected_question_count=count)
        data = await state.get_data()
        await _render_question_count_screen(
            call.bot, data.get("count_screen_chat_id", call.message.chat.id),
            data.get("count_screen_message_id", call.message.message_id), state, target_for_send=call.message,
        )
    except Exception as exc:
        log_error(logger, f"Count pick failed: {exc}", exception=exc)
    finally:
        await call.answer()


@router.callback_query(QuizState.waiting_for_count, F.data == "qcount_custom")
async def handle_count_custom_prompt(call: types.CallbackQuery, state: FSMContext) -> None:
    """يطلب من الطالب كتابة عدد مخصص - نفس الرسالة تتحول لطلب إدخال نصي (edit)."""
    try:
        await call.message.edit_text(
            "✏️ اكتب الآن عدد الأسئلة المطلوب (رقم فقط):",
            reply_markup=types.InlineKeyboardMarkup(inline_keyboard=[
                [types.InlineKeyboardButton(text=BTN_CANCEL_REQUEST, callback_data="cancel_upload_request")],
            ]),
        )
    except Exception as exc:
        log_error(logger, f"Custom count prompt failed: {exc}", exception=exc)
    finally:
        await call.answer()


@router.message(QuizState.waiting_for_count, F.text.isdigit())
async def process_count_custom_text(message: types.Message, state: FSMContext) -> None:
    """
    🆕 استقبال العدد المكتوب يدوياً - يُحدَّث نفس شاشة الأزرار الأصلية (عبر
    count_screen_chat_id/message_id المخزّنة بالحالة) بدل إرسال شاشة تأكيد منفصلة،
    فيبقى التدفق كله شاشة واحدة متجدّدة حتى لو غيّر الطالب طريقة الإدخال بمنتصف الطريق.
    """
    count = int(message.text)
    valid, error = validate_question_count(count)
    if not valid:
        await message.answer(f"❌ {error}")
        return
    await state.update_data(selected_question_count=count)
    data = await state.get_data()
    try:
        await message.delete()  # تنظيف الشات من رقم الطالب المكتوب - آمن التجاهل لو فشل
    except Exception:
        pass
    await _render_question_count_screen(
        message.bot, data.get("count_screen_chat_id", message.chat.id),
        data.get("count_screen_message_id"), state, target_for_send=message,
    )


@router.message(QuizState.waiting_for_count)
async def process_count_invalid(message: types.Message) -> None:
    """معالج إدخال قيمة غير رقمية لعدد الأسئلة"""
    await message.answer(
        "⚠️ <b>الرجاء إرسال رقم صحيح لعدد الأسئلة!</b>\n\nيمكنك استخدام زر التراجع لإلغاء العملية الحالية بشكل نظيف وعادل.",
        parse_mode="HTML"
    )


@router.callback_query(QuizState.waiting_for_count, F.data == "qcount_start")
async def handle_count_start(call: types.CallbackQuery, state: FSMContext) -> None:
    """
    🆕 زر "ابدأ التوليد" بنهاية شاشة عدد الأسئلة - يدمج (كانا سابقاً خطوتين منفصلتين):
    1) التحقق من السقف/الرصيد وحساب التكلفة (كان _process_question_count سابقاً)
    2) الخصم الفعلي وتنفيذ التوليد (كان handle_confirm_quiz_generation سابقاً)
    كل ذلك بضغطة واحدة أخيرة على نفس الشاشة التي عرضت السعر مسبقاً - يطابق الخطة
    الأصلية: "خيارات مسعّرة مسبقاً ... وزر (ابدأ التوليد) بنهاية القائمة".
    """
    await call.answer()
    try:
        data = await state.get_data()
        count = int(data.get("selected_question_count") or 0)
        valid, error = validate_question_count(count)
        if not valid:
            await call.message.answer(f"❌ {error}")
            return

        items = int(data.get("items_count") or 1)
        is_album = bool(data.get("is_album"))
        file_hash = data.get("file_hash")

        if file_hash:
            # 🆕 نفس فحص سقف التركيبة، مُعاد هنا كخط دفاع أخير مباشرة قبل الخصم (قد يكون
            # طالب آخر ولّد كويزاً بنفس التركيبة بين عرض الشاشة والضغط على "ابدأ التوليد").
            current_quizzes = await get_file_quizzes(file_hash)
            max_allowed = max(MIN_QUIZZES_PER_FILE, min(MAX_FILE_QUIZZES_LIMIT, items // PAGES_PER_QUIZ_RATIO))
            existing_combo_count = combo_quiz_count(
                current_quizzes, data.get("subject_type", "other"),
                data.get("question_type", "general"), data.get("difficulty", "medium"),
            )
            if existing_combo_count >= max_allowed:
                await call.message.answer(MSG_MAX_QUIZZES_REACHED, parse_mode="HTML")
                await state.clear()
                return

        cost = calculate_quiz_points_cost(items, count, is_album)
        mode = determine_execution_mode(items, count)
        user_info = await _current_user(call.message, call.from_user)

        if float(user_info["points"]) < cost or await update_user_stats(call.from_user.id, cost, count) is None:
            await _insufficient_balance(call.message, user_info, cost)
            return

        asyncio.create_task(log_usage_event(call.from_user.id, "quiz_generation_requested", {
            "requested_count": count, "items_count": items, "cost": cost, "mode": mode
        }))

        await state.update_data(debited_cost=cost, calculated_cost=cost, requested_count=count, execution_mode=mode)
        data["debited_cost"] = cost  # 🩹 إصلاح خلل موجود مسبقاً: استرجاع النقاط كان يقرأ نسخة قديمة من الحالة بدون هذا الحقل، فيحسب دائماً صفراً
        try:
            await call.message.delete()
        except Exception:
            pass

        status_msg = await call.message.answer(MSG_PROCESSING)

        # 🩹 إصلاح خلل حقيقي: كانت الحالة تضل waiting_for_count (وهي جوا PENDING_REQUEST_STATES)
        # طوال مدة execute_quiz_generation_workflow (يلي ممكن تاخد دقايق طويلة لما Gemini يكون
        # overloaded). فلو الطالب أرسل نفس الملف/صورة مرة ثانية بهالأثناء (توقعاً إنو الطلب الأول
        # ضاع)، handle_media كان يفسّرها كـ"استبدال طلب معلّق" ويحذف ملفات الطلب الأول من الديسك
        # وهي لسا قيد الرفع الفعلي لـ Gemini - يفشل التوليد بالكامل بخطأ "not a valid file path"
        # عبر كل المفاتيح/الموديلات بالـ cascade. قفل الحالة هون يمنع هالتضارب.
        await state.set_state(QuizState.processing_file_quiz)

        # 🩹 UX: Gemini بيصير أحياناً overloaded لدقائق طويلة (شفنا حالات وصلت ~20 دقيقة
        # باللوغز) وMSG_PROCESSING بيوعد بـ"ثوانٍ معدودة" فقط - هالفجوة كانت تدفع الطالب
        # يعيد إرسال نفس الملف ظناً إنو الطلب ضاع (وهو تحديداً السبب يلي كان يفعّل خلل حذف
        # الملف الموصوف فوق). heartbeat خفيف هون بيحدّث نفس الرسالة كل ~20 ثانية بوقت
        # منقضي واضح، بلا أي تأثير على منطق التوليد نفسه - يُلغى تلقائياً بمجرد انتهاء
        # execute_quiz_generation_workflow (نجاحاً أو فشلاً) عبر finally.
        heartbeat_task = asyncio.create_task(_run_processing_heartbeat(status_msg))
        try:
            quiz_data, new_quiz_id, error_code = await execute_quiz_generation_workflow(call.from_user.id, data, count, status_msg)
        finally:
            heartbeat_task.cancel()

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

        await log_usage_event(call.from_user.id, "quiz_generated", {
            "quiz_id": new_quiz_id, "questions_generated": len(quiz_data), "cost": cost
        })
        await reward_referrer_if_eligible(call.from_user.id)

        from handlers.quiz_runner import _start_loaded_quiz
        await _start_loaded_quiz(call, state, quiz_data, data.get("source_title", "كويز"), origin="file" if data.get("input_type") == "media" else "text", quiz_id=new_quiz_id)
        await status_msg.delete()

    except Exception as exc:
        log_error(logger, f"Quiz generation start failed: {exc}", exception=exc)
        await refund_user_on_failure(call.from_user.id, await state.get_data())
        await state.set_state(None)
        await call.message.answer("❌ حدث خطأ، تم إعادة شحن رصيدك تلقائياً.")
    finally:
        for path in (await state.get_data()).get("file_paths", []):
            safe_file_cleanup(path)


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

# ==================== 🆕 رفع ملف/ألبوم صور كبير عبر صفحة الويب (Mini App) ====================

async def _reject_web_upload(chat_id: int, state: FSMContext, status_msg: Optional[types.Message], text: str) -> None:
    """تنظيف موحّد عند رفض/فشل طلب رفع ويب (ملف أو صور): فكّ القفل وعرض رسالة الخطأ."""
    await state.set_state(None)
    if status_msg:
        try:
            await status_msg.edit_text(text, parse_mode="HTML")
            return
        except Exception:
            pass
    await bot.send_message(chat_id, text, parse_mode="HTML")


async def process_web_uploaded_file(
    user_id: int, chat_id: int, object_path: str, declared_file_name: str = "",
) -> None:
    """
    🆕 نظير handle_media (فرع document) لكن لمستند رُفع عبر صفحة الويب (Mini App)
    بدل رسالة مستند مباشرة على تيليجرام. يُستدعى كـ background task من
    webhook_server.py فور تأكيد اكتمال الرفع على Supabase.

    الفروقات عن مسار تيليجرام المباشر:
    - لا يوجد حد MAX_DOC_SIZE (20MB) هون - الحد المطبَّق مسبقاً هو
      MAX_FILE_WEB_UPLOAD_SIZE (100MB) بمرحلة /api/file-upload/init.
    - سقف الصفحات هون MAX_FILE_WEB_UPLOAD_PAGES (150) بدل MAX_SUPER_PAGES (100) -
      نفس السبب اللي خلينا نرفع سقف الحجم: مصدر هذا المسار حصراً طلاب بمحتوى كبير
      متعمّد، فمنطقي يكون سقفه أعلى شوي من المسار العادي.
    - بعد التحميل والفحص، التسليم لباقي خط الأنابيب هو _finalize_media_processing
      نفسه المستخدم بالمسار العادي بلا أي تعديل - راجع توثيق user_id بتوقيعها فوق.
    """
    state = _build_state_for_chat(chat_id, user_id)

    current_state = await state.get_state()
    if current_state == QuizState.answering_quiz:
        await bot.send_message(chat_id, "⚠️ لديك اختبار قائم حالياً؛ أتممه أو أوقفه أولاً قبل رفع ملف جديد.")
        await delete_file_temp(object_path)
        return
    if current_state == QuizState.processing_web_file:
        await bot.send_message(chat_id, "⏳ لديك ملف آخر قيد المعالجة حالياً؛ يرجى انتظار انتهائه.")
        await delete_file_temp(object_path)
        return
    if current_state == QuizState.processing_file_quiz:
        # 🩹 نفس إصلاح handle_media: طلب توليد فعلي قيد التنفيذ عند Gemini حالياً - لازم نرفض
        # هذا الرفع الجديد بدل حذف/استبدال ملفات الطلب الجاري معالجته فعلياً.
        await bot.send_message(chat_id, "⏳ في طلب توليد كويز قيد المعالجة حالياً؛ يرجى الانتظار حتى ينتهي قبل رفع ملف جديد.")
        await delete_file_temp(object_path)
        return
    if current_state in PENDING_REQUEST_STATES:
        if await _discard_pending_upload(state):
            await bot.send_message(chat_id, MSG_PREVIOUS_REQUEST_REPLACED)
        await state.set_state(None)
    elif current_state is not None:
        await state.set_state(None)

    await state.set_state(QuizState.processing_web_file)
    ensure_directory_exists(DOWNLOADS_DIR)
    extension = os.path.splitext(declared_file_name)[1] or os.path.splitext(object_path)[1] or ".pdf"
    destination = os.path.join(DOWNLOADS_DIR, f"file_web_{user_id}_{uuid.uuid4().hex}{extension}")

    status_msg = await bot.send_message(chat_id, "📥 <b>تم استلام الملف، جارٍ تجهيزه...</b>", parse_mode="HTML")

    try:
        # 🆕 خط دفاع ثانٍ: تحقق فعلي من حجم المحتوى المُنزَّل نفسه (وليس فقط الحجم
        # المُصرَّح به بمرحلة /init) - نفس منطق download_audio_temp_to_file تماماً.
        downloaded = await download_file_temp_to_file(object_path, destination, max_size_bytes=MAX_FILE_WEB_UPLOAD_SIZE)
        if not downloaded:
            await delete_file_temp(object_path)
            await _reject_web_upload(chat_id, state, status_msg, "❌ تعذر تحميل الملف المرفوع أو أنه يتجاوز الحجم المسموح، يرجى إعادة المحاولة من البوت.")
            return

        items = 1
        if destination.lower().endswith(".pdf"):
            items = await asyncio.to_thread(get_pdf_page_count_sync, destination)
            if items > MAX_FILE_WEB_UPLOAD_PAGES:
                safe_file_cleanup(destination)
                await delete_file_temp(object_path)
                await _reject_web_upload(
                    chat_id, state, status_msg,
                    f"❌ الحد الأقصى لمعالجة ملفات PDF عبر صفحة الويب هو {MAX_FILE_WEB_UPLOAD_PAGES} صفحة.",
                )
                return

        await delete_file_temp(object_path)  # انتهى الغرض من النسخة المؤقتة بـ Supabase - النسخة المحلية هي اللي رح تُعالَج الآن

        title = os.path.splitext(declared_file_name)[0] if declared_file_name else "ملف مرفوع عبر الويب"
        file_hash = await asyncio.to_thread(calculate_file_hash, destination)

        await state.set_state(None)  # فكّ القفل - _finalize_media_processing بتحدد الحالة التالية بنفسها
        await status_msg.delete()
        await _finalize_media_processing(status_msg, state, [destination], title, items, False, file_hash, user_id=user_id)
    except Exception as exc:
        log_error(logger, f"Web-uploaded file preparation failed: {exc}", exception=exc)
        safe_file_cleanup(destination)
        await delete_file_temp(object_path)
        await _reject_web_upload(chat_id, state, status_msg, "❌ حدث خطأ غير متوقع أثناء تجهيز الملف.")


async def process_web_uploaded_images(
    user_id: int, chat_id: int, object_paths: List[str],
) -> None:
    """
    🆕 نظير process_album_background لكن لألبوم صور كبير (حتى MAX_IMAGE_WEB_UPLOAD_COUNT
    صورة) رُفع عبر صفحة الويب - تيليجرام نفسه يمنع ألبوماً أكبر من MAX_ALBUM_IMAGES
    (10) بمسار الرسائل المباشر، فهذا هو المسار الوحيد الممكن لألبوم أكبر من هيك.

    التوليد الفعلي لاحقاً (بعد اختيار عدد الأسئلة) بيتوجه تلقائياً لوضع "Super Images"
    (3 دفعات متوازية) لو عدد الصور تجاوز SUPER_IMAGE_BATCH_THRESHOLD - راجع
    helpers/gemini_helper.py::generate_quiz_smart، بلا أي تدخل إضافي هون؛ is_album=True
    وitems_count=عدد الصور كافيان تماماً لأن نفس منطق التسعير المسطّح (نقطة/صورة)
    يشتغل تلقائياً بغض النظر عن العدد.
    """
    state = _build_state_for_chat(chat_id, user_id)

    current_state = await state.get_state()
    if current_state == QuizState.answering_quiz:
        await bot.send_message(chat_id, "⚠️ لديك اختبار قائم حالياً؛ أتممه أو أوقفه أولاً قبل رفع ألبوم صور جديد.")
        await delete_file_temp_batch(object_paths)
        return
    if current_state == QuizState.processing_web_file:
        await bot.send_message(chat_id, "⏳ لديك طلب آخر قيد المعالجة حالياً؛ يرجى انتظار انتهائه.")
        await delete_file_temp_batch(object_paths)
        return
    if current_state == QuizState.processing_file_quiz:
        # 🩹 نفس إصلاح handle_media: طلب توليد فعلي قيد التنفيذ عند Gemini حالياً - لازم نرفض
        # هذا الرفع الجديد بدل حذف/استبدال ملفات الطلب الجاري معالجته فعلياً.
        await bot.send_message(chat_id, "⏳ في طلب توليد كويز قيد المعالجة حالياً؛ يرجى الانتظار حتى ينتهي قبل رفع ألبوم جديد.")
        await delete_file_temp_batch(object_paths)
        return
    if current_state in PENDING_REQUEST_STATES:
        if await _discard_pending_upload(state):
            await bot.send_message(chat_id, MSG_PREVIOUS_REQUEST_REPLACED)
        await state.set_state(None)
    elif current_state is not None:
        await state.set_state(None)

    await state.set_state(QuizState.processing_web_file)
    ensure_directory_exists(DOWNLOADS_DIR)

    status_msg = await bot.send_message(
        chat_id, f"📥 <b>تم استلام {len(object_paths)} صورة، جارٍ تجهيزها...</b>", parse_mode="HTML",
    )

    local_paths: List[str] = []
    try:
        for index, object_path in enumerate(object_paths):
            ext = os.path.splitext(object_path)[1] or ".jpg"
            destination = os.path.join(DOWNLOADS_DIR, f"images_web_{user_id}_{uuid.uuid4().hex}_{index}{ext}")
            # 🆕 نفس حد الحجم لكل صورة على حدة (MAX_IMAGE_WEB_UPLOAD_SIZE_PER_IMAGE) -
            # مستورد ضمن MAX_FILE_WEB_UPLOAD_SIZE هون كسقف كلي احترازي إضافي فقط
            # (الفحص الدقيق لكل صورة صار مسبقاً بمرحلة /api/image-upload/complete).
            downloaded = await download_file_temp_to_file(object_path, destination, max_size_bytes=MAX_FILE_WEB_UPLOAD_SIZE)
            if not downloaded:
                for path in local_paths:
                    safe_file_cleanup(path)
                await delete_file_temp_batch(object_paths)
                await _reject_web_upload(chat_id, state, status_msg, "❌ تعذر تحميل إحدى الصور المرفوعة، يرجى إعادة المحاولة من البوت.")
                return
            local_paths.append(destination)

        await delete_file_temp_batch(object_paths)

        is_album = len(local_paths) > 1
        title = f"كويز من ألبوم صور ({len(local_paths)} صور)" if is_album else "كويز من صورة"
        file_hash = await asyncio.to_thread(compute_combined_hash, local_paths)

        await state.set_state(None)
        await status_msg.delete()
        await _finalize_media_processing(status_msg, state, local_paths, title, len(local_paths), is_album, file_hash, user_id=user_id)
    except Exception as exc:
        log_error(logger, f"Web-uploaded images preparation failed: {exc}", exception=exc)
        for path in local_paths:
            safe_file_cleanup(path)
        await delete_file_temp_batch(object_paths)
        await _reject_web_upload(chat_id, state, status_msg, "❌ حدث خطأ غير متوقع أثناء تجهيز الصور.")


files_router = router
