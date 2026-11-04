"""Durable Damascus-20:00 renewal notices; Telegram sends are never retried blindly."""
import asyncio

from aiogram import types
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError, TelegramRetryAfter
from logger import get_logger

logger = get_logger(__name__)


def renewal_keyboard():
    return types.InlineKeyboardMarkup(inline_keyboard=[[
        types.InlineKeyboardButton(
            text="🔕 إيقاف إشعارات التجديد (إعادة التفعيل من الإعدادات)",
            callback_data="renewal:off",
        )
    ]])


def report_text(r):
    return (
        f"📊 تقرير تجديد النقاط — دورة تنتهي يوم {r['day']} الساعة 8 مساءً (توقيت دمشق)\n\n"
        f"✅ استلموا رسالة التجديد: {r['sent']}\n"
        f"🔔 يفعّلون الإشعارات حاليًا: {r['enabled']}\n"
        f"🔕 أوقفوا الإشعارات حاليًا: {r['disabled']}\n"
        f"❌ لم يستلموا (فشل مؤكد): {r['failed']}\n"
        f"❔ تسليم غير مؤكد: {r['uncertain']}\n"
        f"🚫 مستثنون قبل الإرسال (حظر/حساب غير متاح): {r['excluded']}\n"
        f"⏭️ أُوقف إشعارهم أثناء الحملة: {r['skipped']}\n\n"
        "تجدد الرصيد مستقل عن الإشعارات. تفاصيل التعذر محفوظة في إدارة المستخدمين."
    )


def delivery_error(exc):
    """Only Telegram's explicit blocked-by-user error proves a block."""
    message = str(getattr(exc, "message", exc))
    if isinstance(exc, TelegramForbiddenError):
        if "blocked by the user" in message.lower():
            return "blocked", "المستخدم حظر البوت"
        return "unavailable", message[:500]
    if isinstance(exc, TelegramBadRequest):
        return "failed", message[:500]
    return "uncertain", type(exc).__name__


async def renewal_step(client, bot, admin_id):
    await client.rpc("daily_renewal_begin", {}).execute()
    claim = await client.rpc("daily_renewal_claim", {}).execute()
    recipient = claim.data
    if isinstance(recipient, list):
        recipient = recipient[0] if recipient else None
    if recipient:
        params = dict(p_day=recipient["day"], p_user=recipient["user_id"],
                      p_token=recipient["token"], p_status="sent", p_error="", p_retry=0)
        try:
            await bot.send_message(
                recipient["user_id"],
                "🔄 تجددت نقاطك المجانية!\n\n"
                f"الحد المجاني اليومي: {recipient['amount']:g} نقطة.\n"
                "يتجدد يوميًا الساعة 8 مساءً بتوقيت دمشق، دون تراكم.\n"
                "يمكنك إيقاف هذه الإشعارات بالزر أدناه وإعادة تفعيلها من ⚙️ الإعدادات.",
                reply_markup=renewal_keyboard(), request_timeout=10,
            )
        except TelegramRetryAfter as exc:
            params.update(p_status="pending", p_error="Telegram rate limit",
                          p_retry=max(1, int(exc.retry_after) + 1))
        except Exception as exc:
            status, reason = delivery_error(exc)
            params.update(p_status=status, p_error=reason)
        # Persist before attempting anything else. An interrupted/unknown send is
        # marked uncertain after its lease expires, never blindly sent again.
        await client.rpc("daily_renewal_finish", params).execute()
        if params["p_status"] == "pending":
            return min(params["p_retry"], 60)

    result = await client.rpc("daily_renewal_report_claim", {}).execute()
    r = result.data
    if r:
        status = "sent"
        try:
            await bot.send_message(admin_id, report_text(r), request_timeout=10,
                reply_markup=types.InlineKeyboardMarkup(inline_keyboard=[[
                    types.InlineKeyboardButton(text="👥 تعذر التسليم / حظروا البوت",
                                               callback_data="admin_renewal_failures:all:0")
                ]]))
        except TelegramRetryAfter as exc:
            await client.rpc("daily_renewal_report_finish", {
                "p_day": r["day"], "p_token": r["token"], "p_status": "pending",
                "p_retry": max(1, int(exc.retry_after) + 1),
            }).execute()
            return min(int(exc.retry_after) + 1, 60)
        except Exception:
            status = "uncertain"
            logger.exception("Daily renewal admin report delivery uncertain")
        await client.rpc("daily_renewal_report_finish", {
            "p_day": r["day"], "p_token": r["token"], "p_status": status, "p_retry": 0,
        }).execute()
    return 0.1 if recipient else 10


async def daily_renewal_loop():
    from config import bot, ADMIN_ID
    from helpers.supabase_helper._client import supabase
    while True:
        delay = 10
        try:
            delay = await renewal_step(supabase, bot, ADMIN_ID)
        except Exception:
            logger.exception("Daily renewal step failed; durable queue retained")
        await asyncio.sleep(delay)

