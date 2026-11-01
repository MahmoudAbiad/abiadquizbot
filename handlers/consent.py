"""
معالجات أزرار «أوافق» / «لا أوافق». البوابة نفسها في services/consent.py (ConsentMiddleware).
"""

from aiogram import F, Router, types
from aiogram.exceptions import TelegramBadRequest

from logger import get_logger
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

        if decision == "accepted":
            await svc.remember_accepted(user_id, tv, pv)
            if not changed:
                await call.answer("تم تسجيل موافقتك سابقاً ✅")
                return
            payload = await svc.pop_start_payload(user_id)
            markup = None
            if payload:
                username = (await call.bot.get_me()).username
                markup = types.InlineKeyboardMarkup(inline_keyboard=[[
                    types.InlineKeyboardButton(text="▶️ متابعة", url=f"https://t.me/{username}?start={payload}")
                ]])
            await _edit_or_send(call, svc.build_accepted_text(), markup)
            await call.answer("✅ تمت الموافقة")
            return

        # decision == "declined"
        await svc.forget_denied(user_id, tv, pv)
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
