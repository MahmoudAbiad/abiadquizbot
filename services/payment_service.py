"""
services/payment_service.py
==============================================================================
نظام الشحن الذاتي (شام كاش + MacroDroid) - المرحلة الأولى: التقاط + مراجعة يدوية.

المبادئ:
  1) السيرفر هو من يكتشف الفشل لا الجوال: حلقة payment_watchdog_loop تنبّه المجموعة لأي
     إشعار وصل ولم يتبعه حدث سجل، وتنهي الطلبات منتهية الصلاحية.
  2) لا شيء يضيع: كل حدث وارد يُسجَّل خاماً بـ payment_events قبل أي تحليل، وكل بطاقة جديدة
     بالسجل تصل المجموعة.
  3) Fail-closed: الأصل أنّ الشحن بضغطة أدمن على زر فقط، عبر الدالة الذرية payment_credit_order
     (تمنع الشحن المزدوج بقفل الطلب والإيصال). الاستثناء الوحيد: مفتاح payments_auto_confirm
     ("شحن تلقائي" بلوحة الأدمن، مطفي افتراضياً) يصادق تلقائياً على الدفعة المكتملة الصحة فقط
     (راجع payment_parser.auto_confirm_verdict)؛ أي شك = تبقى للتأكيد اليدوي بأزرارها المعتادة.
  4) Supabase مصدر الحقيقة الوحيد؛ Redis للـ heartbeat فقط (ضياعه غير مؤثر).
==============================================================================
"""

import asyncio
import datetime
import html
import os
from decimal import Decimal
from typing import Any, Dict, List, Optional, Set
from zoneinfo import ZoneInfo

from aiogram import types

from config import ADMIN_ID, bot, redis_client
from constants import (
    PAYMENT_ADMIN_GROUP_ID, PAYMENT_ADMIN_IDS_EXTRA, PAYMENT_AUTO_TX_GRACE_AFTER_MINUTES, PAYMENT_AUTO_TX_GRACE_BEFORE_MINUTES, PAYMENT_EVENT_RETENTION_DAYS,
    PAYMENT_HEARTBEAT_TTL_SECONDS, PAYMENT_NOTIFICATION_ORPHAN_MINUTES, PAYMENT_ORDER_TTL_MINUTES,
)
from helpers.supabase_helper import is_feature_enabled, log_usage_event, supabase
from logger import get_logger
from services.payment_parser import (
    ParsedCard, auto_confirm_verdict, clean_screen_text, find_codes_in_note, looks_like_transfers_screen,
    make_unique_code, parse_cards,
)
from task_utils import spawn

logger = get_logger(__name__)

OPEN_STATUSES = ("pending", "awaiting_verification", "manual_review")
CODE_MATCH_STATUSES = OPEN_STATUSES + ("expired",)
NOTIFICATION_MAX_CHARS = 4000
SCREEN_MAX_CHARS = 20000
BASELINE_FRESH_WINDOW_MINUTES = 10
_HEARTBEAT_KEY = "payment:heartbeat"
AUTO_CONFIRM_ACTOR = "auto:shamcash"


class OpenOrderExists(Exception):
    """للطالب طلب شحن مفتوح أصلاً (فهرس payment_orders_open_user_uq)."""


def _now() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


def _iso(dt: datetime.datetime) -> str:
    return dt.astimezone(datetime.timezone.utc).isoformat()


# ==================== أدوات عرض ====================

def format_payment_time(value: Any) -> str:
    """Display stored receipt timestamps in Syria's timezone; storage remains UTC."""
    if not value:
        return "غير متوفر"
    try:
        dt = value if isinstance(value, datetime.datetime) else datetime.datetime.fromisoformat(
            str(value).replace("Z", "+00:00")
        )
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=datetime.timezone.utc)
        local = dt.astimezone(ZoneInfo("Asia/Damascus"))
    except (ValueError, TypeError):
        return "وقت غير صالح"
    period = "صباحًا" if local.hour < 12 else "مساءً"
    clock = f"{local.hour % 12 or 12}:{local.minute:02d}:{local.second:02d}"
    return f"{local:%d/%m/%Y} — {clock} {period} (بتوقيت سوريا)"


