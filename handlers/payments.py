"""
handlers/payments.py
==============================================================================
واجهة الشحن الذاتي عبر شام كاش:
  • الطالب: شاشة الباقات -> اختيار وسيلة الدفع -> بطاقة الطلب (QR + كود التحويل + رمز الملاحظة)
            -> «✅ دفعت» / «❌ إلغاء».
  • الأدمن (بمجموعة الدفع): أزرار pay:ok / pay:no / pay:ig. الشحن الفعلي دائماً عبر
    services.payment_service.credit_order (دالة ذرية)، والأزرار محصورة بمعرّفات الأدمن.

⚠️ الميزة كلها خلف فلاغ payments_enabled (مطفي افتراضياً) + تحتاج PAYMENT_ADMIN_GROUP_ID
وPAYMENT_WEBHOOK_SECRET. إن لم تكتمل الإعدادات أو لا توجد باقة نشطة تبقى شاشة الشحن القديمة (عبر الدعم).
==============================================================================
"""

import html
import os
from typing import Optional

from aiogram import Router, types, F
from aiogram.exceptions import TelegramBadRequest

from constants import (
    ADMIN_CONTACT, PAYMENT_ADMIN_GROUP_ID, PAYMENT_ORDER_TTL_MINUTES, PAYMENT_WEBHOOK_SECRET,
    SHAMCASH_QR_PATH, SHAMCASH_TRANSFER_CODE,
)
from handlers.admin.users import send_points_notification
from helpers.supabase_helper import is_feature_enabled, log_usage_event, set_feature_flag
from logger import get_logger
from services import payment_service as ps
from task_utils import spawn

logger = get_logger(__name__)
router = Router()


# ==================== جانب الطالب ====================

async def payments_available() -> bool:
    if not (PAYMENT_ADMIN_GROUP_ID and PAYMENT_WEBHOOK_SECRET):
        return False
    return await is_feature_enabled("payments_enabled", default=False)


def _support_row() -> list:
    return [types.InlineKeyboardButton(text="💬 تواصل مع الدعم", url=f"https://t.me/{ADMIN_CONTACT.lstrip('@')}")]


async def try_show_payment_packages(call: types.CallbackQuery) -> bool:
    """يُستدعى من شاشة الشحن القديمة (recharge_info). يرجع True لو تولّى العرض."""
    try:
        if not await payments_available():
            return False
        packages = await ps.get_active_packages()
        if not packages:
            return False

        open_order = await ps.get_open_order_for_user(call.from_user.id)
        if open_order:
            await _show_open_order(call, open_order)
            return True

        rows = [[types.InlineKeyboardButton(
            text=f"⭐ {p['points']} نقطة — {ps.format_money(p['price'], p['currency'])}" + (f" ({p['label']})" if p.get("label") else ""),
            callback_data=f"pay:pkg:{p['id']}",
        )] for p in packages]
        rows.append(_support_row())
        await call.message.answer(
            "🔋 <b>شحن النقاط</b>\n\nاختر الباقة المناسبة، وبعدها طريقة الدفع. "
            "يُضاف الرصيد لحسابك فور تأكيد الدفعة. 🚀",
            parse_mode="HTML",
            reply_markup=types.InlineKeyboardMarkup(inline_keyboard=rows),
        )
        spawn(log_usage_event(call.from_user.id, "recharge_info_viewed"))
        return True
    except Exception as exc:
        logger.error(f"try_show_payment_packages failed, falling back to support screen: {exc}", exc_info=True)
        return False


def _order_text(order: dict, auto: bool = False) -> str:
    lines = [
        "💳 <b>الدفع عبر شام كاش</b>",
        "",
        f"📦 <b>{order['points']}</b> نقطة",
        f"💵 المبلغ المطلوب: <b>{ps.format_money(order['amount'], order['currency'])}</b> (بالضبط)",
        "",
        "1️⃣ حوّل المبلغ إلى حسابنا بمسح الـ QR أعلاه، أو بكود التحويل:",
    ]
    if SHAMCASH_TRANSFER_CODE:
        lines.append(f"<code>{html.escape(SHAMCASH_TRANSFER_CODE)}</code>")
    lines += [
        "",
        "2️⃣ <b>مهم جداً:</b> الصق هذا الرمز في خانة <b>الملاحظة</b> عند التحويل (اضغط عليه لنسخه):",
        f"<code>{order['code']}</code>",
        "⚠️ <b>الملاحظة ضرورية جداً لتمييز حوالتك.</b> عدم وضع الرمز كما هو قد يؤدي إلى تعذر مطابقة الحوالة، والبوت غير مسؤول عن ضياع أي حوالة بسبب عدم التقيد بالتعليمات.",
        "",
        "3️⃣ بعد التحويل اضغط «✅ دفعت».",
        "",
        f"⏳ الطلب صالح {PAYMENT_ORDER_TTL_MINUTES} دقيقة. بدون الرمز في الملاحظة يتأخر شحنك لحين المراجعة اليدوية.",
    ]
    if auto:
        lines.append("⚡ عند تحويل المبلغ بالضبط مع الرمز بالملاحظة يُضاف رصيدك عادةً تلقائياً خلال دقائق. "
                     "وإن لم يتحقق ذلك تُراجَع الدفعة يدوياً وقد يستغرق الأمر من ساعة إلى 48 ساعة.")
    else:
        lines.append("⏱️ بعد تأكيد الدفع، تستغرق معالجة وإتمام عملية الشحن عادةً من ساعة إلى 48 ساعة.")
    return "\n".join(lines)


