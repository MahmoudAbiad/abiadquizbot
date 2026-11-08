# handlers/export.py
"""تصدير الكويز إلى Word أو PDF أو Excel من النتيجة أو المفضلة."""
import asyncio
from task_utils import spawn
import re
from typing import List, Optional

from aiogram import F, Router, types
from aiogram.exceptions import TelegramBadRequest
from aiogram.fsm.context import FSMContext
from aiogram.types import BufferedInputFile

from dataclasses import replace
from html import escape

from config import ExportState
from keyboards import (
    get_export_format_keyboard, get_export_input_keyboard, get_export_per_page_keyboard,
    get_export_placement_keyboard, get_export_settings_keyboard, get_export_style_keyboard,
)
from helpers.student_ui import EXPORT_STYLE_TEXT, export_format_text, export_settings_text
from logger import get_logger, log_error
from mem_utils import release_memory_to_os
from services import offload
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
    build_quiz_xlsx,
    max_questions_per_page,
    max_questions_per_page_docx,
)
from services.export_options import (
    ANSWERS_MODES, FIELD_CODES, PLACEMENTS, TEXT_FIELDS, ExportOptions, ExportOptionsError,
)
from helpers.supabase_helper import get_favorite_quiz, log_usage_event

logger = get_logger(__name__)
router = Router()

MSG_NO_QUESTIONS = "❌ لا يوجد كويز متاح للتصدير حالياً."
MSG_FAILED = "تعذّر تجهيز الملف الآن. الكويز ما زال متاحاً؛ أعد المحاولة من خيارات التحميل بعد قليل."


def _estimate_export_seconds(questions: List[dict], fmt: str) -> int:
    """A rough local-work estimate; remote queue and upload time remain variable."""
    count = len(questions)
    math_count = sum(bool(q.get("is_math") or q.get("math_expression")) for q in questions if isinstance(q, dict))
    per_question = {"xlsx": 0.8, "docx": 1.0, "pdf": 1.8}.get(fmt, 1.5)
    estimate = 6 + count * per_question + math_count * (1.5 if fmt == "pdf" else 0.7)
    return max(10, min(180, int(5 * round(estimate / 5))))


def _export_waiting_text(elapsed: float, estimate: int) -> str:
    remaining = estimate - int(elapsed)
    if remaining > 0:
        return f"⏳ جارٍ تجهيز ملفك. الوقت المتوقع المتبقي: نحو {remaining} ثانية."
    if elapsed < estimate * 2:
        return "⏳ استغرق تجهيز الملف وقتاً أطول من المتوقع، وما زلنا نعمل عليه."
    return "⏳ ما زلنا نجهّز الملف. سنرسله هنا فور اكتماله؛ لا تحتاج لإعادة الطلب."

_FORMAT_RE = re.compile(r"^export_(docx|pdf|xlsx)([st]?)_([sma])$")
_STYLE_RE = re.compile(r"^export_style_([sma])$")
_FAV_FORMAT_RE = re.compile(r"^fav_export_(docx|pdf|xlsx)([st]?)_([sma])_(.+)$")
_FAV_STYLE_RE = re.compile(r"^fav_export_style_([sma])_(.+)$")


async def _safe_answer(call: types.CallbackQuery, *args, **kwargs) -> None:
    try:
        await call.answer(*args, **kwargs)
    except TelegramBadRequest:
        pass