def format_money(amount: Any, currency: Optional[str]) -> str:
    try:
        value = Decimal(str(amount)).normalize()
        text = format(value, "f")
    except Exception:
        text = str(amount)
    unit = {"SYP": "ل.س", "USD": "$"}.get(currency or "", currency or "")
    return f"{text} {unit}".strip()


def payment_admin_ids() -> Set[int]:
    ids: Set[int] = set()
    if ADMIN_ID:
        ids.add(int(ADMIN_ID))
    for part in (PAYMENT_ADMIN_IDS_EXTRA or "").split(","):
        part = part.strip()
        if part.lstrip("-").isdigit():
            ids.add(int(part))
    return ids


def is_payment_admin(user_id: int) -> bool:
    return int(user_id) in payment_admin_ids()


async def _user_label(user_id: int) -> str:
    try:
        res = await supabase.table("users").select("username,first_name").eq("user_id", user_id).limit(1).execute()
        row = res.data[0] if res.data else {}
    except Exception:
        row = {}
    name = (row.get("first_name") or "").strip()
    uname = row.get("username")
    parts = [html.escape(name)] if name else []
    if uname and uname != "Unknown":
        parts.append(f"@{html.escape(uname)}")
    parts.append(f"<code>{user_id}</code>")
    return " ".join(parts)


# ==================== الباقات والطلبات ====================

async def get_active_packages() -> List[Dict[str, Any]]:
    res = await (
        supabase.table("payment_packages").select("*").eq("active", True)
        .order("sort_order").order("id").execute()
    )
    return res.data or []


async def get_package(package_id: int) -> Optional[Dict[str, Any]]:
    res = await supabase.table("payment_packages").select("*").eq("id", package_id).eq("active", True).limit(1).execute()
    return res.data[0] if res.data else None


async def get_order(order_id: int) -> Optional[Dict[str, Any]]:
    res = await supabase.table("payment_orders").select("*").eq("id", order_id).limit(1).execute()
    return res.data[0] if res.data else None


async def get_open_order_for_user(user_id: int) -> Optional[Dict[str, Any]]:
    res = await (
        supabase.table("payment_orders").select("*").eq("user_id", user_id)
        .in_("status", list(OPEN_STATUSES)).limit(1).execute()
    )
    return res.data[0] if res.data else None


async def _open_codes() -> Set[str]:
    res = await supabase.table("payment_orders").select("code").in_("status", list(OPEN_STATUSES)).execute()
    return {r["code"] for r in (res.data or [])}


async def create_order(user_id: int, package: Dict[str, Any]) -> Dict[str, Any]:
    """ينشئ طلباً جديداً بكود فريد. يرمي OpenOrderExists لو للطالب طلب مفتوح."""
    expires = _now() + datetime.timedelta(minutes=PAYMENT_ORDER_TTL_MINUTES)
    last_error: Optional[Exception] = None
    for _ in range(5):
        code = make_unique_code(await _open_codes())
        try:
            res = await supabase.table("payment_orders").insert({
                "user_id": user_id,
                "package_id": package["id"],
                "points": package["points"],
                "amount": package["price"],
                "currency": package["currency"],
                "method": "shamcash",
                "code": code,
                "expires_at": _iso(expires),
            }).execute()
            return res.data[0]
        except Exception as exc:  # unique_violation يظهر كنص الخطأ
            text = str(exc)
            if "payment_orders_open_user_uq" in text:
                raise OpenOrderExists() from exc
            if "payment_orders_open_code_uq" in text or "23505" in text:
                last_error = exc
                continue
            raise
    raise RuntimeError(f"could not create payment order: {last_error}")


async def claim_order(order_id: int, user_id: int) -> Optional[Dict[str, Any]]:
    """الطالب أكّد أنه دفع: pending -> awaiting_verification. يرجع الصف أو None لو لم يكن pending."""
    res = await (
        supabase.table("payment_orders")
        .update({"status": "awaiting_verification", "claimed_at": _iso(_now()), "updated_at": _iso(_now())})
        .eq("id", order_id).eq("user_id", user_id).eq("status", "pending").execute()
    )
    return res.data[0] if res.data else None


