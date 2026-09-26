# handlers/admin/feature_flags.py
"""🆕 لوحة "مفاتيح التحكم": تشغيل/إيقاف أي ميزة مسجَّلة بـ
constants.FEATURE_FLAGS_REGISTRY مباشرة من تيليجرام بدون أي تعديل كود أو إعادة نشر.
هذه مفاتيح on/off بسيطة وليست A/B testing حقيقي (ما في تقسيم مستخدمين لمجموعات).
لإضافة مفتاح جديد مستقبلاً: أضفه بقاموس FEATURE_FLAGS_REGISTRY بـ constants.py فقط -
يظهر تلقائياً هنا بلا أي تعديل إضافي بهذا الملف. قسم فرعي ضمن الإعدادات العامة
(راجع keyboards.get_admin_settings_general_keyboard).

🩹 كل زر بالقائمة الرئيسية (admin_flags_menu) بيفتح الآن "لوحة تفاصيل" خاصة بتلك
الميزة (admin_flag_detail_*) بدل ما كان يبدّل الحالة مباشرة من نفس القائمة - هيك
صار فيه مكان طبيعي لأي إعداد إضافي خاص بالميزة (مثل عتبة تثبيت التصنيف لميزة
classification_vote_prompt، التي كانت سابقاً - وبشكل غير منطقي - جزءاً من "إعدادات
النقاط" رغم أنها لا علاقة لها بالنقاط). لربط إعداد موجود بميزة معيّنة: أضفه لقاموس
FEATURE_EXTRA_SETTINGS أدناه + settings_helper.SETTING_TO_FEATURE_FLAG."""

from aiogram import Router, types, F
from aiogram.fsm.context import FSMContext

from constants import FEATURE_FLAGS_REGISTRY
from keyboards import get_feature_flags_keyboard, get_feature_detail_keyboard
from supabase_helper import get_all_feature_flags, set_feature_flag
from settings_helper import get_app_settings, SETTING_LABELS, format_setting_value
from logger import get_logger
from .dashboard import IsAdminFilter, safe_edit_text

logger = get_logger(__name__)
router = Router()

# 🆕 أي إعداد إضافي (من settings_helper.SETTING_LABELS) خاص بميزة معيّنة، يظهر ضمن
# لوحة تفاصيلها هنا بدل لوحة "إعدادات النقاط". راجع settings_helper.SETTING_TO_FEATURE_FLAG
# (الاتجاه المعاكس - تُستخدم بـ handlers/admin/settings.py لمعرفة إلى أي لوحة يعود
# الأدمن بعد تعديل الإعداد).
FEATURE_EXTRA_SETTINGS = {
    "classification_vote_prompt": ["classification_vote_threshold"],
}

router.message.filter(IsAdminFilter())
router.callback_query.filter(IsAdminFilter())
# 🩹 FIX (تسريب خصوصية): محمي أصلاً بهوية الأدمن (IsAdminFilter) بس بلا فلتر مكان -
# لو الأدمن كتب الأمر بالغلط جوا أي غروب هو عضو فيه، اللوحة/العملية كانت رح تنفّذ
# وتظهر علناً هناك. راجع نفس الملاحظة بـ handlers/start.py وhandlers/favorites.py.
router.message.filter(F.chat.type == "private")
router.callback_query.filter(F.message.chat.type == "private")


async def _render_flags_menu(call: types.CallbackQuery) -> None:
    flags = await get_all_feature_flags(FEATURE_FLAGS_REGISTRY)
    text = (
        "🧩 <b>مفاتيح التحكم</b>\n"
        "━━━━━━━━━━━━━━━━━━\n\n"
        "اضغط على أي ميزة لعرض لوحة تحكمها الكاملة (تشغيل/إيقاف + إعداداتها الخاصة):\n"
        "✅ = مفعّلة حالياً | 🚫 = موقوفة حالياً"
    )
    await safe_edit_text(call.message, text, reply_markup=get_feature_flags_keyboard(flags, FEATURE_FLAGS_REGISTRY))


