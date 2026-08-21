# handlers/audio.py
"""
معالج المحاضرات الصوتية: يستقبل رسائل صوتية (voice/audio)، يفرّغها نصياً عبر
services/audio_service.py، ثم يعرض على الطالب أربعة إجراءات ممكنة على النص
الناتج (تصدير Word/PDF، تلخيص أكاديمي، تحويل لكويز، أو استلام النص الخام).
"""

import asyncio
import os
import uuid
from typing import Any, Dict

from aiogram import F, Router, types
from aiogram.fsm.context import FSMContext

from config import QuizState, bot
from constants import ADMIN_CONTACT
from helpers.points_calculator import calculate_audio_transcription_cost
from keyboards import get_audio_action_keyboard, get_document_export_keyboard
from logger import get_logger, log_error
from services.audio_service import summarize_lecture_text, transcribe_audio_lecture
from services.export_service import build_document_docx, build_document_pdf, build_export_filename
from supabase_helper import check_or_add_user, refund_user_points, update_user_stats
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


# ==================== استقبال ومعالجة الرسالة الصوتية ====================

@router.message(F.voice | F.audio)
async def handle_audio_message(message: types.Message, state: FSMContext) -> None:
    """يستقبل محاضرة صوتية، يتحقق من الحجم والرصيد، يخصم النقاط مقدماً، يفرّغ النص،
    ويعرض على الطالب لوحة الإجراءات المتاحة على النص الناتج."""
    current_state = await state.get_state()
    if current_state == QuizState.answering_quiz:
        await message.answer("⚠️ لديك اختبار قائم حالياً؛ أتممه أو أوقفه عبر الضغط على (⏹️ إيقاف) أولاً قبل رفع محاضرة صوتية جديدة.")
        return
    if current_state is not None:
        # أي طلب معلّق آخر (تصنيف نص/ملف سابق لم يُكمل) يُلغى بأمان عند وصول محاضرة صوتية جديدة
        await state.set_state(None)

    media = message.voice or message.audio
    file_size = media.file_size or 0
    if file_size > MAX_AUDIO_FILE_SIZE:
        await message.answer("❌ حجم الملف الصوتي أكبر من الحد المسموح به (20 ميجابايت). يرجى إرسال ملف أصغر.")
        return

    duration_seconds = message.voice.duration if message.voice else message.audio.duration
    duration_minutes = max(1, (duration_seconds + 59) // 60)
    cost = calculate_audio_transcription_cost(duration_minutes)

    user_info = await _current_user(message)
    balance = float(user_info.get("free_points") or 0) + float(user_info.get("paid_points") or 0)
    if balance < cost:
        await _insufficient_balance(message, user_info, cost)
        return

    if await update_user_stats(message.from_user.id, cost) is None:
        await _insufficient_balance(message, await _current_user(message), cost)
        return

    ensure_directory_exists(DOWNLOADS_DIR)
    extension = _extract_extension(message)
    destination = os.path.join(DOWNLOADS_DIR, f"audio_{message.from_user.id}_{uuid.uuid4().hex}{extension}")

    status_msg = await message.answer(
        "🎙️ <b>جارٍ تحميل المحاضرة الصوتية وتفريغها نصياً...</b>\nقد تستغرق العملية بضع دقائق حسب طول المحاضرة.",
        parse_mode="HTML",
    )

    try:
        await bot.download(media, destination=destination)

        try:
            mime_type = media.mime_type or ("audio/ogg" if message.voice else "audio/mp3")
            pure_text = await transcribe_audio_lecture(destination, mime_type)
        except Exception as exc:
            log_error(logger, f"Audio transcription failed: {exc}", exception=exc)
            pure_text = None

        if not pure_text or not pure_text.strip():
            await refund_user_points(message.from_user.id, cost)
            await status_msg.edit_text(
                "⚠️ <b>تعذر تفريغ المحاضرة الصوتية!</b> يرجى التأكد من وضوح الصوت والمحاولة مجدداً. تم إرجاع نقاطك.",
                parse_mode="HTML",
            )
            return

        source_title = message.audio.title if (message.audio and message.audio.title) else f"محاضرة صوتية ({duration_minutes} دقيقة)"
        await state.update_data(
            pure_text=pure_text,
            source_title=source_title,
            duration_minutes=duration_minutes,
            audio_debited_cost=cost,
            input_type="text",
            items_count=1,
            is_album=False,
        )
        await state.set_state(QuizState.waiting_for_audio_action)

        await status_msg.edit_text(
            f"✅ <b>تم تفريغ المحاضرة بنجاح!</b> (⏱️ {duration_minutes} دقيقة)\n\nماذا تريد أن تفعل بالنص المستخرج؟",
            parse_mode="HTML",
            reply_markup=get_audio_action_keyboard(),
        )
    except Exception as exc:
        log_error(logger, f"Audio handling failed: {exc}", exception=exc)
        await refund_user_points(message.from_user.id, cost)
        try:
            await status_msg.edit_text("❌ حدث خطأ غير متوقع أثناء معالجة المحاضرة الصوتية. تم إرجاع نقاطك.")
        except Exception:
            await message.answer("❌ حدث خطأ غير متوقع أثناء معالجة المحاضرة الصوتية. تم إرجاع نقاطك.")
    finally:
        safe_file_cleanup(destination)


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
        summary = await summarize_lecture_text(pure_text)
        if not summary or not summary.strip():
            await status_msg.edit_text("⚠️ تعذر توليد تلخيص للمحاضرة، حاول مجدداً لاحقاً.")
            return

        await state.update_data(summary_text=summary)

        if len(summary) <= MAX_INLINE_TEXT_CHARS:
            await status_msg.edit_text(f"✨ <b>ملخص المحاضرة (صياغة أكاديمية):</b>\n\n{summary}", parse_mode="HTML")
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


audio_router = router