async def cancel_order(order_id: int, user_id: int) -> bool:
    """الإلغاء مسموح فقط قبل تأكيد الدفع (pending)."""
    res = await (
        supabase.table("payment_orders")
        .update({"status": "cancelled", "updated_at": _iso(_now())})
        .eq("id", order_id).eq("user_id", user_id).eq("status", "pending").execute()
    )
    return bool(res.data)


async def reject_order(order_id: int, actor: str, note: str = "") -> Optional[Dict[str, Any]]:
    res = await (
        supabase.table("payment_orders")
        .update({"status": "rejected", "decided_by": actor, "decision_note": note or None, "updated_at": _iso(_now())})
        .eq("id", order_id).in_("status", list(CODE_MATCH_STATUSES)).execute()
    )
    return res.data[0] if res.data else None


async def credit_order(order_id: int, receipt_id: Optional[int], actor: str) -> Dict[str, Any]:
    """الشحن الذري. يرجع صف النتيجة: result ∈ ok/already_paid/not_creditable/receipt_used/..."""
    res = await supabase.rpc("payment_credit_order", {
        "p_order_id": order_id, "p_receipt_id": receipt_id, "p_actor": actor,
    }).execute()
    data = res.data
    if isinstance(data, list):
        data = data[0] if data else {}
    return data or {"result": "error"}


async def set_order_group_message(order_id: int, chat_id: int, message_id: int) -> None:
    try:
        await supabase.table("payment_orders").update(
            {"group_chat_id": chat_id, "group_message_id": message_id}
        ).eq("id", order_id).execute()
    except Exception as exc:
        logger.warning(f"could not store group message ref for order {order_id}: {exc}")


async def expire_stale_orders() -> List[Dict[str, Any]]:
    res = await (
        supabase.table("payment_orders")
        .update({"status": "expired", "updated_at": _iso(_now())})
        .eq("status", "pending").lt("expires_at", _iso(_now())).execute()
    )
    return res.data or []


async def dismiss_receipt(receipt_id: int) -> None:
    await supabase.table("payment_receipts").update(
        {"status": "dismissed", "updated_at": _iso(_now())}
    ).eq("id", receipt_id).eq("status", "new").execute()


# ==================== الإرسال لمجموعة الأدمن ====================

def decision_keyboard(order: Dict[str, Any], receipt_id: Optional[int] = None,
                      with_ignore: bool = False, label: Optional[str] = None) -> List[List[types.InlineKeyboardButton]]:
    rid = receipt_id or 0
    rows = [[
        types.InlineKeyboardButton(text=label or f"✅ تأكيد وشحن {order['points']} نقطة", callback_data=f"pay:ok:{order['id']}:{rid}"),
        types.InlineKeyboardButton(text="❌ رفض", callback_data=f"pay:no:{order['id']}"),
    ]]
    if with_ignore and receipt_id:
        rows.append([types.InlineKeyboardButton(text="🚫 ليست دفعة طالب (تجاهل)", callback_data=f"pay:ig:{receipt_id}")])
    return rows


async def send_admin_message(text: str, rows: Optional[List[List[types.InlineKeyboardButton]]] = None) -> Optional[types.Message]:
    """يرسل لمجموعة الأدمن، وإن فشل (البوت ليس عضواً/المعرّف خاطئ) يرسل للأدمن بالخاص حتى لا يضيع التنبيه."""
    markup = types.InlineKeyboardMarkup(inline_keyboard=rows) if rows else None
    targets = []
    if PAYMENT_ADMIN_GROUP_ID:
        targets.append(PAYMENT_ADMIN_GROUP_ID)
    if ADMIN_ID:
        targets.append(int(ADMIN_ID))
    for target in targets:
        try:
            return await bot.send_message(target, text, parse_mode="HTML", reply_markup=markup,
                                          disable_web_page_preview=True)
        except Exception as exc:
            logger.error(f"payment admin message to {target} failed: {exc}")
    return None


