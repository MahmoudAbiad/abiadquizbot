"""اختبارات خدمة الدفع بدون شبكة: Supabase وهمي بالذاكرة + bot/redis وهميان."""
import asyncio
import datetime
import json
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from reliability_support import HTTPError, load_functions  # noqa: E402


# ---------------- Supabase وهمي ----------------
class Result:
    def __init__(self, data):
        self.data = data


class FakeDB:
    UNIQUE_RECEIPT = "payment_receipts_tx_id_uq"

    def __init__(self):
        self.tables = {n: [] for n in (
            "users", "payment_packages", "payment_orders", "payment_events", "payment_receipts")}
        self.seq = {}

    def table(self, name):
        return Query(self, name)

    def _next_id(self, name):
        self.seq[name] = self.seq.get(name, 0) + 1
        return self.seq[name]


OPEN = ("pending", "awaiting_verification", "manual_review")


class Query:
    def __init__(self, db, name):
        self.db, self.name = db, name
        self.op, self.payload, self.filters, self._limit = "select", None, [], None

    def select(self, *_a, **_k): self.op = "select"; return self
    def insert(self, row): self.op, self.payload = "insert", row; return self
    def update(self, vals): self.op, self.payload = "update", vals; return self
    def delete(self): self.op = "delete"; return self
    def eq(self, c, v): self.filters.append(lambda r: r.get(c) == v); return self
    def in_(self, c, vs): self.filters.append(lambda r: r.get(c) in vs); return self
    def lt(self, c, v): self.filters.append(lambda r: r.get(c) is not None and r.get(c) < v); return self
    def gte(self, c, v): self.filters.append(lambda r: r.get(c) is not None and r.get(c) >= v); return self
    def order(self, c, desc=False): self._order = (c, desc); return self
    def limit(self, n): self._limit = n; return self

    def _match(self):
        rows = [r for r in self.db.tables[self.name] if all(f(r) for f in self.filters)]
        if getattr(self, "_order", None):
            c, desc = self._order
            rows.sort(key=lambda r: r.get(c) or "", reverse=desc)
        return rows[: self._limit] if self._limit else rows

    async def execute(self):
        rows = self.db.tables[self.name]
        if self.op == "select":
            return Result([dict(r) for r in self._match()])
        if self.op == "insert":
            row = dict(self.payload)
            now = datetime.datetime.now(datetime.timezone.utc).isoformat()
            if self.name == "payment_receipts":
                if any(r["tx_id"] == row["tx_id"] for r in rows):
                    raise Exception(f"duplicate key violates unique constraint {self.db.UNIQUE_RECEIPT} 23505")
            if self.name == "payment_orders":
                row.setdefault("status", "pending")
                if row["status"] in OPEN:
                    for r in rows:
                        if r["status"] in OPEN and r["user_id"] == row["user_id"]:
                            raise Exception("violates unique constraint payment_orders_open_user_uq 23505")
                        if r["status"] in OPEN and r["code"] == row["code"]:
                            raise Exception("violates unique constraint payment_orders_open_code_uq 23505")
            row.setdefault("id", self.db._next_id(self.name))
            row.setdefault("received_at", now)
            row.setdefault("created_at", now)
            rows.append(row)
            return Result([dict(row)])
        if self.op == "update":
            hit = self._match()
            for r in hit:
                r.update(self.payload)
            return Result([dict(r) for r in hit])
        if self.op == "delete":
            hit = self._match()
            self.db.tables[self.name] = [r for r in rows if r not in hit]
            return Result([dict(r) for r in hit])


class FakeRedis:
    def __init__(self): self.values = {}
    async def set(self, k, v, ex=None, nx=False): self.values[k] = v; return True
    async def get(self, k): return self.values.get(k)


# ---------------- تجهيز الوحدات ----------------
DB = FakeDB()
BOT = MagicMock()
BOT.send_message = AsyncMock(return_value=types.SimpleNamespace(chat=types.SimpleNamespace(id=-100), message_id=77))
REDIS = FakeRedis()

sys.modules["config"] = types.SimpleNamespace(ADMIN_ID=111, bot=BOT, redis_client=REDIS)
_helpers = types.ModuleType("helpers"); _helpers.__path__ = []
_sh = types.ModuleType("helpers.supabase_helper"); _sh.supabase = DB
sys.modules["helpers"], sys.modules["helpers.supabase_helper"] = _helpers, _sh