def _order_keyboard(order: dict) -> types.InlineKeyboardMarkup:
    return types.InlineKeyboardMarkup(inline_keyboard=[
        [types.InlineKeyboardButton(text="✅ دفعت", callback_data=f"pay:paid:{order['id']}")],
        [types.InlineKeyboardButton(text="❌ إلغاء الطلب", callback_data=f"pay:cancel:{order['id']}")],
    ])


async def _send_order_screen(chat_id: int, order: dict, bot_obj) -> None:
    text = _order_text(order, auto=await ps.auto_confirm_active())
    markup = _order_keyboard(order)
    if SHAMCASH_QR_PATH and os.path.exists(SHAMCASH_QR_PATH):
        try:
            await bot_obj.send_photo(chat_id, types.FSInputFile(SHAMCASH_QR_PATH), caption=text,
                                     parse_mode="HTML", reply_markup=markup)
            return
        except Exception as exc:
            logger.warning(f"could not send Sham Cash QR photo, sending text only: {exc}")
    await bot_obj.send_message(chat_id, text, parse_mode="HTML", reply_markup=markup)


async def _show_open_order(call: types.CallbackQuery, order: dict) -> None:
    if order["status"] == "pending":
        await _send_order_screen(call.message.chat.id, order, call.bot)
    else:
        await call.message.answer(
            f"⏳ طلبك <code>{order['code']}</code> ({order['points']} نقطة) قيد التحقق من الدفعة.\n"
            "سيصلك إشعار فور تأكيده. إن تأخر الأمر تواصل مع الدعم.",
            parse_mode="HTML",
            reply_markup=types.InlineKeyboardMarkup(inline_keyboard=[_support_row()]),
        )


@router.callback_query(F.data.startswith("pay:pkg:"))
async def choose_method(call: types.CallbackQuery):
    try:
        package_id = int(call.data.split(":")[2])
        if not await payments_available():
            await call.answer("الشحن الذاتي غير متاح حالياً، تواصل مع الدعم.", show_alert=True)
            return
        package = await ps.get_package(package_id)
        if not package:
            await call.answer("هذه الباقة لم تعد متاحة.", show_alert=True)
            return
        markup = types.InlineKeyboardMarkup(inline_keyboard=[
            [types.InlineKeyboardButton(text="💳 شام كاش", callback_data=f"pay:m:{package_id}:sc")],
            [types.InlineKeyboardButton(text="⬅️ رجوع", callback_data="recharge_info")],
        ])
        await call.message.answer(
            f"📦 <b>{package['points']} نقطة</b> — {ps.format_money(package['price'], package['currency'])}\n\nاختر وسيلة الدفع:",
            parse_mode="HTML", reply_markup=markup,
        )
    except Exception as exc:
        logger.error(f"choose_method failed: {exc}", exc_info=True)
    finally:
        await call.answer()


@router.callback_query(F.data.startswith("pay:m:"))
async def create_order_for_method(call: types.CallbackQuery):
    try:
        _, _, package_id, method = call.data.split(":")
        if method != "sc" or not await payments_available():
            await call.answer("طريقة الدفع غير متاحة حالياً.", show_alert=True)
            return
        package = await ps.get_package(int(package_id))
        if not package:
            await call.answer("هذه الباقة لم تعد متاحة.", show_alert=True)
            return
        try:
            order = await ps.create_order(call.from_user.id, package)
        except ps.OpenOrderExists:
            existing = await ps.get_open_order_for_user(call.from_user.id)
            if existing:
                await _show_open_order(call, existing)
            return
        await _send_order_screen(call.message.chat.id, order, call.bot)
        spawn(log_usage_event(call.from_user.id, "payment_order_created",
                              {"order_id": order["id"], "points": order["points"]}))
    except Exception as exc:
        logger.error(f"create_order_for_method failed: {exc}", exc_info=True)
        await call.message.answer("⚠️ تعذّر إنشاء طلب الشحن الآن، حاول بعد قليل أو تواصل مع الدعم.")
    finally:
        await call.answer()


