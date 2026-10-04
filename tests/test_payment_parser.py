import json
import sys
import unittest
from datetime import timedelta
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from services.payment_parser import (  # noqa: E402
    CODE_LENGTH, SYRIA_TZ, find_codes_in_note, generate_code, looks_like_transfers_screen,
    auto_confirm_verdict, clean_screen_text, make_unique_code, normalize_digits, parse_cards,
)
from datetime import datetime, timezone  # noqa: E402

# أشكال حقيقية من لقطات شاشة MacroDroid (screen_data)
CARD_OUT_100 = "Syriatel Direct Payment\n\n#461226096\n100\nل.س\n2026/09/18 - 21:45:48"
CARD_IN_250 = "زكي نعيم جولجي\n\n#456076029\n250\nل.س\n2026/09/16 - 16:18:24"
CARD_OUT_WITH_NOTE = "ضحى طالب حريري\n\n#413833019\n5\nل.س\n2026/08/28 - 04:45:33\n8771"


class NormalizeTests(unittest.TestCase):
    def test_arabic_indic_digits(self):
        self.assertEqual(normalize_digits("٤٨٢٩١٣ abc ۱۲۳"), "482913 abc 123")

    def test_code_generation(self):
        for _ in range(200):
            code = generate_code()
            self.assertEqual(len(code), CODE_LENGTH)
            self.assertTrue(code.isdigit())
            self.assertNotEqual(code[0], "0")

    def test_unique_code_avoids_existing(self):
        taken = {generate_code() for _ in range(50)}
        self.assertNotIn(make_unique_code(taken), taken)

    def test_find_codes_in_note(self):
        self.assertEqual(find_codes_in_note("شحن 482913"), ["482913"])
        self.assertEqual(find_codes_in_note("٤٨٢٩١٣"), ["482913"])
        self.assertEqual(find_codes_in_note("8771"), [])  # 4 خانات
        self.assertEqual(find_codes_in_note("#413833019"), [])  # رقم عملية 9 خانات
        self.assertEqual(find_codes_in_note(None), [])
        self.assertEqual(find_codes_in_note("[123456$]: 482913"), ["482913"])


