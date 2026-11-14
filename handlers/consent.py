"""
معالجات أزرار «أوافق» / «لا أوافق» وشاشة اختيار اللغة التي تلي الموافقة مباشرة.
البوابة نفسها في services/consent.py (ConsentMiddleware).
"""

from aiogram import F, Router, types
from aiogram.exceptions import TelegramBadRequest

from logger import get_logger
from task_utils import spawn
from services import consent as svc

logger = get_logger(__name__)
router = Router()
router.callback_query.filter(F.message.chat.type == "private")


@router.callback_query(F.data == svc.CONSENT_ACCEPT)
async def consent_accept(call: types.CallbackQuery):
    await _decide(call, "accepted")


@router.callback_query(F.data == svc.CONSENT_DECLINE)
async def consent_decline(call: types.CallbackQuery):
    await _decide(call, "declined")


@router.callback_query(F.data.startswith("consent:lang:pick:"))
async def consent_language_pick(call: types.CallbackQuery):
    """تغيير اللغة المختارة (وضع ✅ عليها وتحديث زر المتابعة الأخضر) قبل المتابعة للبوت."""
    lang_code = call.data.rsplit(":", 1)[-1]
    text = svc.build_language_prompt_text(lang_code)
    markup = svc.build_language_keyboard(lang_code)
    await _edit_or_send(call, text, markup)
    await call.answer()


@router.callback_query(F.data.startswith("consent:lang:confirm:"))
async def consent_language_confirm(call: types.CallbackQuery):
    """حفظ اللغة المختارة بعد الموافقة على الشروط واستئناف طلب المستخدم الأصلي."""
    user_id = call.from_user.id
    lang_code = call.data.rsplit(":", 1)[-1]
    saved_lang = await svc.save_user_language(user_id, lang_code)
    pending = await svc.pop_pending_messages(user_id, bot=call.bot)
    pending_cb = await svc.pop_pending_callback(user_id)
    payload = await svc.pop_start_payload(user_id)
    markup = None
    if payload:
        username = (await call.bot.get_me()).username
        btn_label = (
            svc.get_continue_button_label(saved_lang)
            if hasattr(svc, "get_continue_button_label")
            else "▶️ متابعة"
        )
        markup = types.InlineKeyboardMarkup(inline_keyboard=[[
            types.InlineKeyboardButton(text=btn_label, url=f"https://t.me/{username}?start={payload}")
        ]])
    accepted_text = (
        svc.build_localized_accepted_message(
            saved_lang,
            resuming=bool(pending),
            callback_pending=bool(pending_cb),
        )
        if hasattr(svc, "build_localized_accepted_message")
        else svc.build_accepted_text(
            resuming=bool(pending),
            callback_pending=bool(pending_cb),
        )
    )
    await _edit_or_send(call, accepted_text, markup)
    toast = (
        svc.get_consent_confirmed_toast(saved_lang)
        if hasattr(svc, "get_consent_confirmed_toast")
        else "✅ تمت الموافقة"
    )
    await call.answer(toast)
    if pending:
        spawn(svc.replay_pending_messages(call.bot, pending))


async def _edit_or_send(call: types.CallbackQuery, text: str, markup) -> None:
    try:
        await call.message.edit_text(text, parse_mode="HTML", reply_markup=markup)
    except TelegramBadRequest as exc:
        if "message is not modified" in str(exc).lower():
            return
        logger.warning("Could not edit consent message: %s", exc)
        try:
            await call.message.answer(text, parse_mode="HTML", reply_markup=markup)
        except TelegramBadRequest:
            pass
    except (AttributeError, TypeError):
        try:
            await call.bot.send_message(call.from_user.id, text, parse_mode="HTML", reply_markup=markup)
        except TelegramBadRequest:
            pass


async def _decide(call: types.CallbackQuery, decision: str) -> None:
    user_id = call.from_user.id
    lock_key = f"consent:lock:{user_id}"
    redis = svc._redis()

    # قفل قصير لكل مستخدم ضد الضغطات المتزامنة (الـ RPC نفسه idempotent أيضاً).
    try:
        got_lock = await redis.set(lock_key, "1", nx=True, ex=5)
    except Exception:
        got_lock = True
    if not got_lock:
        await call.answer()
        return

    try:
        tv, pv = await svc.current_versions()
        from helpers.supabase_helper import supabase
        from helpers.supabase_helper.analytics import log_usage_event
        try:
            result = await supabase.rpc("record_consent", {
                "p_user_id": user_id,
                "p_decision": decision,
                "p_terms_version": tv,
                "p_privacy_version": pv,
            }).execute()
        except Exception as exc:
            logger.error("record_consent failed: %s", exc, exc_info=True)
            await call.answer("⚠️ تعذّر حفظ اختيارك، حاول مرة أخرى.", show_alert=True)
            return

        row = result.data[0] if isinstance(result.data, list) and result.data else result.data
        changed = bool((row or {}).get("changed"))
        if changed:
            await log_usage_event(
                user_id,
                f"consent_{decision}",
                {"terms_version": tv, "privacy_version": pv},
            )

        if decision == "accepted":
            await svc.remember_accepted(user_id, tv, pv)
            if getattr(svc, "LANGUAGE_STEP_ENABLED", False) and changed:
                detected_lang = await svc.detect_initial_language(call.from_user)
                await svc.save_user_language(user_id, detected_lang)
                await _edit_or_send(
                    call,
                    svc.build_language_prompt_text(detected_lang),
                    svc.build_language_keyboard(detected_lang),
                )
                await call.answer("✅ تمت الموافقة · اختر لغتك للمتابعة")
                return

            pending = await svc.pop_pending_messages(user_id, bot=call.bot)
            pending_cb = await svc.pop_pending_callback(user_id)
            if not changed:
                await call.answer("تم تسجيل موافقتك سابقاً ✅")
                if pending:
                    spawn(svc.replay_pending_messages(call.bot, pending))
                return
            payload = await svc.pop_start_payload(user_id)
            markup = None
            if payload:
                username = (await call.bot.get_me()).username
                markup = types.InlineKeyboardMarkup(inline_keyboard=[[
                    types.InlineKeyboardButton(text="▶️ متابعة", url=f"https://t.me/{username}?start={payload}")
                ]])
            await _edit_or_send(call, svc.build_accepted_text(resuming=bool(pending), callback_pending=bool(pending_cb)), markup)
            await call.answer("✅ تمت الموافقة")
            if pending:
                spawn(svc.replay_pending_messages(call.bot, pending))
            return

        # decision == "declined"
        await svc.forget_denied(user_id, tv, pv)
        await svc.clear_pending_messages(user_id)
        await svc.clear_pending_callback(user_id)
        if not changed:
            await call.answer("لن يعمل البوت إلا بعد الموافقة على الشروط.")
            return
        await _edit_or_send(call, svc.build_declined_text(), svc.build_consent_keyboard(include_decline=False))
        await call.answer()
    finally:
        try:
            await redis.delete(lock_key)
        except Exception:
            pass
