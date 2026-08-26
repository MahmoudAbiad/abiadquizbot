# handlers/audio.py
"""
معالج المحاضرات الصوتية: يستقبل رسائل صوتية (voice/audio)، يفرّغها نصياً عبر
services/audio_service.py، ثم يعرض على الطالب أربعة إجراءات ممكنة على النص
الناتج (تصدير Word/PDF، تلخيص أكاديمي، تحويل لكويز، أو استلام النص الخام).
"""

import asyncio
import os
import time
import uuid
from typing import Any, Dict, Optional

from aiogram import F, Router, types
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from mutagen import File as MutagenFile

from config import QuizState, bot, dp
from constants import (
    ADMIN_CONTACT, ESTIMATED_TRANSCRIPTION_SECONDS_PER_MINUTE,
    MAX_AUDIO_DURATION_MINUTES, MAX_AUDIO_WEB_UPLOAD_SIZE,
    MSG_AUDIO_CONFIRM_TEMPLATE, MSG_PREVIOUS_REQUEST_REPLACED,
    MSG_REDIRECT_TO_WEB_UPLOAD,
)
from helpers.gemini_helper import get_safe_mime_type
from helpers.points_calculator import calculate_audio_transcription_cost
from keyboards import (
    get_audio_action_keyboard, get_document_export_keyboard, get_audio_confirm_keyboard,
    get_web_upload_redirect_keyboard,
)
from logger import get_logger, log_error, log_info
from services.audio_service import summarize_lecture_text, transcribe_audio_lecture
from services.export_service import build_document_docx, build_document_pdf, build_export_filename
from supabase_helper import (
    check_or_add_user, refund_user_points, update_user_stats, log_usage_event,
)
from r2_helper import download_audio_temp_to_file, delete_audio_temp
from utils import ensure_directory_exists, safe_file_cleanup

logger = get_logger(__name__)
router = Router()

DOWNLOADS_DIR = "downloads"
MAX_AUDIO_FILE_SIZE = 20 * 1024 * 1024  # حد تيليجرام لتحميل الملفات عبر بوتات API (20 ميجابايت)
MAX_INLINE_TEXT_CHARS = 4000  # حد رسالة تيليجرام النصية الآمن قبل اللجوء لإرسال مستند .txt


# ==================== أدوات مساعدة داخلية ====================

async def _current_user(message: types.Message, user: Any = None) -> Dict[str, Any]:
    user = user or message.from_user
    return await check_or_add_user(
        user.id,
        user.username or "Unknown",
        user.first_name or "Unknown",
        user.last_name or "Unknown",
    )


async def _insufficient_balance(message: types.Message, user_info: Dict[str, Any], required: float) -> None:
    balance = float(user_info.get("free_points") or 0) + float(user_info.get("paid_points") or 0)
    deficit = max(0.0, required - balance)
    contact = ADMIN_CONTACT.lstrip("@")
    keyboard = types.InlineKeyboardMarkup(
        inline_keyboard=[
            [types.InlineKeyboardButton(text="💳 شحن الرصيد الآن", url=f"https://t.me/{contact}")]
        ]
    )
    await message.answer(
        "❌ <b>رصيدك الحالي لا يكفي لتفريغ وتحليل هذه المحاضرة الصوتية.</b>\n\n"
        f"🎁 المجاني: <code>{float(user_info.get('free_points') or 0):.2f}</code>\n"
        f"💳 المدفوع: <code>{float(user_info.get('paid_points') or 0):.2f}</code>\n"
        f"💰 الإجمالي الحالي: <code>{balance:.2f}</code> / المطلوب: <code>{required:.2f}</code>\n"
        f"⚠️ العجز المطلوب شحنه: <b>{deficit:.2f} نقطة</b>",
        reply_markup=keyboard,
        parse_mode="HTML",
    )


def _extract_extension(message: types.Message) -> str:
    if message.voice:
        return ".ogg"
    file_name = message.audio.file_name or ""
    ext = os.path.splitext(file_name)[1]
    return ext if ext else ".mp3"