async def _already_paid_for_user(call: types.CallbackQuery, order_id: int) -> bool:
    """الشحن التلقائي قد يشحن الطلب قبل ضغط الطالب «دفعت/إلغاء»: نخبره بالحقيقة ونزيل الأزرار الباقية."""
    order = await ps.get_order(order_id)
    if not order or order.get("user_id") != call.from_user.id or order.get("status") != "paid":
        return False
    try:
        await call.message.edit_reply_markup(reply_markup=None)
    except TelegramBadRequest:
        pass
    await call.answer("✅ تم شحن رصيدك بالفعل.", show_alert=True)
    return True


@router.callback_query(F.data.startswith("pay:paid:"))
async def user_claims_paid(call: types.CallbackQuery):
    try:
        order_id = int(call.data.split(":")[2])
        order = await ps.claim_order(order_id, call.from_user.id)
        if not order:
            if await _already_paid_for_user(call, order_id):
                return
            await call.answer("لا يمكن تأكيد هذا الطلب (منتهٍ أو مؤكد مسبقاً).", show_alert=True)
            return
        try:
            await call.message.edit_reply_markup(reply_markup=None)
        except TelegramBadRequest:
            pass
        await call.message.answer(
            "✅ <b>استلمنا تأكيدك</b>\nنتحقق من دفعتك الآن، وسيصلك إشعار فور إضافة النقاط. 🙏",
            parse_mode="HTML",
        )
        await ps.notify_claim(order)
    except Exception as exc:
        logger.error(f"user_claims_paid failed: {exc}", exc_info=True)
    finally:
        await call.answer()


@router.callback_query(F.data.startswith("pay:cancel:"))
async def user_cancels(call: types.CallbackQuery):
    try:
        order_id = int(call.data.split(":")[2])
        if await ps.cancel_order(order_id, call.from_user.id):
            try:
                await call.message.edit_reply_markup(reply_markup=None)
            except TelegramBadRequest:
                pass
            await call.message.answer("تم إلغاء الطلب. يمكنك بدء طلب جديد بأي وقت من «شحن الرصيد».")
        else:
            if await _already_paid_for_user(call, order_id):
                return
            await call.answer("لا يمكن إلغاء هذا الطلب (مؤكد مسبقاً أو منتهٍ).", show_alert=True)
            return
    except Exception as exc:
        logger.error(f"user_cancels failed: {exc}", exc_info=True)
    finally:
        await call.answer()


# ==================== جانب الأدمن (مجموعة الدفع) ====================

async def _finish_group_message(call: types.CallbackQuery, suffix: str) -> None:
    try:
        await call.message.edit_text(f"{call.message.html_text}\n\n{suffix}", parse_mode="HTML", reply_markup=None)
    except TelegramBadRequest:
        pass


async def _strip_order_message_buttons(order: Optional[dict], clicked: types.Message) -> None:
    """يشيل أزرار بطاقة 'طالب أكد الدفع' الأصلية لو كانت رسالة مختلفة عن اللي انضغطت."""
    if not order or not order.get("group_message_id") or not order.get("group_chat_id"):
        return
    if order["group_message_id"] == clicked.message_id and order["group_chat_id"] == clicked.chat.id:
        return
    try:
        await clicked.bot.edit_message_reply_markup(
            chat_id=order["group_chat_id"], message_id=order["group_message_id"], reply_markup=None)
    except Exception:
        pass


