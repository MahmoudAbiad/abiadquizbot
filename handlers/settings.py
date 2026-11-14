"""Private student settings; never clears an in-progress quiz/FSM."""
from aiogram import F, Router, types
from aiogram.filters import Command
from aiogram.exceptions import TelegramBadRequest
from helpers.supabase_helper._client import supabase
from helpers.supabase_helper.users import check_or_add_user

router = Router()
router.message.filter(F.chat.type == "private")
router.callback_query.filter(F.message.chat.type == "private")


async def settings_panel(user_id):
    result = await supabase.table("users").select("renewal_notifications").eq("user_id", user_id).single().execute()
    enabled = result.data["renewal_notifications"]
    lang_code = "ar"
    lang_label = "العربية 🇸🇾"
    try:
        from services.i18n import (
            LANGUAGE_LABELS,
            build_settings_panel_content,
            get_user_language,
            install_ui_localization_hooks,
            normalize_language_code,
        )
        install_ui_localization_hooks()
        lang_code = normalize_language_code(await get_user_language(user_id))
        lang_label = LANGUAGE_LABELS.get(lang_code, "العربية 🇸🇾")
        if lang_code != "ar":
            return build_settings_panel_content(lang_code, enabled, lang_label)
    except Exception:
        pass
    text = ("⚙️ الإعدادات\n\n"
            "🔄 النقاط المجانية تتجدد يوميًا عند الساعة 8 مساءً بتوقيت دمشق.\n"
            f"إشعارات التجديد: {'🔔 مفعّلة' if enabled else '🔕 متوقفة'}\n"
            f"🌐 لغة البوت: {lang_label}\n\n"
            "إيقاف الإشعار لا يؤثر على تجدد نقاطك.")
    keyboard = types.InlineKeyboardMarkup(inline_keyboard=[
        [types.InlineKeyboardButton(
            text="🔕 إيقاف إشعارات التجديد" if enabled else "🔔 تفعيل إشعارات التجديد",
            callback_data="renewal:off" if enabled else "renewal:on")],
        [types.InlineKeyboardButton(text="🏠 الرئيسية", callback_data="student_home")],
        [types.InlineKeyboardButton(text=f"🌐 اللغة / Language ({lang_label})", callback_data="settings:lang")],
    ])
    return text, keyboard


async def ensure_user(user):
    await check_or_add_user(user.id, user.username or "Unknown", user.first_name, user.last_name or "Unknown")
    await supabase.table("users").update({"bot_delivery_state": "reachable"}).eq("user_id", user.id).execute()


@router.message(Command("settings"))
async def settings_command(message: types.Message):
    await ensure_user(message.from_user)
    text, kb = await settings_panel(message.from_user.id)
    await message.answer(text, reply_markup=kb)


@router.callback_query(F.data.in_({"student_settings", "renewal:on", "renewal:off"}))
async def settings_callback(call: types.CallbackQuery):
    await ensure_user(call.from_user)
    if call.data != "student_settings":
        await supabase.table("users").update({
            "renewal_notifications": call.data == "renewal:on",
        }).eq("user_id", call.from_user.id).execute()
    text, kb = await settings_panel(call.from_user.id)
    if call.data == "renewal:off":
        banner = "🔕 تم إيقاف إشعارات التجديد. يمكنك إعادة تفعيلها من ⚙️ الإعدادات."
        try:
            from services.i18n import get_renewal_disabled_banner, get_user_language
            lang = await get_user_language(call.from_user.id, fallback_user_or_code=call.from_user)
            banner = get_renewal_disabled_banner(lang)
        except Exception:
            pass
        text = f"{banner}\n\n{text}"
    try:
        await call.message.edit_text(text, reply_markup=kb)
    except TelegramBadRequest as exc:
        if "message is not modified" not in str(exc).lower():
            raise
    await call.answer()


@router.callback_query(F.data == "settings:lang")
async def settings_open_language(call: types.CallbackQuery):
    from services.i18n import build_language_screen_keyboard, build_language_screen_text, get_user_language
    lang = await get_user_language(call.from_user.id, fallback_user_or_code=call.from_user)
    text = build_language_screen_text(lang)
    kb = build_language_screen_keyboard(lang, prefix="settings:lang", is_settings=True)
    try:
        await call.message.edit_text(text, parse_mode="HTML", reply_markup=kb)
    except TelegramBadRequest as exc:
        if "message is not modified" not in str(exc).lower():
            raise
    await call.answer()


@router.callback_query(F.data.startswith("settings:lang:pick:"))
async def settings_pick_language(call: types.CallbackQuery):
    from services.i18n import build_language_screen_keyboard, build_language_screen_text
    lang = call.data.rsplit(":", 1)[-1]
    text = build_language_screen_text(lang)
    kb = build_language_screen_keyboard(lang, prefix="settings:lang", is_settings=True)
    try:
        await call.message.edit_text(text, parse_mode="HTML", reply_markup=kb)
    except TelegramBadRequest as exc:
        if "message is not modified" not in str(exc).lower():
            raise
    await call.answer()


@router.callback_query(F.data.startswith("settings:lang:confirm:"))
async def settings_confirm_language(call: types.CallbackQuery):
    from services.i18n import CONSENT_CONFIRMED_TOASTS, install_ui_localization_hooks, set_user_language
    install_ui_localization_hooks()
    lang = call.data.rsplit(":", 1)[-1]
    saved = await set_user_language(call.from_user.id, lang)
    text, kb = await settings_panel(call.from_user.id)
    try:
        await call.message.edit_text(text, reply_markup=kb)
    except TelegramBadRequest as exc:
        if "message is not modified" not in str(exc).lower():
            raise
    await call.answer(CONSENT_CONFIRMED_TOASTS.get(saved, "✅ تم حفظ اللغة"))


@router.my_chat_member(F.chat.type == "private")
async def private_membership(update: types.ChatMemberUpdated):
    status = update.new_chat_member.status
    status = getattr(status, "value", status)
    await supabase.table("users").update({
        "bot_delivery_state": "blocked" if status == "kicked" else "reachable",
    }).eq("user_id", update.chat.id).execute()


try:
    from services.i18n import install_ui_localization_hooks as _install_hooks
    _install_hooks()
except Exception:
    pass
