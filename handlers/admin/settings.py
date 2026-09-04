# handlers/admin/settings.py
"""لوحة إدارة إعدادات النقاط: تعديل نقاط الترحيب/التجديد اليومي/مكافأة الإحالة مباشرة
من تيليجرام دون الحاجة لتعديل الكود أو إعادة نشر البوت (تُخزَّن في جدول app_settings)."""

from aiogram import Router, types, F
from aiogram.fsm.context import FSMContext
from aiogram.exceptions import TelegramBadRequest

from keyboards import (
    get_admin_settings_keyboard,
    get_admin_settings_general_keyboard,
    get_cancel_keyboard,
    get_admin_dashboard_keyboard,
)
from settings_helper import get_app_settings, update_app_setting, SETTING_LABELS, SETTING_MIN_VALUES
from logger import get_logger
from .dashboard import AdminState, IsAdminFilter, safe_edit_text

logger = get_logger(__name__)
router = Router()

router.message.filter(IsAdminFilter())
router.callback_query.filter(IsAdminFilter())


@router.callback_query(F.data == "admin_settings_general")
async def open_settings_general_menu(call: types.CallbackQuery, state: FSMContext):
    """قسم الإعدادات العامة: نقاط النظام + مفاتيح التحكم (منفصل عن قسم الذكاء
    الاصطناعي)."""
    if state:
        await state.clear()
    text = (
        "⚙️ <b>الإعدادات العامة</b>\n"
        "━━━━━━━━━━━━━━━━━━\n\n"
        "🎯 <b>إعدادات النقاط:</b> نقاط الترحيب/التجديد/الإحالة\n"
        "🧩 <b>مفاتيح التحكم:</b> تشغيل/إيقاف ميزات محددة"
    )
    await safe_edit_text(call.message, text, reply_markup=get_admin_settings_general_keyboard())
    try:
        await call.answer()
    except TelegramBadRequest:
        pass


async def _render_settings_menu(event, state: FSMContext = None):
    """يعرض لوحة إعدادات النقاط بالقيم الحالية (يجلبها مباشرة من قاعدة البيانات
    وليس من الكاش، حتى يرى الأدمن دائماً آخر قيمة فعلية بعد أي تعديل)."""
    if state:
        await state.clear()

    settings = await get_app_settings(force_refresh=True)
    text = (
        "⚙️ <b>إعدادات النقاط</b>\n"
        "━━━━━━━━━━━━━━━━━━\n\n"
        "اضغط على أي إعداد لتعديل قيمته. القيمة الجديدة تُطبَّق فوراً على كل الطلاب.\n\n"
        f"🎁 نقاط الترحيب: <code>{settings.get('welcome_points', 0):.0f}</code>\n"
        f"🔄 التجديد اليومي: <code>{settings.get('daily_renewal_points', 0):.0f}</code>\n"
        f"🤝 مكافأة الإحالة: <code>{settings.get('referral_bonus_points', 0):.0f}</code>\n"
        f"🗳 عتبة تثبيت التصنيف: <code>{settings.get('classification_vote_threshold', 0):.0f}</code>"
    )
    reply_markup = get_admin_settings_keyboard(settings, SETTING_LABELS)

    if isinstance(event, types.Message):
        await event.answer(text, reply_markup=reply_markup, parse_mode="HTML")
    elif isinstance(event, types.CallbackQuery):
        await safe_edit_text(event.message, text, reply_markup=reply_markup)
        await event.answer()


@router.callback_query(F.data == "admin_settings_menu")
async def open_settings_menu(call: types.CallbackQuery, state: FSMContext):
    """فتح لوحة إعدادات النقاط من اللوحة الرئيسية."""
    await _render_settings_menu(call, state)


@router.callback_query(F.data.startswith("admin_setting_edit_"))
async def prompt_setting_edit(call: types.CallbackQuery, state: FSMContext):
    """طلب قيمة جديدة لإعداد محدد."""
    setting_key = call.data.removeprefix("admin_setting_edit_")
    if setting_key not in SETTING_LABELS:
        await call.answer("❌ إعداد غير معروف.", show_alert=True)
        return

    await state.update_data(setting_key=setting_key)
    await state.set_state(AdminState.waiting_for_setting_edit)

    label = SETTING_LABELS[setting_key]
    settings = await get_app_settings()
    current_value = settings.get(setting_key, 0)
    min_value = SETTING_MIN_VALUES.get(setting_key, 0)

    await safe_edit_text(
        call.message,
        f"✍️ <b>تعديل: {label}</b>\n\n"
        f"القيمة الحالية: <code>{current_value:.0f}</code>\n\n"
        f"أرسل القيمة الجديدة (رقم صحيح أو عشري، {min_value:.0f} أو أكثر):",
        reply_markup=get_cancel_keyboard()
    )
    try:
        await call.answer()
    except TelegramBadRequest:
        pass


@router.message(AdminState.waiting_for_setting_edit)
async def process_setting_edit(msg: types.Message, state: FSMContext):
    """تطبيق القيمة الجديدة على الإعداد المطلوب."""
    data = await state.get_data()
    setting_key = data.get("setting_key")

    if not setting_key or setting_key not in SETTING_LABELS:
        await msg.answer("❌ انتهت جلسة التعديل، يرجى إعادة فتح إعدادات النقاط.", reply_markup=get_admin_dashboard_keyboard())
        await state.clear()
        return

    raw_text = (msg.text or "").strip().replace(",", ".")
    try:
        new_value = float(raw_text)
    except ValueError:
        await msg.answer("❌ يرجى إرسال رقم صحيح (مثال: 50 أو 25.5).", reply_markup=get_cancel_keyboard())
        return

    min_value = SETTING_MIN_VALUES.get(setting_key, 0)
    if new_value < min_value:
        await msg.answer(f"❌ القيمة أقل من الحد الأدنى المسموح ({min_value:.0f}).", reply_markup=get_cancel_keyboard())
        return

    if new_value > 100000:
        await msg.answer("❌ القيمة كبيرة جداً. الحد الأقصى هو 100,000.", reply_markup=get_cancel_keyboard())
        return

    updated_value = await update_app_setting(setting_key, new_value)
    await state.clear()

    if updated_value is None:
        await msg.answer("❌ حدث خطأ أثناء تحديث الإعداد. حاول مجدداً.", reply_markup=get_admin_dashboard_keyboard())
        return

    label = SETTING_LABELS[setting_key]
    await msg.answer(
        f"✅ <b>تم التحديث بنجاح!</b>\n\n{label}: <code>{updated_value:.0f}</code>\n\n"
        "القيمة الجديدة سارية المفعول فوراً لكل الطلاب.",
        parse_mode="HTML"
    )
    await _render_settings_menu(msg)
