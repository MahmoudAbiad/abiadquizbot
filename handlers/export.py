# handlers/export.py
"""معالجات تصدير الكويز إلى ملف Word أو PDF (من شاشة النتيجة أو من المفضلة)."""
import asyncio
from typing import List, Optional

from aiogram import F, Router, types
from aiogram.exceptions import TelegramBadRequest
from aiogram.fsm.context import FSMContext
from aiogram.types import BufferedInputFile

from keyboards import get_export_format_keyboard
from logger import get_logger, log_error
from services.export_service import (
    ExportError,
    build_export_filename,
    build_quiz_docx,
    build_quiz_pdf,
)
from supabase_helper import get_favorite_quiz

logger = get_logger(__name__)
router = Router()

MSG_NO_QUESTIONS = "❌ لا يوجد كويز متاح للتصدير حالياً."
MSG_GENERATING = "⏳ جاري تجهيز الملف، لحظات..."
MSG_FAILED = "❌ تعذّر إنشاء الملف، حاول مجدداً بعد قليل."


async def _safe_answer(call: types.CallbackQuery, *args, **kwargs) -> None:
    try:
        await call.answer(*args, **kwargs)
    except TelegramBadRequest:
        pass


async def _generate_and_send(call: types.CallbackQuery, questions: List[dict], title: str, fmt: str) -> None:
    if not questions:
        await _safe_answer(call, MSG_NO_QUESTIONS, show_alert=True)
        return

    status = await call.message.answer(MSG_GENERATING)
    try:
        if fmt == "docx":
            file_bytes = await asyncio.to_thread(build_quiz_docx, title, questions)
            filename = build_export_filename(title, "docx")
            caption = "📄 تفضّل، ملف Word الخاص بالكويز جاهز."
        else:
            file_bytes = await asyncio.to_thread(build_quiz_pdf, title, questions)
            filename = build_export_filename(title, "pdf")
            caption = "📕 تفضّل، ملف PDF الخاص بالكويز جاهز."

        document = BufferedInputFile(file_bytes, filename=filename)
        await call.message.answer_document(document=document, caption=caption)
        await status.delete()
    except ExportError as e:
        try:
            await status.edit_text(f"❌ {e}")
        except TelegramBadRequest:
            pass
    except Exception as e:
        log_error(logger, f"Error generating export ({fmt}): {e}", exception=e)
        try:
            await status.edit_text(MSG_FAILED)
        except TelegramBadRequest:
            pass


# ==================== تصدير الكويز الحالي (من شاشة النتيجة) ====================

@router.callback_query(F.data == "export_menu")
async def export_menu(call: types.CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    questions = data.get("questions") or []
    if not questions:
        await _safe_answer(call, MSG_NO_QUESTIONS, show_alert=True)
        return
    await call.message.answer("📁 اختر صيغة تحميل الكويز:", reply_markup=get_export_format_keyboard())
    await _safe_answer(call)


@router.callback_query(F.data.in_({"export_docx", "export_pdf"}))
async def export_current_quiz(call: types.CallbackQuery, state: FSMContext) -> None:
    fmt = "docx" if call.data == "export_docx" else "pdf"
    data = await state.get_data()
    questions = data.get("questions") or []
    title = data.get("source_title") or "كويز"
    await _generate_and_send(call, questions, title, fmt)
    await _safe_answer(call)


# ==================== تصدير كويز محفوظ في المفضلة ====================

@router.callback_query(F.data.startswith("fav_export_menu_"))
async def export_favorite_menu(call: types.CallbackQuery) -> None:
    fav_id = call.data.replace("fav_export_menu_", "", 1)
    await call.message.answer("📁 اختر صيغة تحميل الكويز:", reply_markup=get_export_format_keyboard(fav_id))
    await _safe_answer(call)


@router.callback_query(F.data.startswith("fav_export_docx_") | F.data.startswith("fav_export_pdf_"))
async def export_favorite_quiz(call: types.CallbackQuery) -> None:
    fmt = "docx" if call.data.startswith("fav_export_docx_") else "pdf"
    fav_id = call.data.replace(f"fav_export_{fmt}_", "", 1)

    favorite: Optional[dict] = await get_favorite_quiz(call.from_user.id, fav_id)
    if not favorite:
        await _safe_answer(call, "❌ الكويز غير موجود أو تم حذفه مسبقاً.", show_alert=True)
        return

    questions = favorite.get("quiz_data") or []
    title = favorite.get("title") or "كويز محفوظ"
    await _generate_and_send(call, questions, title, fmt)
    await _safe_answer(call)


export_router = router
