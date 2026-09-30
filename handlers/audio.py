# handlers/audio.py
"""
معالج المحاضرات الصوتية: يستقبل رسائل صوتية (voice/audio)، يفرّغها نصياً عبر
services/audio_service.py، ثم يعرض على الطالب أربعة إجراءات ممكنة على النص
الناتج (تصدير Word/PDF، تلخيص أكاديمي، تحويل لكويز، أو استلام النص الخام).

🆕 [إعادة هيكلة] هذا الملف أصبح طبقة Handlers خالصة فقط: استقبال الطلب، تحقق
سريع من الحالة/المدخلات، استدعاء الخدمات، ثم الرد على الطالب. منطق الأعمال
الذي كان مدمجاً هنا سابقاً انتقل بالكامل لطبقة Services دون أي تغيير سلوكي:
- قياس مدة الملف الصوتي (mutagen/ffprobe/ffmpeg)  -> services/audio_duration_service.py
- توحيد قياس المدة+التسعير+التحقق من السقف الأقصى (كان مكرراً حرفياً بين المسار
  المباشر ومسار رفع الويب)                        -> services/audio_flow_service.py
- حساب الرصيد/العجز ونص "الرصيد لا يكفي"           -> services/audio_flow_service.py
- حساب الاسترجاع الجزئي عند نجاح جزئي بالتفريغ      -> services/audio_flow_service.py
- كتابة ملف txt مؤقت (نص مفرّغ/ملخص طويل)          -> services/audio_flow_service.py

لم يتغيّر أي من: أسماء حالات QuizState، أي callback_data، مفاتيح state.update_data/
Redis، آلية القفل (QuizState.processing_audio)، أو أي تحقق أمني/حدود أحجام.
"""

import asyncio
import os
import time
import uuid
from typing import Any, Dict

from aiogram import F, Router, types
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey

from config import QuizState, bot, dp
from constants import (
    ADMIN_CONTACT, MAX_AUDIO_WEB_UPLOAD_SIZE,
    MSG_AUDIO_CONFIRM_TEMPLATE, MSG_PREVIOUS_REQUEST_REPLACED,
    MSG_REDIRECT_TO_WEB_UPLOAD,
)
from helpers.gemini_helper import get_safe_mime_type
from keyboards import (
    get_audio_action_keyboard, get_document_export_keyboard, get_audio_confirm_keyboard,
    get_web_upload_redirect_keyboard,
)
from logger import get_logger, log_error, log_info
from services.audio_duration_service import format_duration_cap_exceeded_text
from services.audio_flow_service import (
    DOWNLOADS_DIR,
    AudioCostResult,
    build_insufficient_balance_text,
    compute_balance_and_deficit,
    compute_partial_refund,
    evaluate_audio_cost,
    get_or_create_audio_user,
    log_audio_uploaded_event,
    log_insufficient_balance_event,
    write_temp_text_export,
)
from services.audio_service import summarize_lecture_text, transcribe_audio_lecture
from services.export_service import build_document_docx, build_document_pdf, build_export_filename
from helpers.supabase_helper import (
    log_usage_event, refund_user_points_split, update_user_stats,
)
from helpers.r2_helper import download_audio_temp_to_file, delete_audio_temp
from utils import ensure_directory_exists, safe_file_cleanup

logger = get_logger(__name__)
router = Router()

MAX_AUDIO_FILE_SIZE = 20 * 1024 * 1024  # حد تيليجرام لتحميل الملفات عبر بوتات API (20 ميجابايت)
MAX_INLINE_TEXT_CHARS = 4000  # حد رسالة تيليجرام النصية الآمن قبل اللجوء لإرسال مستند .txt


# ==================== أدوات مساعدة داخلية (طبقة Handler فقط) ====================

def _extract_extension(message: types.Message) -> str:
    if message.voice:
        return ".ogg"
    file_name = message.audio.file_name or ""
    ext = os.path.splitext(file_name)[1]
    return ext if ext else ".mp3"


async def _send_insufficient_balance(
    message: types.Message, user_info: Dict[str, Any], required: float, action: str = "audio_transcription"
) -> None:
    """إرسال رسالة "الرصيد لا يكفي" + زر شحن + تتبع تحليلي - نص الرسالة وحساب
    الرصيد/العجز يأتيان من services/audio_flow_service.py، وبناء لوحة المفاتيح
    (Telegram-specific) يبقى هنا."""
    balance, _ = compute_balance_and_deficit(user_info, required)

    # 🆕 تتبع تحليلي: نفس حدث "insufficient_balance_blocked" المُسجَّل بـ handlers/files.py
    # (راجع التعليق هناك) - نفس منطق اعتماد user_info["user_id"] بدل message.from_user
    # لأن message هون غالباً رسالة البوت نفسها وليست رسالة الطالب.
    tracked_user_id = user_info.get("user_id")
    if tracked_user_id:
        asyncio.create_task(log_insufficient_balance_event(tracked_user_id, action, required, balance, user_info))

    contact = ADMIN_CONTACT.lstrip("@")
    keyboard = types.InlineKeyboardMarkup(
        inline_keyboard=[
            [types.InlineKeyboardButton(text="💳 شحن الرصيد الآن", url=f"https://t.me/{contact}")]
        ]
    )
    await message.answer(
        build_insufficient_balance_text(user_info, required),
        reply_markup=keyboard,
        parse_mode="HTML",
    )


# ==================== استقبال ومعالجة الرسالة الصوتية ====================

