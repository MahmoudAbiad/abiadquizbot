# handlers/export.py
"""معالجات تصدير الكويز إلى ملف Word أو PDF (من شاشة النتيجة أو من المفضلة).
المسار: اختيار الستايل (بسيط/عصري/أكاديمي) → اختيار الصيغة (Word/PDF) → توليد الملف."""
import asyncio
from task_utils import spawn
import re
from typing import List, Optional

from aiogram import F, Router, types
from aiogram.exceptions import TelegramBadRequest
from aiogram.fsm.context import FSMContext
from aiogram.types import BufferedInputFile

from keyboards import get_export_format_keyboard, get_export_style_keyboard
from logger import get_logger, log_error
from mem_utils import release_memory_to_os
# 🩹 FIX (memory-leak): بناء ملف الكويز الكامل يرسم صورة matplotlib لكل سؤال رياضي
# ضمن نفس نداء to_thread - نلف النداء بـ RENDER_SEMAPHORE (نفس السقف المستخدم أثناء
# حل الكويز المباشر) لمنع عدة تصديرات متزامنة من مضاعفة ذروة الذاكرة معًا.
from services.image_quiz_renderer import RENDER_SEMAPHORE
from services.processing_limits import EXPORT_SLOT, HEAVY_PROCESSING_SEMAPHORE, MAX_PENDING_EXPORTS

_PENDING_EXPORT_USERS = set()


async def _build_export(builder, *args):
    # Cancelling a coroutine does not stop its thread. Keep admission slots until
    # the actual worker finishes, including failure/cancellation.
    worker = asyncio.create_task(asyncio.to_thread(builder, *args))
    cancelled = False
    while not worker.done():
        try:
            await asyncio.shield(worker)
        except asyncio.CancelledError:
            cancelled = True
        except Exception:
            break
    if cancelled:
        if not worker.cancelled():
            worker.exception()  # Retrieve a possible error before discarding work.
        raise asyncio.CancelledError
    return worker.result()

from services.export_service import (
    ExportError,
    STYLE_CODE_TO_NAME,
    STYLE_LABELS_AR,
    build_export_filename,
    build_quiz_docx,
    build_quiz_pdf,
)
from helpers.supabase_helper import get_favorite_quiz, log_usage_event

logger = get_logger(__name__)
router = Router()

MSG_NO_QUESTIONS = "❌ لا يوجد كويز متاح للتصدير حالياً."
MSG_GENERATING = "⏳ جاري تجهيز الملف، لحظات..."
MSG_FAILED = "❌ تعذّر إنشاء الملف، حاول مجدداً بعد قليل."

_FORMAT_RE = re.compile(r"^export_(docx|pdf)([st]?)_([sma])$")
_STYLE_RE = re.compile(r"^export_style_([sma])$")
_FAV_FORMAT_RE = re.compile(r"^fav_export_(docx|pdf)([st]?)_([sma])_(.+)$")
_FAV_STYLE_RE = re.compile(r"^fav_export_style_([sma])_(.+)$")


async def _safe_answer(call: types.CallbackQuery, *args, **kwargs) -> None:
    try:
        await call.answer(*args, **kwargs)
    except TelegramBadRequest:
        pass


