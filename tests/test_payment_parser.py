import json
import sys
import unittest
from datetime import timedelta
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from services.payment_parser import (  # noqa: E402
    CODE_LENGTH, SYRIA_TZ, find_codes_in_note, generate_code, looks_like_transfers_screen,
    clean_screen_text, make_unique_code, normalize_digits, parse_cards,
)

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