async def _generate_and_send(call: types.CallbackQuery, questions: List[dict], title: str,
                              fmt: str, style_code: str, include_answers: bool = True,
                              options=None) -> None:
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
    heartbeat_task = None
    file_bytes = document = None
    try:
        await _safe_answer(call)
        estimate = _estimate_export_seconds(questions, fmt)
        started = asyncio.get_running_loop().time()
        status = await call.message.answer(_export_waiting_text(0, estimate))

        async def _export_heartbeat() -> None:
            last_text = _export_waiting_text(0, estimate)
            while True:
                await asyncio.sleep(5)
                text = _export_waiting_text(asyncio.get_running_loop().time() - started, estimate)
                if text == last_text:
                    continue
                try:
                    await status.edit_text(text)
                    last_text = text
                except Exception as exc:
                    retry_after = getattr(exc, "retry_after", None)
                    if isinstance(retry_after, (int, float)) and retry_after > 0:
                        await asyncio.sleep(min(float(retry_after), 30))

        heartbeat_task = asyncio.create_task(_export_heartbeat())
        style = STYLE_CODE_TO_NAME.get(style_code, "simple")
        # إعدادات ورقة الاختبار: تُمرَّر للمولّد فقط إن غيّرها المعلم؛ غير ذلك تبقى الاستدعاءات كما كانت.
        custom = options is not None and not options.is_default
        opt_args = (options,) if custom else ()
        remote_opt_args = (options.to_dict(),) if custom else ()
        async with EXPORT_SLOT:
            if fmt == "xlsx":
                # Math workbooks render panels locally; share the same resource
                # gates and cancellation protection as other rendered exports.
                async with HEAVY_PROCESSING_SEMAPHORE:
                    async with RENDER_SEMAPHORE:
                        file_bytes = await _build_export(build_quiz_xlsx, title, questions, style, include_answers, *opt_args)
            if fmt != "xlsx":
                file_bytes = await offload.remote_build_quiz(fmt, title, questions, style, include_answers, *remote_opt_args)
            if file_bytes is None:
                async with HEAVY_PROCESSING_SEMAPHORE:
                    async with RENDER_SEMAPHORE:
                        builder = build_quiz_docx if fmt == "docx" else build_quiz_pdf
                        file_bytes = await _build_export(builder, title, questions, style, include_answers, *opt_args)
            # Keep the export slot through upload: only one completed file in RAM.
            version = "الأستاذ" if include_answers else "الطالب"
            file_title = (options.exam_title or title) if custom else title
            filename = build_export_filename(f"{file_title}_{version}", fmt)
            document = BufferedInputFile(file_bytes, filename=filename)
            caption = f"📄 نسخة {version} جاهزة."
            if custom:
                # يؤكد للمعلم ما طُبّق فعلاً (الإعدادات محفوظة في جلسته لا في الزر نفسه).
                caption += "\n⚙️ " + "، ".join(options.summary_lines_ar())
            await call.message.answer_document(document=document, caption=caption[:1000])
            document = file_bytes = None
            heartbeat_task.cancel()
            await asyncio.gather(heartbeat_task, return_exceptions=True)
            await status.delete()
        spawn(log_usage_event(user_id, "quiz_exported", {
            "format": fmt, "style": style, "title": title,
            "include_answers": include_answers,
        }))
    except ExportError as e:
        if heartbeat_task:
            heartbeat_task.cancel()
            await asyncio.gather(heartbeat_task, return_exceptions=True)
        if status:
            try:
                await status.edit_text(f"❌ {e}")
            except TelegramBadRequest:
                pass
    except Exception as e:
        log_error(logger, f"Error generating export ({fmt}/{style_code}): {e}", exception=e)
        if heartbeat_task:
            heartbeat_task.cancel()
            await asyncio.gather(heartbeat_task, return_exceptions=True)
        if status:
            try:
                await status.edit_text(MSG_FAILED)
            except TelegramBadRequest:
                pass
    finally:
        if heartbeat_task:
            heartbeat_task.cancel()
            await asyncio.gather(heartbeat_task, return_exceptions=True)
        document = file_bytes = None
        _PENDING_EXPORT_USERS.discard(user_id)
        release_memory_to_os()


# ==================== تصدير الكويز الحالي (من شاشة النتيجة) ====================

async def _edit_export_screen(call, text, keyboard):
    try:
        await call.message.edit_text(text, parse_mode="HTML", reply_markup=keyboard)
    except TelegramBadRequest as exc:
        if "message is not modified" not in str(exc).lower():
            await call.message.answer(text, parse_mode="HTML", reply_markup=keyboard)


@router.callback_query(F.data == "export_close")
async def close_export(call: types.CallbackQuery):
    await _edit_export_screen(call, "تم إغلاق خيارات التحميل. يمكنك فتحها مجدداً من رسالة نتيجة الكويز.", None)
    await _safe_answer(call)


@router.callback_query(F.data == "export_back")
async def export_back(call: types.CallbackQuery):
    await _edit_export_screen(call, EXPORT_STYLE_TEXT, get_export_style_keyboard())
    await _safe_answer(call)