class ParseCardsTests(unittest.TestCase):
    def test_macrodroid_prefix_on_counterparty(self):
        for prefix in ("[5$]: ", "[12$] ", "[٥$]: "):
            with self.subTest(prefix=prefix):
                (card,) = parse_cards(prefix + CARD_OUT_WITH_NOTE)
                self.assertEqual(card.counterparty, "ضحى طالب حريري")
                self.assertEqual(card.amount, Decimal("5"))
                self.assertEqual(card.currency, "SYP")
                self.assertEqual(card.note, "8771")

    def test_indexed_lines_keep_cards_and_codes_separate(self):
        raw = "آخر التحويلات\n" + CARD_IN_250 + "\n482913\n" + CARD_OUT_WITH_NOTE
        indexed = "\n".join(f"[{i}$]: {line}" for i, line in enumerate(raw.splitlines()))
        for screen in (indexed, json.dumps([indexed])):
            with self.subTest(screen=screen):
                cards = parse_cards(screen)
                self.assertEqual([c.counterparty for c in cards], ["زكي نعيم جولجي", "ضحى طالب حريري"])
                self.assertEqual([c.amount for c in cards], [Decimal("250"), Decimal("5")])
                self.assertEqual([c.note for c in cards], ["482913", "8771"])
                self.assertEqual(find_codes_in_note(cards[0].note), ["482913"])

    def test_cleanup_preserves_actual_text_and_currency(self):
        for text in ("5$", "$5", "[VIP] أحمد", "أحمد [5$]", "شحن 482913", "65 ل.س"):
            self.assertEqual(clean_screen_text(text), text)
        self.assertEqual(clean_screen_text("[5$]: <أحمد & علي>"), "<أحمد & علي>")
        self.assertEqual(clean_screen_text(None), "")

    def test_single_card_json_item(self):
        (card,) = parse_cards(json.dumps([CARD_OUT_100]))
        self.assertEqual(card.tx_id, "461226096")
        self.assertEqual(card.counterparty, "Syriatel Direct Payment")
        self.assertEqual(card.amount, Decimal("100"))
        self.assertEqual(card.currency, "SYP")
        self.assertEqual(card.tx_time.tzinfo, SYRIA_TZ)
        self.assertEqual((card.tx_time.year, card.tx_time.month, card.tx_time.day), (2026, 9, 18))
        self.assertEqual(card.note, "")

    def test_note_is_last_line_after_date(self):
        (card,) = parse_cards(json.dumps([CARD_OUT_WITH_NOTE]))
        self.assertEqual(card.tx_id, "413833019")
        self.assertEqual(card.note, "8771")
        self.assertEqual(card.amount, Decimal("5"))

    def test_json_list_of_cards_keeps_notes_separate(self):
        cards = parse_cards(json.dumps([CARD_OUT_100, CARD_IN_250, CARD_OUT_WITH_NOTE]))
        self.assertEqual([c.tx_id for c in cards], ["461226096", "456076029", "413833019"])
        self.assertEqual([c.note for c in cards], ["", "", "8771"])
        self.assertEqual(cards[1].counterparty, "زكي نعيم جولجي")

    def test_plain_text_with_ui_noise(self):
        screen = "\n".join([
            "مساء الخير", "1", "آخر التحويلات", "متقدم",
            CARD_OUT_100, CARD_IN_250, CARD_OUT_WITH_NOTE,
            "الرئيسية", "التحويلات", "الخدمات", "حسابي",
        ])
        cards = parse_cards(screen)
        self.assertEqual([c.tx_id for c in cards], ["461226096", "456076029", "413833019"])
        self.assertEqual(cards[0].counterparty, "Syriatel Direct Payment")
        self.assertEqual(cards[1].counterparty, "زكي نعيم جولجي")
        self.assertEqual(cards[2].counterparty, "ضحى طالب حريري")
        # الملاحظة تتوقف عند اسم البطاقة التالية وعند نصوص واجهة التطبيق
        self.assertEqual([c.note for c in cards], ["", "", "8771"])

    def test_note_with_code_in_plain_text(self):
        card = CARD_IN_250 + "\nشحن 482913"
        cards = parse_cards("\n".join(["آخر التحويلات", card, CARD_OUT_100]))
        self.assertEqual(find_codes_in_note(cards[0].note), ["482913"])
        self.assertEqual(cards[1].note, "")

    def test_arabic_digits_in_card(self):
        card = "أحمد\n#٤٥٦٠٧٦٠٢٩\n٢٥٠\nل.س\n٢٠٢٦/٠٩/١٦ - ١٦:١٨:٢٤\n٤٨٢٩١٣"
        (c,) = parse_cards(json.dumps([card]))
        self.assertEqual(c.tx_id, "456076029")
        self.assertEqual(c.amount, Decimal("250"))
        self.assertEqual(find_codes_in_note(c.note), ["482913"])

    def test_dedupes_repeated_tx_id(self):
        cards = parse_cards(json.dumps([CARD_OUT_100, CARD_OUT_100]))
        self.assertEqual(len(cards), 1)

    def test_garbage_and_empty(self):
        self.assertEqual(parse_cards(""), [])
        self.assertEqual(parse_cards("لا شيء مفيد هنا"), [])
        self.assertEqual(parse_cards("[not json"), [])

    def test_transfers_screen_marker(self):
        self.assertTrue(looks_like_transfers_screen("مساء الخير\nآخر التحويلات"))
        self.assertFalse(looks_like_transfers_screen("الرئيسية\nحسابي"))

    def test_tx_time_delta_is_utc_plus_3(self):
        (c,) = parse_cards(json.dumps([CARD_OUT_100]))
        self.assertEqual(c.tx_time.utcoffset(), timedelta(hours=3))


if __name__ == "__main__":
    unittest.main()


class EscapedJsonTests(unittest.TestCase):
    def test_marker_found_even_when_json_escapes_arabic(self):
        raw = json.dumps(["مساء الخير", "آخر التحويلات", CARD_OUT_100])  # ensure_ascii => \uXXXX
        self.assertNotIn("آخر التحويلات", raw)
        self.assertTrue(looks_like_transfers_screen(raw))
        self.assertEqual(parse_cards(raw)[0].tx_id, "461226096")


class AutoConfirmLogicTests(unittest.TestCase):
    """auto_confirm_verdict (منطق نقي): لا يفحص اتجاه الحوالة، يكفي كود الطلب بالملاحظة."""

    def setUp(self):
        from datetime import datetime, timezone
        self.now = datetime(2026, 10, 4, 12, 0, tzinfo=timezone.utc)
        self.order = {"id": 1, "code": "482913", "status": "pending", "amount": 5000, "currency": "SYP",
                      "created_at": "2026-10-04T11:50:00+00:00", "expires_at": "2026-10-04T12:50:00+00:00"}
        self.receipt = {"note": "482913", "amount": 5000.0, "currency": "SYP", "tx_time": "2026-10-04T11:58:00+00:00"}

    def verdict(self, order=None, **receipt_changes):
        from services.payment_parser import auto_confirm_verdict
        return auto_confirm_verdict(self.order if order is None else order, {**self.receipt, **receipt_changes}, self.now)

    def test_fully_valid_payment_passes_without_any_notification(self):
        self.assertEqual(self.verdict(), (True, ""))
        self.assertTrue(self.verdict(order={**self.order, "status": "awaiting_verification"})[0])
        self.assertTrue(self.verdict(note="[8$]: 482913")[0])

    def test_no_order_or_wrong_state(self):
        from services.payment_parser import auto_confirm_verdict
        self.assertFalse(auto_confirm_verdict(None, self.receipt, self.now)[0])
        for status in ("expired", "manual_review", "paid", "rejected", "cancelled"):
            self.assertFalse(self.verdict(order={**self.order, "status": status})[0], status)

    def test_code_must_be_single_and_equal_to_order_code(self):
        self.assertFalse(self.verdict(note="")[0])
        self.assertFalse(self.verdict(note="999999")[0])
        self.assertFalse(self.verdict(note="482913 111222")[0])

    def test_amount_and_currency_must_match_exactly(self):
        self.assertFalse(self.verdict(amount=4999.0)[0])
        self.assertFalse(self.verdict(amount=5000.5)[0])
        self.assertFalse(self.verdict(currency="USD")[0])
        self.assertFalse(self.verdict(amount=None)[0])

    def test_transfer_time_must_be_inside_order_lifetime(self):
        self.assertFalse(self.verdict(tx_time=None)[0])
        self.assertFalse(self.verdict(tx_time="2026-10-04T11:40:00+00:00")[0])   # قبل إنشاء الطلب
        self.assertFalse(self.verdict(tx_time="2026-10-04T13:30:00+00:00")[0])   # بعد الانتهاء/مستقبل
        self.assertTrue(self.verdict(tx_time="2026-10-04T11:49:00+00:00")[0])    # ضمن سماحية الدقيقتين