async def _open_orders_for_buttons(limit: int = 5) -> List[Dict[str, Any]]:
    res = await (
        supabase.table("payment_orders").select("*").in_("status", list(OPEN_STATUSES))
        .order("created_at", desc=True).limit(limit).execute()
    )
    return res.data or []


async def notify_claim(order: Dict[str, Any]) -> None:
    """الطالب ضغط 'دفعت': ترسل بطاقة الطلب للمجموعة (المرحلة الأولى: مراجعة يدوية)."""
    text = (
        f"🧾 <b>طالب أكّد الدفع — طلب #{order['id']}</b>\n"
        f"👤 {await _user_label(order['user_id'])}\n"
        f"📦 {order['points']} نقطة — {format_money(order['amount'], order['currency'])}\n"
        f"🔑 الكود المتوقع بالملاحظة: <code>{order['code']}</code>\n\n"
        "تحقق من شام كاش (المبلغ + الكود بالملاحظة) ثم اضغط تأكيد أو رفض."
    )
    msg = await send_admin_message(text, decision_keyboard(order))
    if msg:
        await set_order_group_message(order["id"], msg.chat.id, msg.message_id)


# ==================== الأحداث الواردة من MacroDroid ====================

async def record_heartbeat(device_id: Optional[str]) -> None:
    try:
        await redis_client.set(_HEARTBEAT_KEY, f"{device_id or '-'}:{int(_now().timestamp())}", ex=PAYMENT_HEARTBEAT_TTL_SECONDS)
    except Exception as exc:
        logger.warning(f"payment heartbeat store failed: {exc}")


async def phone_last_seen() -> Optional[int]:
    """آخر heartbeat (epoch) أو None لو انقطع/انتهت صلاحيته. للمرحلة الثانية (قرار التحقق الآلي)."""
    try:
        raw = await redis_client.get(_HEARTBEAT_KEY)
        if raw is None:
            return None
        raw = raw.decode() if isinstance(raw, bytes) else str(raw)
        return int(raw.rsplit(":", 1)[1])
    except Exception:
        return None


async def record_event(event_type: str, device_id: Optional[str], notification_text: str, screen_text: str) -> int:
    res = await supabase.table("payment_events").insert({
        "event_type": event_type[:40],
        "device_id": (device_id or "")[:80] or None,
        "notification_text": (notification_text or "")[:NOTIFICATION_MAX_CHARS] or None,
        "screen_text": (screen_text or "")[:SCREEN_MAX_CHARS] or None,
        # التنبيهات (notification) تبقى غير معالجة لحد ما يصل حدث السجل المرافق لها
        "processed": event_type not in ("notification",),
    }).execute()
    return res.data[0]["id"]


async def handle_incoming_event(payload: Dict[str, Any]) -> Dict[str, Any]:
    """نقطة الدخول من الـ endpoint. تسجّل الحدث خاماً أولاً ثم تعالجه بالخلفية، وترد فوراً."""
    notification = str(payload.get("notification_text") or "")
    screen = str(payload.get("screen_data") or payload.get("screen_text") or "")
    event_type = str(payload.get("event_type") or "").strip().lower()
    if not event_type:
        event_type = "screen" if screen else ("notification" if notification else "unknown")
    device_id = str(payload.get("device_id") or "") or None

    if event_type == "heartbeat":
        await record_heartbeat(device_id)
        return {"ok": True}

    event_id = await record_event(event_type, device_id, notification, screen)
    if event_type in ("screen", "manual_request"):
        spawn(process_event(event_id, event_type, device_id, notification, screen))
    return {"ok": True, "event_id": event_id}


async def _mark_events_processed(ids: List[int]) -> None:
    if ids:
        await supabase.table("payment_events").update({"processed": True}).in_("id", ids).execute()