@router.callback_query(F.data.startswith("fav_export_back_"))
async def export_favorite_back(call: types.CallbackQuery):
    favorite_id = call.data.removeprefix("fav_export_back_")
    await _edit_export_screen(call, EXPORT_STYLE_TEXT, get_export_style_keyboard(favorite_id))
    await _safe_answer(call)

@router.callback_query(F.data == "export_menu")
async def export_menu(call: types.CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    questions = data.get("questions") or []
    if not questions:
        await _safe_answer(call, MSG_NO_QUESTIONS, show_alert=True)
        return
    await call.message.answer(EXPORT_STYLE_TEXT, parse_mode="HTML", reply_markup=get_export_style_keyboard())
    await _safe_answer(call)


@router.callback_query(F.data.regexp(_STYLE_RE))
async def export_style_chosen(call: types.CallbackQuery, state: FSMContext) -> None:
    style_code = _STYLE_RE.match(call.data).group(1)
    label = STYLE_LABELS_AR[STYLE_CODE_TO_NAME[style_code]]
    await _edit_export_screen(call, export_format_text(label) + await _active_settings_note(state),
                              get_export_format_keyboard(style_code))
    await _safe_answer(call)


@router.callback_query(F.data.regexp(_FORMAT_RE))
async def export_current_quiz(call: types.CallbackQuery, state: FSMContext) -> None:
    fmt, variant, style_code = _FORMAT_RE.match(call.data).groups()
    data = await state.get_data()
    questions = data.get("questions") or []
    title = data.get("source_title") or "كويز"
    await _leave_input_state(state)
    await _generate_and_send(call, questions, title, fmt, style_code, include_answers=variant != "s",
                             options=ExportOptions.from_state_data(data))
    await _safe_answer(call)


# ==================== إعدادات إضافية لورقة الاختبار ====================
# تُحفظ في بيانات جلسة المستخدم (FSM على Redis، مفتاح export_options) وتُقرأ لحظة الضغط على
# زر الصيغة، فلا تعتمد على callback_data ولا على ذاكرة العملية. تنتهي بانتهاء الجلسة (24 ساعة
# خمول) أو بمسح الجلسة؛ والملف المُرسل يذكر الإعدادات المطبّقة في تعليقه.

_XO_RE = re.compile(r"^xo\|([a-z]+)\|([A-Za-z0-9_-]*)\|([sma])\|(.*)$")

_FIELD_PROMPTS = {
    "exam_title": "أرسل <b>عنوان الاختبار</b> (يظهر أعلى الورقة وفي اسم الملف):",
    "subject": "أرسل <b>اسم المادة</b>، مثل: الرياضيات",
    "grade": "أرسل <b>الصف</b>، مثل: التاسع",
    "exam_date": "أرسل <b>تاريخ الاختبار</b> بأي صيغة تناسبك، مثل: 2026/11/07",
    "duration": "أرسل <b>مدة الاختبار</b>، مثل: 45 دقيقة",
    "total_marks": "أرسل <b>العلامة الكلية</b> (رقم فقط)، مثل: 100",
    "teacher_name": "أرسل <b>اسم الأستاذ</b> الذي أنشأ الاختبار:",
}


async def _get_options(state: FSMContext) -> ExportOptions:
    return ExportOptions.from_state_data(await state.get_data())


async def _save_options(state: FSMContext, options: ExportOptions) -> None:
    await state.update_data(export_options=options.to_dict())


async def _active_settings_note(state: FSMContext) -> str:
    """سطر تذكير بالإعدادات الإضافية الفعّالة على شاشة اختيار الصيغة (فارغ إن لا شيء)."""
    lines = (await _get_options(state)).summary_lines_ar()
    if not lines:
        return ""
    return "\n\n⚙️ <b>إعدادات إضافية فعّالة:</b>\n" + "\n".join(f"• {escape(line)}" for line in lines)


async def _leave_input_state(state: FSMContext) -> None:
    """يخرج من انتظار قيمة نصية (إن كان فيها) ويعيد الحالة السابقة؛ لا يمس بقية البيانات."""
    if await state.get_state() != ExportState.waiting_for_value.state:
        return
    pending = (await state.get_data()).get("export_input") or {}
    await state.set_state(pending.get("prev"))
    await state.update_data(export_input=None)


async def _questions_for_settings(state: FSMContext, user_id: int, favorite_id: str) -> List[dict]:
    if favorite_id:
        favorite = await get_favorite_quiz(user_id, favorite_id)
        return (favorite or {}).get("quiz_data") or []
    return (await state.get_data()).get("questions") or []


async def _show_settings(call, state: FSMContext, style_code: str, favorite_id: str, notice: str = "") -> None:
    options = await _get_options(state)
    await _edit_export_screen(call, export_settings_text(options.summary_lines_ar(), notice),
                              get_export_settings_keyboard(options.to_dict(), style_code, favorite_id))


@router.callback_query(F.data.regexp(_XO_RE))
async def export_settings_action(call: types.CallbackQuery, state: FSMContext) -> None:
    action, arg, style_code, favorite_id = _XO_RE.match(call.data).groups()
    options = await _get_options(state)
    notice = ""
    try:
        if action == "open":
            await _leave_input_state(state)
        elif action == "done":
            await _leave_input_state(state)
            label = STYLE_LABELS_AR[STYLE_CODE_TO_NAME[style_code]]
            await _edit_export_screen(
                call, export_format_text(label) + await _active_settings_note(state),
                get_export_format_keyboard(style_code, favorite_id))
            await _safe_answer(call)
            return
        elif action == "pp":
            questions = await _questions_for_settings(state, call.from_user.id, favorite_id)
            if not questions:
                await _safe_answer(call, MSG_NO_QUESTIONS, show_alert=True)
                return
            style = STYLE_CODE_TO_NAME.get(style_code, "simple")
            max_allowed = await asyncio.to_thread(max_questions_per_page, questions, style)
            word_max = min(max_allowed, await asyncio.to_thread(max_questions_per_page_docx, questions, style))
            await _edit_export_screen(
                call,
                "🔢 <b>عدد الأسئلة في الصفحة الواحدة</b>\n\n"
                f"اختر من 1 إلى {max_allowed}. هذا الحد الأعلى مناسب لطول أسئلة كويزك وتنسيق «{escape(STYLE_LABELS_AR[style])}» في PDF"
                + (f"، وفي Word يتسع عادةً حتى {word_max} لأن صفحته أصغر.\n" if word_max < max_allowed else ".\n") +
                "إن كانت بعض الأسئلة أطول من المعتاد توضع أسئلة أقل في صفحتها تلقائياً بدل التزاحم أو القص.\n"
                "«تلقائي» يملأ كل صفحة بأكبر عدد يتسع (السلوك المعتاد).\n\n"
                f"يمكنك أيضاً إرسال الرقم كرسالة نصية.",
                get_export_per_page_keyboard(max_allowed, options.per_page, style_code, favorite_id))
            await state.update_data(export_max_per_page=max_allowed)
            await _start_input(state, "per_page", style_code, favorite_id)
            await _safe_answer(call)
            return
        elif action == "ppv":
            await _leave_input_state(state)
            if arg == "auto":
                options = replace(options, per_page=None)
            else:
                max_allowed = (await state.get_data()).get("export_max_per_page") or 10
                options = options.with_per_page(arg, max_allowed)
            await _save_options(state, options)
        elif action == "ed":
            field = FIELD_CODES.get(arg)
            if not field:
                await _safe_answer(call)
                return
            current = getattr(options, field)
            text = _FIELD_PROMPTS[field]
            if current:
                text += f"\n\nالقيمة الحالية: <b>{escape(current)}</b>"
            await _edit_export_screen(call, "✏️ " + text,
                                      get_export_input_keyboard(arg, bool(current), style_code, favorite_id))
            await _start_input(state, field, style_code, favorite_id)
            await _safe_answer(call)
            return
        elif action == "clr":
            field = FIELD_CODES.get(arg)
            if field:
                await _leave_input_state(state)
                options = replace(options, **{field: ""})
                await _save_options(state, options)
                notice = f"تم مسح «{TEXT_FIELDS[field][0]}»."
        elif action == "pl":
            await _edit_export_screen(
                call, "📍 <b>مكان ظهور اسم الأستاذ</b>\n\nاختر أين يُطبع الاسم في الملف:",
                get_export_placement_keyboard(options.teacher_placement, style_code, favorite_id))
            await _safe_answer(call)
            return
        elif action == "plv":
            if arg in PLACEMENTS:
                options = replace(options, teacher_placement=arg)
                await _save_options(state, options)
        elif action == "sf":
            options = replace(options, student_fields=not options.student_fields)
            await _save_options(state, options)
        elif action == "am":
            options = replace(options, answers_mode="key" if options.answers_mode == "full" else "full")
            await _save_options(state, options)
        elif action == "pn":
            cycle = {None: True, True: False, False: None}
            options = replace(options, page_numbers=cycle[options.page_numbers])
            await _save_options(state, options)
        elif action == "rs":
            await _leave_input_state(state)
            await state.update_data(export_options=None)
            notice = "أُعيدت كل الإعدادات إلى الوضع الافتراضي."
        else:
            await _safe_answer(call)
            return
    except ExportOptionsError as exc:
        await _safe_answer(call, str(exc), show_alert=True)
        return
    await _show_settings(call, state, style_code, favorite_id, notice)
    await _safe_answer(call)


async def _start_input(state: FSMContext, field: str, style_code: str, favorite_id: str) -> None:
    """ينتظر رسالة نصية لحقل واحد، ويتذكر الحالة السابقة لاستعادتها بعد الإدخال."""
    current = await state.get_state()
    pending = (await state.get_data()).get("export_input") or {}
    prev = pending.get("prev") if current == ExportState.waiting_for_value.state else current
    await state.update_data(export_input={"field": field, "style": style_code, "fav": favorite_id, "prev": prev})
    await state.set_state(ExportState.waiting_for_value)


@router.message(ExportState.waiting_for_value, F.text, ~F.text.startswith("/"))
async def export_setting_value(msg: types.Message, state: FSMContext) -> None:
    data = await state.get_data()
    pending = data.get("export_input") or {}
    field = pending.get("field")
    style_code = pending.get("style") or "s"
    favorite_id = pending.get("fav") or ""
    if not field:
        await _leave_input_state(state)
        return
    options = ExportOptions.from_state_data(data)
    try:
        if field == "per_page":
            options = options.with_per_page(msg.text, data.get("export_max_per_page") or 10)
        else:
            options = options.with_text(field, msg.text)
    except ExportOptionsError as exc:
        # يبقى بانتظار قيمة صحيحة؛ زر «إلغاء/رجوع» يخرج من الانتظار.
        await msg.answer(f"❌ {escape(str(exc))}", parse_mode="HTML")
        return
    await _save_options(state, options)
    await _leave_input_state(state)
    await msg.answer(export_settings_text(options.summary_lines_ar(), "✅ تم الحفظ."), parse_mode="HTML",
                     reply_markup=get_export_settings_keyboard(options.to_dict(), style_code, favorite_id))


# ==================== تصدير كويز محفوظ في المفضلة ====================

@router.callback_query(F.data.startswith("fav_export_menu_"))
async def export_favorite_menu(call: types.CallbackQuery) -> None:
    fav_id = call.data.replace("fav_export_menu_", "", 1)
    await call.message.answer(EXPORT_STYLE_TEXT, parse_mode="HTML", reply_markup=get_export_style_keyboard(fav_id))
    await _safe_answer(call)


@router.callback_query(F.data.regexp(_FAV_STYLE_RE))
async def export_favorite_style_chosen(call: types.CallbackQuery, state: FSMContext) -> None:
    style_code, fav_id = _FAV_STYLE_RE.match(call.data).groups()
    label = STYLE_LABELS_AR[STYLE_CODE_TO_NAME[style_code]]
    await _edit_export_screen(call, export_format_text(label) + await _active_settings_note(state),
                              get_export_format_keyboard(style_code, fav_id))
    await _safe_answer(call)


@router.callback_query(F.data.regexp(_FAV_FORMAT_RE))
async def export_favorite_quiz(call: types.CallbackQuery, state: FSMContext) -> None:
    fmt, variant, style_code, fav_id = _FAV_FORMAT_RE.match(call.data).groups()

    favorite: Optional[dict] = await get_favorite_quiz(call.from_user.id, fav_id)
    if not favorite:
        await _safe_answer(call, "❌ الكويز غير موجود أو تم حذفه مسبقاً.", show_alert=True)
        return

    questions = favorite.get("quiz_data") or []
    title = favorite.get("title") or "كويز محفوظ"
    await _leave_input_state(state)
    await _generate_and_send(call, questions, title, fmt, style_code, include_answers=variant != "s",
                             options=ExportOptions.from_state_data(await state.get_data()))
    await _safe_answer(call)


export_router = router