async def _generate_and_send(call: types.CallbackQuery, questions: List[dict], title: str,
                              fmt: str, style_code: str, include_answers: bool = True) -> None:
    if not questions:
        await _safe_answer(call, MSG_NO_QUESTIONS, show_alert=True)
        return
    user_id = call.from_user.id
    if user_id in _PENDING_EXPORT_USERS:
        await _safe_answer(call, "طلب التصدير السابق قيد الانتظار أو التجهيز.", show_alert=True)
        return
    if len(_PENDING_EXPORT_USERS) >= MAX_PENDING_EXPORTS:
        await _safe_answer(call, "طابور التصدير ممتلئ حاليًا، حاول بعد قليل.", show_alert=True)
        return
    # Reserve synchronously before the first await to prevent duplicate admission.
    _PENDING_EXPORT_USERS.add(user_id)
    status = None
    file_bytes = document = None
    try:
        await _safe_answer(call)
        status = await call.message.answer("⏳ تم تسجيل طلبك في طابور التصدير؛ يبدأ التجهيز عند توفر الدور.")
        style = STYLE_CODE_TO_NAME.get(style_code, "simple")
        async with EXPORT_SLOT:
            async with HEAVY_PROCESSING_SEMAPHORE:
                await status.edit_text(MSG_GENERATING)
                async with RENDER_SEMAPHORE:
                    builder = build_quiz_docx if fmt == "docx" else build_quiz_pdf
                    file_bytes = await _build_export(builder, title, questions, style, include_answers)
            # Keep the export slot through upload: only one completed file in RAM.
            version = "الأستاذ" if include_answers else "الطالب"
            filename = build_export_filename(f"{title}_{version}", fmt)
            document = BufferedInputFile(file_bytes, filename=filename)
            await call.message.answer_document(document=document, caption=f"📄 نسخة {version} جاهزة.")
            document = file_bytes = None
            await status.delete()
        spawn(log_usage_event(user_id, "quiz_exported", {
            "format": fmt, "style": style, "title": title,
            "include_answers": include_answers,
        }))
    except ExportError as e:
        if status:
            try:
                await status.edit_text(f"❌ {e}")
            except TelegramBadRequest:
                pass
    except Exception as e:
        log_error(logger, f"Error generating export ({fmt}/{style_code}): {e}", exception=e)
        if status:
            try:
                await status.edit_text(MSG_FAILED)
            except TelegramBadRequest:
                pass
    finally:
        document = file_bytes = None
        _PENDING_EXPORT_USERS.discard(user_id)
        release_memory_to_os()


# ==================== تصدير الكويز الحالي (من شاشة النتيجة) ====================

@router.callback_query(F.data == "export_menu")
async def export_menu(call: types.CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    questions = data.get("questions") or []
    if not questions:
        await _safe_answer(call, MSG_NO_QUESTIONS, show_alert=True)
        return
    await call.message.answer("🎨 اختر شكل تنسيق الملف:", reply_markup=get_export_style_keyboard())
    await _safe_answer(call)


@router.callback_query(F.data.regexp(_STYLE_RE))
async def export_style_chosen(call: types.CallbackQuery) -> None:
    style_code = _STYLE_RE.match(call.data).group(1)
    label = STYLE_LABELS_AR[STYLE_CODE_TO_NAME[style_code]]
    await call.message.answer(f"📁 اخترت: {label}\nهلق اختر صيغة تحميل الكويز:",
                               reply_markup=get_export_format_keyboard(style_code))
    await _safe_answer(call)


@router.callback_query(F.data.regexp(_FORMAT_RE))
async def export_current_quiz(call: types.CallbackQuery, state: FSMContext) -> None:
    fmt, variant, style_code = _FORMAT_RE.match(call.data).groups()
    data = await state.get_data()
    questions = data.get("questions") or []
    title = data.get("source_title") or "كويز"
    await _generate_and_send(call, questions, title, fmt, style_code, include_answers=variant != "s")
    await _safe_answer(call)


# ==================== تصدير كويز محفوظ في المفضلة ====================

@router.callback_query(F.data.startswith("fav_export_menu_"))
async def export_favorite_menu(call: types.CallbackQuery) -> None:
    fav_id = call.data.replace("fav_export_menu_", "", 1)
    await call.message.answer("🎨 اختر شكل تنسيق الملف:", reply_markup=get_export_style_keyboard(fav_id))
    await _safe_answer(call)


@router.callback_query(F.data.regexp(_FAV_STYLE_RE))
async def export_favorite_style_chosen(call: types.CallbackQuery) -> None:
    style_code, fav_id = _FAV_STYLE_RE.match(call.data).groups()
    label = STYLE_LABELS_AR[STYLE_CODE_TO_NAME[style_code]]
    await call.message.answer(f"📁 اخترت: {label}\nهلق اختر صيغة تحميل الكويز:",
                               reply_markup=get_export_format_keyboard(style_code, fav_id))
    await _safe_answer(call)


@router.callback_query(F.data.regexp(_FAV_FORMAT_RE))
async def export_favorite_quiz(call: types.CallbackQuery) -> None:
    fmt, variant, style_code, fav_id = _FAV_FORMAT_RE.match(call.data).groups()

    favorite: Optional[dict] = await get_favorite_quiz(call.from_user.id, fav_id)
    if not favorite:
        await _safe_answer(call, "❌ الكويز غير موجود أو تم حذفه مسبقاً.", show_alert=True)
        return

    questions = favorite.get("quiz_data") or []
    title = favorite.get("title") or "كويز محفوظ"
    await _generate_and_send(call, questions, title, fmt, style_code, include_answers=variant != "s")
    await _safe_answer(call)


export_router = router

