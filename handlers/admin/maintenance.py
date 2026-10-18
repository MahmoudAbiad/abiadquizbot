"""Manual maintenance switch and optional plain-text notice editor."""
from html import escape

from aiogram import Router, types, F
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup

from .dashboard import IsAdminFilter, safe_edit_text
from services.maintenance import (
    DEFAULT_MESSAGE, MAX_MESSAGE_LENGTH, get_maintenance, save_maintenance,
)
from logger import get_logger

logger = get_logger(__name__)
router = Router()
router.message.filter(IsAdminFilter(), F.chat.type == "private")
router.callback_query.filter(IsAdminFilter(), F.message.chat.type == "private")


class MaintenanceState(StatesGroup):
    editing_message = State()
    confirming_message = State()


def _keyboard(settings):
    button = types.InlineKeyboardButton
    return types.InlineKeyboardMarkup(inline_keyboard=[
        [button(text="🟢 إيقاف الصيانة" if settings.enabled else "🔴 تفعيل الصيانة",
            callback_data="admin_maintenance_off" if settings.enabled else "admin_maintenance_on")],
        [button(text="✏️ تخصيص رسالة الصيانة", callback_data="admin_maintenance_edit")],
        [button(text="↩️ استخدام الرسالة الافتراضية", callback_data="admin_maintenance_reset")],
        [button(text="👁 معاينة الرسالة", callback_data="admin_maintenance_preview")],
        [button(text="🔙 لوحة الإدارة", callback_data="admin_main_menu")],
    ])


async def render_panel(event, state):
    await state.clear()
    settings = await get_maintenance(fresh=True)
    status = "🔴 مفعّل — المستخدمون ممنوعون من استخدام البوت" if settings.enabled else "🟢 متوقف — البوت متاح للمستخدمين"
    text = (f"🛠 <b>وضع الصيانة</b>\n\n{status}\n\n"
        "<b>الرسالة التي تظهر للمستخدم:</b>\n"
        f"{escape(settings.message)}\n\n"
        "يمكنك كتابة موعد الرجوع داخل الرسالة إن رغبت؛ النص لا يحدد وقت تشغيل أو إيقاف تلقائي.\n"
        "يبقى وصول الأدمن متاحاً. الطلبات التي بدأت مسبقاً تكمل عملها.")
    if isinstance(event, types.CallbackQuery):
        await safe_edit_text(event.message, text, reply_markup=_keyboard(settings))
    else:
        await event.answer(text, reply_markup=_keyboard(settings), parse_mode="HTML")


@router.callback_query(F.data == "admin_maintenance")
async def open_panel(call: types.CallbackQuery, state: FSMContext):
    await render_panel(call, state)
    await call.answer()


@router.callback_query(F.data.in_({"admin_maintenance_on", "admin_maintenance_off", "admin_maintenance_reset"}))
async def change_setting(call: types.CallbackQuery, state: FSMContext):
    try:
        if call.data == "admin_maintenance_reset":
            await save_maintenance(message=DEFAULT_MESSAGE)
        else:
            await save_maintenance(enabled=call.data == "admin_maintenance_on")
    except Exception:
        logger.exception("Could not save maintenance setting")
        await call.answer("تعذّر حفظ التغيير. حاول مجدداً.", show_alert=True)
        return
    await render_panel(call, state)
    await call.answer("تم الحفظ ✅")


@router.callback_query(F.data == "admin_maintenance_preview")
async def preview(call: types.CallbackQuery):
    settings = await get_maintenance(fresh=True)
    await call.message.answer(settings.message, parse_mode=None)
    await call.answer()


@router.callback_query(F.data == "admin_maintenance_edit")
async def edit_message(call: types.CallbackQuery, state: FSMContext):
    await state.clear()
    await state.set_state(MaintenanceState.editing_message)
    await safe_edit_text(call.message,
        f"✏️ أرسل رسالة الصيانة الجديدة (حتى {MAX_MESSAGE_LENGTH} حرف).\n\n"
        "يمكنك إضافة التاريخ أو الوقت داخل النص، مثلاً:\n"
        "البوت قيد الصيانة. نعود اليوم الساعة 8 مساءً بتوقيت دمشق.\n\n"
        "ستظهر معاينة قبل الحفظ. الرسالة نصية بدون تنسيق HTML.",
        reply_markup=types.InlineKeyboardMarkup(inline_keyboard=[[
            types.InlineKeyboardButton(text="إلغاء", callback_data="admin_maintenance")]]))
    await call.answer()


@router.message(MaintenanceState.editing_message)
async def receive_message(msg: types.Message, state: FSMContext):
    from services.maintenance import validate_message
    try:
        text = validate_message(msg.text or "")
    except ValueError as exc:
        await msg.answer(str(exc))
        return
    await state.update_data(maintenance_draft=text)
    await state.set_state(MaintenanceState.confirming_message)
    await msg.answer("👁 معاينة رسالة الصيانة:")
    await msg.answer(text, parse_mode=None, reply_markup=types.InlineKeyboardMarkup(inline_keyboard=[
        [types.InlineKeyboardButton(text="💾 حفظ الرسالة", callback_data="admin_maintenance_save")],
        [types.InlineKeyboardButton(text="✏️ تعديل", callback_data="admin_maintenance_edit"),
         types.InlineKeyboardButton(text="إلغاء", callback_data="admin_maintenance")],
    ]))


@router.callback_query(MaintenanceState.confirming_message, F.data == "admin_maintenance_save")
async def confirm_message(call: types.CallbackQuery, state: FSMContext):
    draft = (await state.get_data()).get("maintenance_draft")
    if not draft:
        await call.answer("انتهت المعاينة. أعد إدخال الرسالة.", show_alert=True)
        return
    try:
        await save_maintenance(message=draft)
    except Exception:
        logger.exception("Could not save maintenance notice")
        await call.answer("تعذّر الحفظ. يمكنك المحاولة مجدداً.", show_alert=True)
        return
    await render_panel(call, state)
    await call.answer("تم حفظ الرسالة ✅")
