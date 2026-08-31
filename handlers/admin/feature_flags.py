# handlers/admin/feature_flags.py
"""🆕 لوحة "A/B Tests ومفاتيح التحكم": تشغيل/إيقاف أي ميزة مسجَّلة بـ
constants.FEATURE_FLAGS_REGISTRY مباشرة من تيليجرام بدون أي تعديل كود أو إعادة نشر.
لإضافة مفتاح جديد مستقبلاً: أضفه بقاموس FEATURE_FLAGS_REGISTRY بـ constants.py فقط -
يظهر تلقائياً هنا بلا أي تعديل إضافي بهذا الملف."""

from aiogram import Router, types, F
from aiogram.fsm.context import FSMContext

from constants import FEATURE_FLAGS_REGISTRY
from keyboards import get_feature_flags_keyboard
from supabase_helper import get_all_feature_flags, set_feature_flag
from logger import get_logger
from .dashboard import IsAdminFilter, safe_edit_text

logger = get_logger(__name__)
router = Router()

router.message.filter(IsAdminFilter())
router.callback_query.filter(IsAdminFilter())


async def _render_flags_menu(call: types.CallbackQuery) -> None:
    flags = await get_all_feature_flags(FEATURE_FLAGS_REGISTRY)
    text = (
        "🧪 <b>A/B Tests ومفاتيح التحكم</b>\n"
        "━━━━━━━━━━━━━━━━━━\n\n"
        "اضغط على أي مفتاح لتشغيله أو إيقافه فوراً لكل الطلاب:\n"
        "✅ = مفعّل حالياً | 🚫 = موقوف حالياً"
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
        await _render_flags_menu(call)
        await call.answer("✅ تم التفعيل" if new_value else "🚫 تم الإيقاف")
    except Exception as e:
        logger.error(f"Error toggling feature flag '{key}': {e}")
        await call.answer("❌ حدث خطأ أثناء تبديل المفتاح.", show_alert=True)