async def process_event(event_id: int, event_type: str, device_id: Optional[str],
                        notification_text: str, screen_text: str) -> None:
    try:
        # الإشعار المسبق من نفس الجهاز لم يعد "يتيماً": وصل حدثه التالي.
        since = _iso(_now() - datetime.timedelta(minutes=15))
        res = await (
            supabase.table("payment_events").select("id").eq("event_type", "notification")
            .eq("processed", False).gte("received_at", since).execute()
        )
        await _mark_events_processed([r["id"] for r in (res.data or [])])

        if event_type == "manual_request":
            await _alert_manual_request(notification_text)
            return

        if not looks_like_transfers_screen(screen_text):
            await _alert_unrecognized_screen(notification_text, screen_text)
            return

        cards = [c for c in parse_cards(screen_text) if c.amount is not None]
        if not cards:
            await _alert_unrecognized_screen(notification_text, screen_text)
            return
        await _ingest_cards(cards, event_id, notification_text)
    except Exception as exc:
        logger.error(f"payment process_event failed (event {event_id}): {exc}", exc_info=True)
        # الفشل غير المتوقع يصل للمجموعة أيضاً: لا يضيع شيء بصمت.
        await send_admin_message(
            f"⚠️ <b>خطأ داخلي بمعالجة حدث دفع #{event_id}</b>\nراجع الحدث الخام بجدول payment_events.\n"
            f"<code>{html.escape(str(exc))[:300]}</code>"
        )


async def _insert_receipt(card: ParsedCard, event_id: int, status: str) -> Optional[Dict[str, Any]]:
    """يسجّل الإيصال؛ يرجع None لو رقم العملية موجود أصلاً (UNIQUE) - يعني ليس جديداً."""
    try:
        res = await supabase.table("payment_receipts").insert({
            "tx_id": card.tx_id,
            "counterparty": card.counterparty[:200] or None,
            "amount": float(card.amount) if card.amount is not None else None,
            "currency": card.currency,
            "tx_time": _iso(card.tx_time) if card.tx_time else None,
            "note": card.note[:500] or None,
            "raw_card": card.raw[:1000],
            "status": status,
            "event_id": event_id,
        }).execute()
        return res.data[0] if res.data else None
    except Exception as exc:
        if "payment_receipts_tx_id_uq" in str(exc) or "23505" in str(exc):
            return None
        raise


async def _ingest_cards(cards: List[ParsedCard], event_id: int, notification_text: str) -> None:
    first_run = not (await supabase.table("payment_receipts").select("id").limit(1).execute()).data
    fresh_cutoff = _now() - datetime.timedelta(minutes=BASELINE_FRESH_WINDOW_MINUTES)
    baseline_count = 0
    new_receipts: List[Dict[str, Any]] = []
    for card in cards:
        is_old = bool(card.tx_time and card.tx_time < fresh_cutoff)
        if first_run and is_old:
            if await _insert_receipt(card, event_id, "baseline"):
                baseline_count += 1
            continue
        row = await _insert_receipt(card, event_id, "new")
        if row:
            new_receipts.append(row)

    if first_run:
        await send_admin_message(
            f"🧱 <b>تهيئة نظام الدفع</b>\nسُجّلت {baseline_count} حوالة قديمة كخط أساس ولن تُعرض كدفعات."
        )
    auto_on = False if first_run else await _auto_confirm_enabled()
    for receipt in new_receipts:
        auto_note = None
        if auto_on:
            done, auto_note = await _try_auto_confirm(receipt)
            if done:
                continue
        await _notify_receipt(receipt, notification_text, auto_note=auto_note)


async def match_orders(receipt: Dict[str, Any]) -> Dict[str, Any]:
    """يرجع {'by_code': order|None, 'by_amount': [orders]} للإيصال."""
    codes = find_codes_in_note(receipt.get("note"))
    by_code = None
    if codes:
        res = await (
            supabase.table("payment_orders").select("*").in_("code", codes)
            .in_("status", list(CODE_MATCH_STATUSES)).order("created_at", desc=True).limit(1).execute()
        )
        by_code = res.data[0] if res.data else None
    by_amount: List[Dict[str, Any]] = []
    if not by_code and receipt.get("amount") is not None:
        amount = Decimal(str(receipt["amount"]))
        for order in await _open_orders_for_buttons(limit=50):
            if order["currency"] == receipt.get("currency") and Decimal(str(order["amount"])) == amount:
                by_amount.append(order)
    return {"by_code": by_code, "by_amount": by_amount[:3]}