def _stamp(dt):
    return dt.astimezone(SYRIA_TZ).strftime("%Y/%m/%d - %H:%M:%S")


class InjectionAndFormatTests(unittest.TestCase):
    """حقن بطاقة وهمية عبر ملاحظة الحوالة + مبلغ ملتصق بالعملة."""

    def setUp(self):
        self.now = datetime.now(timezone.utc)
        self.order = {"id": 1, "code": "123456", "status": "pending", "amount": "65", "currency": "SYP",
                      "created_at": (self.now - timedelta(minutes=5)).isoformat(),
                      "expires_at": (self.now + timedelta(minutes=55)).isoformat()}

    def _verdict(self, card):
        rec = {"amount": card.amount, "currency": card.currency, "tx_time": card.tx_time, "note": card.note}
        return auto_confirm_verdict(self.order, rec, self.now)

    def test_forged_card_inside_multiline_note_is_never_first_card(self):
        forged = f"x\nفلان\n#900000001\n65\nل.س\n{_stamp(self.now)}\n123456"
        screen = f"آخر التحويلات\nمهاجم\n\n#100000009\n1\nل.س\n{_stamp(self.now)}\n{forged}"
        cards = parse_cards(screen)
        self.assertEqual(cards[0].tx_id, "100000009")      # الحوالة الحقيقية هي الأولى دائماً
        self.assertEqual(cards[0].amount, Decimal("1"))
        self.assertFalse(self._verdict(cards[0])[0])        # 1 ل.س لا تطابق طلب 65

    def test_exotic_line_separators_do_not_create_cards(self):
        for sep in ("\u2028", "\u2029", "\x85", "\x0b", "\x0c", "\x1c"):
            with self.subTest(sep=repr(sep)):
                note = sep.join(["x", "فلان", "#900000001", "65", "ل.س", _stamp(self.now), "123456"])
                screen = f"آخر التحويلات\nمهاجم\n#100000009\n1\nل.س\n{_stamp(self.now)}\n{note}"
                cards = parse_cards(screen)
                self.assertEqual([c.tx_id for c in cards], ["100000009"])
                self.assertFalse(self._verdict(cards[0])[0])

    def test_tx_marker_must_be_its_own_line(self):
        cards = parse_cards("آخر التحويلات\nاسم #123456789 ملحق\n55\nل.س\n2026/09/01 - 10:00:00")
        self.assertEqual(cards, [])

    def test_amount_and_currency_on_same_line(self):
        screen = f"آخر التحويلات\nأحمد\n#100000001\n65 ل.س\n{_stamp(self.now)}\n123456"
        (card,) = parse_cards(screen)
        self.assertEqual((card.amount, card.currency, card.note), (Decimal("65"), "SYP", "123456"))
        self.assertTrue(self._verdict(card)[0])

    def test_currency_only_line_still_has_no_amount(self):
        (card,) = parse_cards("أحمد\n#100000001\nل.س\n2026/09/01 - 10:00:00")
        self.assertIsNone(card.amount)

    def test_old_receipt_is_refused_only_when_max_age_given(self):
        old = self.now - timedelta(minutes=30)
        self.order["created_at"] = (old - timedelta(minutes=5)).isoformat()
        self.order["expires_at"] = (self.now + timedelta(minutes=55)).isoformat()
        rec = {"amount": "65", "currency": "SYP", "tx_time": old, "note": "123456"}
        self.assertTrue(auto_confirm_verdict(self.order, rec, self.now)[0])
        ok, reason = auto_confirm_verdict(self.order, rec, self.now, max_age_minutes=15)
        self.assertFalse(ok)
        self.assertIn("قديمة", reason)
