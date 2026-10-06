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
from helpers.settings_helper import (
    get_app_settings,
    update_app_setting,
    SETTING_LABELS,
    SETTING_MIN_VALUES,
    SETTING_MAX_VALUES,
    AI_SETTING_KEYS,
    SETTING_TO_FEATURE_FLAG,
    POINTS_SETTING_LABELS,
    format_setting_value,
)
from logger import get_logger
from .dashboard import AdminState, IsAdminFilter, safe_edit_text

logger = get_logger(__name__)
router = Router()

router.message.filter(IsAdminFilter())
router.callback_query.filter(IsAdminFilter())
# 🩹 FIX (تسريب خصوصية): محمي أصلاً بهوية الأدمن (IsAdminFilter) بس بلا فلتر مكان -
# لو الأدمن كتب الأمر بالغلط جوا أي غروب هو عضو فيه، اللوحة/العملية كانت رح تنفّذ
# وتظهر علناً هناك. راجع نفس الملاحظة بـ handlers/start.py وhandlers/favorites.py.
router.message.filter(F.chat.type == "private")
router.callback_query.filter(F.message.chat.type == "private")


@router.callback_query(F.data == "admin_settings_general")
async def open_settings_general_menu(call: types.CallbackQuery, state: FSMContext):
    """قسم الإعدادات العامة: نقطة الدخول الموحدة لكل إعدادات النظام والإدارة التقنية."""
    if state:
        await state.clear()
    text = (
        "⚙️ <b>الإعدادات العامة</b>\n"
        "━━━━━━━━━━━━━━━━━━\n\n"
        "🎯 <b>إعدادات النقاط:</b> نقاط الترحيب/التجديد/الإحالة\n"
        "🧩 <b>مفاتيح التحكم:</b> تشغيل/إيقاف ميزات محددة\n"
        "🤖 <b>الذكاء الاصطناعي:</b> الموديلات وسجل التوليد\n"
        "🏆 <b>إدارة التحديات:</b> المحتوى والفروع والاختبارات\n"
        "🏰 <b>إعدادات هجوم الأرض:</b> المهل، الإشعارات والحد اليومي"
    )
    await safe_edit_text(call.message, text, reply_markup=get_admin_settings_general_keyboard())
    try:
        await call.answer()
    except TelegramBadRequest:
        pass


async def _render_settings_menu(event, state: FSMContext = None):
    """يعرض لوحة إعدادات النقاط بالقيم الحالية (يجلبها مباشرة من قاعدة البيانات
    وليس من الكاش، حتى يرى الأدمن دائماً آخر قيمة فعلية بعد أي تعديل).
    🩹 عتبة تثبيت التصنيف لم تعد تُعرض هنا - صارت جزءاً من لوحة تفاصيل ميزة
    "رسالة التحقق المجتمعي من التصنيف" ضمن "🧩 مفاتيح التحكم" (راجع
    settings_helper.SETTING_TO_FEATURE_FLAG وhandlers/admin/feature_flags.py)
    لأنها إعداد خاص بتلك الميزة تحديداً وليست إعداد نقاط."""
    if state:
        await state.clear()

    settings = await get_app_settings(force_refresh=True)
    text = (
        "⚙️ <b>إعدادات النقاط</b>\n"
        "━━━━━━━━━━━━━━━━━━\n\n"
        "اضغط على أي إعداد لتعديل قيمته. القيمة الجديدة تُطبَّق فوراً على كل الطلاب.\n\n"
        f"🎁 نقاط الترحيب: <code>{format_setting_value(settings.get('welcome_points'))}</code>\n"
        f"🔄 التجديد اليومي: <code>{format_setting_value(settings.get('daily_renewal_points'))}</code>\n"
        f"🤝 مكافأة الإحالة: <code>{format_setting_value(settings.get('referral_bonus_points'))}</code>"
    )
    reply_markup = get_admin_settings_keyboard(settings, POINTS_SETTING_LABELS)

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
        f"القيمة الحالية: <code>{format_setting_value(current_value)}</code>\n\n"
        f"أرسل القيمة الجديدة (رقم صحيح أو عشري، {format_setting_value(min_value)} أو أكثر):",
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
        await msg.answer(f"❌ القيمة أقل من الحد الأدنى المسموح ({format_setting_value(min_value)}).", reply_markup=get_cancel_keyboard())
        return

    max_value = SETTING_MAX_VALUES.get(setting_key, 100000)
    if new_value > max_value:
        await msg.answer(f"❌ القيمة كبيرة جداً. الحد الأقصى هو {format_setting_value(max_value)}.", reply_markup=get_cancel_keyboard())
        return

    updated_value = await update_app_setting(setting_key, new_value)
    await state.clear()

    if updated_value is None:
        await msg.answer("❌ حدث خطأ أثناء تحديث الإعداد. حاول مجدداً.", reply_markup=get_admin_dashboard_keyboard())
        return

    label = SETTING_LABELS[setting_key]
    await msg.answer(
        f"✅ <b>تم التحديث بنجاح!</b>\n\n{label}: <code>{format_setting_value(updated_value)}</code>\n\n"
        "القيمة الجديدة سارية المفعول فوراً لكل الطلاب.",
        parse_mode="HTML"
    )

    # 🩹 لو الإعدد المُعدَّل خاص بميزة معيّنة (مثلاً عتبة تثبيت التصنيف)، نرجّع الأدمن
    # للوحة تفاصيل تلك الميزة ضمن "مفاتيح التحكم" بدل لوحة "إعدادات النقاط" - لأنه
    # فعلياً جاء من هناك (راجع settings_helper.SETTING_TO_FEATURE_FLAG).
    feature_key = SETTING_TO_FEATURE_FLAG.get(setting_key)
    if setting_key in AI_SETTING_KEYS:
        from .ai_control import _render_ai_menu
        await _render_ai_menu(msg)
    elif feature_key:
        from .feature_flags import render_feature_detail
        await render_feature_detail(msg, feature_key)
    else:
        await _render_settings_menu(msg)
