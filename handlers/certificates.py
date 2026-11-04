"""Private flow for claiming and customizing group quiz certificates."""

from aiogram import F, Router, types
from aiogram.fsm.context import FSMContext

from config import QuizState, bot
from logger import get_logger, log_error
from services.group_certificate_delivery import get_certificate_claim, send_certificate


logger = get_logger(__name__)
router = Router()
router.message.filter(F.chat.type == "private")
router.callback_query.filter(F.message.chat.type == "private")


def _telegram_name(user: types.User) -> str:
    full = " ".join(part for part in (user.first_name, user.last_name) if part).strip()
    return full or (f"@{user.username}" if user.username else str(user.id))


def _certificate_keyboard(token: str) -> types.InlineKeyboardMarkup:
    return types.InlineKeyboardMarkup(inline_keyboard=[
        [types.InlineKeyboardButton(text="📜 إصدار بالاسم الظاهر على تلغرام", callback_data=f"gqcert:issue:{token}")],
        [types.InlineKeyboardButton(text="✏️ تغيير الاسم على الشهادة", callback_data=f"gqcert:name:{token}")],
        [types.InlineKeyboardButton(text="❌ إلغاء", callback_data=f"gqcert:cancel:{token}")],
    ])


async def resume_group_certificate(msg: types.Message, payload: str) -> None:
    token = payload.removeprefix("gqcert_")
    claim = await get_certificate_claim(token, msg.from_user.id)
    if not claim:
        await msg.answer("⌛ رابط الشهادة منتهي أو غير مخصص لهذا الحساب.")
        return
    default_name = _telegram_name(msg.from_user)
    await msg.answer(
        "🎓 <b>شهادة تقدير رمزية</b>\n\n"
        f"المركز: <b>{claim['rank']}</b> · النتيجة: <b>{claim['score']}/{claim['total_questions']}</b>\n"
        f"الاسم الحالي: <b>{default_name}</b>\n\n"
        "اختر الاسم الذي تريد طباعته على الشهادة:",
        parse_mode="HTML",
        reply_markup=_certificate_keyboard(token),
    )


@router.callback_query(F.data.startswith("gqcert:"))
async def certificate_action(call: types.CallbackQuery, state: FSMContext) -> None:
    try:
        _, action, token = call.data.split(":", 2)
        claim = await get_certificate_claim(token, call.from_user.id)
        if not claim:
            await call.answer("⌛ الرابط منتهي أو غير مخصص لك.", show_alert=True)
            return
        if action == "cancel":
            await state.clear()
            await call.message.edit_text("تم إلغاء إصدار الشهادة.")
            return
        if action == "name":
            await state.update_data(certificate_token=token)
            await state.set_state(QuizState.waiting_for_certificate_name)
            await call.message.edit_text(
                "✏️ أرسل الآن الاسم الذي تريد طباعته على الشهادة.\n"
                "يمكنك كتابة الاسم بالعربية أو الإنكليزية (حتى 120 حرفاً)."
            )
            return
        if action != "issue":
            await call.answer("❌ طلب غير صالح.", show_alert=True)
            return
        await send_certificate(bot, call.from_user.id, claim, _telegram_name(call.from_user))
        await call.message.edit_text("✅ تم إصدار الشهادة وإرسال الصورة ونسخة PDF إلى هذه المحادثة.")
        await state.clear()
    except Exception as e:
        log_error(logger, f"Error in certificate action: {e}", exception=e)
        await call.answer("❌ تعذر إصدار الشهادة حالياً.", show_alert=True)
    finally:
        try:
            await call.answer()
        except Exception:
            pass


@router.message(QuizState.waiting_for_certificate_name, F.text)
async def custom_certificate_name(msg: types.Message, state: FSMContext) -> None:
    name = msg.text.strip()[:120]
    if not name:
        await msg.answer("❌ الاسم فارغ. أرسل اسماً صالحاً أو اضغط إلغاء من الرسالة السابقة.")
        return
    data = await state.get_data()
    claim = await get_certificate_claim(data.get("certificate_token", ""), msg.from_user.id)
    if not claim:
        await state.clear()
        await msg.answer("⌛ انتهت صلاحية رابط الشهادة. اطلبها من رسالة النتائج مجدداً.")
        return
    try:
        await send_certificate(bot, msg.from_user.id, claim, name)
        await msg.answer("✅ تم إصدار الشهادة بالاسم الذي اخترته وإرسال الصورة ونسخة PDF.")
    except Exception as e:
        log_error(logger, f"Error sending custom certificate: {e}", exception=e)
        await msg.answer("❌ تعذر إصدار الشهادة حالياً، جرّب بعد قليل.")
    finally:
        await state.clear()