# ==================== 🤖 المصادقة التلقائية ====================

async def _auto_confirm_enabled() -> bool:
    """الشحن التلقائي يتطلب تشغيل الشحن الذاتي نفسه + مفتاح payments_auto_confirm صراحةً (مطفي افتراضياً)."""
    try:
        return (await is_feature_enabled("payments_enabled", default=False)
                and await is_feature_enabled("payments_auto_confirm", default=False))
    except Exception as exc:
        logger.error(f"auto-confirm flag check failed (treated as off): {exc}")
        return False


async def _try_auto_confirm(receipt: Dict[str, Any]) -> "tuple[bool, Optional[str]]":
    """يحاول مصادقة الإيصال تلقائياً. يرجع (True, None) لو شُحن، وإلا (False, السبب) فيكمل المسار اليدوي.
    الشحن نفسه دائماً عبر credit_order الذرية (قفل الطلب والإيصال)، فالاستدعاء المتكرر لا يشحن مرتين."""
    try:
        order = (await match_orders(receipt))["by_code"]
        ok, reason = auto_confirm_verdict(
            order, receipt, _now(),
            grace_before_minutes=PAYMENT_AUTO_TX_GRACE_BEFORE_MINUTES,
            grace_after_minutes=PAYMENT_AUTO_TX_GRACE_AFTER_MINUTES,
        )
        if not ok:
            return False, reason
        result = await credit_order(order["id"], receipt["id"], AUTO_CONFIRM_ACTOR)
        if result.get("result") != "ok":
            return False, f"رفضت الدالة الذرية الشحن ({result.get('result')})"
    except Exception as exc:
        logger.error(f"auto-confirm failed before crediting, falling back to manual: {exc}", exc_info=True)
        return False, "خطأ داخلي أثناء المصادقة التلقائية"

    # بعد هذه النقطة النقاط أُضيفت فعلاً: أي فشل بالإشعارات يُسجَّل فقط ولا يعيد الدفعة للمسار اليدوي.
    try:
        points = int(result["credited_points"])
        total = int(result.get("new_total_points") or 0)
        user_id = int(result["order_user_id"])
        await _notify_user_credited(user_id, points, total)
        spawn(log_usage_event(user_id, "payment_credited", {"order_id": order["id"], "points": points, "by": AUTO_CONFIRM_ACTOR}))
        await _strip_group_buttons(order)
        await send_admin_message(
            f"🤖 <b>شحن تلقائي — طلب #{order['id']}</b>\n"
            f"👤 {await _user_label(user_id)}\n"
            f"✅ أُضيف <b>{points}</b> نقطة (المبلغ {format_money(receipt.get('amount'), receipt.get('currency'))})\n"
            f"🔢 رقم العملية: <code>#{receipt['tx_id']}</code> — 🔑 الكود: <code>{order['code']}</code>"
        )
    except Exception as exc:
        logger.error(f"auto-confirm credited order {order['id']} but a follow-up step failed: {exc}", exc_info=True)
    return True, None


async def _notify_user_credited(user_id: int, points: int, total: int) -> None:
    try:
        await bot.send_message(
            user_id,
            "🎉 <b>أخبار سارة! تم تحديث رصيدك</b>\n\n"
            f"عزيزي الطالب، تم إضافة <code>{points}</code> نقاط جديدة إلى حسابك بنجاح. ✨\n\n"
            f"🟢 <b>الكمية المضافة:</b> <code>+{points}</code>\n"
            f"💰 <b>رصيدك الحالي أصبح:</b> <code>{total}</code> نقطة\n\n"
            "نتمنى لك رحلة تعليمية ممتعة ومليئة بالتوفيق والنجاح! 🚀",
            parse_mode="HTML",
        )
    except Exception as exc:
        logger.error(f"could not notify user {user_id} about auto top-up: {exc}")


