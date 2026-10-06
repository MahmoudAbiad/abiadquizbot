"""لوحة أدمن صغيرة لإدارة مهلة إعادة مهاجمة نفس أرض الطالب.

تعرض فقط التحكم المطلوب حالياً:
1) تغيير المهلة العامة.
2) تصفير كل المهل الحالية دفعة واحدة دون حذف سجل المباريات.
"""
from __future__ import annotations

from aiogram import Router, types, F
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup

from helpers.supabase_helper.challenge_settings import (
    MAX_COOLDOWN_SECONDS,
    get_land_attack_cooldown_settings,
    reset_all_land_attack_cooldowns,
    set_land_attack_cooldown_seconds,
)
from logger import get_logger
from .dashboard import IsAdminFilter, safe_edit_text

logger = get_logger(__name__)
router = Router()
router.message.filter(IsAdminFilter())
router.callback_query.filter(IsAdminFilter())
router.message.filter(F.chat.type == "private")
router.callback_query.filter(F.message.chat.type == "private")


class ChallengeCooldownAdminState(StatesGroup):
    waiting_hours = State()


def _button(text: str, data: str) -> types.InlineKeyboardButton:
    return types.InlineKeyboardButton(text=text, callback_data=data)


def _format_duration(seconds: int) -> str:
    seconds = max(0, int(seconds))
    if seconds == 0:
        return "بدون مهلة"
    if seconds % 3600 == 0:
        hours = seconds // 3600
        return f"{hours} ساعة"
    if seconds >= 3600:
        hours = seconds / 3600
        return f"{hours:.2f}".rstrip("0").rstrip(".") + " ساعة"
    if seconds % 60 == 0:
        return f"{seconds // 60} دقيقة"
    return f"{seconds} ثانية"


async def render_challenge_cooldown_panel(event, state: FSMContext | None = None, notice: str | None = None) -> None:
    if state:
        await state.clear()
    settings = await get_land_attack_cooldown_settings()
    current = _format_duration(settings["cooldown_seconds"])
    text = (
        "🏰 <b>مهلة هجوم الأرض</b>\n"
        "━━━━━━━━━━━━━━━━━━\n\n"
        f"المهلة الحالية لإعادة مهاجمة <b>نفس أرض الطالب</b>: <code>{current}</code>\n\n"
        "تُطبّق المهلة على نفس المهاجم ونفس صاحب الأرض.\n"
        "التصفير يجعل كل المهل الحالية منتهية فوراً، من دون حذف سجل الهجمات أو النتائج."
    )
    if notice:
        text = f"{notice}\n\n{text}"
    markup = types.InlineKeyboardMarkup(inline_keyboard=[
        [_button("⏱ تغيير المهلة", "admin_challenge_cooldown_edit")],
        [_button("♻️ تصفير كل المهل الحالية", "admin_challenge_cooldown_reset")],
        [_button("🔙 رجوع للوحة الإدارة", "admin_main_menu")],
    ])
    if isinstance(event, types.CallbackQuery):
        await safe_edit_text(event.message, text, reply_markup=markup)
        await event.answer()
    else:
        await event.answer(text, reply_markup=markup, parse_mode="HTML")


@router.callback_query(F.data == "admin_challenge_cooldown")
async def open_challenge_cooldown_panel(call: types.CallbackQuery, state: FSMContext) -> None:
    try:
        await render_challenge_cooldown_panel(call, state)
    except Exception as exc:
        logger.exception("challenge cooldown admin panel failed: %s", exc)
        await call.answer("❌ تعذر جلب إعداد المهلة.", show_alert=True)


@router.callback_query(F.data == "admin_challenge_cooldown_edit")
async def prompt_challenge_cooldown_edit(call: types.CallbackQuery, state: FSMContext) -> None:
    try:
        current = (await get_land_attack_cooldown_settings())["cooldown_seconds"]
        await state.set_state(ChallengeCooldownAdminState.waiting_hours)
        max_hours = MAX_COOLDOWN_SECONDS // 3600
        text = (
            "⏱ <b>تغيير مهلة إعادة الهجوم</b>\n\n"
            f"القيمة الحالية: <code>{_format_duration(current)}</code>\n\n"
            "أرسل عدد الساعات الجديدة. يمكن استخدام كسر عشري مثل <code>0.5</code> لنصف ساعة.\n"
            f"القيم المسموحة: من <code>0</code> إلى <code>{max_hours}</code> ساعة.\n"
            "القيمة <code>0</code> تعني عدم وجود مهلة."
        )
        markup = types.InlineKeyboardMarkup(inline_keyboard=[
            [_button("❌ إلغاء", "admin_challenge_cooldown")]
        ])
        await safe_edit_text(call.message, text, reply_markup=markup)
        await call.answer()
    except Exception as exc:
        logger.exception("challenge cooldown edit prompt failed: %s", exc)
        await call.answer("❌ تعذر فتح تعديل المهلة.", show_alert=True)


@router.message(ChallengeCooldownAdminState.waiting_hours)
async def save_challenge_cooldown(msg: types.Message, state: FSMContext) -> None:
    raw = (msg.text or "").strip().replace(",", ".")
    try:
        hours = float(raw)
    except ValueError:
        await msg.answer("❌ أرسل رقماً فقط، مثل 6 أو 0.5.")
        return
    max_hours = MAX_COOLDOWN_SECONDS / 3600
    if hours < 0 or hours > max_hours:
        await msg.answer(f"❌ المهلة يجب أن تكون بين 0 و{int(max_hours)} ساعة.")
        return
    seconds = int(round(hours * 3600))
    try:
        await set_land_attack_cooldown_seconds(seconds)
    except Exception as exc:
        logger.exception("saving challenge cooldown failed: %s", exc)
        await msg.answer("❌ تعذر حفظ المهلة. حاول مرة أخرى.")
        return
    await render_challenge_cooldown_panel(
        msg,
        state,
        notice=f"✅ تم تغيير المهلة إلى <b>{_format_duration(seconds)}</b>."
    )


@router.callback_query(F.data == "admin_challenge_cooldown_reset")
async def confirm_challenge_cooldown_reset(call: types.CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    text = (
        "♻️ <b>تصفير كل مهل إعادة الهجوم؟</b>\n\n"
        "سيتمكن جميع الطلاب من مهاجمة الأراضي التي كانت ما تزال ضمن المهلة فوراً.\n"
        "لن تُحذف أي مباراة، ولن تتغير XP أو الكؤوس أو السجلات."
    )
    markup = types.InlineKeyboardMarkup(inline_keyboard=[
        [_button("✅ نعم، صفّر كل المهل", "admin_challenge_cooldown_reset_yes")],
        [_button("❌ إلغاء", "admin_challenge_cooldown")],
    ])
    await safe_edit_text(call.message, text, reply_markup=markup)
    await call.answer()


@router.callback_query(F.data == "admin_challenge_cooldown_reset_yes")
async def do_challenge_cooldown_reset(call: types.CallbackQuery, state: FSMContext) -> None:
    try:
        await reset_all_land_attack_cooldowns()
    except Exception as exc:
        logger.exception("resetting challenge cooldowns failed: %s", exc)
        await call.answer("❌ تعذر تصفير المهل.", show_alert=True)
        return
    await render_challenge_cooldown_panel(
        call,
        state,
        notice="✅ تم تصفير جميع مهل إعادة الهجوم الحالية."
    )