def _probe_actual_duration_seconds_sync(file_path: str) -> Optional[int]:
    """يقرأ المدة الفعلية للملف الصوتي بعد تحميله محلياً (من ترويسة الملف نفسها،
    وليس من الـ metadata التي أرسلها تطبيق تيليجرام للعميل مع الرسالة). مهم لأن
    `message.audio.duration` (خلافاً لـ `message.voice.duration` الذي يقيسه تيليجرام
    وقت التسجيل) قيمة يُرسلها العميل مع الملف وقد لا تطابق الملف الفعلي (بالخطأ أو
    عمداً)، بينما التكلفة الحقيقية عند Gemini مرتبطة بطول الصوت الفعلي لا المُعلَن.
    يُرجع None إن تعذّرت القراءة (صيغة غير مدعومة من mutagen، ملف تالف...) - عندها
    نرجع للاعتماد على مدة تيليجرام المُعلَنة كحل احتياطي."""
    try:
        audio = MutagenFile(file_path)
        if audio is not None and audio.info is not None and audio.info.length:
            return int(audio.info.length + 0.999)  # تقريب لأعلى لأقرب ثانية
    except Exception:  # أي فشل هون (صيغة غير مدعومة، ملف تالف...) غير حرج، نرجع None فقط
        pass
    return None