@router.callback_query(F.data == "admin_flags_menu")
async def open_flags_menu(call: types.CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    try:
        await _render_flags_menu(call)
    except Exception as e:
        logger.error(f"Error opening feature flags menu: {e}")
        await call.answer("❌ تعذر جلب مفاتيح التحكم.", show_alert=True)
    finally:
        await call.answer()


async def render_feature_detail(event, flag_key: str) -> None:
    """يعرض لوحة تفاصيل ميزة واحدة: حالتها الحالية + أي إعدادات خاصة بها
    (FEATURE_EXTRA_SETTINGS). event ممكن يكون CallbackQuery (تعديل الرسالة الحالية)
    أو Message (رسالة جديدة - مثلاً بعد تعديل إعداد من handlers/admin/settings.py)."""
    label = FEATURE_FLAGS_REGISTRY.get(flag_key, flag_key)
    flags = await get_all_feature_flags(FEATURE_FLAGS_REGISTRY)
    enabled = flags.get(flag_key, True)

    extra_settings = []
    setting_keys = FEATURE_EXTRA_SETTINGS.get(flag_key, [])
    if setting_keys:
        settings = await get_app_settings(force_refresh=True)
        for setting_key in setting_keys:
            value = settings.get(setting_key, 0)
            extra_settings.append((
                setting_key,
                SETTING_LABELS.get(setting_key, setting_key),
                format_setting_value(value),
            ))

    status_line = "✅ مفعّلة حالياً" if enabled else "🚫 موقوفة حالياً"
    text = (
        f"🧩 <b>{label}</b>\n"
        "━━━━━━━━━━━━━━━━━━\n\n"
        f"الحالة: {status_line}\n\n"
        "اضغط لتشغيل/إيقاف الميزة، أو لتعديل أي إعداد خاص بها أدناه."
    )
    reply_markup = get_feature_detail_keyboard(flag_key, enabled, extra_settings)

    if isinstance(event, types.CallbackQuery):
        await safe_edit_text(event.message, text, reply_markup=reply_markup)
    else:
        await event.answer(text, reply_markup=reply_markup, parse_mode="HTML")


@router.callback_query(F.data.startswith("admin_flag_detail_"))
async def open_flag_detail(call: types.CallbackQuery, state: FSMContext) -> None:
    key = call.data.removeprefix("admin_flag_detail_")
    if key not in FEATURE_FLAGS_REGISTRY:
        await call.answer("❌ مفتاح غير معروف.", show_alert=True)
        return
    await state.clear()
    try:
        await render_feature_detail(call, key)
    except Exception as e:
        logger.error(f"Error opening feature detail for '{key}': {e}")
        await call.answer("❌ تعذر عرض تفاصيل الميزة.", show_alert=True)
    finally:
        await call.answer()


@router.callback_query(F.data.startswith("admin_flag_toggle_"))
async def toggle_flag(call: types.CallbackQuery) -> None:
    key = call.data.replace("admin_flag_toggle_", "", 1)
    if key not in FEATURE_FLAGS_REGISTRY:
        await call.answer("❌ مفتاح غير معروف.", show_alert=True)
        return
    try:
        flags = await get_all_feature_flags(FEATURE_FLAGS_REGISTRY)
        new_value = not flags.get(key, True)
        saved = await set_feature_flag(key, new_value)
        if not saved:
            await call.answer("❌ تعذر حفظ التغيير، حاول مجدداً.", show_alert=True)
            return
        # 🩹 التبديل بيصير الآن من داخل لوحة تفاصيل الميزة نفسها (بعد فتحها عبر
        # admin_flag_detail_)، فبنرجّع نفس اللوحة بحالتها المحدَّثة بدل الرجوع
        # للقائمة الرئيسية كما كان سابقاً.
        await render_feature_detail(call, key)
        await call.answer("✅ تم التفعيل" if new_value else "🚫 تم الإيقاف")
    except Exception as e:
        logger.error(f"Error toggling feature flag '{key}': {e}")
        await call.answer("❌ حدث خطأ أثناء تبديل المفتاح.", show_alert=True)
