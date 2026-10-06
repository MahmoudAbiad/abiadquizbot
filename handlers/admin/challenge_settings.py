"""لوحة أدمن صغيرة لإدارة إعدادات هجوم الأرض غير المتزامن.

تعرض حالياً:
1) تغيير مهلة إعادة مهاجمة نفس الأرض.
2) تغيير عدد هجمات الأرض اليومية لكل طالب.
3) تغيير مهلة تكرار إشعار "تتعرض أرضك للهجوم".
4) تصفير كل مهل إعادة الهجوم الحالية دون حذف سجل المباريات.
"""
from __future__ import annotations

from aiogram import Router, types, F
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup

from helpers.supabase_helper.challenge_settings import (
    MAX_COOLDOWN_SECONDS,
    MAX_DAILY_ATTACK_LIMIT,
    MAX_ATTACK_NOTIFICATION_COOLDOWN_SECONDS,
    get_land_attack_settings,
    reset_all_land_attack_cooldowns,
    set_land_attack_cooldown_seconds,
    set_land_attack_daily_limit,
    set_attack_notification_cooldown_seconds,
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
    waiting_daily_limit = State()
    waiting_notification_minutes = State()


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
    settings = await get_land_attack_settings()
    current = _format_duration(settings["cooldown_seconds"])
    daily_limit = settings["daily_attack_limit"]
    notification_cooldown = _format_duration(settings["attack_notification_cooldown_seconds"])
    text = (
        "🏰 <b>إعدادات هجوم الأرض</b>\n"
        "━━━━━━━━━━━━━━━━━━\n\n"
        f"⏱ مهلة إعادة مهاجمة <b>نفس أرض الطالب</b>: <code>{current}</code>\n"
        f"⚔️ عدد هجمات الأرض اليومية لكل طالب: <code>{daily_limit}</code>\n"
        f"🔔 مهلة تكرار إشعار <b>تتعرض أرضك للهجوم</b>: <code>{notification_cooldown}</code>\n\n"
        "المهلة الأولى تُطبّق على نفس المهاجم ونفس صاحب الأرض.\n"
        "الحد اليومي يحسب هجمات الأرض فقط، ولا يحسب مباريات التحدي المباشر.\n"
        "مهلة الإشعار تخص إشعار بدء الهجوم فقط، ولا تغيّر إشعار نتيجة الدفاع.\n"
        "تصفير المهل يجعل مهل إعادة الهجوم الحالية منتهية فوراً، دون حذف السجل أو النتائج."
    )
    if notice:
        text = f"{notice}\n\n{text}"
    markup = types.InlineKeyboardMarkup(inline_keyboard=[
        [_button("⏱ تغيير مهلة إعادة الهجوم", "admin_challenge_cooldown_edit")],
        [_button("⚔️ تغيير عدد الهجمات اليومية", "admin_challenge_daily_limit_edit")],
        [_button("🔔 تغيير مهلة إشعار الهجوم", "admin_challenge_notification_cooldown_edit")],
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
        logger.exception("challenge land settings admin panel failed: %s", exc)
        await call.answer("❌ تعذر جلب إعدادات هجوم الأرض.", show_alert=True)


@router.callback_query(F.data == "admin_challenge_cooldown_edit")
async def prompt_challenge_cooldown_edit(call: types.CallbackQuery, state: FSMContext) -> None:
    try:
        current = (await get_land_attack_settings())["cooldown_seconds"]
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
        notice=f"✅ تم تغيير مهلة إعادة الهجوم إلى <b>{_format_duration(seconds)}</b>."
    )


@router.callback_query(F.data == "admin_challenge_daily_limit_edit")
async def prompt_daily_attack_limit_edit(call: types.CallbackQuery, state: FSMContext) -> None:
    try:
        current = (await get_land_attack_settings())["daily_attack_limit"]
        await state.set_state(ChallengeCooldownAdminState.waiting_daily_limit)
        text = (
            "⚔️ <b>تغيير عدد هجمات الأرض اليومية</b>\n\n"
            f"القيمة الحالية: <code>{current}</code> هجوم لكل طالب يومياً.\n\n"
            f"أرسل عدداً صحيحاً من <code>0</code> إلى <code>{MAX_DAILY_ATTACK_LIMIT}</code>.\n"
            "القيمة <code>0</code> تمنع بدء هجمات أرض جديدة."
        )
        markup = types.InlineKeyboardMarkup(inline_keyboard=[
            [_button("❌ إلغاء", "admin_challenge_cooldown")]
        ])
        await safe_edit_text(call.message, text, reply_markup=markup)
        await call.answer()
    except Exception as exc:
        logger.exception("challenge daily attack limit edit prompt failed: %s", exc)
        await call.answer("❌ تعذر فتح تعديل الحد اليومي.", show_alert=True)


@router.message(ChallengeCooldownAdminState.waiting_daily_limit)
async def save_daily_attack_limit(msg: types.Message, state: FSMContext) -> None:
    raw = (msg.text or "").strip()
    if not raw.isdigit():
        await msg.answer("❌ أرسل عدداً صحيحاً فقط، مثل 20 أو 50.")
        return
    limit = int(raw)
    if limit < 0 or limit > MAX_DAILY_ATTACK_LIMIT:
        await msg.answer(f"❌ العدد يجب أن يكون بين 0 و{MAX_DAILY_ATTACK_LIMIT}.")
        return
    try:
        await set_land_attack_daily_limit(limit)
    except Exception as exc:
        logger.exception("saving challenge daily attack limit failed: %s", exc)
        await msg.answer("❌ تعذر حفظ الحد اليومي. حاول مرة أخرى.")
        return
    await render_challenge_cooldown_panel(
        msg,
        state,
        notice=f"✅ تم تغيير عدد هجمات الأرض اليومية إلى <b>{limit}</b>."
    )


@router.callback_query(F.data == "admin_challenge_notification_cooldown_edit")
async def prompt_attack_notification_cooldown_edit(call: types.CallbackQuery, state: FSMContext) -> None:
    try:
        current = (await get_land_attack_settings())["attack_notification_cooldown_seconds"]
        await state.set_state(ChallengeCooldownAdminState.waiting_notification_minutes)
        max_minutes = MAX_ATTACK_NOTIFICATION_COOLDOWN_SECONDS // 60
        text = (
            "🔔 <b>تغيير مهلة تكرار إشعار الهجوم</b>\n\n"
            "هذا الإعداد يخص رسالة <b>🚨 تتعرض أرضك للهجوم!</b> فقط.\n"
            f"القيمة الحالية: <code>{_format_duration(current)}</code>\n\n"
            "أرسل عدد الدقائق الجديدة. يمكن استخدام كسر عشري مثل <code>0.5</code> لـ30 ثانية.\n"
            f"القيم المسموحة: من <code>0</code> إلى <code>{max_minutes}</code> دقيقة.\n"
            "القيمة <code>0</code> تسمح بإشعار جديد مع كل هجوم مؤهل."
        )
        markup = types.InlineKeyboardMarkup(inline_keyboard=[
            [_button("❌ إلغاء", "admin_challenge_cooldown")]
        ])
        await safe_edit_text(call.message, text, reply_markup=markup)
        await call.answer()
    except Exception as exc:
        logger.exception("attack notification cooldown edit prompt failed: %s", exc)
        await call.answer("❌ تعذر فتح تعديل مهلة الإشعار.", show_alert=True)


@router.message(ChallengeCooldownAdminState.waiting_notification_minutes)
async def save_attack_notification_cooldown(msg: types.Message, state: FSMContext) -> None:
    raw = (msg.text or "").strip().replace(",", ".")
    try:
        minutes = float(raw)
    except ValueError:
        await msg.answer("❌ أرسل رقماً فقط، مثل 10 أو 60 أو 0.5.")
        return
    max_minutes = MAX_ATTACK_NOTIFICATION_COOLDOWN_SECONDS / 60
    if minutes < 0 or minutes > max_minutes:
        await msg.answer(f"❌ المهلة يجب أن تكون بين 0 و{int(max_minutes)} دقيقة.")
        return
    seconds = int(round(minutes * 60))
    try:
        await set_attack_notification_cooldown_seconds(seconds)
    except Exception as exc:
        logger.exception("saving attack notification cooldown failed: %s", exc)
        await msg.answer("❌ تعذر حفظ مهلة الإشعار. حاول مرة أخرى.")
        return
    await render_challenge_cooldown_panel(
        msg,
        state,
        notice=f"✅ تم تغيير مهلة إشعار الهجوم إلى <b>{_format_duration(seconds)}</b>."
    )


@router.callback_query(F.data == "admin_challenge_cooldown_reset")
async def confirm_challenge_cooldown_reset(call: types.CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    text = (
        "♻️ <b>تصفير كل مهل إعادة الهجوم؟</b>\n\n"
        "سيتمكن جميع الطلاب من مهاجمة الأراضي التي كانت ما تزال ضمن المهلة فوراً.\n"
        "هذا لا يغيّر مهلة الإشعارات ولا يصفّر عداد الهجمات اليومية.\n"
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