@router.callback_query(F.data.startswith("pay:ok:"))
async def admin_confirm(call: types.CallbackQuery):
    if not ps.is_payment_admin(call.from_user.id):
        await call.answer("غير مصرّح لك.", show_alert=True)
        return
    try:
        _, _, order_id, receipt_id = call.data.split(":")
        order_id, receipt_id = int(order_id), int(receipt_id) or None
        actor = f"tg:{call.from_user.id}"
        result = await ps.credit_order(order_id, receipt_id, actor)
        status = result.get("result")
        who = html.escape(call.from_user.full_name or str(call.from_user.id))

        if status == "ok":
            user_id = int(result["order_user_id"])
            points = int(result["credited_points"])
            total = int(result.get("new_total_points") or 0)
            await _finish_group_message(call, f"✅ <b>تم شحن {points} نقطة</b> بواسطة {who}")
            await _strip_order_message_buttons(await ps.get_order(order_id), call.message)
            await send_points_notification(user_id, points, total)
            spawn(log_usage_event(user_id, "payment_credited", {"order_id": order_id, "points": points, "by": actor}))
            await call.answer("تم الشحن ✅")
            return

        messages = {
            "already_paid": "هذا الطلب مشحون مسبقاً — لم يُضف شيء.",
            "not_creditable": "الطلب مرفوض أو ملغى — لا يمكن شحنه.",
            "receipt_used": "هذا الإيصال استُخدم بالفعل لشحن طلب آخر.",
            "receipt_not_creditable": "هذا الإيصال معلَّم كتجاهل/خط أساس — لا يمكن استخدامه للشحن.",
            "receipt_not_found": "الإيصال غير موجود.",
            "user_not_found": "حساب الطالب غير موجود.",
            "not_found": "الطلب غير موجود.",
        }
        await call.answer(messages.get(status, "تعذّر تنفيذ الشحن، راجع السجلات."), show_alert=True)
        if status in ("already_paid", "not_creditable", "receipt_used", "receipt_not_creditable"):
            await _finish_group_message(call, f"ℹ️ {messages[status]}")
    except Exception as exc:
        logger.error(f"admin_confirm failed: {exc}", exc_info=True)
        await call.answer("حدث خطأ غير متوقع، لم يُضف شيء. راجع السجلات.", show_alert=True)


@router.callback_query(F.data.startswith("pay:no:"))
async def admin_reject(call: types.CallbackQuery):
    if not ps.is_payment_admin(call.from_user.id):
        await call.answer("غير مصرّح لك.", show_alert=True)
        return
    try:
        order_id = int(call.data.split(":")[2])
        order = await ps.reject_order(order_id, f"tg:{call.from_user.id}")
        if not order:
            await call.answer("لا يمكن رفض هذا الطلب (مشحون/مرفوض/ملغى مسبقاً).", show_alert=True)
            return
        who = html.escape(call.from_user.full_name or str(call.from_user.id))
        await _finish_group_message(call, f"❌ <b>تم رفض الطلب #{order_id}</b> بواسطة {who}")
        await _strip_order_message_buttons(order, call.message)
        try:
            await call.bot.send_message(
                order["user_id"],
                f"⚠️ لم نتمكن من تأكيد دفعتك للطلب <code>{order['code']}</code>.\n"
                f"إن كنت قد حوّلت المبلغ فتواصل مع الدعم: {ADMIN_CONTACT}",
                parse_mode="HTML",
            )
        except Exception:
            pass
        await call.answer("تم الرفض")
    except Exception as exc:
        logger.error(f"admin_reject failed: {exc}", exc_info=True)
        await call.answer("حدث خطأ غير متوقع.", show_alert=True)


@router.callback_query(F.data.startswith("pay:ig:"))
async def admin_ignore(call: types.CallbackQuery):
    if not ps.is_payment_admin(call.from_user.id):
        await call.answer("غير مصرّح لك.", show_alert=True)
        return
    try:
        await ps.dismiss_receipt(int(call.data.split(":")[2]))
        who = html.escape(call.from_user.full_name or str(call.from_user.id))
        await _finish_group_message(call, f"🚫 <b>تم تجاهل الحوالة</b> بواسطة {who}")
        await call.answer("تم التجاهل")
    except Exception as exc:
        logger.error(f"admin_ignore failed: {exc}", exc_info=True)
        await call.answer("حدث خطأ غير متوقع.", show_alert=True)


@router.callback_query(F.data == "pay:autooff")
async def admin_auto_off(call: types.CallbackQuery):
    """زر الطوارئ تحت رسالة «شحن تلقائي»: يوقف المفتاح الفرعي فوراً (يعود كل شيء للتأكيد اليدوي)."""
    if not ps.is_payment_admin(call.from_user.id):
        await call.answer("غير مصرّح لك.", show_alert=True)
        return
    try:
        if not await set_feature_flag("payments_auto_confirm", False):
            await call.answer("تعذّر حفظ الإيقاف، أوقفه من لوحة الأدمن.", show_alert=True)
            return
        who = html.escape(call.from_user.full_name or str(call.from_user.id))
        await _finish_group_message(call, f"🛑 <b>أُوقف الشحن التلقائي</b> بواسطة {who}")
        await call.answer("أُوقف الشحن التلقائي")
    except Exception as exc:
        logger.error(f"admin_auto_off failed: {exc}", exc_info=True)
        await call.answer("حدث خطأ غير متوقع.", show_alert=True)
