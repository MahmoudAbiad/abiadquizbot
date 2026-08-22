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
from constants import ADMIN_CONTACT, ESTIMATED_TRANSCRIPTION_SECONDS_PER_MINUTE
from helpers.gemini_helper import get_safe_mime_type
from helpers.points_calculator import calculate_audio_transcription_cost
from keyboards import get_audio_action_keyboard, get_document_export_keyboard
from logger import get_logger, log_error, log_info
from services.audio_service import summarize_lecture_text, transcribe_audio_lecture
from services.export_service import build_document_docx, build_document_pdf, build_export_filename
from supabase_helper import (
    check_or_add_user, refund_user_points, update_user_stats,
    download_audio_temp_to_file, delete_audio_temp,
)
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


# ==================== استقبال ومعالجة الرسالة الصوتية ====================

@router.message(F.voice | F.audio)
async def handle_audio_message(message: types.Message, state: FSMContext) -> None:
    """يستقبل محاضرة صوتية، يتحقق من الحجم، يحمّلها، يتحقق من مدتها الفعلية والرصيد،
    يخصم النقاط، يفرّغ النص، ويعرض على الطالب لوحة الإجراءات المتاحة على النص الناتج.

    🆕 قفل معالجة (`QuizState.processing_audio`): يُضبط فور التأكد من الحجم، قبل أي
    عملية تحميل/شبكة، لمنع معالجة مضاعفة (وخصم نقاط مرتين، وضياع نص أول محاضرة من
    الـ state لصالح ثانية) لو وصل مقطع صوتي جديد من نفس الطالب قبل اكتمال معالجة
    السابق. يُفكّ القفل صراحة بكل مسارات الخروج المبكر (رصيد غير كافٍ، فشل تفريغ...)
    وضمنياً بمسار النجاح (الانتقال لـ waiting_for_audio_action).

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
    if current_state is not None:
        # أي طلب معلّق آخر (تصنيف نص/ملف سابق لم يُكمل، أو شاشة إجراءات محاضرة سابقة
        # جاهزة لم يُقرَّر مصيرها بعد) يُلغى بأمان عند وصول محاضرة صوتية جديدة
        await state.set_state(None)

    media = message.voice or message.audio
    file_size = media.file_size or 0
    if file_size > MAX_AUDIO_FILE_SIZE:
        await message.answer("❌ حجم الملف الصوتي أكبر من الحد المسموح به (20 ميجابايت). يرجى إرسال ملف أصغر.")
        return

    # 🆕 نضبط القفل فوراً هون - قبل أي await لعملية شبكة/تحميل - لتضييق نافذة السباق
    # الزمنية قدر الإمكان إذا وصل مقطع صوتي ثانٍ من نفس الطالب بنفس اللحظة تقريباً.
    await state.set_state(QuizState.processing_audio)

    reported_duration_seconds = message.voice.duration if message.voice else (message.audio.duration or 0)

    ensure_directory_exists(DOWNLOADS_DIR)
    extension = _extract_extension(message)
    destination = os.path.join(DOWNLOADS_DIR, f"audio_{message.from_user.id}_{uuid.uuid4().hex}{extension}")

    status_msg = await message.answer(
        "🎙️ <b>جارٍ تحميل المحاضرة الصوتية...</b>\nقد تستغرق العملية بضع دقائق حسب طول المحاضرة.",
        parse_mode="HTML",
    )

    cost = 0.0
    charged = False
    try:
        await bot.download(media, destination=destination)

        actual_duration_seconds = await asyncio.to_thread(_probe_actual_duration_seconds_sync, destination)
        duration_seconds = max(reported_duration_seconds, actual_duration_seconds or 0) or reported_duration_seconds
        duration_minutes = max(1, (duration_seconds + 59) // 60)
        cost = calculate_audio_transcription_cost(duration_minutes)

        user_info = await _current_user(message)
        balance = float(user_info.get("free_points") or 0) + float(user_info.get("paid_points") or 0)
        if balance < cost:
            await state.set_state(None)  # فكّ القفل - لم يُخصَم أي شيء بعد
            try:
                await status_msg.delete()
            except Exception:
                pass
            await _insufficient_balance(message, user_info, cost)
            return

        if await update_user_stats(message.from_user.id, cost) is None:
            # 🆕 حالة سباق رصيد نادرة جداً (خصم متزامن آخر أفرغ الرصيد بين لحظة الفحص
            # ولحظة الخصم الذري). لا داعي لاستعلام DB إضافي (`_current_user` مجدداً)
            # فقط لعرض رقم رصيد دقيق هون - الرسالة الأصلية `_insufficient_balance`
            # بتحتاج بيانات طازجة عشان ما تعرض تناقض (رصيد كافٍ ظاهرياً + رسالة
            # "غير كافٍ")، فبدل هيك نعرض رسالة تعارض عامة وبنطلب إعادة المحاولة مباشرة.
            await state.set_state(None)  # فكّ القفل - فشل الخصم فعلياً
            race_text = "⚠️ <b>حصل تعارض بسيط برصيدك أثناء المعالجة</b> (عملية أخرى متزامنة على حسابك). يرجى إعادة إرسال المحاضرة الصوتية مجدداً."
            try:
                await status_msg.edit_text(race_text, parse_mode="HTML")
            except Exception:
                await message.answer(race_text, parse_mode="HTML")
            return
        charged = True

        await status_msg.edit_text(
            "🎙️ <b>جارٍ تفريغ المحاضرة نصياً...</b>\nقد تستغرق العملية بضع دقائق حسب طول المحاضرة.",
            parse_mode="HTML",
        )

        try:
            mime_type = media.mime_type or ("audio/ogg" if message.voice else "audio/mp3")
            pure_text = await transcribe_audio_lecture(destination, mime_type)
        except Exception as exc:
            log_error(logger, f"Audio transcription failed: {exc}", exception=exc)
            pure_text = None

        if not pure_text or not pure_text.strip():
            await refund_user_points(message.from_user.id, cost)
            await state.set_state(None)  # فكّ القفل
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
        await state.set_state(QuizState.waiting_for_audio_action)  # ينهي القفل ضمنياً

        await status_msg.edit_text(
            f"✅ <b>تم تفريغ المحاضرة بنجاح!</b> (⏱️ {duration_minutes} دقيقة)\n\nماذا تريد أن تفعل بالنص المستخرج؟",
            parse_mode="HTML",
            reply_markup=get_audio_action_keyboard(),
        )
    except Exception as exc:
        log_error(logger, f"Audio handling failed: {exc}", exception=exc)
        if charged:
            await refund_user_points(message.from_user.id, cost)
        await state.set_state(None)  # فكّ القفل بكل الأحوال
        try:
            await status_msg.edit_text("❌ حدث خطأ غير متوقع أثناء معالجة المحاضرة الصوتية. تم إرجاع نقاطك." if charged else "❌ حدث خطأ غير متوقع أثناء معالجة المحاضرة الصوتية.")
        except Exception:
            await message.answer("❌ حدث خطأ غير متوقع أثناء معالجة المحاضرة الصوتية. تم إرجاع نقاطك." if charged else "❌ حدث خطأ غير متوقع أثناء معالجة المحاضرة الصوتية.")
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
    تأكيد اكتمال الرفع على Supabase.

    الفروقات عن handle_audio_message:
    - لا يوجد حد MAX_AUDIO_FILE_SIZE (20MB) هون - الحد المطبَّق مسبقاً هو
      MAX_AUDIO_WEB_UPLOAD_SIZE (250MB) بمرحلة /api/audio-upload/init.
    - لا يوجد "مدة معلَنة" من تيليجرام لملف مرفوع عبر الويب - الاعتماد الكامل على
      القياس الفعلي بعد التحميل المحلي (_probe_actual_duration_seconds_sync).
      ⚠️ قرار معلّق: لو تعذّر القياس (mutagen فشل)، الكود حالياً يفترض دقيقة واحدة
      فقط كحد أدنى (نفس سلوك duration_minutes = max(1, ...) بالتدفق العادي) - هاد
      ممكن يعني دفع أقل من التكلفة الحقيقية لمحاضرة طويلة بصيغة غير مدعومة. راجع
      PROJECT_STATUS.md قبل الإنتاج لتقرر إذا بدك رفض الملف بدل هيك بهالحالة تحديداً.
    - الملف يُحذف من Supabase Storage حتماً بنهاية الدالة (finally)، بالإضافة
      لحذف النسخة المحلية (safe_file_cleanup) - نفس مبدأ "لا تخزين دائم" المطلوب.
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
    if current_state is not None:
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

    cost = 0.0
    charged = False
    try:
        # 1) تنزيل الملف من التخزين المؤقت بـ Supabase للقرص المحلي
        downloaded = await download_audio_temp_to_file(object_path, destination)
        if not downloaded:
            await state.set_state(None)
            await status_msg.edit_text("❌ تعذر تحميل الملف المرفوع، يرجى إعادة المحاولة من البوت.")
            return

        await status_msg.edit_text(
            "🎙️ <b>جارٍ تفريغ المحاضرة نصياً...</b>\nقد تستغرق العملية بضع دقائق حسب طول المحاضرة.",
            parse_mode="HTML",
        )

        actual_duration_seconds = await asyncio.to_thread(_probe_actual_duration_seconds_sync, destination)
        if actual_duration_seconds is None:
            log_error(logger, f"Could not probe duration for web-uploaded audio '{destination}' - defaulting to 1 minute charge.")
        duration_seconds = actual_duration_seconds or 0
        duration_minutes = max(1, (duration_seconds + 59) // 60)
        cost = calculate_audio_transcription_cost(duration_minutes)

        # 🆕 عرض الوقت المتوقع للتفريغ (تقدير تقريبي - راجع ملاحظة المعايرة بـ constants.py)
        estimated_seconds = duration_minutes * ESTIMATED_TRANSCRIPTION_SECONDS_PER_MINUTE
        estimated_label = (
            f"~{max(1, estimated_seconds // 60)} دقيقة" if estimated_seconds >= 60
            else f"~{estimated_seconds} ثانية"
        )
        await status_msg.edit_text(
            f"⏱️ <b>مدة المحاضرة:</b> {duration_minutes} دقيقة\n"
            f"⏳ <b>الوقت المتوقع لإنهاء التفريغ:</b> {estimated_label} تقريباً\n\n"
            f"جارٍ التفريغ الآن...",
            parse_mode="HTML",
        )

        fake_user = type("U", (), {
            "id": user_id, "username": None, "first_name": "Unknown", "last_name": "Unknown",
        })()
        user_info = await _current_user(None, user=fake_user)
        balance = float(user_info.get("free_points") or 0) + float(user_info.get("paid_points") or 0)
        if balance < cost:
            await state.set_state(None)
            await status_msg.delete()
            # 🆕 إعادة استخدام _insufficient_balance تتطلب كائن message حقيقي - هون نبني رسالة مبسطة بديلة
            await bot.send_message(
                chat_id,
                f"❌ رصيدك الحالي لا يكفي لتفريغ هذه المحاضرة.\n"
                f"💰 الإجمالي الحالي: <code>{balance:.2f}</code> / المطلوب: <code>{cost:.2f}</code>",
                parse_mode="HTML",
            )
            return

        if await update_user_stats(user_id, cost) is None:
            await state.set_state(None)
            await status_msg.edit_text(
                "⚠️ <b>حصل تعارض بسيط برصيدك أثناء المعالجة.</b> يرجى إعادة رفع الملف من جديد.",
                parse_mode="HTML",
            )
            return
        charged = True

        # 2) استنتاج نوع MIME من محتوى الملف الفعلي (لا يوجد mime_type جاهز من
        # تيليجرام هون خلافاً لرسائل audio/voice العادية) - نفس الدالة المستخدمة
        # أصلاً بـ services/audio_service.py وhelpers/gemini_helper.py
        mime_type = get_safe_mime_type(destination)

        transcription_started_at = time.monotonic()
        try:
            pure_text = await transcribe_audio_lecture(destination, mime_type)
        except Exception as exc:
            log_error(logger, f"Web-uploaded audio transcription failed: {exc}", exception=exc)
            pure_text = None
        finally:
            # 🆕 سجل الزمن الفعلي مقابل مدة المحاضرة - استخدم هاي القيم لاحقاً لمعايرة
            # ESTIMATED_TRANSCRIPTION_SECONDS_PER_MINUTE بدل الرقم التقريبي الحالي (12)
            elapsed = time.monotonic() - transcription_started_at
            per_minute = elapsed / duration_minutes if duration_minutes else elapsed
            log_info(logger, f"Transcription timing: {duration_minutes}min audio took {elapsed:.1f}s ({per_minute:.1f}s/min)")

        if not pure_text or not pure_text.strip():
            await refund_user_points(user_id, cost)
            await state.set_state(None)
            await status_msg.edit_text(
                "⚠️ <b>تعذر تفريغ المحاضرة الصوتية!</b> يرجى التأكد من وضوح الصوت والمحاولة مجدداً. تم إرجاع نقاطك.",
                parse_mode="HTML",
            )
            return

        source_title = declared_file_name or f"محاضرة صوتية ({duration_minutes} دقيقة)"
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
        log_error(logger, f"Web-uploaded audio handling failed: {exc}", exception=exc)
        if charged:
            await refund_user_points(user_id, cost)
        await state.set_state(None)
        try:
            await status_msg.edit_text("❌ حدث خطأ غير متوقع أثناء معالجة المحاضرة الصوتية." + (" تم إرجاع نقاطك." if charged else ""))
        except Exception:
            await bot.send_message(chat_id, "❌ حدث خطأ غير متوقع أثناء معالجة المحاضرة الصوتية." + (" تم إرجاع نقاطك." if charged else ""))
    finally:
        # 🆕 حذف مضمون بكل الأحوال - محلياً + من Supabase Storage (لا تخزين دائم مطلقاً)
        safe_file_cleanup(destination)
        await delete_audio_temp(object_path)


audio_router = router