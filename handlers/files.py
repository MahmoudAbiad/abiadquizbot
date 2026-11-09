# Handlers/files.py
from handlers.session_navigation import offer_transition, message_action
import asyncio
from task_utils import spawn
import json
import os
import uuid
from typing import Any, Dict, List, Optional

from aiogram import F, Router, types
from aiogram.filters import StateFilter
from aiogram.fsm.context import FSMContext

from config import QuizState, bot, redis_client, ADMIN_ID
from helpers.settings_helper import get_setting
from constants import (
    QUESTION_TYPE_SIMILAR_MATH, SUBJECT_MATH,
    ADMIN_CONTACT, BTN_CANCEL_REQUEST, ERROR_ALBUM_TOO_LARGE,
    MAX_ALBUM_IMAGES, MAX_SUPER_PAGES, MAX_TEXT_INPUT_SIZE, MSG_NOTHING_TO_CANCEL,
    MSG_PREVIOUS_REQUEST_REPLACED, MSG_PROCESSING, MSG_REQUEST_CANCELLED,
    MSG_SUPER_PROCESSING_ALERT, SUCCESS_MEDIA_RECEIVED, PAGES_PER_QUIZ_RATIO,
    MAX_FILE_QUIZZES_LIMIT, MIN_QUIZZES_PER_FILE, MSG_MAX_QUIZZES_REACHED,
    MSG_ENGLISH_CONTENT_DETECTED, SUBJECT_ENGLISH, SUBJECT_OTHER,
    MSG_FRENCH_CONTENT_DETECTED, SUBJECT_FRENCH, MSG_DETECTING_CONTENT,
    QUESTION_TYPE_GENERAL, DIFFICULTY_MEDIUM, MSG_QUIZ_TYPE_PROMPT,
    MAX_DOC_SIZE, MAX_FILE_WEB_UPLOAD_SIZE, MAX_FILE_WEB_UPLOAD_PAGES,
    MAX_IMAGE_WEB_UPLOAD_COUNT, MAX_IMAGE_WEB_UPLOAD_SIZE_PER_IMAGE, MAX_IMAGE_WEB_UPLOAD_TOTAL_SIZE, MSG_REDIRECT_TO_WEB_UPLOAD,
    # 🆕 اختبار محلول/غير محلول (SUBJECT_QUIZ_SOLVED/SUBJECT_QUIZ_UNSOLVED)
    SUBJECT_QUIZ_SOLVED, SUBJECT_QUIZ_UNSOLVED,
    QUIZ_EXTRACTION_MODE_AS_IS, QUIZ_EXTRACTION_MODE_AI_SOLVE,
    MSG_QUIZ_SOLVED_DETECTED, MSG_QUIZ_UNSOLVED_DETECTED,
    MSG_QUIZ_DETECTED_COUNT_TEMPLATE, MAX_QUESTIONS_TO_GENERATE, DEFAULT_DETECTED_QUIZ_COUNT_FALLBACK,
    # 🆕 التحقق المجتمعي من تصنيف المادة (راجع migration_classification_votes.sql)
    CLASSIFICATION_SUBJECT_LABELS_AR, CLASSIFICATION_SUBJECT_CODES_REVERSE,
    MSG_CLASSIFICATION_VOTE_PROMPT,
    MSG_CLASSIFICATION_VOTE_EDITED_YES, MSG_CLASSIFICATION_VOTE_EDITED_NO,
    MSG_CLASSIFICATION_VOTE_ALREADY, MSG_CLASSIFICATION_VOTE_ERROR,
    MSG_CLASSIFICATION_LOCKED_SUFFIX, MSG_CLASSIFICATION_VOTE_ADMIN_ALERT,
    MSG_UPLOAD_FILE_MISSING,
)
from helpers.gemini_helper import get_pdf_page_count_sync
from helpers.points_calculator import calculate_cached_points_cost, calculate_quiz_points_cost
from keyboards import (
    get_multiple_quizzes_keyboard, get_question_count_keyboard,
    get_translation_choice_keyboard, get_quiz_type_keyboard,
    get_web_upload_redirect_keyboard, get_quiz_extraction_choice_keyboard,
    get_quiz_detected_count_keyboard, get_classification_vote_keyboard,
    get_quiz_creation_summary_keyboard,
)
from logger import get_logger, log_error, log_warning
from helpers.supabase_helper import (
    check_or_add_user, get_file_quizzes, update_user_stats, log_usage_event, mark_quiz_attempt_stopped,
    reward_referrer_if_eligible, get_classification_lock, submit_classification_vote,
    has_user_voted_on_classification, is_feature_enabled,
)
from services.usage_tracking import track_upload_rejection
from helpers.r2_helper import delete_file_temp, delete_file_temp_batch, download_file_temp_to_file
from utils import calculate_file_hash, ensure_directory_exists, safe_file_cleanup, safe_title, short_token_to_hash
from validators import validate_file_size, validate_question_count
from services.upload_validation import validate_image, validate_document, supported_extension, DOCUMENT_EXTENSIONS, TELEGRAM_IMAGE_DOCUMENT_EXTENSIONS, AUDIO_EXTENSIONS, InvalidUploadError
from services.redis_reliability import UserLock, user_locked
from services.generation_eta import GenerationEta, build_processing_text, estimate_generation_eta
from services.generation_heartbeat import run_processing_heartbeat

# استيراد الخدمات الجديدة
from services.file_service import compute_combined_hash, download_photos_service, extract_office_text_if_needed
from services.subject_classifier import classify_subject, get_pending_classification_snapshot, SubjectClassification
from services.quiz_service import (
    determine_execution_mode, build_transparency_text, refund_user_on_failure, execute_quiz_generation_workflow,
    combo_quiz_count, build_question_type_label, estimate_pages_from_text,
)
from services.flow_state import build_state_for_chat  # 🆕 نفس بناء FSMContext اليدوي المستخدم لرفع الصوت عبر الويب
from services.wallet_errors import WalletDebitError, WALLET_DEBIT_ERROR_TEXT
from handlers.quiz_runner import start_loaded_quiz, _start_loaded_quiz
from services.excel_import import ExcelImportError, read_quiz_xlsx

logger = get_logger(__name__)
router = Router()
# Uploads, photos and free text generate quizzes only in private chats.
router.message.filter(F.chat.type == "private")
DOWNLOADS_DIR = "downloads"


_QUIZ_CHARGE_STATE_KEYS = ("debited_cost", "debited_free", "debited_paid", "debited_gift", "debit_id")

async def _clear_quiz_charge_markers(state: FSMContext) -> None:
    """يمسح فقط بيانات عملية الخصم الحالية من FSM بدون حذف بيانات الكويز/النتيجة الأخرى."""
    current_data = await state.get_data()
    changed = False
    for key in _QUIZ_CHARGE_STATE_KEYS:
        if key in current_data:
            current_data.pop(key, None)
            changed = True
    if changed:
        await state.set_data(current_data)

PENDING_REQUEST_STATES = (
    QuizState.waiting_for_count, 
    QuizState.waiting_for_cache_decision, 
    QuizState.waiting_for_translation_choice,
    QuizState.waiting_for_quiz_options,       # 🆕
    QuizState.waiting_for_custom_question_type,  # 🆕
    QuizState.waiting_for_quiz_extraction_choice,  # 🆕 اختيار "كما هي/حل بالذكاء الاصطناعي"
)

# 🆕 حقل "content_source" الصريح لسجل توليد الكويزات بلوحة الأدمن (راجع log_usage_event
# لحدث quiz_generated بـhandle_count_start أدناه) - يُحدَّد هون فقط من امتداد المستند
# الفعلي وقت الاستقبال المباشر (قبل أي معالجة/استخراج لاحق)، لا استنتاجاً لاحقاً. يُستخدم
# من مساري تيليجرام (handle_media) والويب (process_web_uploaded_file) كلاهما لتوحيد
# المنطق بمكان واحد فقط.
def _document_content_source(extension: str) -> str:
    ext = (extension or "").lower()
    if ext == ".pdf":
        return "pdf"
    if ext in (".docx", ".doc"):
        return "docx"
    if ext in (".pptx", ".ppt"):
        return "pptx"
    if ext == ".txt":
        return "txt"
    # 🆕 امتداد مستند غير متوقع (خارج الأنواع الأربعة المدعومة فعلياً بالبوت - راجع
    # extract_office_text_if_needed بـservices/file_service.py) - نادر جداً عملياً، لكن
    # نتفادى قيمة مضلِّلة (مثل "pdf" لملف ليس PDF فعلاً)؛ يُعرض بلوحة الأدمن كـ"غير معروف".
    return "unknown_document"


def _cancel_keyboard() -> types.InlineKeyboardMarkup:
    return types.InlineKeyboardMarkup(inline_keyboard=[[types.InlineKeyboardButton(text=BTN_CANCEL_REQUEST, callback_data="cancel_upload_request")]])

async def _discard_pending_upload(state: FSMContext) -> int:
    data = await state.get_data()
    file_paths = data.get("file_paths", []) or []
    removed = sum(1 for path in file_paths if safe_file_cleanup(path))
    return removed


def _missing_upload_paths(data: Dict[str, Any]) -> List[str]:
    """يرجع المسارات المحلية المفقودة من القرص لطلب ملف قيد التنفيذ (قائمة فارغة = كل شيء
    سليم أو لا حاجة للملف). مصدر الفقد الشائع: إعادة تشغيل الـ dyno (نظام ملفات مؤقت) أو
    تنظيف الملفات القديمة بينما حالة الطالب (FSM) ما زالت محفوظة بـ Redis. نص مباشر لا
    يحتاج ملفاً، ومستند أوفيس استُخرج نصه مسبقاً (cached_office_text) لا يُعاد فتحه."""
    if data.get("input_type") != "media" or data.get("cached_office_text"):
        return []
    paths = data.get("file_paths") or []
    if not paths:
        return ["<no-file-paths>"]
    return [path for path in paths if not os.path.exists(path)]


async def _run_processing_heartbeat(status_msg: types.Message, eta: Optional[GenerationEta] = None) -> None:
    """🩹 يحدّث رسالة "جاري المعالجة" دورياً طول مدة انتظار الذكاء الاصطناعي (قد تطول جداً وقت
    الازدحام). مع تقدير زمني (eta، راجع services/generation_eta.py): كل 5 ثوانٍ مع عدّ تنازلي، ثم
    كل 15 ثانية بعد تجاوز الوقت المعتاد؛ بلا تقدير: النص العام القديم كل 20 ثانية. القواعد التفصيلية
    (تخطّي النص المكرر، انتظار retry_after من تيليجرام) بـ services/generation_heartbeat.py. يُنهى فوراً
    عبر asyncio.CancelledError بمجرد ما يخلص التوليد (finally بمكان الاستدعاء)."""
    await run_processing_heartbeat(status_msg, eta)


async def _renewal_notice(message: types.Message, user_info: Dict[str, Any]) -> None:
    if user_info.get("status") == "renewed":
        # نستخدم free_points الفعلية المُطبَّقة من الدالة الذرية (وليس إعادة جلب الإعداد)
        # لضمان تطابق الرقم المعروض مع ما تمت إضافته فعلياً لحساب الطالب.
        applied_points = float(user_info.get("free_points") or 0)
        await message.answer(f"☀️ تم تجديد رصيدك اليومي إلى <b>{applied_points:.0f} نقطة مجانية</b>.", parse_mode="HTML")