async def _strip_group_buttons(order: Dict[str, Any]) -> None:
    """يشيل أزرار بطاقة 'طالب أكّد الدفع' (لو وُجدت) حتى لا تبقى أزرار تأكيد قابلة للضغط لطلب مشحون."""
    if not order.get("group_chat_id") or not order.get("group_message_id"):
        return
    try:
        await bot.edit_message_reply_markup(
            chat_id=order["group_chat_id"], message_id=order["group_message_id"], reply_markup=None)
    except Exception:
        pass


async def _notify_receipt(receipt: Dict[str, Any], notification_text: str, auto_note: Optional[str] = None) -> None:
    matches = await match_orders(receipt)
    tx_time = receipt.get("tx_time") or ""
    lines = [
        "💳 <b>حوالة جديدة بسجل شام كاش</b>",
        "⚠️ <i>الاتجاه (وارد/صادر) غير ظاهر بالسجل — تأكد قبل التأكيد.</i>",
        "",
        f"🔢 رقم العملية: <code>#{receipt['tx_id']}</code>",
        f"💰 المبلغ: <b>{format_money(receipt.get('amount'), receipt.get('currency'))}</b>",
        f"👤 الطرف الآخر: {html.escape(clean_screen_text(receipt.get('counterparty')) or '-')}",
        f"🕒 وقت الحوالة: {html.escape(format_payment_time(tx_time))}",
        f"📝 الملاحظة: <code>{html.escape(clean_screen_text(receipt.get('note')) or '-')}</code>",
    ]
    if notification_text:
        lines.append(f"\n🔔 نص الإشعار:\n<code>{html.escape(notification_text[:500])}</code>")
    if auto_note:
        lines.append(f"\n🤖 <i>الشحن التلقائي مفعّل لكن لم تُصادَق هذه الدفعة تلقائياً: {html.escape(auto_note)}. المراجعة يدوية.</i>")

    rows: List[List[types.InlineKeyboardButton]] = []
    order = matches["by_code"]
    if order:
        same_amount = (
            order["currency"] == receipt.get("currency")
            and receipt.get("amount") is not None
            and Decimal(str(order["amount"])) == Decimal(str(receipt["amount"]))
        )
        verdict = "✅ الكود والمبلغ متطابقان" if same_amount else (
            f"⚠️ الكود مطابق لكن المبلغ مختلف (المتوقع {format_money(order['amount'], order['currency'])})"
        )
        if order["status"] == "expired":
            verdict += " — ⏳ الطلب منتهي الصلاحية (دفعة متأخرة)"
        lines += ["", f"{verdict}", f"🧾 طلب #{order['id']} — {await _user_label(order['user_id'])} — {order['points']} نقطة"]
        rows = decision_keyboard(order, receipt["id"], with_ignore=True)
    elif matches["by_amount"]:
        lines += ["", "🔎 لا كود بالملاحظة. طلبات مفتوحة بنفس المبلغ:"]
        for cand in matches["by_amount"]:
            lines.append(f"• #{cand['id']} — {await _user_label(cand['user_id'])} — {cand['points']} نقطة")
            rows.append([types.InlineKeyboardButton(
                text=f"🔗 ربط بطلب #{cand['id']} وشحن {cand['points']}",
                callback_data=f"pay:ok:{cand['id']}:{receipt['id']}")])
        rows.append([types.InlineKeyboardButton(text="🚫 ليست دفعة طالب (تجاهل)", callback_data=f"pay:ig:{receipt['id']}")])
    else:
        lines += ["", "❔ لا طلب مفتوح مطابق. إن كانت دفعة لطالب فاشحنه يدوياً من لوحة الأدمن."]
        rows = [[types.InlineKeyboardButton(text="🚫 تجاهل", callback_data=f"pay:ig:{receipt['id']}")]]
    await send_admin_message("\n".join(lines), rows)