@router.message(F.voice | F.audio)
async def handle_audio_message(message: types.Message, state: FSMContext) -> None:
    """يستقبل محاضرة صوتية، يتحقق من الحجم، يحمّلها، يتحقق من مدتها الفعلية، ثم يعرض
    شاشة تأكيد واحدة (🆕 QuizState.waiting_for_audio_confirm) تجمع: إقرار الطالب بأنه
    يملك حق استخدام هذا الصوت أو حاصل على إذن بذلك + المدة الفعلية بالدقائق + التكلفة
    بالنقاط. لا يُخصم أي رصيد ولا تُستدعى خدمة التفريغ إطلاقاً قبل ضغط الطالب صراحة
    على زر "✅ تأكيد وبدء التفريغ" (التنفيذ الفعلي بـ handle_audio_confirm_start أدناه).

    🆕 قفل معالجة (`QuizState.processing_audio`): يُضبط فور التأكد من الحجم، قبل أي
    عملية تحميل/شبكة، لمنع معالجة مضاعفة لو وصل مقطع صوتي جديد من نفس الطالب قبل
    اكتمال تجهيز السابق. يُفكّ القفل صراحة بكل مسارات الخروج المبكر وضمنياً عند
    الانتقال لشاشة التأكيد (waiting_for_audio_confirm).

    🆕 حساب المدة على الملف الفعلي بعد تحميله (services.audio_flow_service.evaluate_audio_cost)
    بدل الاعتماد حصراً على `message.audio.duration` (قيمة يُرسلها تطبيق العميل ضمن
    الرسالة نفسها لرسائل audio - قابلة للتعارض مع محتوى الملف الحقيقي، خلافاً لرسائل
    voice التي يقيس تيليجرام مدتها بنفسه وقت التسجيل). التسعير يُحتسب دائماً من القيمة
    الأكبر بين المُعلنة والفعلية تفادياً لأي دفع أقل من التكلفة الحقيقية."""
    current_state = await state.get_state()
    if current_state == QuizState.answering_quiz:
        await message.answer("⚠️ لديك اختبار قائم حالياً؛ أتممه أو أوقفه عبر الضغط على (⏹️ إيقاف) أولاً قبل رفع محاضرة صوتية جديدة.")
        return
    if current_state == QuizState.processing_audio:
        await message.answer("⏳ لديك محاضرة صوتية أخرى قيد التحميل/التفريغ حالياً؛ يرجى انتظار انتهائها قبل إرسال مقطع جديد.")
        return
    if current_state == QuizState.waiting_for_audio_confirm:
        # 🆕 طلب محاضرة سابق بانتظار تأكيد الطالب لم يُخصَم منه أي رصيد بعد - يُلغى
        # تلقائياً مع حذف ملفه المؤقت (محلياً، ومن Supabase Storage لو كان قادماً من
        # رفع ويب) بدل تركه معلّقاً بلا تنظيف عند وصول مقطع صوتي جديد يستبدله.
        pending = await state.get_data()
        old_path = pending.get("pending_audio_path")
        if old_path:
            safe_file_cleanup(old_path)
        old_object_path = pending.get("pending_audio_object_path")
        if old_object_path:
            await delete_audio_temp(old_object_path)
        await message.answer(MSG_PREVIOUS_REQUEST_REPLACED)
        await state.set_state(None)
    elif current_state is not None:
        # أي طلب معلّق آخر (تصنيف نص/ملف سابق لم يُكمل، أو شاشة إجراءات محاضرة سابقة
        # جاهزة لم يُقرَّر مصيرها بعد) يُلغى بأمان عند وصول محاضرة صوتية جديدة
        await state.set_state(None)

    media = message.voice or message.audio
    file_size = media.file_size or 0
    if file_size > MAX_AUDIO_FILE_SIZE:
        # 🆕 بدل رفض جاف بلا بديل: نوجّه الطالب لصفحة رفع الصوت الكبير (حتى 250MB)
        # الموجودة أصلاً - هذا الحجم يتجاوز حد تحميل تيليجرام المباشر (Bot API) نفسه.
        keyboard = get_web_upload_redirect_keyboard("audio")
        if keyboard.inline_keyboard:
            await message.answer(
                MSG_REDIRECT_TO_WEB_UPLOAD.format(limit_mb=MAX_AUDIO_FILE_SIZE // (1024 * 1024)),
                parse_mode="HTML", reply_markup=keyboard,
            )
        else:
            await message.answer("❌ حجم الملف الصوتي أكبر من الحد المسموح به (20 ميجابايت). يرجى إرسال ملف أصغر.")
        return

    # 🆕 نضبط القفل فوراً هون - قبل أي await لعملية شبكة/تحميل - لتضييق نافذة السباق
    # الزمنية قدر الإمكان إذا وصل مقطع صوتي ثانٍ من نفس الطالب بنفس اللحظة تقريباً.
    await state.set_state(QuizState.processing_audio)

    reported_duration_seconds = message.voice.duration if message.voice else (message.audio.duration or 0)
    mime_type = media.mime_type or ("audio/ogg" if message.voice else "audio/mp3")
    source_title = message.audio.title if (message.audio and message.audio.title) else None

    ensure_directory_exists(DOWNLOADS_DIR)
    extension = _extract_extension(message)
    destination = os.path.join(DOWNLOADS_DIR, f"audio_{message.from_user.id}_{uuid.uuid4().hex}{extension}")

    status_msg = await message.answer(
        "🎙️ <b>جارٍ تحميل المحاضرة الصوتية...</b>\nقد تستغرق العملية بضع دقائق حسب طول المحاضرة.",
        parse_mode="HTML",
    )

    try:
        await bot.download(media, destination=destination)

        # 🆕 قياس المدة الفعلية + دمجها مع المُعلَنة + حساب الدقائق + التحقق من السقف
        # الأقصى + حساب التكلفة - كل ذلك موحّد الآن بدالة خدمة واحدة (كانت أربع خطوات
        # مكررة هون وبـ process_web_uploaded_audio أدناه). راجع AI-NOTE بجانب
        # MAX_AUDIO_DURATION_MINUTES بـ constants.py لشرح سبب اختيار 4 ساعات تحديداً
        # (مرتبط بحد توكنز إخراج Gemini).
        cost_result: AudioCostResult = await evaluate_audio_cost(destination, reported_duration_seconds)
        duration_minutes = cost_result.duration_minutes
        cost = cost_result.cost

        if cost_result.exceeds_cap:
            await state.set_state(None)
            safe_file_cleanup(destination)
            try:
                await status_msg.edit_text(format_duration_cap_exceeded_text(duration_minutes))
            except Exception:
                await message.answer(format_duration_cap_exceeded_text(duration_minutes))
            return

        user_info = await get_or_create_audio_user(message.from_user)
        balance, _ = compute_balance_and_deficit(user_info, cost)

        # 🆕 تتبع تحليلي: يسجَّل بمجرد معرفة مدة/تكلفة الملف، بغض النظر عن قرار الطالب
        # لاحقاً بشاشة التأكيد - يسد فجوة "صفر أحداث" الحالية لميزة الصوت بالكامل.
        asyncio.create_task(log_audio_uploaded_event(message.from_user.id, duration_minutes, cost, "telegram_message"))

        if balance < cost:
            await state.set_state(None)  # فكّ القفل - لم يُخصَم أي شيء إطلاقاً
            try:
                await status_msg.delete()
            except Exception:
                pass
            safe_file_cleanup(destination)
            await _send_insufficient_balance(message, user_info, cost)
            return

        # 🆕 لا خصم ولا تفريغ هون - فقط تخزين مؤقت لبيانات الطلب بانتظار تأكيد الطالب
        await state.update_data(
            pending_audio_path=destination,
            pending_audio_object_path=None,
            pending_audio_mime_type=mime_type,
            pending_audio_duration_minutes=duration_minutes,
            pending_audio_cost=cost,
            pending_audio_source_title=source_title,
        )
        await state.set_state(QuizState.waiting_for_audio_confirm)

        await status_msg.edit_text(
            MSG_AUDIO_CONFIRM_TEMPLATE.format(duration_minutes=duration_minutes, cost=cost, balance=balance),
            parse_mode="HTML",
            reply_markup=get_audio_confirm_keyboard(),
        )
    except Exception as exc:
        log_error(logger, f"Audio handling failed (pre-confirm stage): {exc}", exception=exc)
        await state.set_state(None)
        safe_file_cleanup(destination)
        try:
            await status_msg.edit_text("❌ حدث خطأ غير متوقع أثناء تجهيز المحاضرة الصوتية.")
        except Exception:
            await message.answer("❌ حدث خطأ غير متوقع أثناء تجهيز المحاضرة الصوتية.")


# ==================== 🆕 تنفيذ التفريغ الفعلي بعد شاشة التأكيد ====================

@router.callback_query(QuizState.waiting_for_audio_confirm, F.data == "audio_confirm_start")
async def handle_audio_confirm_start(call: types.CallbackQuery, state: FSMContext) -> None:
    """🆕 التنفيذ الفعلي (خصم نقاط + تفريغ) بعد ضغط الطالب على زر التأكيد بشاشة
    الحقوق/المدة/التكلفة. مشترك بين مسار رسالة تيليجرام المباشرة ومسار رفع الويب
    (Mini App) على حد سواء - كل ما يلزم التنفيذ مُخزَّن مسبقاً بـ state data تحت
    مفاتيح pending_audio_* بغض النظر عن مصدر الرفع. يُعاد التحقق من الرصيد هنا بقيم
    طازجة (لا يُعتمد على الرصيد المعروض بشاشة التأكيد، فقد يكون مضى عليه وقت)."""
    await call.answer()
    data = await state.get_data()
    destination = data.get("pending_audio_path")
    object_path = data.get("pending_audio_object_path")
    mime_type = data.get("pending_audio_mime_type") or "audio/mp3"
    duration_minutes = data.get("pending_audio_duration_minutes") or 1
    cost = data.get("pending_audio_cost") or 0.0
    source_title = data.get("pending_audio_source_title")

    if not destination or not os.path.exists(destination):
        await state.set_state(None)
        error_text = "❌ انتهت صلاحية هذا الطلب أو تعذر العثور على الملف، يرجى إعادة إرسال المحاضرة الصوتية."
        try:
            await call.message.edit_text(error_text)
        except Exception:
            await call.message.answer(error_text)
        return

    charged = False
    debited_free = 0.0
    debited_paid = 0.0
    try:
        user_info = await get_or_create_audio_user(call.from_user)
        balance, _ = compute_balance_and_deficit(user_info, cost)
        if balance < cost:
            await state.set_state(None)
            safe_file_cleanup(destination)
            if object_path:
                await delete_audio_temp(object_path)
            await call.message.edit_text(
                f"❌ رصيدك لم يعد كافياً لتفريغ هذه المحاضرة.\n"
                f"💰 الإجمالي الحالي: <code>{balance:.2f}</code> / المطلوب: <code>{cost:.2f}</code>",
                parse_mode="HTML",
            )
            return

        deduction = await update_user_stats(call.from_user.id, cost)
        if deduction is None:
            # 🆕 حالة سباق رصيد نادرة جداً (خصم متزامن آخر أفرغ الرصيد بين لحظة الفحص
            # ولحظة الخصم الذري)
            await state.set_state(None)
            safe_file_cleanup(destination)
            if object_path:
                await delete_audio_temp(object_path)
            await call.message.edit_text(
                "⚠️ <b>حصل تعارض بسيط برصيدك أثناء المعالجة.</b> يرجى إعادة رفع الملف من جديد.",
                parse_mode="HTML",
            )
            return
        charged = True
        # 🆕 [طبقة 2] نحفظ التقسيم الفعلي (كم اتخصم من free_points وكم من paid_points)
        # عشان أي ريفوند لاحق بهالدالة (كامل أو جزئي أو عند استثناء) يرجع كل جزء
        # لمصدره الصحيح بدل ما يرجعه كله لـpaid_points.
        debited_free = deduction["debited_free"]
        debited_paid = deduction["debited_paid"]

        asyncio.create_task(log_usage_event(call.from_user.id, "audio_transcription_confirmed", {
            "duration_minutes": duration_minutes, "cost": cost,
        }))

        await call.message.edit_text(
            "🎙️ <b>جارٍ تفريغ المحاضرة نصياً...</b>\nقد تستغرق العملية بضع دقائق حسب طول المحاضرة.",
            parse_mode="HTML",
        )

        transcription_started_at = time.monotonic()
        try:
            transcription_result = await transcribe_audio_lecture(destination, mime_type)
        except Exception as exc:
            log_error(logger, f"Audio transcription failed: {exc}", exception=exc)
            transcription_result = None
        finally:
            # 🆕 سجل الزمن الفعلي مقابل مدة المحاضرة - استخدم هاي القيم لاحقاً لمعايرة
            # ESTIMATED_TRANSCRIPTION_SECONDS_PER_MINUTE بدل الرقم التقريبي الحالي (12)
            elapsed = time.monotonic() - transcription_started_at
            per_minute = elapsed / duration_minutes if duration_minutes else elapsed
            log_info(logger, f"Transcription timing: {duration_minutes}min audio took {elapsed:.1f}s ({per_minute:.1f}s/min)")

        pure_text, truncated = transcription_result if transcription_result else (None, False)

        if not pure_text or not pure_text.strip():
            await refund_user_points_split(call.from_user.id, debited_free, debited_paid)
            await state.set_state(None)
            await call.message.edit_text(
                "⚠️ <b>تعذر تفريغ المحاضرة الصوتية!</b> يرجى التأكد من وضوح الصوت والمحاولة مجدداً. تم إرجاع نقاطك.",
                parse_mode="HTML",
            )
            return

        # 🆕 نجاح جزئي: انقطع التفريغ قبل نهاية المحاضرة (استُنفد حد توكنز الإخراج
        # الأقصى لدى Gemini - راجع generate_text_with_cascade). نسترجع نصف التكلفة
        # كتعويض تقريبي عادل (الطالب استلم جزءاً فعلياً من الخدمة وليس لا شيء) - عدّل
        # هذه النسبة لاحقاً بناءً على ملاحظات فعلية لطول الجزء المفقود بالمتوسط.
        partial_refund_amount = 0.0
        if truncated:
            # 🆕 [طبقة 3] نفس نسبة الـ50% تُطبَّق على الشقّين (free/paid) على حدة عبر
            # services/audio_flow_service.compute_partial_refund بدل ما يُحسب المبلغ من
            # cost الإجمالي ويرجع كله لـpaid_points - هيك التوزيع النسبي بين الرصيدين
            # يضل صحيح بعد الريفوند الجزئي.
            free_refund, paid_refund, partial_refund_amount = compute_partial_refund(debited_free, debited_paid)
            if partial_refund_amount > 0:
                await refund_user_points_split(call.from_user.id, free_refund, paid_refund)
            asyncio.create_task(log_usage_event(call.from_user.id, "audio_transcription_truncated", {
                "duration_minutes": duration_minutes, "cost": cost, "partial_refund": partial_refund_amount,
            }))

        final_title = source_title or f"محاضرة صوتية ({duration_minutes} دقيقة)"
        await state.update_data(
            pure_text=pure_text,
            source_title=final_title,
            duration_minutes=duration_minutes,
            audio_debited_cost=cost - partial_refund_amount,
            input_type="text",
            items_count=1,
            is_album=False,
        )
        await state.set_state(QuizState.waiting_for_audio_action)

        asyncio.create_task(log_usage_event(call.from_user.id, "audio_transcription_completed", {
            "duration_minutes": duration_minutes, "cost": cost,
        }))

        if truncated:
            await call.message.edit_text(
                f"⚠️ <b>تم تفريغ جزء من المحاضرة فقط!</b> (⏱️ {duration_minutes} دقيقة)\n\n"
                "المحاضرة طويلة جداً وتوقف التفريغ قبل الوصول لنهايتها (حد أقصى لطول النص "
                f"الذي يمكن للذكاء الصناعي توليده دفعة واحدة). تم استرجاع <code>{partial_refund_amount:.2f}</code> "
                "نقطة تلقائياً كتعويض عن الجزء غير المكتمل.\n\n"
                "ماذا تريد أن تفعل بالجزء المتوفر من النص؟",
                parse_mode="HTML",
                reply_markup=get_audio_action_keyboard(),
            )
        else:
            await call.message.edit_text(
                f"✅ <b>تم تفريغ المحاضرة بنجاح!</b> (⏱️ {duration_minutes} دقيقة)\n\nماذا تريد أن تفعل بالنص المستخرج؟",
                parse_mode="HTML",
                reply_markup=get_audio_action_keyboard(),
            )
    except Exception as exc:
        log_error(logger, f"Audio confirm/transcription failed: {exc}", exception=exc)
        if charged:
            await refund_user_points_split(call.from_user.id, debited_free, debited_paid)
        await state.set_state(None)
        error_text = "❌ حدث خطأ غير متوقع أثناء معالجة المحاضرة الصوتية." + (" تم إرجاع نقاطك." if charged else "")
        try:
            await call.message.edit_text(error_text)
        except Exception:
            await call.message.answer(error_text)
    finally:
        safe_file_cleanup(destination)
        if object_path:
            await delete_audio_temp(object_path)


@router.callback_query(QuizState.waiting_for_audio_confirm, F.data == "cancel_upload_request")
async def handle_audio_confirm_cancel(call: types.CallbackQuery, state: FSMContext) -> None:
    """🆕 إلغاء نظيف من شاشة التأكيد نفسها: يحذف الملف الصوتي المؤقت (محلياً + من
    Supabase Storage لو كان الطلب قادماً من رفع ويب) بدون أي استرجاع نقاط، لأنه لم
    يُخصم أي شيء أصلاً بهذه المرحلة (خلافاً لإلغاء شاشة waiting_for_audio_action
    اللي بعد التفريغ الفعلي، حيث النقاط مخصومة سلفاً)."""
    try:
        data = await state.get_data()
        destination = data.get("pending_audio_path")
        object_path = data.get("pending_audio_object_path")
        if destination:
            safe_file_cleanup(destination)
        if object_path:
            await delete_audio_temp(object_path)
        await state.clear()
        try:
            await call.message.edit_text("❌ تم إلغاء الطلب دون أي خصم لنقاطك.")
        except Exception:
            await call.message.answer("❌ تم إلغاء الطلب دون أي خصم لنقاطك.")
        await call.answer()
    except Exception as exc:
        log_error(logger, f"Audio confirm cancel failed: {exc}", exception=exc)
        await call.answer("❌ تعذر إلغاء الطلب، حاول مجدداً.", show_alert=True)


# ==================== إلغاء الطلب من داخل شاشة إجراءات المحاضرة ====================

@router.callback_query(QuizState.waiting_for_audio_action, F.data == "cancel_upload_request")
async def handle_audio_cancel(call: types.CallbackQuery, state: FSMContext) -> None:
    """إلغاء نظيف لطلب المحاضرة الصوتية المعلّق (النقاط لا تُسترجع هنا لأن التفريغ
    تم فعلياً بنجاح واستُهلكت الخدمة بالكامل - الإلغاء هنا فقط للتخلص من النص المؤقت)."""
    try:
        await state.clear()
        try:
            await call.message.edit_text("❌ تم إلغاء الطلب وحذف نص المحاضرة المؤقت.")
        except Exception:
            await call.message.answer("❌ تم إلغاء الطلب وحذف نص المحاضرة المؤقت.")
        await call.answer()
    except Exception as exc:
        log_error(logger, f"Audio cancel failed: {exc}", exception=exc)
        await call.answer("❌ تعذر إلغاء الطلب، حاول مجدداً.", show_alert=True)


# ==================== معالجات لوحة إجراءات المحاضرة ====================

@router.callback_query(QuizState.waiting_for_audio_action, F.data == "audio_act_send_text")
async def handle_audio_send_text(call: types.CallbackQuery, state: FSMContext) -> None:
    """إرسال النص المفرغ مباشرة كرسالة لو كان قصيراً، أو كملف .txt لو تجاوز الحد الآمن."""
    await call.answer()
    txt_path = None
    try:
        data = await state.get_data()
        pure_text = data.get("pure_text") or ""
        if not pure_text.strip():
            await call.message.answer("❌ تعذر العثور على نص المحاضرة، يرجى إعادة إرسال الملف الصوتي.")
            return

        if len(pure_text) <= MAX_INLINE_TEXT_CHARS:
            await call.message.answer(pure_text)
        else:
            txt_path = write_temp_text_export("transcript", call.from_user.id, pure_text)
            await call.message.answer_document(
                types.FSInputFile(txt_path),
                caption="📋 النص الكامل المفرغ من المحاضرة الصوتية.",
            )
    except Exception as exc:
        log_error(logger, f"Audio send text failed: {exc}", exception=exc)
        await call.message.answer("❌ تعذر إرسال النص، حاول مجدداً.")
    finally:
        if txt_path:
            safe_file_cleanup(txt_path)


@router.callback_query(QuizState.waiting_for_audio_action, F.data == "audio_act_summarize")
async def handle_audio_summarize(call: types.CallbackQuery, state: FSMContext) -> None:
    """يلخّص النص المفرغ ويعيد صياغته أكاديمياً عبر summarize_lecture_text، ثم يعرض
    نفس لوحة الإجراءات مجدداً لتمكين الطالب من تصدير الملخص أو توليد كويز منه."""
    await call.answer()
    status_msg = None
    try:
        data = await state.get_data()
        pure_text = data.get("pure_text")
        if not pure_text:
            await call.message.answer("❌ تعذر العثور على نص المحاضرة، يرجى إعادة إرسال الملف الصوتي.")
            return

        status_msg = await call.message.answer("✨ جارٍ تلخيص المحاضرة وصياغتها أكاديمياً، يرجى الانتظار...")
        summary_result = await summarize_lecture_text(pure_text)
        if not summary_result or not summary_result[0] or not summary_result[0].strip():
            await status_msg.edit_text("⚠️ تعذر توليد تلخيص للمحاضرة، حاول مجدداً لاحقاً.")
            return
        summary, summary_truncated = summary_result

        await state.update_data(summary_text=summary)

        truncated_note = (
            "\n\n⚠️ <i>ملاحظة: النص طويل جداً وتوقف التلخيص قبل تغطية كامل المحاضرة.</i>"
            if summary_truncated else ""
        )

        if len(summary) <= MAX_INLINE_TEXT_CHARS:
            await status_msg.edit_text(f"✨ <b>ملخص المحاضرة (صياغة أكاديمية):</b>\n\n{summary}{truncated_note}", parse_mode="HTML")
        else:
            await status_msg.delete()
            txt_path = write_temp_text_export("summary", call.from_user.id, summary)
            try:
                await call.message.answer_document(
                    types.FSInputFile(txt_path),
                    caption="✨ ملخص المحاضرة (صياغة أكاديمية) - النص طويل فأُرسل كملف.",
                )
            finally:
                safe_file_cleanup(txt_path)

        await call.message.answer(
            "يمكنك الآن تحميل الملخص كملف Word/PDF، أو إنشاء كويز من محتوى المحاضرة:",
            reply_markup=get_audio_action_keyboard(),
        )
    except Exception as exc:
        log_error(logger, f"Audio summarize failed: {exc}", exception=exc)
        target = status_msg or call.message
        try:
            await target.edit_text("❌ حدث خطأ أثناء التلخيص، حاول مجدداً.")
        except Exception:
            await call.message.answer("❌ حدث خطأ أثناء التلخيص، حاول مجدداً.")


@router.callback_query(QuizState.waiting_for_audio_action, F.data == "audio_act_export")
async def handle_audio_export_menu(call: types.CallbackQuery, state: FSMContext) -> None:
    """يعرض اختيار الصيغة (Word/PDF) قبل توليد ملف التصدير النهائي."""
    await call.answer()
    try:
        await call.message.answer("📄 اختر صيغة الملف التي تريد تحميلها:", reply_markup=get_document_export_keyboard())
    except Exception as exc:
        log_error(logger, f"Audio export menu failed: {exc}", exception=exc)
        await call.message.answer("❌ تعذر عرض خيارات التصدير، حاول مجدداً.")


@router.callback_query(QuizState.waiting_for_audio_action, F.data.in_({"audio_export_docx", "audio_export_pdf"}))
async def handle_audio_export_format(call: types.CallbackQuery, state: FSMContext) -> None:
    """يولّد ملف Word أو PDF من الملخص الأكاديمي إن وُجد، وإلا من النص الخام المفرغ."""
    await call.answer()
    try:
        data = await state.get_data()
        content = data.get("summary_text") or data.get("pure_text")
        title = data.get("source_title", "محاضرة صوتية")
        if not content:
            await call.message.answer("❌ تعذر العثور على محتوى لتصديره، يرجى إعادة إرسال الملف الصوتي.")
            return

        is_docx = call.data == "audio_export_docx"
        ext = "docx" if is_docx else "pdf"

        if is_docx:
            file_bytes = await asyncio.to_thread(build_document_docx, title, content)
        else:
            file_bytes = await asyncio.to_thread(build_document_pdf, title, content)

        if not file_bytes:
            await call.message.answer("❌ تعذر إنشاء الملف، حاول مجدداً.")
            return

        filename = build_export_filename(title, ext)
        doc = types.BufferedInputFile(file_bytes, filename=filename)
        await call.message.answer_document(doc, caption=f"📄 {title}")
    except Exception as exc:
        log_error(logger, f"Audio export format failed: {exc}", exception=exc)
        await call.message.answer("❌ تعذر تصدير الملف، حاول مجدداً.")


@router.callback_query(QuizState.waiting_for_audio_action, F.data == "audio_act_quiz")
async def handle_audio_to_quiz(call: types.CallbackQuery, state: FSMContext) -> None:
    """ينقل نص المحاضرة المفرغ بسلاسة لخط أنابيب توليد الكويز في handlers/files.py -
    _ask_question_count يتكفّل باستدعاء classify_subject والانتقال بعدها تلقائياً
    لشاشة QuizState.waiting_for_quiz_options (أو شاشة اختيار الترجمة أولاً لو كان
    محتوى المحاضرة إنجليزياً)."""
    await call.answer()
    try:
        data = await state.get_data()
        pure_text = data.get("pure_text")
        if not pure_text:
            await call.message.answer("❌ تعذر العثور على نص المحاضرة، يرجى إعادة إرسال الملف الصوتي.")
            return

        from handlers.files import _ask_question_count  # تفادي استيراد دائري بين audio.py و files.py

        await state.update_data(
            pure_text=pure_text,
            source_title=data.get("source_title", "كويز من محاضرة صوتية"),
            input_type="text",
            items_count=1,
            is_album=False,
            # 🆕 مصدر/نوع المحتوى الصريح لسجل توليد الكويزات بلوحة الأدمن - راجع
            # handlers/files.py::handle_count_start حيث يُرحَّل هذا الحقل لحدث quiz_generated.
            content_source="audio_transcript",
        )
        await _ask_question_count(
            call.message,
            state,
            "✅ تم نقل نص المحاضرة بنجاح. كم سؤالاً تريد توليده من محتواها؟\nاختر من الأزرار أدناه، أو أرسل رقماً مخصصاً مباشرة.",
            edit=True,
        )
    except Exception as exc:
        log_error(logger, f"Audio to quiz transfer failed: {exc}", exception=exc)
        await call.message.answer("❌ تعذر بدء توليد الكويز من هذه المحاضرة، حاول مجدداً.")


# ==================== رفع محاضرة صوتية عبر صفحة الويب (Mini App) ====================

def _build_state_for_chat(chat_id: int, user_id: int) -> FSMContext:
    """
    يبني FSMContext يدوياً لمستخدم/محادثة معيّنة خارج سياق رسالة تيليجرام عادية
    (هون: من endpoint استقبال ويب). يستخدم نفس RedisStorage المُهيّأ أصلاً بـ
    config.py (dp.storage)، فالحالة متوافقة 100% مع بقية تدفقات البوت العادية -
    يعني بعد ما تخلص هاي الدالة، أي زر يضغطه الطالب (تلخيص/تصدير/كويز) من نفس
    لوحة get_audio_action_keyboard الحالية بيشتغل عادي بدون أي تعديل عليه.
    """
    key = StorageKey(bot_id=bot.id, chat_id=chat_id, user_id=user_id)
    return FSMContext(storage=dp.storage, key=key)


async def process_web_uploaded_audio(
    user_id: int,
    chat_id: int,
    object_path: str,
    declared_file_name: str = "",
) -> None:
    """
    🆕 نظير handle_audio_message لكن لملف رُفع عبر صفحة الويب (Mini App) بدل رسالة
    صوتية مباشرة على تيليجرام. يُستدعى كـ background task من webhook_server.py فور
    تأكيد اكتمال الرفع على Supabase. تنتهي هاي الدالة عند عرض شاشة التأكيد فقط
    (QuizState.waiting_for_audio_confirm) - لا خصم ولا تفريغ فعلي هون؛ التنفيذ
    الفعلي مشترك مع مسار الرسالة المباشرة عبر handle_audio_confirm_start.

    الفروقات عن handle_audio_message:
    - لا يوجد حد MAX_AUDIO_FILE_SIZE (20MB) هون - الحد المطبَّق مسبقاً هو
      MAX_AUDIO_WEB_UPLOAD_SIZE (250MB) بمرحلة /api/audio-upload/init.
    - لا يوجد "مدة معلَنة" من تيليجرام لملف مرفوع عبر الويب - الاعتماد الكامل على
      القياس الفعلي بعد التحميل المحلي (services.audio_flow_service.evaluate_audio_cost
      بلا reported_duration_seconds). ⚠️ قرار معلّق: لو تعذّر القياس (mutagen فشل)،
      الكود حالياً يفترض دقيقة واحدة فقط كحد أدنى (نفس سلوك duration_minutes =
      max(1, ...) بالتدفق العادي) - هاد ممكن يعني دفع أقل من التكلفة الحقيقية
      لمحاضرة طويلة بصيغة غير مدعومة. راجع PROJECT_STATUS.md قبل الإنتاج لتقرر إذا
      بدك رفض الملف بدل هيك بهالحالة تحديداً.
    - نسخة Supabase Storage المؤقتة (object_path) تُخزَّن بـ state data بدل حذفها
      فوراً، وتُحذف لاحقاً إما بالتأكيد (handle_audio_confirm_start.finally) أو
      بالإلغاء (handle_audio_confirm_cancel) - نفس مبدأ "لا تخزين دائم" المطلوب،
      بس بعد ما يحسم الطالب قراره لا قبله.
    """
    state = _build_state_for_chat(chat_id, user_id)

    current_state = await state.get_state()
    if current_state == QuizState.answering_quiz:
        await bot.send_message(chat_id, "⚠️ لديك اختبار قائم حالياً؛ أتممه أو أوقفه أولاً قبل رفع محاضرة صوتية جديدة.")
        await delete_audio_temp(object_path)
        return
    if current_state == QuizState.processing_audio:
        await bot.send_message(chat_id, "⏳ لديك محاضرة صوتية أخرى قيد المعالجة حالياً؛ يرجى انتظار انتهائها.")
        await delete_audio_temp(object_path)
        return
    if current_state == QuizState.waiting_for_audio_confirm:
        # 🆕 طلب سابق بانتظار تأكيد الطالب لم يُخصَم منه أي رصيد - يُلغى تلقائياً
        # مع حذف ملفاته المؤقتة (محلياً + Supabase Storage) قبل قبول الرفع الجديد.
        pending = await state.get_data()
        old_path = pending.get("pending_audio_path")
        if old_path:
            safe_file_cleanup(old_path)
        old_object_path = pending.get("pending_audio_object_path")
        if old_object_path:
            await delete_audio_temp(old_object_path)
        await bot.send_message(chat_id, MSG_PREVIOUS_REQUEST_REPLACED)
        await state.set_state(None)
    elif current_state is not None:
        await state.set_state(None)

    await state.set_state(QuizState.processing_audio)

    ensure_directory_exists(DOWNLOADS_DIR)
    extension = os.path.splitext(declared_file_name)[1] or os.path.splitext(object_path)[1] or ".mp3"
    destination = os.path.join(DOWNLOADS_DIR, f"audio_web_{user_id}_{uuid.uuid4().hex}{extension}")

    status_msg = await bot.send_message(
        chat_id,
        "🎙️ <b>تم استلام الملف، جارٍ تجهيزه للمعالجة...</b>",
        parse_mode="HTML",
    )

    try:
        # 1) تنزيل الملف من التخزين المؤقت بـ Supabase للقرص المحلي
        # 🆕 max_size_bytes: خط دفاع ثانٍ يرفض أي محتوى تم تحميله فعلياً يتجاوز
        # الحد المسموح، حتى لو مرّ فحص الحجم الأول (metadata) بـ webhook_server.py
        # لأي سبب - راجع get_audio_temp_object_size هناك للفحص الأول والأهم.
        downloaded = await download_audio_temp_to_file(
            object_path, destination, max_size_bytes=MAX_AUDIO_WEB_UPLOAD_SIZE
        )
        if not downloaded:
            await state.set_state(None)
            safe_file_cleanup(destination)
            await status_msg.edit_text("❌ تعذر تحميل الملف المرفوع أو أنه يتجاوز الحجم المسموح، يرجى إعادة المحاولة من البوت.")
            await delete_audio_temp(object_path)
            return

        # 🆕 نفس دالة evaluate_audio_cost المستخدمة بـ handle_audio_message أعلاه (بلا
        # reported_duration_seconds - لا توجد مدة مُعلَنة من تيليجرام بمسار الويب أصلاً).
        cost_result: AudioCostResult = await evaluate_audio_cost(destination)
        if cost_result.probed_seconds is None:
            log_error(logger, f"Could not probe duration for web-uploaded audio '{destination}' - defaulting to 1 minute charge.")
        duration_minutes = cost_result.duration_minutes

        # 🆕 سقف مدة فعلية - نفس المنطق المطبَّق بالمسار المباشر بـ handle_audio_message،
        # وأهم هون تحديداً لأن حد الحجم بهالمسار (250MB) أعلى بكثير وما بيعكس المدة الفعلية.
        if cost_result.exceeds_cap:
            await state.set_state(None)
            safe_file_cleanup(destination)
            await delete_audio_temp(object_path)
            try:
                await status_msg.edit_text(format_duration_cap_exceeded_text(duration_minutes))
            except Exception:
                await bot.send_message(chat_id, format_duration_cap_exceeded_text(duration_minutes))
            return

        cost = cost_result.cost

        # 2) استنتاج نوع MIME من محتوى الملف الفعلي (لا يوجد mime_type جاهز من
        # تيليجرام هون خلافاً لرسائل audio/voice العادية) - نفس الدالة المستخدمة
        # أصلاً بـ services/audio_service.py وhelpers/gemini_helper.py
        mime_type = get_safe_mime_type(destination)

        fake_user = type("U", (), {
            "id": user_id, "username": None, "first_name": "Unknown", "last_name": "Unknown",
        })()
        user_info = await get_or_create_audio_user(fake_user)
        balance, _ = compute_balance_and_deficit(user_info, cost)

        asyncio.create_task(log_audio_uploaded_event(user_id, duration_minutes, cost, "web_upload"))

        if balance < cost:
            await state.set_state(None)
            safe_file_cleanup(destination)
            await delete_audio_temp(object_path)
            try:
                await status_msg.delete()
            except Exception:
                pass
            # 🆕 تتبع تحليلي: نفس حدث "insufficient_balance_blocked" المستخدم بباقي مسارات
            # الحجب (راجع _send_insufficient_balance أعلى الملف) - هالمسار تحديداً (رفع محاضرة
            # عبر صفحة الويب) ما كان يمر أصلاً عبر تلك الدالة المشتركة (رسالة مختلفة/بدون
            # زر شحن) فكان النشاط هون غير مُتتبَّع إطلاقاً قبل هالإضافة.
            asyncio.create_task(log_insufficient_balance_event(user_id, "audio_transcription_web", cost, balance, user_info))
            await bot.send_message(
                chat_id,
                f"❌ رصيدك الحالي لا يكفي لتفريغ هذه المحاضرة.\n"
                f"💰 الإجمالي الحالي: <code>{balance:.2f}</code> / المطلوب: <code>{cost:.2f}</code>",
                parse_mode="HTML",
            )
            return

        # 🆕 لا خصم ولا تفريغ هون - فقط تخزين مؤقت لبيانات الطلب بانتظار تأكيد الطالب.
        # object_path (نسخة Supabase Storage) يُحفظ أيضاً بالـ state ليُحذف لاحقاً
        # عند التأكيد أو الإلغاء بدل حذفه فوراً هون.
        source_title = declared_file_name or f"محاضرة صوتية ({duration_minutes} دقيقة)"
        await state.update_data(
            pending_audio_path=destination,
            pending_audio_object_path=object_path,
            pending_audio_mime_type=mime_type,
            pending_audio_duration_minutes=duration_minutes,
            pending_audio_cost=cost,
            pending_audio_source_title=source_title,
        )
        await state.set_state(QuizState.waiting_for_audio_confirm)

        await status_msg.edit_text(
            MSG_AUDIO_CONFIRM_TEMPLATE.format(duration_minutes=duration_minutes, cost=cost, balance=balance),
            parse_mode="HTML",
            reply_markup=get_audio_confirm_keyboard(),
        )
    except Exception as exc:
        log_error(logger, f"Web-uploaded audio preparation failed: {exc}", exception=exc)
        await state.set_state(None)
        safe_file_cleanup(destination)
        await delete_audio_temp(object_path)
        error_text = "❌ حدث خطأ غير متوقع أثناء تجهيز المحاضرة الصوتية."
        try:
            await status_msg.edit_text(error_text)
        except Exception:
            await bot.send_message(chat_id, error_text)


audio_router = router