async def _insufficient_balance(message: types.Message, user_info: Dict[str, Any], required: float, action: str = "unknown") -> None:
    balance = float(user_info.get("points") or 0)
    if balance >= required:
        # A failed debit or a stale balance snapshot must not prompt a funded
        # student to recharge, nor count as an insufficient-balance event.
        await message.answer(WALLET_DEBIT_ERROR_TEXT)
        return
    deficit = max(0.0, required - balance)

    # 🆕 تتبع تحليلي: تسجيل كل مرة يُعترَض فيها إجراء الطالب بسبب رصيد غير كافٍ،
    # مع توضيح نوع الإجراء المحجوب وعدد النقاط الفاصلة بالضبط (deficit) - يسمح لاحقاً
    # بمعرفة كم طالب يوصل لهالحاجز فعلياً وبكم نقطة بالمتوسط، لتقييم التسعير/نقاط
    # التجديد المجاني. نأخذ user_id من user_info["user_id"] وليس message.from_user -
    # لأن message هون غالباً call.message (رسالة البوت نفسه بالأزرار) وليس رسالة
    # الطالب. asyncio.create_task حتى ما يأخر رد الطالب أبداً (نفس نمط باقي أحداث
    # log_usage_event بهالملف).
    tracked_user_id = user_info.get("user_id")
    if tracked_user_id:
        spawn(log_usage_event(tracked_user_id, "insufficient_balance_blocked", {
            "action": action, "required_points": required, "current_balance": balance,
            "deficit_points": deficit, "free_points": float(user_info.get("free_points") or 0),
            "paid_points": float(user_info.get("paid_points") or 0),
        }))

    daily_renewal_points = await get_setting("daily_renewal_points")
    contact = ADMIN_CONTACT.lstrip("@")
    keyboard = types.InlineKeyboardMarkup(inline_keyboard=[[types.InlineKeyboardButton(text="💳 شحن الرصيد الآن", url=f"https://t.me/{contact}")]])
    await message.answer(
        "❌ <b>رصيدك الحالي لا يكفي لإتمام هذه العملية.</b>\n\n"
        f"🔄 المجاني: <code>{float(user_info.get('free_points') or 0):.2f}</code>\n"
        f"🎁 الهدايا: <code>{float(user_info.get('gift_points') or 0):.2f}</code>\n"
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

@user_locked(delay=1.5, wait_seconds=2)
async def process_album_background(message: types.Message, state: FSMContext):
    try:
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
            track_upload_rejection(
                message.from_user.id, "image", "item_count_exceeded", "telegram",
                item_count=len(photos), max_item_count=MAX_ALBUM_IMAGES,
            )
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
            track_upload_rejection(message.from_user.id, "image", "download_failed", "telegram")
            await message.answer(err)
            return
        if not file_paths: return
        
        is_album = len(file_paths) > 1
        title = f"كويز من ألبوم صور ({len(file_paths)} صور)" if is_album else "كويز من صورة"
        file_hash = await asyncio.to_thread(compute_combined_hash, file_paths)
        
        await _finalize_media_processing(message, state, file_paths, title, len(file_paths), is_album, file_hash, content_source="images")
    except Exception as exc:
        track_upload_rejection(
            message.from_user.id, "image", "processing_failed", "telegram",
            failure_stage="album_processing", exception_type=type(exc).__name__,
        )
        log_error(logger, f"Album background processing failed: {exc}", exception=exc)
        await message.answer("❌ حدث خطأ غير متوقع أثناء تجميع الألبوم.")

DEFAULT_COUNT_SUGGESTIONS = [5, 10, 15, 20]



async def _render_question_count_screen(bot, chat_id: int, message_id: Optional[int], state: FSMContext, target_for_send: Optional[types.Message] = None) -> None:
    """
    🆕 يعرض شاشة ملخص الإنشاء الموحّدة (الإعدادات الافتراضية المضبوطة مسبقاً + التكلفة)
    مع 3 أزرار فقط:
    1) 🚀 إنشاء الاختبار (qcount_start)
    2) ⚙️ إعدادات الاختبار (qsetup_menu)
    3) ❌ إلغاء الطلب (cancel_upload_request)
    """
    data = await state.get_data()
    items = int(data.get("items_count") or 1)
    is_album = bool(data.get("is_album"))
    subject_type = data.get("subject_type")
    ready_exam = subject_type in (SUBJECT_QUIZ_SOLVED, SUBJECT_QUIZ_UNSOLVED) and data.get("question_type") != QUESTION_TYPE_SIMILAR_MATH
    default_suggestions = globals().get("DEFAULT_COUNT_SUGGESTIONS", [5, 10, 15, 20])
    default_fallback = globals().get("DEFAULT_DETECTED_QUIZ_COUNT_FALLBACK", 10)
    selected_count = int(data.get("selected_question_count") or (default_fallback if ready_exam else default_suggestions[1]))
    count_prompt_text = data.get(
        "count_prompt_text",
        "✅ تم تجهيز الإعدادات الافتراضية للاختبار. يمكنك الإنشاء مباشرة أو تعديل الإعدادات.",
    )

    if ready_exam:
        count_prompt_text = (
            MSG_QUIZ_DETECTED_COUNT_TEMPLATE.format(count=selected_count)
            if selected_count
            else "⚠️ تعذر تحديد عدد الأسئلة تلقائياً. اضغط «⚙️ إعدادات الاختبار» لتحديد عدد الأسئلة الموجود في اختبارك."
        )
        notice = data.get("quiz_detection_notice")
        if notice:
            count_prompt_text = f"{notice}\n\n{count_prompt_text}"

    mode = determine_execution_mode(items, selected_count, is_album=is_album, content_source=data.get("content_source"))
    cost = await calculate_quiz_points_cost(items, selected_count, is_album)
    difficulty = None if ready_exam else data.get("difficulty", DIFFICULTY_MEDIUM)
    default_extraction = (
        QUIZ_EXTRACTION_MODE_AS_IS if subject_type == SUBJECT_QUIZ_SOLVED
        else QUIZ_EXTRACTION_MODE_AI_SOLVE if subject_type == SUBJECT_QUIZ_UNSOLVED
        else None
    )
    extraction_mode = data.get("quiz_extraction_mode") or default_extraction
    question_type_label = build_question_type_label(
        subject_type or "other", data.get("question_type", QUESTION_TYPE_GENERAL),
        data.get("custom_question_type_text"), data.get("suggested_question_types", []),
        extraction_mode=extraction_mode,
    )

    eff_subject = (
        data.get("content_subject_type")
        if subject_type in (SUBJECT_QUIZ_SOLVED, SUBJECT_QUIZ_UNSOLVED) and data.get("content_subject_type")
        else subject_type
    )
    lang_line = ""
    if eff_subject in (SUBJECT_ENGLISH, SUBJECT_FRENCH):
        lang_label = "🔤 مترجمة للعربية" if data.get("english_mode") == "translated" else "🔠 بدون ترجمة"
        lang_line = f"\n• لغة الأسئلة: {lang_label}"

    heading = "📋 ملخص إعدادات الاختبار"
    summary_body = build_transparency_text(items, selected_count, mode, cost, difficulty, question_type_label)
    if lang_line and "لغة الأسئلة:" not in summary_body:
        summary_body = f"{summary_body}{lang_line}"
    text = f"<b>{heading}</b>\n\n{count_prompt_text}\n\n{summary_body}"
    text += (
        "\n\nاضغط «🚀 إنشاء الاختبار» للبدء فوراً، أو «⚙️ إعدادات الاختبار» لتعديل أي خيار."
        if not ready_exam or selected_count
        else "\n\nحدد عدد الأسئلة من «⚙️ إعدادات الاختبار» قبل بدء الاستخراج."
    )
    if mode in ("Super-PDF", "Super-Images", "Super-Text"):
        text += f"\n\n{MSG_SUPER_PROCESSING_ALERT}"

    summary_kb_fn = globals().get("get_quiz_creation_summary_keyboard")
    if summary_kb_fn is not None:
        keyboard = summary_kb_fn()
    elif ready_exam:
        keyboard = get_quiz_detected_count_keyboard(selected_count)
    else:
        keyboard = get_question_count_keyboard(items, is_album, selected_count, default_suggestions)

    if ready_exam and not selected_count:
        text = f"<b>{heading}</b>\n\n{count_prompt_text}"

    if message_id:
        try:
            await bot.edit_message_text(text, chat_id=chat_id, message_id=message_id, reply_markup=keyboard, parse_mode="HTML")
            await state.update_data(count_screen_chat_id=chat_id, count_screen_message_id=message_id)
            return
        except Exception as exc:
            if "message is not modified" in str(exc).lower():
                await state.update_data(count_screen_chat_id=chat_id, count_screen_message_id=message_id)
                return

    msg = await target_for_send.answer(text, reply_markup=keyboard, parse_mode="HTML")
    await state.update_data(count_screen_chat_id=msg.chat.id, count_screen_message_id=msg.message_id)


async def show_question_count_screen(reply_target: types.Message, state: FSMContext, edit: bool = False) -> None:
    """
    🆕 يعرض شاشة الملخص الموحّدة مع الحفاظ على الإعدادات المختارة أو الافتراضية.
    للاختبار الجاهز المحلول يكون الافتراضي «استخدام أجوبة المصدر»، ولغير المحلول «حل بالذكاء الاصطناعي».
    """
    data = await state.get_data()
    subject_type = data.get("subject_type")
    if subject_type in (SUBJECT_QUIZ_SOLVED, SUBJECT_QUIZ_UNSOLVED) and data.get("question_type") != QUESTION_TYPE_SIMILAR_MATH:
        default_fallback = globals().get("DEFAULT_DETECTED_QUIZ_COUNT_FALLBACK", 10)
        detected_count = int(data.get("selected_question_count") or data.get("detected_question_count") or default_fallback)
        as_is_mode = globals().get("QUIZ_EXTRACTION_MODE_AS_IS", "as_is")
        ai_solve_mode = globals().get("QUIZ_EXTRACTION_MODE_AI_SOLVE", "ai_solve")
        default_mode = as_is_mode if subject_type == SUBJECT_QUIZ_SOLVED else ai_solve_mode
        extraction_mode = data.get("quiz_extraction_mode") or default_mode
        notice = data.get("quiz_detection_notice")
        count_text = MSG_QUIZ_DETECTED_COUNT_TEMPLATE.format(count=detected_count)
        await state.update_data(
            selected_question_count=detected_count,
            quiz_extraction_mode=extraction_mode,
            count_screen_message_id=None,
            count_prompt_text=f"{notice}\n\n{count_text}" if notice else count_text,
        )
    else:
        default_suggestions = globals().get("DEFAULT_COUNT_SUGGESTIONS", [5, 10, 15, 20])
        existing_selected = data.get("selected_question_count")
        if (
            subject_type in (SUBJECT_QUIZ_SOLVED, SUBJECT_QUIZ_UNSOLVED)
            and data.get("question_type") == QUESTION_TYPE_SIMILAR_MATH
            and existing_selected == data.get("detected_question_count")
            and not data.get("review_count_to_restore")
        ):
            existing_selected = None
        restored_count = int(data.get("review_count_to_restore") or existing_selected or default_suggestions[1])
        await state.update_data(
            selected_question_count=restored_count,
            review_count_to_restore=None,
            count_screen_message_id=None,
        )
    await state.set_state(QuizState.waiting_for_count)
    if edit:
        await _render_question_count_screen(reply_target.bot, reply_target.chat.id, reply_target.message_id, state, target_for_send=reply_target)
    else:
        await _render_question_count_screen(reply_target.bot, reply_target.chat.id, None, state, target_for_send=reply_target)


async def _show_quiz_options_screen(reply_target: types.Message, state: FSMContext, edit: bool = False) -> None:
    """يعرض شاشة اختيار نوع الأسئلة الفرعية (عند فتحها من قائمة الإعدادات)."""
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


async def _proceed_to_quiz_solved_screen(reply_target: types.Message, state: FSMContext, edit: bool = True) -> None:
    """يعرض شاشة اختيار مسار الاختبار الجاهز المحلول (من قائمة إعدادات الاختبار)."""
    await state.update_data(quiz_detection_notice=None)
    await state.set_state(QuizState.waiting_for_quiz_extraction_choice)
    data = await state.get_data()
    selected_mode = data.get("quiz_extraction_mode") or QUIZ_EXTRACTION_MODE_AS_IS
    try:
        keyboard = get_quiz_extraction_choice_keyboard(
            is_math=data.get("content_subject_type") == SUBJECT_MATH,
            solved=True,
            selected_mode=selected_mode,
        )
    except TypeError:
        keyboard = get_quiz_extraction_choice_keyboard(is_math=data.get("content_subject_type") == SUBJECT_MATH)
    if edit:
        await reply_target.edit_text(MSG_QUIZ_SOLVED_DETECTED, parse_mode="HTML", reply_markup=keyboard)
    else:
        await reply_target.answer(MSG_QUIZ_SOLVED_DETECTED, parse_mode="HTML", reply_markup=keyboard)


async def _proceed_to_quiz_unsolved_screen(reply_target: types.Message, state: FSMContext, edit: bool = True) -> None:
    """يعرض شاشة اختيار مسار الاختبار الجاهز غير المحلول (من قائمة إعدادات الاختبار)."""
    data = await state.get_data()
    await state.update_data(quiz_detection_notice=None)
    await state.set_state(QuizState.waiting_for_quiz_extraction_choice)
    text = ("🗂️ <b>وجدنا أسئلة جاهزة بلا إجابات مرفقة.</b>\n\n"
            "نحوّل الأسئلة المؤتمتة والتقليدية القابلة للتحويل إلى أتمتة. نستبعد الإنشاء والمهمات المفتوحة الواسعة. اختر أجوبة الذكاء الاصطناعي أو توليد أسئلة جديدة من المحتوى.")
    selected_mode = data.get("quiz_extraction_mode") or QUIZ_EXTRACTION_MODE_AI_SOLVE
    try:
        keyboard = get_quiz_extraction_choice_keyboard(
            is_math=data.get("content_subject_type") == SUBJECT_MATH,
            solved=False,
            selected_mode=selected_mode,
        )
    except TypeError:
        keyboard = get_quiz_extraction_choice_keyboard(
            is_math=data.get("content_subject_type") == SUBJECT_MATH, solved=False)
    if edit:
        await reply_target.edit_text(text, parse_mode="HTML", reply_markup=keyboard)
    else:
        await reply_target.answer(text, parse_mode="HTML", reply_markup=keyboard)


async def ask_question_count(
    reply_target: types.Message, state: FSMContext, count_prompt_text: str, edit: bool = False,
    user_id: Optional[int] = None,
) -> None:
    """
    🆕 نقطة الدخول الموحّدة بعد استقبال المحتوى (ملف / صور / نص / محاضرة مفرغة / رفض الكاش):
    1) تصنيف المحتوى تلقائياً (classify_subject)
    2) ضبط جميع الإعدادات الافتراضية مسبقاً:
       - اختبار جاهز محلول (SUBJECT_QUIZ_SOLVED): المسار الافتراضي «استخدام أجوبة المصدر» (as_is)
       - اختبار جاهز غير محلول (SUBJECT_QUIZ_UNSOLVED): المسار الافتراضي «حل بالذكاء الاصطناعي» (ai_solve)
       - محتوى دراسي عادي: نوع الأسئلة «أسئلة شاملة عن الدرس»، الصعوبة «متوسط»، العدد 10 أسئلة
       - محتوى إنجليزي/فرنسي: اللغة الافتراضية «بدون ترجمة» (plain)
    3) عرض شاشة الملخص الموحّدة مباشرة مع 3 أزرار فقط («🚀 إنشاء الاختبار»، «⚙️ إعدادات الاختبار»، «❌ إلغاء الطلب»).
    """
    await state.update_data(count_prompt_text=count_prompt_text)
    data = await state.get_data()
    is_media = data.get("input_type") == "media"
    file_paths = data.get("file_paths", []) or []
    pure_text = data.get("pure_text") if not is_media else None

    if edit:
        try:
            await reply_target.edit_text(MSG_DETECTING_CONTENT, reply_markup=None)
        except Exception:
            pass
        status_target = reply_target
    else:
        status_target = await reply_target.answer(MSG_DETECTING_CONTENT)

    detection_text, detection_paths = pure_text, (file_paths if is_media else None)
    if is_media and file_paths:
        ext = os.path.splitext(file_paths[0])[1].lower()
        if ext in [".docx", ".doc", ".pptx", ".ppt", ".txt"]:
            extracted_text, is_valid = await extract_office_text_if_needed(file_paths[0])
            if is_valid and extracted_text:
                await state.update_data(cached_office_text=extracted_text)
                detection_text, detection_paths = extracted_text, None
                est_pages, word_count = estimate_pages_from_text(extracted_text)
                await state.update_data(items_count=est_pages, estimated_word_count=word_count)
            else:
                detection_text, detection_paths = None, None
    elif not is_media and pure_text:
        est_pages, word_count = estimate_pages_from_text(pure_text)
        await state.update_data(items_count=est_pages, estimated_word_count=word_count)

    try:
        classification = await classify_subject(detection_paths, detection_text, file_hash=data.get("file_hash"))
    except Exception as exc:
        log_error(logger, f"Subject classification failed, continuing with standard mode: {exc}")
        classification = SubjectClassification(subject=SUBJECT_OTHER, suggested_types=[])

    eff_subject = (
        classification.content_subject
        if classification.subject in (SUBJECT_QUIZ_SOLVED, SUBJECT_QUIZ_UNSOLVED) and classification.content_subject
        else classification.subject
    )
    default_english_mode = "plain" if eff_subject in (SUBJECT_ENGLISH, SUBJECT_FRENCH) else None
    default_suggestions = globals().get("DEFAULT_COUNT_SUGGESTIONS", [5, 10, 15, 20])

    if classification.subject in (SUBJECT_QUIZ_SOLVED, SUBJECT_QUIZ_UNSOLVED):
        default_fallback = globals().get("DEFAULT_DETECTED_QUIZ_COUNT_FALLBACK", 10)
        raw_count = classification.question_count or default_fallback
        detected_count = max(1, min(int(raw_count), MAX_QUESTIONS_TO_GENERATE))
        default_extraction_mode = (
            QUIZ_EXTRACTION_MODE_AS_IS
            if classification.subject == SUBJECT_QUIZ_SOLVED
            else QUIZ_EXTRACTION_MODE_AI_SOLVE
        )
        default_notice = (
            MSG_QUIZ_UNSOLVED_DETECTED
            if classification.subject == SUBJECT_QUIZ_UNSOLVED
            else None
        )
        selected_count = detected_count
    else:
        detected_count = None
        default_extraction_mode = None
        default_notice = None
        selected_count = default_suggestions[1]

    await state.update_data(
        subject_type=classification.subject,
        detected_subject_type=classification.subject,
        content_subject_type=classification.content_subject,
        suggested_question_types=classification.suggested_types,
        question_type=QUESTION_TYPE_GENERAL,
        custom_question_type_text=None,
        difficulty=DIFFICULTY_MEDIUM,
        english_mode=default_english_mode,
        quiz_extraction_mode=default_extraction_mode,
        detected_question_count=detected_count,
        detected_exam_question_count=detected_count,
        selected_question_count=selected_count,
        quiz_detection_notice=default_notice,
    )

    file_hash = data.get("file_hash")
    if file_hash and await is_feature_enabled("classification_vote_prompt"):
        try:
            resolved_user_id = user_id if user_id is not None else (
                reply_target.from_user.id if reply_target.from_user else None
            )
            already_locked = await get_classification_lock(file_hash)
            already_voted = (
                await has_user_voted_on_classification(file_hash, resolved_user_id)
                if not already_locked and resolved_user_id else False
            )
            if not already_locked and not already_voted:
                subject_label = CLASSIFICATION_SUBJECT_LABELS_AR.get(classification.subject, classification.subject)
                await status_target.answer(
                    MSG_CLASSIFICATION_VOTE_PROMPT.format(subject_label=subject_label),
                    parse_mode="HTML",
                    reply_markup=get_classification_vote_keyboard(file_hash, classification.subject),
                )
        except Exception as exc:
            log_warning(logger, f"Could not send classification vote prompt for {file_hash}: {exc}")

    show_summary_fn = globals().get("show_question_count_screen") or globals().get("_show_quiz_options_screen")
    await show_summary_fn(status_target, state, edit=True)

async def _render_cache_decision_screen(reply_target: types.Message, state: FSMContext, edit: bool = False, viewer_id: Optional[int] = None) -> None:
    """
    🆕 يعرض/يحدّث شاشة "قرار الكاش" (الكويزات المخزّنة + أزرار الفلترة) اعتماداً على
    available_quizzes وقيم الفلتر الحالية (cache_filter_type/cache_filter_difficulty)
    المخزّنة بحالة الـ FSM. تُستدعى مرة أولى من _finalize_media_processing (edit=False)،
    وبعدها من معالجات الفلترة أدناه مع كل ضغطة فلتر (edit=True، بنفس الرسالة).

    🆕 viewer_id: معرّف الطالب الفعلي الحالي (وليس reply_target.from_user.id، لأنه بمسار
    التعديل reply_target هي رسالة البوت نفسه لا الطالب) - يُمرَّر لإظهار زر حذف كل كويز
    فقط للأدمن أو لمالكه الفعلي، مطابقةً لنفس نمط resolved_user_id بـ
    _finalize_media_processing تحت.
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

    keyboard = await get_multiple_quizzes_keyboard(
        all_quizzes, filtered_quizzes, data.get("items_count", 1), bool(data.get("is_album")),
        show_generate_btn=show_generate_btn, filter_type=filter_type, filter_difficulty=filter_difficulty,
        viewer_id=viewer_id,
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
        await _render_cache_decision_screen(call.message, state, edit=True, viewer_id=call.from_user.id)
    except Exception:
        pass  # "message is not modified" لو نفس الفلتر ضُغط مرتين متتاليتين - آمن التجاهل
    await call.answer()


@router.callback_query(QuizState.waiting_for_cache_decision, F.data.startswith("cachefilter_diff_"))
async def handle_cache_filter_difficulty(call: types.CallbackQuery, state: FSMContext) -> None:
    """🆕 فلترة الكويزات المعروضة بالكاش حسب الصعوبة (محلياً، بدون أي استعلام إضافي)."""
    value = call.data.replace("cachefilter_diff_", "", 1)
    await state.update_data(cache_filter_difficulty=value)
    try:
        await _render_cache_decision_screen(call.message, state, edit=True, viewer_id=call.from_user.id)
    except Exception:
        pass
    await call.answer()


async def _finalize_media_processing(
    message: types.Message, state: FSMContext, file_paths: List[str], title: str, items: int,
    is_album: bool, file_hash: str, content_source: str, user_id: Optional[int] = None, input_channel: str = "telegram",
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
    failure_stage = "media_finalization"
    try:
        content_type = "album" if is_album else ("photo" if len(file_paths) == 1 and file_paths[0].lower().endswith((".jpg", ".jpeg", ".png")) else "document")
        spawn(log_usage_event(resolved_user_id, "content_uploaded", {
            "content_type": content_type, "items_count": items, "is_album": is_album, "file_hash": file_hash, "input_channel": input_channel,
        }))

        cached_quizzes = await get_file_quizzes(file_hash)
        common_state = {
            "file_paths": file_paths, "source_title": title, "input_type": "media",
            "file_hash": file_hash, "items_count": items, "is_album": is_album,
            # 🆕 مصدر/نوع المحتوى الصريح (pdf/docx/pptx/txt/images) - مُحدَّد صراحة عند نقطة
            # الاستقبال الفعلية (handle_media/process_web_uploaded_file/_images)، يُرحَّل
            # لاحقاً لسجل حدث quiz_generated بـhandle_count_start.
            "content_source": content_source,
            "input_channel": input_channel,
        }
        # This is a new, validated input, not an update to the completed quiz.
        # Drop old text, extracted office text, result controls and charge IDs
        # before either the cache-choice or classification path can read them.
        await state.set_data(common_state)
        
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
                available_quizzes=cached_quizzes,
                cache_filter_type="all", cache_filter_difficulty="all",
            )
            await state.set_state(QuizState.waiting_for_cache_decision)
            await _render_cache_decision_screen(message, state, edit=False, viewer_id=resolved_user_id)
            return

        await ask_question_count(message, state, SUCCESS_MEDIA_RECEIVED)
    except Exception as exc:
        for path in file_paths: safe_file_cleanup(path)
        if resolved_user_id is not None:
            track_upload_rejection(
                resolved_user_id, content_source or "document", "processing_failed", input_channel,
                item_count=items, failure_stage=failure_stage, exception_type=type(exc).__name__,
            )
        log_error(logger, f"Finalize media failed: {exc}", exception=exc)
        await message.answer("❌ حدث خطأ غير متوقع أثناء معالجة الوسائط.")

# ==================== Handlers ====================

@router.message(F.document | F.photo)
@user_locked(wait_seconds=2)
async def handle_media(message: types.Message, state: FSMContext) -> None:
    destination = None
    transferred = False
    media_type = "image" if message.photo else "document"
    failure_stage = "preflight"
    try:
        current_state = await state.get_state()
        if await offer_transition(state, message.from_user.id, message.chat.id, message_action(message, "media")):
            return
        if current_state == QuizState.processing_file_quiz:
            # 🩹 طلب توليد فعلي قيد التنفيذ عند Gemini حالياً (بعد ضغط "ابدأ التوليد") - لازم
            # نرفض أي ملف جديد بدل حذف/استبدال الطلب الحالي، وإلا بنحذف الملف يلي عم يُرفع
            # فعلياً لـ Gemini بهالّحظة ونفشّل التوليد بالكامل (راجع handle_count_start).
            track_upload_rejection(message.from_user.id, media_type, "session_busy", "telegram")
            await message.answer("⏳ في طلب توليد كويز قيد المعالجة حالياً؛ يرجى الانتظار حتى ينتهي قبل إرسال ملف جديد.")
            return

        elif current_state in PENDING_REQUEST_STATES:
            if await _discard_pending_upload(state):
                await message.answer(MSG_PREVIOUS_REQUEST_REPLACED)
            await state.clear()

        ensure_directory_exists(DOWNLOADS_DIR)

        document_extension = os.path.splitext(message.document.file_name or "")[1].lower() if message.document else ""
        document_mime = (message.document.mime_type or "").lower() if message.document else ""
        if message.document and (
            document_extension in (AUDIO_EXTENSIONS - {".mp4", ".webm", ".3gp"})
            or document_mime.startswith("audio/")
        ):
            from handlers.audio import handle_audio_message
            target_fn = getattr(handle_audio_message, "__wrapped__", handle_audio_message)
            await target_fn(message, state)
            return
        if message.photo or document_extension in TELEGRAM_IMAGE_DOCUMENT_EXTENSIONS:
            photo = message.photo[-1] if message.photo else message.document
            current = {"file_id": photo.file_id, "file_unique_id": photo.file_unique_id, "file_size": photo.file_size,
                       "extension": ".jpg" if message.photo else document_extension}
            if message.media_group_id:
                group_id = message.media_group_id
                await redis_client.rpush(f"album_list:{group_id}", json.dumps(current))
                await redis_client.expire(f"album_list:{group_id}", 30)
                if await redis_client.set(f"album_lock:{group_id}", "1", nx=True, ex=15):
                    await message.answer("📥 جارٍ تجميع الصور ومعالجة الألبوم بالخلفية...")
                    spawn(process_album_background(message, state))
                return
            else:
                failure_stage = "telegram_download"
                file_paths, err = await download_photos_service(message.from_user.id, [current])
                if err or not file_paths:
                    track_upload_rejection(message.from_user.id, "image", "download_failed", "telegram")
                    if err:
                        await message.answer(err)
                    return
                file_hash = await asyncio.to_thread(compute_combined_hash, file_paths)
                await _finalize_media_processing(message, state, file_paths, "كويز من صورة", 1, False, file_hash, content_source="images")
        else:
            valid, error = validate_file_size(message.document.file_size, "document")
            if not valid:
                track_upload_rejection(
                    message.from_user.id, "document", "size_exceeded", "telegram",
                    file_size_bytes=message.document.file_size, max_size_bytes=MAX_DOC_SIZE,
                )
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
            failure_stage = "file_type_validation"
            if document_extension == ".xls":
                track_upload_rejection(message.from_user.id, "document", "unsupported_type", "telegram")
                await message.answer("❌ احفظ ملف Excel بصيغة .xlsx ثم أعد إرساله للبوت.")
                return
            extension = supported_extension(message.document.file_name, DOCUMENT_EXTENSIONS | {".xlsx"})
            title = os.path.splitext(message.document.file_name)[0]
            destination = os.path.join(DOWNLOADS_DIR, f"{message.from_user.id}_{uuid.uuid4().hex}{extension}")
            failure_stage = "telegram_download"
            await bot.download(message.document, destination=destination)

            if extension == ".xlsx":
                await _import_excel_quiz(message, state, destination, title)
                return
            
            failure_stage = "content_validation"
            items = await asyncio.to_thread(validate_document, destination)
            if destination.lower().endswith(".pdf"):
                failure_stage = "page_validation"
                items = await asyncio.to_thread(get_pdf_page_count_sync, destination)
                if items > MAX_SUPER_PAGES:
                    track_upload_rejection(
                        message.from_user.id, "document", "page_limit_exceeded", "telegram",
                        page_count=items, max_page_count=MAX_SUPER_PAGES,
                    )
                    await message.answer(f"❌ الحد الأقصى لمعالجة ملفات PDF هو {MAX_SUPER_PAGES} صفحة.")
                    safe_file_cleanup(destination)
                    return
            file_hash = await asyncio.to_thread(calculate_file_hash, destination)
            await _finalize_media_processing(
                message, state, [destination], title, items, False, file_hash,
                content_source=_document_content_source(extension),
            )

            transferred = True

    except ExcelImportError as exc:
        await message.answer(f"❌ {exc}", parse_mode=None)
    except Exception as exc:
        reason = {
            "file_type_validation": "unsupported_type",
            "content_validation": "invalid_content",
            "page_validation": "page_count_failed",
            "telegram_download": "download_failed",
        }.get(failure_stage, "processing_failed")
        track_upload_rejection(
            message.from_user.id, media_type, reason, "telegram",
            failure_stage=failure_stage, exception_type=type(exc).__name__,
        )
        log_error(logger, f"Media handling failed: {exc}", exception=exc)
        await message.answer("❌ حدث خطأ غير متوقع أثناء معالجة الوسائط.")
    finally:
        if destination and not transferred:
            safe_file_cleanup(destination)


async def _import_excel_quiz(message, state, path, title):
    # handle_media owns the user lock; do not reacquire start_loaded_quiz's lock.
    title, questions = await asyncio.to_thread(read_quiz_xlsx, path, title)
    await state.set_data({"input_type": "excel", "content_source": "xlsx", "input_channel": "telegram"})
    await message.answer(f"✅ تم استيراد {len(questions)} سؤالاً من Excel مع الإجابات. سيبدأ الكويز الآن، دون خصم نقاط.")
    await _start_loaded_quiz(message, state, questions, title, origin="excel_import")

@router.message(StateFilter(None, QuizState.answering_quiz), F.text, ~F.text.startswith("/"), F.text != ".")
@user_locked()
async def handle_pure_text(message: types.Message, state: FSMContext) -> None:
    text = message.text.strip()
    action = message_action(message, "text") if len(text) >= 30 else {"kind": "stop"}
    if await offer_transition(state, message.from_user.id, message.chat.id, action):
        return

    if len(text) < 30:
        track_upload_rejection(message.from_user.id, "text", "text_too_short", "telegram", text_length=len(text), max_text_length=30)
        await message.answer("⚠️ النص قصير جداً؛ أرسل 30 حرفاً على الأقل لضمان دقة الأسئلة.")
        return
    if len(text) > MAX_TEXT_INPUT_SIZE:
        track_upload_rejection(message.from_user.id, "text", "text_too_long", "telegram", text_length=len(text), max_text_length=MAX_TEXT_INPUT_SIZE)
        await message.answer(f"❌ الحد الأقصى للنص المباشر هو {MAX_TEXT_INPUT_SIZE} حرفاً.")
        return

    # إصلاح التتبع: تسجيل حدث رفع النص المباشر
    spawn(log_usage_event(message.from_user.id, "content_uploaded", {
        "content_type": "text", "text_length": len(text)
    }))

    # Text has no file identity. Retaining a previous file_hash would read or
    # overwrite the shared classification/quiz cache for an unrelated file.
    await state.set_data({
        "pure_text": text, "source_title": text[:20] + "...", "input_type": "text",
        "items_count": 1, "is_album": False,
        "content_source": "telegram_text", "input_channel": "telegram",
    })
    await ask_question_count(
        message, state,
        "✅ تم استقبال النص بنجاح. كم سؤالاً تريد توليده من هذا المحتوى؟\nاختر من الأزرار أدناه، أو أرسل رقماً مخصصاً مباشرة."
    )

# ==================== 🆕 معالجات اختيار "مترجمة/بدون ترجمة" للمحتوى الإنجليزي والفرنسي ====================

async def _apply_translation_choice(reply_target: types.Message, state: FSMContext, english_mode: str, edit: bool) -> None:
    """يحفظ اختيار الطالب (مترجمة/بدون ترجمة) بالحالة ويعيده مباشرة إلى شاشة الملخص الموحدة."""
    await state.update_data(english_mode=english_mode)
    await show_question_count_screen(reply_target, state, edit=edit)

@router.callback_query(StateFilter(*PENDING_REQUEST_STATES), F.data == "translate_choice_yes")
async def handle_translate_choice_yes(call: types.CallbackQuery, state: FSMContext) -> None:
    """الطالب اختار الأسئلة مترجمة (إنجليزي/فرنسي + عربي)"""
    try:
        await _apply_translation_choice(call.message, state, "translated", edit=True)
    except Exception as exc:
        log_error(logger, f"Translation choice (yes) failed: {exc}", exception=exc)
        data = await state.get_data()
        await call.message.answer(
            "❌ تعذر تنفيذ الاختيار، حاول مجدداً.",
            reply_markup=get_translation_choice_keyboard(data.get("content_subject_type") or data.get("subject_type", SUBJECT_ENGLISH)),
        )
    finally:
        await call.answer()

@router.callback_query(StateFilter(*PENDING_REQUEST_STATES), F.data == "translate_choice_no")
async def handle_translate_choice_no(call: types.CallbackQuery, state: FSMContext) -> None:
    """الطالب اختار الأسئلة بلغتها الأصلية فقط (إنجليزي أو فرنسي) بدون ترجمة"""
    try:
        await _apply_translation_choice(call.message, state, "plain", edit=True)
    except Exception as exc:
        log_error(logger, f"Translation choice (no) failed: {exc}", exception=exc)
        data = await state.get_data()
        await call.message.answer(
            "❌ تعذر تنفيذ الاختيار، حاول مجدداً.",
            reply_markup=get_translation_choice_keyboard(data.get("content_subject_type") or data.get("subject_type", SUBJECT_ENGLISH)),
        )
    finally:
        await call.answer()

# ==================== 🆕 معالجات اختيار مسار الاختبار الجاهز ====================

async def _apply_quiz_extraction_choice(call: types.CallbackQuery, state: FSMContext, extraction_mode: str) -> None:
    """يحفظ اختيار الطالب (استخدام أجوبة المصدر / حل بالذكاء الاصطناعي) ويعيده مباشرة لشاشة الملخص الموحدة."""
    data = await state.get_data()
    origin_subject = data.get("detected_subject_type") or data.get("subject_type")
    if extraction_mode == QUIZ_EXTRACTION_MODE_AS_IS and origin_subject != SUBJECT_QUIZ_SOLVED:
        return
    updates = {
        "subject_type": origin_subject,
        "quiz_extraction_mode": extraction_mode,
        "question_type": QUESTION_TYPE_GENERAL,
        "custom_question_type_text": None,
    }
    if "detected_exam_question_count" in data and data.get("detected_question_count") is None:
        updates["detected_question_count"] = data.get("detected_exam_question_count")
        updates["selected_question_count"] = data.get("detected_exam_question_count")
    await state.update_data(**updates)
    await show_question_count_screen(call.message, state, edit=True)

@router.callback_query(StateFilter(*PENDING_REQUEST_STATES), F.data == "quiz_generate_new")
async def handle_quiz_generate_new(call: types.CallbackQuery, state: FSMContext) -> None:
    """Reuse detection and suggestions, but switch to ordinary generation and return to summary."""
    data = await state.get_data()
    origin_subject = data.get("detected_subject_type") or data.get("subject_type")
    if origin_subject not in (SUBJECT_QUIZ_SOLVED, SUBJECT_QUIZ_UNSOLVED):
        await call.answer()
        return
    await state.update_data(
        subject_type=data.get("content_subject_type") or SUBJECT_OTHER,
        quiz_extraction_mode=None, quiz_detection_notice=None,
        question_type=QUESTION_TYPE_GENERAL, custom_question_type_text=None,
        difficulty=DIFFICULTY_MEDIUM, detected_question_count=None,
        selected_question_count=None, review_count_to_restore=None,
        count_prompt_text="📝 كم سؤالاً جديداً تريد توليده من هذا المحتوى؟",
    )
    next_screen = globals().get("show_question_count_screen") or globals().get("_show_quiz_options_screen")
    await next_screen(call.message, state, edit=True)
    await call.answer()


@router.callback_query(StateFilter(*PENDING_REQUEST_STATES), F.data == "quiz_generate_similar_math")
async def handle_quiz_generate_similar_math(call: types.CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    if data.get("content_subject_type") != SUBJECT_MATH:
        await call.answer("هذا الخيار مخصص لمحتوى الرياضيات.", show_alert=True)
        return
    await state.update_data(
        question_type=QUESTION_TYPE_SIMILAR_MATH, custom_question_type_text=None,
        quiz_extraction_mode=None, quiz_detection_notice=None,
        selected_question_count=None, review_count_to_restore=None,
        count_prompt_text="🧩 كم مسألة جديدة تريد توليدها بنفس النمط؟",
    )
    next_screen = globals().get("show_question_count_screen")
    if next_screen is not None:
        await next_screen(call.message, state, edit=True)
    else:
        await state.set_state(QuizState.waiting_for_quiz_options)
        from handlers.quiz_options import _render_difficulty_screen
        await _render_difficulty_screen(call.message, state, edit=True)
    await call.answer()


@router.callback_query(StateFilter(*PENDING_REQUEST_STATES), F.data == "quiz_extract_as_is")
async def handle_quiz_extract_as_is(call: types.CallbackQuery, state: FSMContext) -> None:
    """الطالب اختار اعتماد الأجوبة المدوّنة بالمستند كما هي دون حلّها بالذكاء الاصطناعي."""
    try:
        await _apply_quiz_extraction_choice(call, state, QUIZ_EXTRACTION_MODE_AS_IS)
    except Exception as exc:
        log_error(logger, f"Quiz extraction choice (as_is) failed: {exc}", exception=exc)
        await call.message.answer(
            "❌ تعذر تنفيذ الاختيار، حاول مجدداً.", reply_markup=get_quiz_extraction_choice_keyboard(),
        )
    finally:
        await call.answer()

@router.callback_query(StateFilter(*PENDING_REQUEST_STATES), F.data == "quiz_extract_ai_solve")
async def handle_quiz_extract_ai_solve(call: types.CallbackQuery, state: FSMContext) -> None:
    """الطالب اختار تجاهل أجوبة المستند وحلّ الاختبار بالذكاء الاصطناعي بدلاً منها."""
    try:
        await _apply_quiz_extraction_choice(call, state, QUIZ_EXTRACTION_MODE_AI_SOLVE)
    except Exception as exc:
        log_error(logger, f"Quiz extraction choice (ai_solve) failed: {exc}", exception=exc)
        await call.message.answer(
            "❌ تعذر تنفيذ الاختيار، حاول مجدداً.", reply_markup=get_quiz_extraction_choice_keyboard(),
        )
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
        cost = await calculate_cached_points_cost(
            int(data.get("items_count", 1)), len(selected_quiz["quiz_data"]), bool(data.get("is_album"))
        )

        if float(user_info["points"]) < cost:
            await _insufficient_balance(call.message, user_info, cost, action="cached_quiz_use")
            return
            
        try:
            remaining = await update_user_stats(call.from_user.id, cost, len(selected_quiz["quiz_data"]))
        except WalletDebitError:
            await call.message.answer(WALLET_DEBIT_ERROR_TEXT)
            return
        if remaining is None:
            await _insufficient_balance(call.message, await _current_user(call.message, call.from_user), cost, action="cached_quiz_use")
            return
            
        spawn(log_usage_event(call.from_user.id, "cached_quiz_used", {
            "quiz_id": quiz_uuid, "cost": cost,
            # 🆕 يُعرض الآن بلوحة "📊 سجل توليد الكويزات" (راجع admin_get_quiz_generation_log
            # وai_control.py) جنب توليد AI الفعلي - questions_generated هون هو نفس المفتاح
            # المستخدَم بصف quiz_generated العادي، لعرض عدد الأسئلة بنفس السطر بلا فرع خاص.
            "questions_generated": len(selected_quiz["quiz_data"]),
            "input_channel": data.get("input_channel"),
            "content_source": data.get("content_source"),
        }))

        await start_loaded_quiz(call, state, selected_quiz["quiz_data"], data.get("source_title", "كويز"), origin="cached_file", quiz_id=quiz_uuid)
        
        for path in data.get("file_paths", []):
            safe_file_cleanup(path)
    except Exception as exc:
        log_error(logger, f"Multi-cached selection trigger failed: {exc}", exception=exc)
        await call.message.answer("❌ تعذر بدء تشغيل الاختبار المخزّن.")

@router.callback_query(QuizState.waiting_for_cache_decision, F.data == "cache_action_no")
async def handle_cache_no(call: types.CallbackQuery, state: FSMContext) -> None:
    """في حال رفض الكاش ورغبة الطالب بتوليد كويز جديد كلياً"""
    await ask_question_count(
        call.message, state,
        "📝 كم سؤالاً تريد استخراجه وتوليده من هذا المحتوى؟\nاختر من الأزرار أدناه، أو أرسل رقماً مخصصاً مباشرة.",
        edit=True,
        user_id=call.from_user.id,
    )
    await call.answer()

# ==================== معالجات شاشة عدد الأسئلة المدمجة (اختيار + تكلفة + بدء) ====================

@router.callback_query(QuizState.waiting_for_count, F.data.startswith("qcount_pick_"))
async def handle_count_pick(call: types.CallbackQuery, state: FSMContext) -> None:
    """اختيار عدد من الأزرار السريعة (toggle) - يحدّث نفس الشاشة (سعر + زر البدء) فوراً."""
    data = await state.get_data()
    if (data.get("subject_type") in (SUBJECT_QUIZ_SOLVED, SUBJECT_QUIZ_UNSOLVED)
            and data.get("question_type") != QUESTION_TYPE_SIMILAR_MATH):
        await call.answer("عدد أسئلة الاختبار يُحدّد تلقائياً من محتواه.")
        return
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


@router.callback_query(StateFilter(*PENDING_REQUEST_STATES), F.data == "qcount_custom")
async def handle_count_custom_prompt(call: types.CallbackQuery, state: FSMContext) -> None:
    """يطلب من الطالب كتابة عدد مخصص - نفس الرسالة تتحول لطلب إدخال نصي (edit)."""
    try:
        if hasattr(state, "set_state") and "QuizState" in globals():
            await state.set_state(QuizState.waiting_for_count)
        await call.message.edit_text(
            "✏️ اكتب الآن عدد الأسئلة المطلوب (رقم فقط):",
            reply_markup=types.InlineKeyboardMarkup(inline_keyboard=[
                [types.InlineKeyboardButton(text="🔙 رجوع للإعدادات", callback_data="qsetup_menu")],
                [types.InlineKeyboardButton(text="🔙 الرجوع للملخص", callback_data="qcount_review")],
                [types.InlineKeyboardButton(text=BTN_CANCEL_REQUEST, callback_data="cancel_upload_request")],
            ]),
        )
    except Exception as exc:
        log_error(logger, f"Custom count prompt failed: {exc}", exception=exc)
    finally:
        await call.answer()


@router.callback_query(StateFilter(*PENDING_REQUEST_STATES), F.data == "qcount_review")
async def back_to_count_review(call: types.CallbackQuery, state: FSMContext):
    if hasattr(state, "set_state") and "QuizState" in globals():
        await state.set_state(QuizState.waiting_for_count)
    await _render_question_count_screen(call.bot, call.message.chat.id,
        call.message.message_id, state, target_for_send=call.message)
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


def _track_quiz_generation_failure(
    user_id: int,
    data: Dict[str, Any],
    reason: str,
    *,
    refund_status: str,
    cost: Optional[float] = None,
    requested_count: Optional[int] = None,
    items_count: Optional[int] = None,
    mode: Optional[str] = None,
    failure_stage: str = "generation",
    exception_type: Optional[str] = None,
    generation_meta: Optional[Dict[str, Any]] = None,
) -> None:
    """Record a structured generation failure without logging source content."""
    generation_meta = generation_meta if isinstance(generation_meta, dict) else {}
    metadata = {
        "failure_reason": str(reason)[:64],
        "refund_status": str(refund_status)[:24],
        "failure_stage": str(failure_stage)[:32],
        "requested_count": requested_count,
        "items_count": items_count,
        "cost": cost,
        "business_mode": mode,
        "question_type": data.get("question_type"),
        "quiz_extraction_mode": data.get("quiz_extraction_mode"),
        "content_source": data.get("content_source"),
        "input_channel": data.get("input_channel"),
        "ai_provider": generation_meta.get("provider"),
        "ai_model": generation_meta.get("model"),
        "generation_seconds": generation_meta.get("duration_seconds"),
        "exception_type": exception_type,
    }
    spawn(log_usage_event(user_id, "quiz_generation_failed", {
        key: value for key, value in metadata.items() if value is not None
    }))


@router.callback_query(QuizState.waiting_for_count, F.data == "qcount_start")
async def handle_count_start(call: types.CallbackQuery, state: FSMContext) -> None:
    """
    🆕 زر "ابدأ التوليد" بنهاية شاشة عدد الأسئلة - يدمج (كانا سابقاً خطوتين منفصلتين):
    1) التحقق من السقف/الرصيد وحساب التكلفة (كان _process_question_count سابقاً)
    2) الخصم الفعلي وتنفيذ التوليد (كان handle_confirm_quiz_generation سابقاً)
    كل ذلك بضغطة واحدة أخيرة على نفس الشاشة التي عرضت السعر مسبقاً - يطابق الخطة
    الأصلية: "خيارات مسعّرة مسبقاً ... وزر (ابدأ التوليد) بنهاية القائمة".

    🩹 إصلاح خلل حقيقي (سبب مباشر لخطأ "is not a valid file path" عبر كل المفاتيح/
    الموديلات دفعة واحدة باللوغز): ضغطة مزدوجة سريعة على هذا الزر (شائعة على الموبايل،
    خصوصاً إن الرسالة ما تختفي فوراً) كانت تُشغّل هذا المعالج مرتين بالتوازي لنفس الطالب
    على نفس ملفات state.file_paths. أول تنفيذ ينتهي (نجاحاً أو فشلاً) كان يحذف الملفات
    عبر finally بالأسفل، بينما التنفيذ الثاني لسا يرفعها فعلياً لـ Gemini بمنتصف الـ
    cascade - يفشل فوراً بخطأ "is not a valid file path" عبر كل زوج (مفتاح، موديل).
    قفل Redis بملكية محددة ومدة صلاحية متجددة يمنع التداخل بين العمليات والنسخ المختلفة.
    """
    await call.answer()
    user_id = call.from_user.id
    lock = UserLock(redis_client, user_id)
    try:
        acquired = await lock.acquire()
    except Exception:
        await call.message.answer("❌ تعذر بدء المعالجة حالياً، يرجى إعادة المحاولة بعد قليل.")
        return
    if not acquired:
        await call.message.answer("⏳ طلبك قيد المعالجة بالفعل، الرجاء الانتظار حتى ينتهي قبل الضغط مجدداً.")
        return
    charged_this_attempt = False
    refunded_this_attempt = False
    # 🩹 يبقى True عندما نعود لحالة waiting_for_count بانتظار إعادة محاولة الطالب (رصيد غير
    # كافٍ / عدد أسئلة غير صالح): الملف المرفوع لا يُحذف حينها، وإلا بقيت الحالة تشير لملف
    # محذوف فيفشل التوليد لاحقاً عند الضغط مجدداً (راجع finally بنهاية الدالة). الحذف يصير
    # فقط عند الإلغاء/استبدال الطلب (handle_cancel_upload/handle_media) أو انتهاء التوليد.
    preserve_upload = False
    quiz_ready = False  # True بعد نجاح التوليد (لتمييز "فشل بالعرض" عن "فشل قبل/أثناء التوليد")
    data = {}
    try:
        if await state.get_state() != QuizState.waiting_for_count:
            return
        data = await state.get_data()
        count = int(data.get("selected_question_count") or 0)
        valid, error = validate_question_count(count)
        if not valid:
            preserve_upload = True
            await call.message.answer(f"❌ {error}")
            return

        # 🩹 إصلاح خلل حقيقي (السبب الفعلي لأخطاء "no such file"/"is not a valid file
        # path" بلوغز الإنتاج): كان القفل على processing_file_quiz يُفعَّل متأخراً جداً -
        # بعد فحص سقف التركيبة (get_file_quizzes)، حساب التكلفة، جلب/خصم الرصيد
        # (_current_user وupdate_user_stats - نداءات شبكة فعلية لـ Supabase قد تاخد
        # ثواني تحت الضغط)، حذف رسالة الشاشة، وإرسال رسالة "جاري المعالجة". طوال هالنافذة
        # (رُصدت عملياً بعشرات الثواني باللوغز) كانت الحالة لسا waiting_for_count - عضو
        # PENDING_REQUEST_STATES - فلو الطالب أرسل ملف جديد بهالأثناء، handle_media كان
        # يفسّرها "استبدال طلب معلّق" ويحذف نفس ملفات state.file_paths يلي هالمعالج قرأها
        # بأول سطر فوق (data محلية، ما بتنعاد قراءتها) وبينوي يرفعها لاحقاً لـ Gemini -
        # فيفشل التوليد بالكامل بمجرد ما يوصلها. نقل القفل هون - فوراً بعد التحقق من صحة
        # العدد وقبل أي await آخر - يسكّر النافذة الفعلية بالكامل بدل ما يسكّرها متأخراً
        # بعد كل هالنداءات البطيئة. (راجع أيضاً استرجاع الحالة على مسارات الخروج المبكر
        # تحت - وإلا الطالب بيعلق بحالة processing_file_quiz بدون أي توليد فعلي شغال).
        await state.set_state(QuizState.processing_file_quiz)

        # 🩹 فحص وجود الملف *قبل* أي خصم: لو حُذف من القرص (إعادة تشغيل السيرفر مثلاً) لا
        # معنى لمتابعة الخصم ثم الفشل ثم الاسترجاع - نُبلغ الطالب بسبب الفشل الحقيقي
        # ونمسح الطلب ليرسل الملف من جديد.
        if _missing_upload_paths(data):
            log_warning(logger, f"Upload file missing before generation start (user {user_id}): {data.get('file_paths')}")
            await state.clear()
            await call.message.answer(f"{MSG_UPLOAD_FILE_MISSING}\nلم يتم خصم أي نقاط.", parse_mode="HTML")
            return

        items = int(data.get("items_count") or 1)
        is_album = bool(data.get("is_album"))
        file_hash = data.get("file_hash")

        subject_type_for_limit = data.get("subject_type", "other")
        # 🆕 إلغاء سقف عدد الكويزات (MAX_FILE_QUIZZES_LIMIT) لنوع "اختبار" (استخراج اختبار
        # جاهز محلول/غير محلول - SUBJECT_QUIZ_SOLVED/SUBJECT_QUIZ_UNSOLVED): هذا السقف
        # مصمَّم أصلاً لتنويع الكويزات المولَّدة بالذكاء الاصطناعي من نفس الملف (تجنّب
        # تكرار نفس المحتوى بلا داعٍ)، بينما هنا كل استخراج ينقل محتوى اختبار حقيقي محدَّد
        # مسبقاً بالمستند نفسه - لا معنى لتحديد عدد مرات استخراجه.
        if file_hash and (subject_type_for_limit not in (SUBJECT_QUIZ_SOLVED, SUBJECT_QUIZ_UNSOLVED) or data.get("question_type") == QUESTION_TYPE_SIMILAR_MATH):
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

        cost = await calculate_quiz_points_cost(items, count, is_album)
        mode = determine_execution_mode(items, count, is_album=is_album, content_source=data.get("content_source"))
        user_info = await _current_user(call.message, call.from_user)

        deduction = None
        if float(user_info["points"]) >= cost:
            try:
                deduction = await update_user_stats(call.from_user.id, cost, count)
            except WalletDebitError:
                await state.set_state(QuizState.waiting_for_count)
                preserve_upload = True
                await call.message.answer(WALLET_DEBIT_ERROR_TEXT)
                return
        if deduction is None:
            # 🩹 لسا ما بدأنا الخصم/التوليد فعلياً - نرجّع الحالة لـ waiting_for_count
            # (بدل ما تضل عالقة بـ processing_file_quiz بلا أي توليد شغال فعلياً) حتى
            # يقدر الطالب يلغي الطلب أو يستبدله بملف جديد عادي.
            await state.set_state(QuizState.waiting_for_count)
            preserve_upload = True  # الطالب سيشحن/ينتظر التجديد ثم يعيد الضغط على نفس الملف
            fresh_balance = await _current_user(call.message, call.from_user)
            await _insufficient_balance(call.message, fresh_balance, cost, action="quiz_generation")
            return

        # من هذه اللحظة فقط يحق لهذا التنفيذ أن يعمل refund. هذا يمنع أي بيانات
        # debited_* قديمة بقيت في FSM من عملية سابقة من التسبب باسترجاع مضاعف.
        charged_this_attempt = True

        spawn(log_usage_event(call.from_user.id, "quiz_generation_requested", {
            "requested_count": count, "items_count": items, "cost": cost, "mode": mode,
            "question_type": data.get("question_type"), "quiz_extraction_mode": data.get("quiz_extraction_mode")
        }))

        # 🆕 [طبقة 2] نخزّن التقسيم الفعلي (كم اتخصم من free_points وكم من paid_points -
        # راجع deduct_user_points_atomic) بجانب debited_cost الإجمالي، عشان أي ريفوند
        # لاحق (refund_user_on_failure بـservices/quiz_service.py) يرجع كل جزء لمصدره
        # الصحيح بدل ما يرجعه كله لـpaid_points.
        #
        # نحدّث النسخة المحلية أولاً قبل أي await على Redis. لو انقطع Redis مباشرةً
        # بعد نجاح الخصم، يبقى لدينا سجل الخصم الحالي محلياً ونستطيع عمل refund صحيح.
        data["debited_cost"] = cost
        data["debit_id"] = deduction["debit_id"]
        data["debited_gift"] = deduction["debited_gift"]
        data["debited_free"] = deduction["debited_free"]
        data["debited_paid"] = deduction["debited_paid"]
        await state.update_data(
            debited_cost=cost, calculated_cost=cost, requested_count=count, execution_mode=mode,
            debited_free=deduction["debited_free"], debited_paid=deduction["debited_paid"],
            debit_id=deduction["debit_id"], debited_gift=deduction["debited_gift"],
        )
        try:
            await call.message.delete()
        except Exception:
            pass

        # 🆕 رسالة الانتظار بتقدير زمني (services/generation_eta.py). تجميلي بحت: أي فشل بالتقدير يُبقي
        # النص القديم (MSG_PROCESSING ثم نص النبضة العام) ولا يؤثر على التوليد أو الخصم أو الاسترجاع.
        eta = None
        initial_text = MSG_PROCESSING
        try:
            eta = estimate_generation_eta(data, items, count, mode, is_album=is_album)
            initial_text = build_processing_text(0, eta)
        except Exception as exc:
            eta = None
            log_warning(logger, f"Generation ETA unavailable, using the generic waiting message: {exc}")
        status_msg = await call.message.answer(initial_text)

        # (ملاحظة: قفل processing_file_quiz صار يُفعَّل فوراً بأول الدالة فوق - راجع
        # التعليق هناك - قبل أي await بطيء، وليس هون بعد الخصم والحذف والإرسال).

        # 🩹 UX: Gemini بيصير أحياناً overloaded لدقائق طويلة (شفنا حالات وصلت ~20 دقيقة
        # باللوغز) وMSG_PROCESSING بيوعد بـ"ثوانٍ معدودة" فقط - هالفجوة كانت تدفع الطالب
        # يعيد إرسال نفس الملف ظناً إنو الطلب ضاع (وهو تحديداً السبب يلي كان يفعّل خلل حذف
        # الملف الموصوف فوق). heartbeat خفيف هون بيحدّث نفس الرسالة كل ~20 ثانية بوقت
        # منقضي واضح، بلا أي تأثير على منطق التوليد نفسه - يُلغى تلقائياً بمجرد انتهاء
        # execute_quiz_generation_workflow (نجاحاً أو فشلاً) عبر finally.
        heartbeat_task = asyncio.create_task(_run_processing_heartbeat(status_msg, eta))
        try:
            # status_message=None عمداً: أنيميشن helpers/gemini_helper/loading.py كان يكتب على نفس الرسالة
            # ويمحو التوقيت خلال 3 ثوانٍ؛ النبضة أعلاه هي المحرّك الوحيد الآن (docs/generation-eta-policy.md).
            quiz_data, new_quiz_id, error_code, generation_meta = await execute_quiz_generation_workflow(call.from_user.id, data, count, None)
        finally:
            heartbeat_task.cancel()

        if error_code in ("extraction_count_mismatch", "ai_timeout"):
            if charged_this_attempt:
                await refund_user_on_failure(call.from_user.id, data)
                charged_this_attempt = False
                refunded_this_attempt = True
            _track_quiz_generation_failure(
                call.from_user.id, data, error_code,
                refund_status="refunded" if refunded_this_attempt else "not_charged",
                cost=cost, requested_count=count, items_count=items, mode=mode,
                generation_meta=generation_meta,
            )
            await _clear_quiz_charge_markers(state)
            preserve_upload = True
            await state.set_state(QuizState.waiting_for_count)
            actual_count = (generation_meta or {}).get("extracted_count", 0)
            if error_code == "ai_timeout":
                await status_msg.edit_text(
                    "⏳ استغرقت خدمة توليد الأسئلة وقتاً أطول من المهلة المخصصة لطلبك.\n"
                    "أوقفنا المحاولة وأرجعنا نقاطك. ملفك محفوظ؛ يمكنك إعادة المحاولة "
                    "أو تقليل عدد الأسئلة."
                )
            else:
                await status_msg.edit_text(
                    f"⚠️ تمكّنا من استخراج {actual_count} سؤال من أصل {count} معتمداً.\n"
                    "لم نبدأ كويزاً ناقصاً، وتم إرجاع نقاطك. "
                    "تحقق من الاختبار وعدّل العدد أو أعد المحاولة."
                )
            await _render_question_count_screen(
                call.bot, call.message.chat.id, None, state, target_for_send=call.message,
            )
            return

        if error_code == "unreadable_office":
            if charged_this_attempt:
                await refund_user_on_failure(call.from_user.id, data)
                charged_this_attempt = False
                refunded_this_attempt = True
            _track_quiz_generation_failure(
                call.from_user.id, data, "unreadable_office",
                refund_status="refunded" if refunded_this_attempt else "not_charged",
                cost=cost, requested_count=count, items_count=items, mode=mode,
                failure_stage="content_extraction", generation_meta=generation_meta,
            )
            await _clear_quiz_charge_markers(state)
            await state.set_state(None)
            await status_msg.edit_text("⚠️ <b>تعذر استخراج نص مفيد من المستند!</b> يرجى التأكد من أنه يحتوي على نصوص وليس صوراً. تم إرجاع نقاطك.", parse_mode="HTML")
            return
        elif error_code == "ai_failed" or not quiz_data:
            if charged_this_attempt:
                await refund_user_on_failure(call.from_user.id, data)
                charged_this_attempt = False
                refunded_this_attempt = True
            _track_quiz_generation_failure(
                call.from_user.id, data, error_code or "empty_generation_result",
                refund_status="refunded" if refunded_this_attempt else "not_charged",
                cost=cost, requested_count=count, items_count=items, mode=mode,
                failure_stage="ai_generation", generation_meta=generation_meta,
            )
            await _clear_quiz_charge_markers(state)
            await state.set_state(None)
            await status_msg.edit_text("⚠️ <b>فشل توليد الأسئلة!</b> رصيدك آمن ولم يتم خصم أي نقاط.", parse_mode="HTML")
            return

        # 🆕 (لوحة الأدمن: "📊 سجل توليد الكويزات") - نُرفق بيانات آخر توليد ناجح (الموديل
        # ومزوّده والمدة الكاملة بالثواني، راجع gemini_helper.get_last_generation_metadata)
        # ضمن نفس حدث quiz_generated - لا حاجة لجدول/حدث جديد منفصل.
        # 🆕 التوكنز (input/output/thoughts/total) مرفقة بنفس generation_meta أصلاً (نفس
        # المصدر يلي بيتغذّى منه ai_generation_log بـ services/quiz_service.py) - إضافتها
        # هون بتخلي شاشة "سجل توليد الكويزات" تعرض التفصيل + التكلفة التقديرية لكل كويز
        # بدون أي حاجة لربط جدولين بالوقت/المستخدم (طريقة هشة كنا رح نتجنبها).
        # Saved quiz is successful; auxiliary errors must not cause a refund.
        charged_this_attempt = False
        quiz_ready = True
        await _clear_quiz_charge_markers(state)
        try:
            generation_meta = generation_meta or {}
            await log_usage_event(call.from_user.id, "quiz_generated", {
                "quiz_id": new_quiz_id, "questions_generated": len(quiz_data), "cost": cost,
                "question_type": data.get("question_type"), "quiz_extraction_mode": data.get("quiz_extraction_mode"),
                "ai_provider": generation_meta.get("provider"),
                "ai_model": generation_meta.get("model"),
                "generation_seconds": generation_meta.get("duration_seconds"),
                "input_tokens": generation_meta.get("input_tokens", 0),
                "output_tokens": generation_meta.get("output_tokens", 0),
                "thoughts_tokens": generation_meta.get("thoughts_tokens", 0),
                "total_tokens": generation_meta.get("total_tokens", 0),
                "cascade_rank": generation_meta.get("cascade_rank"),
                "cascade_total": generation_meta.get("cascade_total"),
                "generation_mode": generation_meta.get("mode"),
                # 🆕 FIX: سبب تخطّي Groq الاستباقي لو صار ("text_too_long") - None بالحالة
                # العادية (راجع gemini_helper._generate_text_quiz + _finalize بـ generate_quiz_smart).
                "groq_skip_reason": generation_meta.get("groq_skip_reason"),
                # 🆕 تفصيل كل جزء بمسار Super PDF/Images (موديل + مفتاح + رتبته بالكاسكيد لحظة
                # نجاحه) - None لأي مسار غير super (راجع gemini_helper._generate_super_pdf/
                # _generate_super_images). يُعرض بلوحة الأدمن "📊 سجل توليد الكويزات".
                "chunk_details": generation_meta.get("chunk_details"),
                # 🆕 تصنيف "مستوى الطلب" الحقيقي (Standard/Over-Limit/Super-PDF/Super-Images -
                # راجع determine_execution_mode بـ services/quiz_service.py) - محسوب أصلاً
                # فوق بهذه الدالة (متغيّر `mode`) قبل التوليد لغرض التسعير، ونُرفقه هنا أيضاً
                # لأن `generation_mode` وحده (من gemini_helper) لا يفرّق بين Standard وOver-Limit:
                # الاتنين ينفَّذان عبر _generate_regular بالضبط (نفس مسار الكود)، فـ
                # `generation_mode` بيرجع "regular" لكليهما. لوحة الأدمن (ai_control.py) بتعتمد
                # `generation_mode` كمصدر حقيقة أساسي لـ"سوبر" (فعلي 100%)، وبترجع لهذا الحقل
                # فقط للتفريق بين عادي/موسّع بالحالة غير-السوبر.
                "business_mode": mode,
                # 🆕 عدد "الصفحات" الفعلي/المُقدَّر المُستخدَم بحساب التصنيف/التسعير أعلاه -
                # لم يكن مُسجَّلاً بهذا السجل إطلاقاً سابقاً (راجع services/quiz_service.py:
                # estimate_pages_from_text لتفاصيل كيف يُقدَّر لمصادر بلا صفحات فعلية).
                "items_count": items,
                # 🆕 None لملف PDF/صورة حقيقي (له عدد صفحات/صور فعلي، لا تقدير). رقم فعلي فقط
                # لمصادر نصية بلا مفهوم صفحة (نص مباشر، تفريغ صوتي، مستندات أوفيس) - وجوده هو
                # إشارة "items_count أعلاه تقدير من كلمات، لا عدّ فعلي" تُعرض صراحة بلوحة الأدمن.
                "estimated_word_count": data.get("estimated_word_count"),
                # 🆕 نوع/مصدر المحتوى الصريح (pdf/docx/pptx/txt/images/telegram_text/
                # audio_transcript) - مُحدَّد صراحة عند نقطة الاستقبال الفعلية (راجع
                # _document_content_source وhandle_pure_text/handle_audio_to_quiz)، لا استنتاجاً
                # لاحقاً هون. None لأي كويز قديم قبل هذا التحديث (لوحة الأدمن تعرضه "غير مسجَّل").
                "content_source": data.get("content_source"),
                "input_channel": data.get("input_channel"),
            })
            await reward_referrer_if_eligible(call.from_user.id)
        except Exception as exc:
            log_warning(logger, f"Post-generation analytics/referral failed: {exc}")

        # التوليد وحفظ الكويز نجحا؛ لا تبقِ بيانات الخصم القديمة في FSM ولا تسمح
        # لأي خطأ UI/Telegram لاحق أن يحوّل نجاحاً فعلياً إلى refund خاطئ.
        charged_this_attempt = False
        await _clear_quiz_charge_markers(state)

        if (data.get("subject_type") in (SUBJECT_QUIZ_SOLVED, SUBJECT_QUIZ_UNSOLVED)
                and data.get("question_type") != QUESTION_TYPE_SIMILAR_MATH):
            await call.message.answer(
                f"✅ أصبح الاختبار جاهزاً: {len(quiz_data)} سؤال اختيار من متعدد.\n"
                "يشمل الأسئلة الأصلية المؤتمتة والتقليدية التي أمكن تحويلها؛ "
                "ولا يشمل الإنشاء أو المهمات المفتوحة التي لا يحافظ تحويلها على المهارة."
            )

        # Generation already owns UserLock; do not try to acquire it twice.
        await start_loaded_quiz.__wrapped__(call, state, quiz_data, data.get("source_title", "كويز"), origin="file" if data.get("input_type") == "media" else "text", quiz_id=new_quiz_id)
        await status_msg.delete()

    except (FileNotFoundError, InvalidUploadError) as exc:
        # 🩹 رسالة تطابق السبب الفعلي بدل رسالة عامة: ملف مفقود من القرص، أو PDF محمي/تالف.
        # الخصم (إن حصل) يُسترجع، والحالة تُمسح لأن هذا الطلب لا يمكن إكماله بهذا الملف.
        log_error(logger, f"Quiz generation aborted, unusable upload: {exc!r}", exception=exc)
        if charged_this_attempt:
            await refund_user_on_failure(call.from_user.id, data)
            charged_this_attempt = False
            refunded_this_attempt = True
        _track_quiz_generation_failure(
            call.from_user.id, data, "unusable_upload",
            refund_status="refunded" if refunded_this_attempt else "not_charged",
            cost=data.get("debited_cost"), requested_count=data.get("requested_count"),
            items_count=data.get("items_count"), mode=data.get("execution_mode"),
            failure_stage="input_validation", exception_type=type(exc).__name__,
        )
        await _clear_quiz_charge_markers(state)
        await state.set_state(None)
        money_note = "تم إرجاع نقاطك." if refunded_this_attempt else "لم يتم خصم أي نقاط."
        if isinstance(exc, FileNotFoundError):
            reason = f"{MSG_UPLOAD_FILE_MISSING}\n{money_note}"
        else:
            reason = f"⚠️ <b>تعذر معالجة الملف:</b> {safe_title(str(exc))}\n{money_note}"
        try:
            await call.message.answer(reason, parse_mode="HTML")
        except Exception as send_exc:
            log_warning(logger, f"Could not send file-error message: {send_exc}")
    except Exception as exc:
        log_error(logger, f"Quiz generation start failed: {exc}", exception=exc)
        if charged_this_attempt:
            # استخدم بيانات هذه المحاولة المحلية؛ لا تعتمد على Redis هنا لأن الخطأ
            # نفسه قد يكون انقطاع Redis بعد الخصم مباشرة.
            await refund_user_on_failure(call.from_user.id, data)
            charged_this_attempt = False
            refunded_this_attempt = True
        if not quiz_ready:
            _track_quiz_generation_failure(
                call.from_user.id, data, "unexpected_error",
                refund_status="refunded" if refunded_this_attempt else "not_charged",
                cost=data.get("debited_cost"), requested_count=data.get("requested_count"),
                items_count=data.get("items_count"), mode=data.get("execution_mode"),
                failure_stage="generation_or_persistence", exception_type=type(exc).__name__,
            )
        await _clear_quiz_charge_markers(state)
        await state.set_state(None)
        # 🩹 الرسالة تطابق ما حصل فعلاً (بدل رسالة "تعذر عرض النتيجة" لكل الحالات):
        if refunded_this_attempt:
            failure_text = "❌ حدث خطأ أثناء التوليد، وتم إرجاع نقاطك تلقائياً. أعد المحاولة بعد قليل."
        elif quiz_ready:
            failure_text = ("⚠️ تم توليد الكويز لكن تعذر عرضه بسبب خطأ تقني، ولم يُسترجع رصيدك لأن التوليد تم فعلاً. "
                            "جرّب إعادة إرسال الملف، وإن لم يظهر الكويز تواصل مع الدعم.")
        else:
            failure_text = "❌ تعذر بدء الطلب بسبب خطأ تقني، ولم يتم خصم أي نقاط. أعد المحاولة بعد قليل."
        await call.message.answer(failure_text)
    except asyncio.CancelledError:
        if charged_this_attempt:
            await refund_user_on_failure(user_id, data)
        try:
            await _clear_quiz_charge_markers(state)
            await state.set_state(None)
        finally:
            raise
    finally:
        if not preserve_upload:
            for path in data.get("file_paths", []):
                safe_file_cleanup(path)
        try:
            await lock.release()
        except Exception as exc:
            log_warning(logger, f"Could not release processing lock (TTL will expire): {exc}")


@router.callback_query(F.data == "cancel_upload_request")
@user_locked()
async def handle_cancel_upload(call: types.CallbackQuery, state: FSMContext) -> None:
    """معالج التراجع والضغط على زر إلغاء الطلب"""
    try:
        current_state = await state.get_state()
        if current_state not in PENDING_REQUEST_STATES:
            await call.answer(MSG_NOTHING_TO_CANCEL, show_alert=True)
            return
        
        # إصلاح التتبع: تسجيل حدث إلغاء الطلب
        spawn(log_usage_event(call.from_user.id, "request_cancelled", {"cancelled_from_state": str(current_state)}))
        
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

# ==================== 🆕 التحقق المجتمعي من تصنيف المادة ====================
# راجع migration_classification_votes.sql + services/subject_classifier.py +
# keyboards.get_classification_vote_keyboard. بلا أي StateFilter عمداً: رسالة التصويت
# مستقلة عن أي تدفق FSM، وقد يضغطها الطالب بعد أن يكون انتقل لشاشات لاحقة (أو حتى بعد
# إنهاء الجلسة بالكامل) - نفس منطق "cancel_upload_request" أعلاه (لا اعتماد على state).

@router.callback_query(F.data.startswith("cv_y_") | F.data.startswith("cv_n_"))
async def handle_classification_vote(call: types.CallbackQuery) -> None:
    try:
        parts = call.data.split("_", 3)
        # الصيغة: cv_<y|n>_<subj_code>_<token> - راجع keyboards.get_classification_vote_keyboard
        if len(parts) != 4:
            await call.answer(MSG_CLASSIFICATION_VOTE_ERROR, show_alert=True)
            return
        _, vote_code, subj_code, token = parts
        subject = CLASSIFICATION_SUBJECT_CODES_REVERSE.get(subj_code)
        if not subject:
            await call.answer(MSG_CLASSIFICATION_VOTE_ERROR, show_alert=True)
            return
        try:
            file_hash = short_token_to_hash(token)
        except Exception:
            await call.answer(MSG_CLASSIFICATION_VOTE_ERROR, show_alert=True)
            return

        vote = "yes" if vote_code == "y" else "no"
        # 🆕 لقطة الـ classification الكاملة (suggested_types/question_count/content_subject)
        # تُلتقط من كاش Redis غير المؤكَّد إن وُجد (لسا صالح ضمن TTL) لتثبيتها لاحقاً كاملة
        # لو فاز هذا التصنيف بالتحقق - وإلا (كاش منتهي الصلاحية) نكتفي بـ subject فقط
        # (فشل آمن: لا يمنع التثبيت، فقط يفقد الحقول الثانوية الإضافية).
        cached_snapshot = await get_pending_classification_snapshot(file_hash)
        classification_data = (
            cached_snapshot.model_dump() if cached_snapshot is not None
            else {"subject": subject, "suggested_types": [], "question_count": None, "content_subject": None}
        )

        result = await submit_classification_vote(file_hash, call.from_user.id, vote, subject, classification_data)

        if result.get("duplicate"):
            await call.answer(MSG_CLASSIFICATION_VOTE_ALREADY, show_alert=True)
            return

        if vote == "no":
            # 🆕 بلاغ خطأ تصنيف - تنبيه فوري للأدمن للمراجعة اليدوية (لا يوقف تدفق الطالب،
            # فشل الإرسال هنا لا يظهر له إطلاقاً - نفس فلسفة asyncio.create_task المستخدمة
            # لباقي الأحداث غير الحرجة بهذا الملف).
            if ADMIN_ID:
                subject_label = CLASSIFICATION_SUBJECT_LABELS_AR.get(subject, subject)
                alert_text = MSG_CLASSIFICATION_VOTE_ADMIN_ALERT.format(
                    file_hash=file_hash, subject_label=subject_label, user_id=call.from_user.id,
                )
                spawn(_notify_admin_safe(alert_text))

        # 🆕 تعديل رسالة التصويت نفسها مباشرة (بدل تركها بنصها الطويل الأصلي وعرض
        # تنبيه منبثق فقط) - تصير رسالة قصيرة "شكراً لمشاركتك!" وتُزال الأزرار منها،
        # فيشعر الطالب فوراً إنه صوته سُجّل فعلاً بدل رسالة معلّقة بلا تغيير.
        thanks_text = MSG_CLASSIFICATION_VOTE_EDITED_NO if vote == "no" else MSG_CLASSIFICATION_VOTE_EDITED_YES
        if result.get("locked_now"):
            thanks_text += MSG_CLASSIFICATION_LOCKED_SUFFIX
        try:
            await call.message.edit_text(thanks_text, parse_mode="HTML", reply_markup=None)
        except Exception:
            pass  # فشل التعديل (مثال: الرسالة قديمة جداً أو محذوفة) لا يوقف تسجيل الصوت نفسه أعلاه

        await call.answer()
    except Exception as exc:
        log_error(logger, f"Error in handle_classification_vote: {exc}", exception=exc)
        await call.answer(MSG_CLASSIFICATION_VOTE_ERROR, show_alert=True)


async def _notify_admin_safe(text: str) -> None:
    """إرسال تنبيه للأدمن مع كبت أي خطأ (شبكة/حظر البوت من الأدمن إلخ) - غير حرج بما
    يكفي لتسجيله فقط بلا أي تأثير على تدفق الطالب الذي أطلق الحدث."""
    try:
        await bot.send_message(ADMIN_ID, text, parse_mode="HTML")
    except Exception as exc:
        log_warning(logger, f"Could not notify admin: {exc}")

# ==================== 🆕 رفع ملف/ألبوم صور كبير عبر صفحة الويب (Mini App) ====================

async def _reject_web_upload(
    chat_id: int, state: FSMContext, status_msg: Optional[types.Message], text: str,
    *, user_id: int, media_type: str, reason: str, **metadata,
) -> None:
    """تنظيف موحّد عند رفض/فشل طلب رفع ويب (ملف أو صور): فكّ القفل وعرض رسالة الخطأ."""
    track_upload_rejection(user_id, media_type, reason, "web_upload", **metadata)
    await state.set_state(None)
    if status_msg:
        try:
            await status_msg.edit_text(text, parse_mode="HTML")
            return
        except Exception:
            pass
    await bot.send_message(chat_id, text, parse_mode="HTML")


@user_locked(web_kind="file")
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
    state = build_state_for_chat(chat_id, user_id)

    current_state = await state.get_state()
    if await offer_transition(state, user_id, chat_id, {"kind": "web_file",
            "user_id": user_id, "chat_id": chat_id, "object_path": object_path, "declared_file_name": declared_file_name}):
        return
    if current_state == QuizState.processing_web_file:
        track_upload_rejection(user_id, "document", "session_busy", "web_upload")
        await bot.send_message(chat_id, "⏳ لديك ملف آخر قيد المعالجة حالياً؛ يرجى انتظار انتهائه.")
        await delete_file_temp(object_path)
        return
    if current_state == QuizState.processing_file_quiz:
        # 🩹 نفس إصلاح handle_media: طلب توليد فعلي قيد التنفيذ عند Gemini حالياً - لازم نرفض
        # هذا الرفع الجديد بدل حذف/استبدال ملفات الطلب الجاري معالجته فعلياً.
        track_upload_rejection(user_id, "document", "session_busy", "web_upload")
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

    status_msg = None
    failure_stage = "download"


    try:
        status_msg = await bot.send_message(chat_id, "📥 <b>تم استلام الملف، جارٍ تجهيزه...</b>", parse_mode="HTML")
        # 🆕 خط دفاع ثانٍ: تحقق فعلي من حجم المحتوى المُنزَّل نفسه (وليس فقط الحجم
        # المُصرَّح به بمرحلة /init) - نفس منطق download_audio_temp_to_file تماماً.
        downloaded = await download_file_temp_to_file(object_path, destination, max_size_bytes=MAX_FILE_WEB_UPLOAD_SIZE)
        if not downloaded:
            await delete_file_temp(object_path)
            await _reject_web_upload(
                chat_id, state, status_msg,
                "❌ تعذر تحميل الملف المرفوع أو أنه يتجاوز الحجم المسموح، يرجى إعادة المحاولة من البوت.",
                user_id=user_id, media_type="document", reason="download_failed_or_size_exceeded",
                max_size_bytes=MAX_FILE_WEB_UPLOAD_SIZE,
            )
            return

        failure_stage = "content_validation"
        items = await asyncio.to_thread(validate_document, destination)
        if destination.lower().endswith(".pdf"):
            failure_stage = "page_validation"
            if items > MAX_FILE_WEB_UPLOAD_PAGES:
                safe_file_cleanup(destination)
                await delete_file_temp(object_path)
                await _reject_web_upload(
                    chat_id, state, status_msg,
                    f"❌ الحد الأقصى لمعالجة ملفات PDF عبر صفحة الويب هو {MAX_FILE_WEB_UPLOAD_PAGES} صفحة.",
                    user_id=user_id, media_type="document", reason="page_limit_exceeded",
                    page_count=items, max_page_count=MAX_FILE_WEB_UPLOAD_PAGES,
                )
                return

        await delete_file_temp(object_path)  # انتهى الغرض من النسخة المؤقتة بـ Supabase - النسخة المحلية هي اللي رح تُعالَج الآن

        title = os.path.splitext(declared_file_name)[0] if declared_file_name else "ملف مرفوع عبر الويب"
        file_hash = await asyncio.to_thread(calculate_file_hash, destination)

        await state.set_state(None)  # فكّ القفل - _finalize_media_processing بتحدد الحالة التالية بنفسها
        await status_msg.delete()
        await _finalize_media_processing(
            status_msg, state, [destination], title, items, False, file_hash,
            content_source=_document_content_source(extension), user_id=user_id, input_channel="web_upload",
        )
    except asyncio.CancelledError:
        safe_file_cleanup(destination)
        await delete_file_temp(object_path)
        await state.set_state(None)
        raise
    except Exception as exc:
        log_error(logger, f"Web-uploaded file preparation failed: {exc}", exception=exc)
        safe_file_cleanup(destination)
        await delete_file_temp(object_path)
        reason = "invalid_content" if failure_stage == "content_validation" else "processing_failed"
        await _reject_web_upload(
            chat_id, state, status_msg, "❌ حدث خطأ غير متوقع أثناء تجهيز الملف.",
            user_id=user_id, media_type="document", reason=reason,
            failure_stage=failure_stage, exception_type=type(exc).__name__,
        )


@user_locked(web_kind="images")
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
    state = build_state_for_chat(chat_id, user_id)

    current_state = await state.get_state()
    if await offer_transition(state, user_id, chat_id, {"kind": "web_images",
            "user_id": user_id, "chat_id": chat_id, "object_paths": object_paths}):
        return
    if current_state == QuizState.processing_web_file:
        track_upload_rejection(user_id, "image", "session_busy", "web_upload")
        await bot.send_message(chat_id, "⏳ لديك طلب آخر قيد المعالجة حالياً؛ يرجى انتظار انتهائه.")
        await delete_file_temp_batch(object_paths)
        return
    if current_state == QuizState.processing_file_quiz:
        # 🩹 نفس إصلاح handle_media: طلب توليد فعلي قيد التنفيذ عند Gemini حالياً - لازم نرفض
        # هذا الرفع الجديد بدل حذف/استبدال ملفات الطلب الجاري معالجته فعلياً.
        track_upload_rejection(user_id, "image", "session_busy", "web_upload")
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

    status_msg = None


    local_paths: List[str] = []
    total_downloaded = 0
    failure_reason = "processing_failed"
    failure_stage = "image_count_validation"
    try:
        status_msg = await bot.send_message(
            chat_id, f"📥 <b>تم استلام {len(object_paths)} صورة، جارٍ تجهيزها...</b>", parse_mode="HTML",
        )
        if not object_paths or len(object_paths) > MAX_IMAGE_WEB_UPLOAD_COUNT:
            failure_reason = "item_count_exceeded"
            raise ValueError("عدد الصور يتجاوز الحد المسموح")
        for index, object_path in enumerate(object_paths):
            ext = os.path.splitext(object_path)[1] or ".jpg"
            destination = os.path.join(DOWNLOADS_DIR, f"images_web_{user_id}_{uuid.uuid4().hex}_{index}{ext}")
            local_paths.append(destination)
            remaining = MAX_IMAGE_WEB_UPLOAD_TOTAL_SIZE - total_downloaded
            if remaining <= 0:
                failure_reason = "total_size_exceeded"
                raise ValueError("الحجم الإجمالي للصور يتجاوز 100 ميغابايت")
            failure_stage = "image_download"
            downloaded = await download_file_temp_to_file(
                object_path, destination, max_size_bytes=min(MAX_IMAGE_WEB_UPLOAD_SIZE_PER_IMAGE, remaining)
            )
            if not downloaded:
                failure_reason = "download_failed_or_size_exceeded"
                raise ValueError("تعذر تحميل إحدى الصور أو تجاوزت الحجم المسموح")
            total_downloaded += os.path.getsize(destination)
            if total_downloaded > MAX_IMAGE_WEB_UPLOAD_TOTAL_SIZE:
                failure_reason = "total_size_exceeded"
                raise ValueError("الحجم الإجمالي للصور يتجاوز 100 ميغابايت")
            failure_stage = "image_validation"
            await asyncio.to_thread(validate_image, destination)

        await delete_file_temp_batch(object_paths)

        is_album = len(local_paths) > 1
        title = f"كويز من ألبوم صور ({len(local_paths)} صور)" if is_album else "كويز من صورة"
        file_hash = await asyncio.to_thread(compute_combined_hash, local_paths)

        await state.set_state(None)
        await status_msg.delete()
        await _finalize_media_processing(
            status_msg, state, local_paths, title, len(local_paths), is_album, file_hash,
            content_source="images", user_id=user_id, input_channel="web_upload",
        )
    except asyncio.CancelledError:
        for path in local_paths:
            safe_file_cleanup(path)
        await delete_file_temp_batch(object_paths)
        await state.set_state(None)
        raise
    except Exception as exc:
        log_error(logger, f"Web-uploaded images preparation failed: {exc}", exception=exc)
        for path in local_paths:
            safe_file_cleanup(path)
        await delete_file_temp_batch(object_paths)
        if failure_reason == "processing_failed" and isinstance(exc, ValueError):
            failure_reason = "invalid_image_count_or_content"
        await _reject_web_upload(
            chat_id, state, status_msg, "❌ حدث خطأ غير متوقع أثناء تجهيز الصور.",
            user_id=user_id, media_type="image", reason=failure_reason,
            item_count=len(object_paths), max_item_count=MAX_IMAGE_WEB_UPLOAD_COUNT,
            failure_stage=failure_stage, exception_type=type(exc).__name__,
        )


files_router = router