async def _alert_manual_request(notification_text: str) -> None:
    orders = await _open_orders_for_buttons()
    lines = [
        "🖐️ <b>معالجة يدوية مطلوبة</b>",
        "اخترت تحويل هذه الدفعة للمعالجة اليدوية من الجوال.",
        f"\n🔔 نص الإشعار:\n<code>{html.escape((notification_text or '-')[:700])}</code>",
    ]
    rows: List[List[types.InlineKeyboardButton]] = []
    if orders:
        lines.append("\nالطلبات المفتوحة:")
        for o in orders:
            lines.append(f"• #{o['id']} — {await _user_label(o['user_id'])} — {format_money(o['amount'], o['currency'])} — كود <code>{o['code']}</code>")
            rows.append([types.InlineKeyboardButton(text=f"✅ شحن طلب #{o['id']} ({o['points']})", callback_data=f"pay:ok:{o['id']}:0")])
    await send_admin_message("\n".join(lines), rows or None)


async def _alert_unrecognized_screen(notification_text: str, screen_text: str) -> None:
    orders = await _open_orders_for_buttons()
    lines = [
        "❓ <b>شاشة غير مفهومة من الجوال</b>",
        "لم أجد سجل التحويلات بما قرأه الـ macro (تغيّر التطبيق أو فُتحت شاشة أخرى).",
        f"\n🔔 نص الإشعار:\n<code>{html.escape((notification_text or '-')[:500])}</code>",
        f"\n📄 مقتطف الشاشة:\n<code>{html.escape((screen_text or '-')[:700])}</code>",
    ]
    rows = [[types.InlineKeyboardButton(text=f"✅ شحن طلب #{o['id']} ({o['points']})", callback_data=f"pay:ok:{o['id']}:0")] for o in orders]
    await send_admin_message("\n".join(lines), rows or None)


# ==================== الـ watchdog ====================

async def alert_orphan_notifications() -> int:
    """إشعار شام كاش وصل ولم يتبعه حدث سجل خلال المهلة (الجوال فقد النت/انتهى الـ macro): تنبيه المجموعة."""
    cutoff = _iso(_now() - datetime.timedelta(minutes=PAYMENT_NOTIFICATION_ORPHAN_MINUTES))
    res = await (
        supabase.table("payment_events").select("*").eq("event_type", "notification")
        .eq("processed", False).lt("received_at", cutoff).limit(20).execute()
    )
    count = 0
    for event in res.data or []:
        claim = await supabase.table("payment_events").update({"processed": True}).eq("id", event["id"]).eq("processed", False).execute()
        if not claim.data:
            continue
        count += 1
        await _alert_manual_request(
            "[لم يصل حدث السجل من الجوال خلال "
            f"{PAYMENT_NOTIFICATION_ORPHAN_MINUTES} دقائق]\n" + (event.get("notification_text") or "")
        )
    return count


async def purge_old_events() -> None:
    cutoff = _iso(_now() - datetime.timedelta(days=PAYMENT_EVENT_RETENTION_DAYS))
    await supabase.table("payment_events").delete().lt("received_at", cutoff).execute()


async def payment_watchdog_loop() -> None:
    """حلقة خلفية كل 60 ثانية: إنهاء الطلبات المنتهية، تنبيه الإشعارات اليتيمة، وتنظيف دوري."""
    tick = 0
    while True:
        try:
            for order in await expire_stale_orders():
                try:
                    await bot.send_message(
                        order["user_id"],
                        f"⌛ انتهت صلاحية طلب الشحن <code>{order['code']}</code>.\n"
                        "إن كنت قد حوّلت المبلغ فعلاً فتواصل مع الدعم وسنراجع دفعتك.",
                        parse_mode="HTML",
                    )
                except Exception:
                    pass
            await alert_orphan_notifications()
            if tick % 360 == 0:  # كل ~6 ساعات
                await purge_old_events()
        except Exception as exc:
            logger.error(f"payment watchdog iteration failed: {exc}", exc_info=True)
        tick += 1
        await asyncio.sleep(60)