import constants  # noqa: E402
constants.PAYMENT_ADMIN_GROUP_ID = -1004031122773

from services import payment_service as ps  # noqa: E402
ps.PAYMENT_ADMIN_GROUP_ID = -1004031122773

NOW = datetime.datetime.now(datetime.timezone.utc)
SYR = datetime.timezone(datetime.timedelta(hours=3))


def card_text(name, tx, amount, when, note=""):
    stamp = when.astimezone(SYR).strftime("%Y/%m/%d - %H:%M:%S")
    return f"{name}\n\n#{tx}\n{amount}\nل.س\n{stamp}" + (f"\n{note}" if note else "")


def screen(*cards):
    return json.dumps(["مساء الخير", "آخر التحويلات", *cards])


class Base(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        DB.__init__()
        BOT.send_message.reset_mock()
        REDIS.values.clear()

    def sent(self):
        return [(c.args[1], c.kwargs.get("reply_markup")) for c in BOT.send_message.call_args_list]

    def buttons(self, markup):
        return [b.callback_data for row in markup.inline_keyboard for b in row] if markup else []

    async def make_order(self, user_id=5, code="482913", amount=5000, points=100, status="pending"):
        DB.tables["users"].append({"user_id": user_id, "first_name": "طالب", "username": "stud"})
        res = await DB.table("payment_orders").insert({
            "user_id": user_id, "points": points, "amount": amount, "currency": "SYP",
            "code": code, "status": status, "expires_at": (NOW + datetime.timedelta(hours=1)).isoformat()}).execute()
        return res.data[0]


class IngestTests(Base):
    async def test_first_run_baselines_old_cards_without_alerting_per_card(self):
        old = NOW - datetime.timedelta(days=3)
        raw = screen(card_text("أ", "111111111", 100, old), card_text("ب", "222222222", 250, old))
        await ps.process_event(1, "screen", "d", "", raw)
        self.assertEqual({r["status"] for r in DB.tables["payment_receipts"]}, {"baseline"})
        texts = [t for t, _ in self.sent()]
        self.assertEqual(len(texts), 1)
        self.assertIn("خط أساس", texts[0])

    async def test_fresh_card_on_first_run_is_not_baselined(self):
        fresh = NOW - datetime.timedelta(minutes=1)
        await ps.process_event(1, "screen", "d", "", screen(card_text("أ", "333333333", 5000, fresh)))
        self.assertEqual(DB.tables["payment_receipts"][0]["status"], "new")

    async def test_code_and_amount_match_offers_confirm_with_order_and_receipt(self):
        order = await self.make_order()
        DB.tables["payment_receipts"].append({"id": 99, "tx_id": "000000001", "status": "baseline"})
        fresh = NOW - datetime.timedelta(minutes=2)
        await ps.process_event(2, "screen", "d", "حوالة جديدة", screen(card_text("طالب", "444444444", 5000, fresh, "482913")))
        text, markup = self.sent()[-1]
        self.assertIn("الكود والمبلغ متطابقان", text)
        rid = [r for r in DB.tables["payment_receipts"] if r["tx_id"] == "444444444"][0]["id"]
        self.assertIn(f"pay:ok:{order['id']}:{rid}", self.buttons(markup))
        self.assertIn(f"pay:no:{order['id']}", self.buttons(markup))

    async def test_code_match_with_wrong_amount_is_flagged_not_auto(self):
        await self.make_order()
        DB.tables["payment_receipts"].append({"id": 99, "tx_id": "000000001", "status": "baseline"})
        fresh = NOW - datetime.timedelta(minutes=2)
        await ps.process_event(2, "screen", "d", "", screen(card_text("طالب", "555555555", 3000, fresh, "482913")))
        text, _ = self.sent()[-1]
        self.assertIn("المبلغ مختلف", text)

    async def test_late_payment_for_expired_order_still_reaches_admin(self):
        await self.make_order(status="expired")
        DB.tables["payment_receipts"].append({"id": 99, "tx_id": "000000001", "status": "baseline"})
        fresh = NOW - datetime.timedelta(minutes=2)
        await ps.process_event(2, "screen", "d", "", screen(card_text("طالب", "666666666", 5000, fresh, "482913")))
        text, _ = self.sent()[-1]
        self.assertIn("منتهي الصلاحية", text)

    async def test_no_code_falls_back_to_amount_candidates_and_ignore_button(self):
        order = await self.make_order()
        DB.tables["payment_receipts"].append({"id": 99, "tx_id": "000000001", "status": "baseline"})
        fresh = NOW - datetime.timedelta(minutes=2)
        await ps.process_event(2, "screen", "d", "", screen(card_text("طالب", "777777777", 5000, fresh)))
        _, markup = self.sent()[-1]
        data = self.buttons(markup)
        self.assertTrue(any(d.startswith(f"pay:ok:{order['id']}:") for d in data))
        self.assertTrue(any(d.startswith("pay:ig:") for d in data))

    async def test_unmatched_card_only_offers_ignore(self):
        DB.tables["payment_receipts"].append({"id": 99, "tx_id": "000000001", "status": "baseline"})
        fresh = NOW - datetime.timedelta(minutes=2)
        await ps.process_event(2, "screen", "d", "", screen(card_text("x", "888888888", 42, fresh)))
        _, markup = self.sent()[-1]
        self.assertEqual([d.split(":")[1] for d in self.buttons(markup)], ["ig"])

    async def test_duplicate_tx_is_not_notified_twice(self):
        DB.tables["payment_receipts"].append({"id": 99, "tx_id": "000000001", "status": "baseline"})
        fresh = NOW - datetime.timedelta(minutes=2)
        raw = screen(card_text("x", "999999999", 42, fresh))
        await ps.process_event(3, "screen", "d", "", raw)
        await ps.process_event(4, "screen", "d", "", raw)
        self.assertEqual(len(self.sent()), 1)
        self.assertEqual(len([r for r in DB.tables["payment_receipts"] if r["tx_id"] == "999999999"]), 1)

    async def test_unrecognized_screen_alerts_admin_and_clears_orphan_notification(self):
        await DB.table("payment_events").insert({"event_type": "notification", "processed": False,
                                                 "received_at": NOW.isoformat()}).execute()
        await ps.process_event(5, "screen", "d", "حوالة", "الرئيسية\nحسابي")
        self.assertIn("شاشة غير مفهومة", self.sent()[-1][0])
        self.assertTrue(DB.tables["payment_events"][0]["processed"])

    async def test_manual_request_lists_open_orders(self):
        order = await self.make_order()
        await ps.process_event(6, "manual_request", "d", "وصل 5000", "")
        text, markup = self.sent()[-1]
        self.assertIn("معالجة يدوية", text)
        self.assertIn(f"pay:ok:{order['id']}:0", self.buttons(markup))

    async def test_internal_error_still_alerts_admin(self):
        original = ps.parse_cards
        ps.parse_cards = MagicMock(side_effect=RuntimeError("boom"))
        try:
            await ps.process_event(7, "screen", "d", "", "آخر التحويلات #123456789")
        finally:
            ps.parse_cards = original
        self.assertIn("خطأ داخلي", self.sent()[-1][0])


class EventEntryTests(Base):
    async def test_heartbeat_records_and_creates_no_event(self):
        out = await ps.handle_incoming_event({"event_type": "heartbeat", "device_id": "phone1"})
        self.assertTrue(out["ok"])
        self.assertEqual(DB.tables["payment_events"], [])
        self.assertIsNotNone(await ps.phone_last_seen())

    async def test_notification_is_recorded_unprocessed_and_event_type_inferred(self):
        out = await ps.handle_incoming_event({"notification_text": "وصلتك حوالة"})
        row = DB.tables["payment_events"][0]
        self.assertEqual((row["event_type"], row["processed"], out["ok"]), ("notification", False, True))

    async def test_screen_event_is_recorded_and_processed_in_background(self):
        spawned = []
        original = ps.spawn
        ps.spawn = lambda coro: spawned.append(coro)
        try:
            out = await ps.handle_incoming_event({"screen_data": "x"})
        finally:
            ps.spawn = original
        self.assertEqual(len(spawned), 1)
        spawned[0].close()
        self.assertEqual(DB.tables["payment_events"][0]["event_type"], "screen")
        self.assertIn("event_id", out)

    async def test_large_texts_are_truncated(self):
        await ps.handle_incoming_event({"event_type": "notification", "notification_text": "ا" * 10000})
        self.assertEqual(len(DB.tables["payment_events"][0]["notification_text"]), ps.NOTIFICATION_MAX_CHARS)


class WatchdogTests(Base):
    async def test_orphan_notification_alerts_once(self):
        old = (NOW - datetime.timedelta(minutes=10)).isoformat()
        await DB.table("payment_events").insert({"event_type": "notification", "processed": False,
                                                 "notification_text": "وصلتك 5000", "received_at": old}).execute()
        self.assertEqual(await ps.alert_orphan_notifications(), 1)
        self.assertEqual(await ps.alert_orphan_notifications(), 0)
        self.assertIn("لم يصل حدث السجل", self.sent()[-1][0])

    async def test_fresh_notification_is_not_orphan_yet(self):
        await DB.table("payment_events").insert({"event_type": "notification", "processed": False,
                                                 "received_at": NOW.isoformat()}).execute()
        self.assertEqual(await ps.alert_orphan_notifications(), 0)

    async def test_expire_only_pending_past_deadline(self):
        past = (NOW - datetime.timedelta(minutes=1)).isoformat()
        DB.tables["payment_orders"] += [
            {"id": 1, "user_id": 1, "status": "pending", "expires_at": past, "code": "111111"},
            {"id": 2, "user_id": 2, "status": "awaiting_verification", "expires_at": past, "code": "222222"},
            {"id": 3, "user_id": 3, "status": "pending", "expires_at": (NOW + datetime.timedelta(hours=1)).isoformat(), "code": "333333"},
        ]
        expired = await ps.expire_stale_orders()
        self.assertEqual([o["id"] for o in expired], [1])
        self.assertEqual({o["id"]: o["status"] for o in DB.tables["payment_orders"]},
                         {1: "expired", 2: "awaiting_verification", 3: "pending"})


class OrderTests(Base):
    PKG = {"id": 1, "points": 100, "price": 5000, "currency": "SYP"}

    async def test_create_order_unique_per_user(self):
        order = await ps.create_order(5, self.PKG)
        self.assertEqual(len(order["code"]), 6)
        with self.assertRaises(ps.OpenOrderExists):
            await ps.create_order(5, self.PKG)

    async def test_claim_only_by_owner_and_only_once(self):
        order = await ps.create_order(5, self.PKG)
        self.assertIsNone(await ps.claim_order(order["id"], 6))
        self.assertIsNotNone(await ps.claim_order(order["id"], 5))
        self.assertIsNone(await ps.claim_order(order["id"], 5))

    async def test_cannot_cancel_after_claim(self):
        order = await ps.create_order(5, self.PKG)
        await ps.claim_order(order["id"], 5)
        self.assertFalse(await ps.cancel_order(order["id"], 5))

    async def test_notify_claim_posts_card_and_stores_message_ref(self):
        DB.tables["users"].append({"user_id": 5, "first_name": "طالب"})
        order = await ps.create_order(5, self.PKG)
        claimed = await ps.claim_order(order["id"], 5)
        await ps.notify_claim(claimed)
        self.assertEqual(DB.tables["payment_orders"][0]["group_message_id"], 77)
        self.assertIn(order["code"], self.sent()[-1][0])
        self.assertEqual(BOT.send_message.call_args_list[0].args[0], -1004031122773)

    async def test_admin_message_falls_back_to_private_chat_when_group_fails(self):
        BOT.send_message.side_effect = [Exception("chat not found"), types.SimpleNamespace(chat=types.SimpleNamespace(id=111), message_id=1)]
        try:
            msg = await ps.send_admin_message("x")
        finally:
            BOT.send_message.side_effect = None
        self.assertEqual(msg.chat.id, 111)


# ---------------- الـ endpoint (دوال مساعدة) ----------------
class WebhookHelperTests(unittest.TestCase):
    def env(self, secret="s3cret", nxt=""):
        return load_functions("webhook_server.py", ["_payment_secret_ok", "_parse_payment_body", "_apply_payment_query"],
                              hmac=__import__("hmac"), PAYMENT_WEBHOOK_SECRET=secret, PAYMENT_WEBHOOK_SECRET_NEXT=nxt,
                              Dict=dict, Any=object, Request=object)

    def req(self, **headers):
        return types.SimpleNamespace(headers={k.lower().replace("_", "-"): v for k, v in headers.items()})

    def test_secret_header_accepted_and_wrong_rejected(self):
        ok = self.env()["_payment_secret_ok"]
        self.assertTrue(ok(self.req(x_payment_secret="s3cret")))
        self.assertFalse(ok(self.req(x_payment_secret="nope")))
        self.assertFalse(ok(self.req()))

    def test_bearer_and_rotation_secret(self):
        ok = self.env(nxt="newer")["_payment_secret_ok"]
        self.assertTrue(ok(self.req(authorization="Bearer newer")))
        self.assertTrue(ok(self.req(authorization="Bearer s3cret")))

    def test_empty_configured_secret_never_matches_empty_header(self):
        ok = self.env(secret="", nxt="")["_payment_secret_ok"]
        self.assertFalse(ok(self.req(x_payment_secret="")))
        self.assertFalse(ok(self.req(x_payment_secret="anything")))

    def test_body_json_form_and_raw(self):
        parse = self.env()["_parse_payment_body"]
        self.assertEqual(parse(b'{"event_type":"notification","notification_text":"x"}', "application/json")["event_type"], "notification")
        self.assertEqual(parse("event_type=screen&screen_data=a%0Ab".encode(), "application/x-www-form-urlencoded")["screen_data"], "a\nb")
        raw = parse("سطر1\nسطر2 = بدون json".encode(), "text/plain")
        self.assertEqual((raw["event_type"], raw["malformed_body"]), ("screen", True))

    def test_text_plain_body_is_raw_even_with_equals_and_ampersand(self):
        env = self.env()
        out = env["_parse_payment_body"]("وصلت حوالة a=b&c=d بقيمة 5".encode(), "text/plain; charset=utf-8")
        self.assertEqual(out["raw_text"], "وصلت حوالة a=b&c=d بقيمة 5")

    def test_query_params_route_raw_body_by_event_type(self):
        env = self.env()
        apply, parse = env["_apply_payment_query"], env["_parse_payment_body"]
        note = apply(parse("نص الإشعار".encode(), "text/plain"), {"event_type": "notification", "device_id": "phone1"})
        self.assertEqual((note["event_type"], note["notification_text"], note["device_id"]),
                         ("notification", "نص الإشعار", "phone1"))
        self.assertNotIn("screen_data", note)
        man = apply(parse("x".encode(), "text/plain"), {"event_type": "manual_request"})
        self.assertEqual(man["notification_text"], "x")
        scr = apply(parse("آخر التحويلات".encode(), "text/plain"), {"event_type": "SCREEN"})
        self.assertEqual((scr["event_type"], scr["screen_data"]), ("screen", "آخر التحويلات"))

    def test_query_does_not_override_json_fields_except_event_type_and_keeps_device(self):
        env = self.env()
        out = env["_apply_payment_query"]({"event_type": "screen", "device_id": "own", "screen_data": "s"},
                                          {"event_type": "heartbeat", "device_id": "other"})
        self.assertEqual((out["event_type"], out["device_id"], out["screen_data"]), ("heartbeat", "own", "s"))

    def test_no_query_leaves_payload_untouched(self):
        env = self.env()
        payload = {"event_type": "screen", "screen_data": "s"}
        self.assertEqual(env["_apply_payment_query"](dict(payload), {}), payload)

    def test_broken_json_from_unescaped_quotes_is_not_rejected(self):
        parse = self.env()["_parse_payment_body"]
        broken = '{"event_type":"screen","screen_data":"اسم "مقتبس" هنا"}'.encode()
        out = parse(broken, "application/json")
        self.assertEqual(out["event_type"], "screen")
        self.assertIn("مقتبس", out["screen_data"])


# ---------------- أزرار الأدمن ----------------
class AdminHandlerTests(unittest.IsolatedAsyncioTestCase):
    def load(self, **overrides):
        ps_stub = types.SimpleNamespace(
            is_payment_admin=lambda uid: uid == 111,
            credit_order=AsyncMock(), get_order=AsyncMock(return_value=None),
            reject_order=AsyncMock(), dismiss_receipt=AsyncMock())
        ps_stub.__dict__.update(overrides)
        notify = AsyncMock()
        env = load_functions(
            "handlers/payments.py", ["admin_confirm", "admin_reject", "admin_ignore", "_finish_group_message",
                                      "_strip_order_message_buttons"],
            ps=ps_stub, html=__import__("html"), send_points_notification=notify, spawn=lambda c: c.close(),
            log_usage_event=AsyncMock(), logger=MagicMock(), TelegramBadRequest=Exception,
            ADMIN_CONTACT="@abiadd", Optional=object, types=types.SimpleNamespace(CallbackQuery=object, Message=object))
        return env, ps_stub, notify

    def call(self, data, uid=111):
        msg = types.SimpleNamespace(html_text="نص", edit_text=AsyncMock(), message_id=1,
                                    chat=types.SimpleNamespace(id=-100), bot=MagicMock(edit_message_reply_markup=AsyncMock()))
        user = types.SimpleNamespace(id=uid, full_name="Admin")
        return types.SimpleNamespace(data=data, from_user=user, message=msg, answer=AsyncMock(),
                                     bot=MagicMock(send_message=AsyncMock()))

    async def test_non_admin_cannot_credit(self):
        env, ps_stub, notify = self.load()
        call = self.call("pay:ok:1:2", uid=999)
        await env["admin_confirm"](call)
        ps_stub.credit_order.assert_not_awaited()
        notify.assert_not_awaited()
        self.assertTrue(call.answer.await_args.kwargs.get("show_alert"))

    async def test_confirm_credits_and_notifies_student(self):
        env, ps_stub, notify = self.load()
        ps_stub.credit_order.return_value = {"result": "ok", "order_user_id": 5, "credited_points": 100, "new_total_points": 150.0}
        call = self.call("pay:ok:7:9")
        await env["admin_confirm"](call)
        ps_stub.credit_order.assert_awaited_once_with(7, 9, "tg:111")
        notify.assert_awaited_once_with(5, 100, 150)

    async def test_zero_receipt_id_means_no_receipt(self):
        env, ps_stub, _ = self.load()
        ps_stub.credit_order.return_value = {"result": "not_found"}
        await env["admin_confirm"](self.call("pay:ok:7:0"))
        ps_stub.credit_order.assert_awaited_once_with(7, None, "tg:111")

    async def test_already_paid_never_notifies_again(self):
        env, ps_stub, notify = self.load()
        ps_stub.credit_order.return_value = {"result": "already_paid", "order_user_id": 5, "credited_points": 100}
        await env["admin_confirm"](self.call("pay:ok:7:9"))
        notify.assert_not_awaited()

    async def test_rpc_failure_adds_nothing_and_alerts(self):
        env, ps_stub, notify = self.load()
        ps_stub.credit_order.side_effect = RuntimeError("db down")
        call = self.call("pay:ok:7:9")
        await env["admin_confirm"](call)
        notify.assert_not_awaited()
        self.assertTrue(call.answer.await_args.kwargs.get("show_alert"))

    async def test_reject_notifies_student_only_when_state_allowed(self):
        env, ps_stub, _ = self.load()
        ps_stub.reject_order.return_value = None
        call = self.call("pay:no:7")
        await env["admin_reject"](call)
        call.bot.send_message.assert_not_awaited()
        ps_stub.reject_order.return_value = {"id": 7, "user_id": 5, "code": "482913"}
        call2 = self.call("pay:no:7")
        await env["admin_reject"](call2)
        call2.bot.send_message.assert_awaited_once()

    async def test_ignore_requires_admin(self):
        env, ps_stub, _ = self.load()
        await env["admin_ignore"](self.call("pay:ig:3", uid=5))
        ps_stub.dismiss_receipt.assert_not_awaited()
        await env["admin_ignore"](self.call("pay:ig:3"))
        ps_stub.dismiss_receipt.assert_awaited_once_with(3)


if __name__ == "__main__":
    unittest.main()
