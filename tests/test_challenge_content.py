"""اختبارات منطق محتوى التحديات النقي (بلا قاعدة بيانات ولا Telegram)."""
import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from services.challenge_content import (  # noqa: E402
    ChallengeContentError, parse_challenge_json, from_existing_quiz, content_hash,
    strip_answers, paginate, MAX_FILE_BYTES, MAX_QUESTIONS,
)


def q(text="س", options=("أ", "ب"), correct=0, **extra):
    d = {"question": text, "options": list(options), "correct_option_id": correct}
    d.update(extra)
    return d


def jb(obj):
    return json.dumps(obj, ensure_ascii=False).encode("utf-8")


class ParseJsonTests(unittest.TestCase):
    def test_valid_object_with_variable_question_count(self):
        p = parse_challenge_json(jb({"title": "T", "questions": [q(), q("س2", ("أ", "ب", "ج"), 2, explanation=" شرح ")]}))
        self.assertEqual(p.question_count, 2)  # لا افتراض 10
        self.assertEqual(p.questions[1]["explanation"], "شرح")
        self.assertEqual(p.source_type, "json_upload")

    def test_bare_list_uses_default_title(self):
        p = parse_challenge_json(jb([q()]), default_title="ملف")
        self.assertEqual(p.title, "ملف")

    def test_corrupt_json(self):
        with self.assertRaises(ChallengeContentError) as c:
            parse_challenge_json(b'{"title": "x", "questions": [')
        self.assertIn("JSON", str(c.exception))

    def test_not_utf8(self):
        with self.assertRaises(ChallengeContentError):
            parse_challenge_json("س".encode("cp1256") + b"\xff")

    def test_question_without_correct_answer(self):
        bad = {"question": "س", "options": ["أ", "ب"]}
        with self.assertRaises(ChallengeContentError) as c:
            parse_challenge_json(jb({"title": "T", "questions": [q(), bad]}))
        self.assertIn("السؤال 2", str(c.exception))
        self.assertIn("correct_option_id", str(c.exception))

    def test_correct_answer_out_of_range_or_wrong_type(self):
        for bad in (2, -1, "0", True, 1.5, None):
            with self.assertRaises(ChallengeContentError, msg=repr(bad)):
                parse_challenge_json(jb({"title": "T", "questions": [q(correct=bad)]}))

    def test_missing_options_or_too_few_or_too_many(self):
        for opts in (None, ["أ"], list("abcdefghijk"), "أ,ب", ["أ", ""], ["أ", "أ"]):
            with self.assertRaises(ChallengeContentError, msg=repr(opts)):
                parse_challenge_json(jb({"title": "T", "questions": [{"question": "س", "options": opts, "correct_option_id": 0}]}))

    def test_missing_title_and_empty_questions(self):
        with self.assertRaises(ChallengeContentError):
            parse_challenge_json(jb({"questions": [q()]}))
        with self.assertRaises(ChallengeContentError):
            parse_challenge_json(jb({"title": "T", "questions": []}))

    def test_limits(self):
        with self.assertRaises(ChallengeContentError):
            parse_challenge_json(b" " * (MAX_FILE_BYTES + 1))
        with self.assertRaises(ChallengeContentError):
            parse_challenge_json(jb({"title": "T", "questions": [q(str(i)) for i in range(MAX_QUESTIONS + 1)]}))

    def test_errors_are_capped_and_counted(self):
        items = [{"question": "", "options": [], "correct_option_id": 0} for _ in range(12)]
        with self.assertRaises(ChallengeContentError) as c:
            parse_challenge_json(jb({"title": "T", "questions": items}))
        self.assertIn("أخطاء أخرى", str(c.exception))
        shown = [l for l in str(c.exception).splitlines() if l.startswith("السؤال ")]
        self.assertEqual(len(shown), 5)

    def test_image_url_only_https_kept_in_metadata(self):
        p = parse_challenge_json(jb({"title": "T", "questions": [q(image_url="https://x/y.png"), q("2", image_url="javascript:1")]}))
        self.assertEqual(p.questions[0]["metadata"], {"image_url": "https://x/y.png"})
        self.assertNotIn("metadata", p.questions[1])


class HashTests(unittest.TestCase):
    def test_same_content_same_hash_regardless_of_title_and_explanation(self):
        a = parse_challenge_json(jb({"title": "A", "questions": [q(explanation="x")]}))
        b = parse_challenge_json(jb({"title": "B", "questions": [q()]}))
        self.assertEqual(a.content_hash, b.content_hash)

    def test_different_answer_or_order_changes_hash(self):
        base = [q("1"), q("2")]
        self.assertNotEqual(content_hash(base), content_hash([q("1"), q("2", correct=1)]))
        self.assertNotEqual(content_hash(base), content_hash(list(reversed(base))))


class ExistingQuizTests(unittest.TestCase):
    ROW = {"id": "3f6c7c0e-0000-4000-8000-000000000001", "creator_id": 777, "share_code": "ABC", "likes": 9,
           "source_title": "كويز قديم", "is_math_quiz": False, "subject_type": "physics", "difficulty": "hard",
           "quiz_data": [q("س", ("أ", "ب", "ج"), 2, explanation="ش")]}

    def test_snapshot_copies_content_only(self):
        p = from_existing_quiz(self.ROW)
        self.assertEqual(p.source_type, "existing_abiad_quiz")
        self.assertEqual(p.source_reference, self.ROW["id"])
        blob = json.dumps({"q": p.questions, "m": p.metadata, "t": p.title}, ensure_ascii=False)
        for forbidden in ("777", "ABC", "creator"):
            self.assertNotIn(forbidden, blob)

    def test_snapshot_is_independent_copy(self):
        row = json.loads(json.dumps(self.ROW))
        p = from_existing_quiz(row)
        row["quiz_data"][0]["question"] = "تغيّر"
        row["quiz_data"].clear()
        self.assertEqual(p.questions[0]["question"], "س")

    def test_quiz_data_as_json_string_and_corrupt(self):
        row = dict(self.ROW, quiz_data=json.dumps(self.ROW["quiz_data"], ensure_ascii=False))
        self.assertEqual(from_existing_quiz(row).question_count, 1)
        with self.assertRaises(ChallengeContentError):
            from_existing_quiz(dict(self.ROW, quiz_data="{bad"))


class HelpersTests(unittest.TestCase):
    def test_strip_answers(self):
        row = {"id": "1", "question_text": "س", "options": ["أ", "ب"], "correct_option_id": 1, "explanation": "ش"}
        out = strip_answers(row)
        self.assertNotIn("correct_option_id", out)
        self.assertNotIn("explanation", out)
        self.assertEqual(out["options"], ["أ", "ب"])

    def test_paginate(self):
        items = list(range(20))
        self.assertEqual(paginate(items, 0, 8), (items[:8], 0, 3))
        self.assertEqual(paginate(items, 99, 8), (items[16:], 2, 3))
        self.assertEqual(paginate([], 5, 8), ([], 0, 1))


if __name__ == "__main__":
    unittest.main()