def _duration_cap_exceeded_text(duration_minutes: int) -> str:
    """🆕 رسالة موحّدة عند تجاوز مدة المحاضرة للحد الأقصى (MAX_AUDIO_DURATION_MINUTES) -
    مشتركة بين مسار تيليجرام المباشر ومسار رفع الويب."""
    return (
        f"❌ مدة هذه المحاضرة ({duration_minutes} دقيقة) تتجاوز الحد الأقصى المسموح به حالياً "
        f"({MAX_AUDIO_DURATION_MINUTES // 60} ساعات لكل ملف واحد). يرجى تقسيم التسجيل إلى "
        "أجزاء أصغر وإرسال كل جزء على حدة."
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

    🆕 حساب المدة على الملف الفعلي بعد تحميله (`_probe_actual_duration_seconds_sync`)
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

        actual_duration_seconds = await asyncio.to_thread(_probe_actual_duration_seconds_sync, destination)
        duration_seconds = max(reported_duration_seconds, actual_duration_seconds or 0) or reported_duration_seconds
        duration_minutes = max(1, (duration_seconds + 59) // 60)

        # 🆕 سقف مدة فعلية (وليس فقط حجم الملف) - راجع AI-NOTE بجانب MAX_AUDIO_DURATION_MINUTES
        # بـ constants.py لشرح سبب اختيار 4 ساعات تحديداً (مرتبط بحد توكنز إخراج Gemini).
        if duration_minutes > MAX_AUDIO_DURATION_MINUTES:
            await state.set_state(None)
            safe_file_cleanup(destination)
            try:
                await status_msg.edit_text(_duration_cap_exceeded_text(duration_minutes))
            except Exception:
                await message.answer(_duration_cap_exceeded_text(duration_minutes))
            return

        cost = calculate_audio_transcription_cost(duration_minutes)

        user_info = await _current_user(message)
        balance = float(user_info.get("free_points") or 0) + float(user_info.get("paid_points") or 0)

        # 🆕 تتبع تحليلي: يسجَّل بمجرد معرفة مدة/تكلفة الملف، بغض النظر عن قرار الطالب
        # لاحقاً بشاشة التأكيد - يسد فجوة "صفر أحداث" الحالية لميزة الصوت بالكامل.
        asyncio.create_task(log_usage_event(message.from_user.id, "audio_uploaded", {
            "duration_minutes": duration_minutes, "cost": cost, "source": "telegram_message",
        }))

        if balance < cost:
            await state.set_state(None)  # فكّ القفل - لم يُخصَم أي شيء إطلاقاً
            try:
                await status_msg.delete()
            except Exception:
                pass
            safe_file_cleanup(destination)
            await _insufficient_balance(message, user_info, cost)
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
    try:
        user_info = await _current_user(None, user=call.from_user)
        balance = float(user_info.get("free_points") or 0) + float(user_info.get("paid_points") or 0)
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

        if await update_user_stats(call.from_user.id, cost) is None:
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
            await refund_user_points(call.from_user.id, cost)
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
            partial_refund_amount = round(cost * 0.5, 2)
            if partial_refund_amount > 0:
                await refund_user_points(call.from_user.id, partial_refund_amount)
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
            await refund_user_points(call.from_user.id, cost)
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
            ensure_directory_exists(DOWNLOADS_DIR)
            txt_path = os.path.join(DOWNLOADS_DIR, f"transcript_{call.from_user.id}_{uuid.uuid4().hex}.txt")
            with open(txt_path, "w", encoding="utf-8") as f:
                f.write(pure_text)
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
            ensure_directory_exists(DOWNLOADS_DIR)
            txt_path = os.path.join(DOWNLOADS_DIR, f"summary_{call.from_user.id}_{uuid.uuid4().hex}.txt")
            try:
                with open(txt_path, "w", encoding="utf-8") as f:
                    f.write(summary)
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
      القياس الفعلي بعد التحميل المحلي (_probe_actual_duration_seconds_sync).
      ⚠️ قرار معلّق: لو تعذّر القياس (mutagen فشل)، الكود حالياً يفترض دقيقة واحدة
      فقط كحد أدنى (نفس سلوك duration_minutes = max(1, ...) بالتدفق العادي) - هاد
      ممكن يعني دفع أقل من التكلفة الحقيقية لمحاضرة طويلة بصيغة غير مدعومة. راجع
      PROJECT_STATUS.md قبل الإنتاج لتقرر إذا بدك رفض الملف بدل هيك بهالحالة تحديداً.
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

        actual_duration_seconds = await asyncio.to_thread(_probe_actual_duration_seconds_sync, destination)
        if actual_duration_seconds is None:
            log_error(logger, f"Could not probe duration for web-uploaded audio '{destination}' - defaulting to 1 minute charge.")
        duration_seconds = actual_duration_seconds or 0
        duration_minutes = max(1, (duration_seconds + 59) // 60)

        # 🆕 سقف مدة فعلية - نفس المنطق المطبَّق بالمسار المباشر بـ handle_audio_message،
        # وأهم هون تحديداً لأن حد الحجم بهالمسار (250MB) أعلى بكثير وما بيعكس المدة الفعلية.
        if duration_minutes > MAX_AUDIO_DURATION_MINUTES:
            await state.set_state(None)
            safe_file_cleanup(destination)
            await delete_audio_temp(object_path)
            try:
                await status_msg.edit_text(_duration_cap_exceeded_text(duration_minutes))
            except Exception:
                await bot.send_message(chat_id, _duration_cap_exceeded_text(duration_minutes))
            return

        cost = calculate_audio_transcription_cost(duration_minutes)

        # 2) استنتاج نوع MIME من محتوى الملف الفعلي (لا يوجد mime_type جاهز من
        # تيليجرام هون خلافاً لرسائل audio/voice العادية) - نفس الدالة المستخدمة
        # أصلاً بـ services/audio_service.py وhelpers/gemini_helper.py
        mime_type = get_safe_mime_type(destination)

        fake_user = type("U", (), {
            "id": user_id, "username": None, "first_name": "Unknown", "last_name": "Unknown",
        })()
        user_info = await _current_user(None, user=fake_user)
        balance = float(user_info.get("free_points") or 0) + float(user_info.get("paid_points") or 0)

        asyncio.create_task(log_usage_event(user_id, "audio_uploaded", {
            "duration_minutes": duration_minutes, "cost": cost, "source": "web_upload",
        }))

        if balance < cost:
            await state.set_state(None)
            safe_file_cleanup(destination)
            await delete_audio_temp(object_path)
            try:
                await status_msg.delete()
            except Exception:
                pass
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