import sys
import unittest
from unittest.mock import patch

from services.generation_eta import (
    GenerationEta,
    build_processing_text,
    estimate_generation_eta,
    format_elapsed,
    format_remaining,
)

IMAGES = {"input_type": "media", "content_source": "images", "subject_type": "other"}
PDF = {"input_type": "media", "content_source": "pdf", "subject_type": "other"}
TEXT = {"input_type": "text", "subject_type": "other"}


def eta(data, items, questions, mode="Standard", is_album=False):
    return estimate_generation_eta(data, items, questions, mode, is_album)


class EstimateTests(unittest.TestCase):
    def test_single_image_matches_observed_bands(self):
        self.assertLessEqual(eta(IMAGES, 1, 5).typical, 30)
        self.assertTrue(30 <= eta(IMAGES, 1, 10).typical <= 45)
        self.assertGreater(eta(IMAGES, 1, 20, "Over-Limit").typical, eta(IMAGES, 1, 10).typical)

    def test_more_questions_never_shorten_the_estimate(self):
        for source, mode in ((IMAGES, "Standard"), (PDF, "Standard"), (TEXT, "Over-Limit")):
            previous = 0
            for questions in (5, 10, 15, 20, 30):
                typical = eta(source, 3, questions, mode).typical
                self.assertGreaterEqual(typical, previous, (source, questions))
                previous = typical

    def test_each_extra_image_adds_time(self):
        album = {**IMAGES, "is_album": True}
        self.assertGreater(eta(album, 7, 10, "Standard", True).typical, eta(IMAGES, 1, 10).typical)

    def test_short_plain_text_uses_the_fast_path(self):
        self.assertLessEqual(eta(TEXT, 1, 8).typical, 15)
        self.assertGreater(eta(TEXT, 1, 20, "Over-Limit").typical, 40)

    def test_math_text_does_not_use_the_fast_path(self):
        math = {**TEXT, "subject_type": "math"}
        self.assertGreater(eta(math, 1, 8).typical, eta(TEXT, 1, 8).typical)

    def test_ready_math_exam_is_slower_than_a_ready_general_exam(self):
        general = {**PDF, "subject_type": "quiz_solved"}
        math = {**general, "content_subject_type": "math"}
        self.assertGreater(eta(math, 5, 20).typical, eta(general, 5, 20).typical)

    def test_similar_math_generation_is_not_treated_as_extraction(self):
        similar = {**PDF, "subject_type": "quiz_solved", "content_subject_type": "math",
                   "question_type": "similar_math"}
        extraction = {**PDF, "subject_type": "quiz_solved", "content_subject_type": "math"}
        self.assertNotEqual(eta(similar, 5, 20).typical, eta(extraction, 5, 20).typical)

    def test_cached_office_text_counts_as_text_source(self):
        office = {"input_type": "media", "content_source": "docx", "subject_type": "other",
                  "cached_office_text": "some extracted text"}
        self.assertLessEqual(eta(office, 2, 8).typical, 15)

    def test_ordering_invariant_holds_for_many_inputs(self):
        sources = (IMAGES, PDF, TEXT, {**TEXT, "subject_type": "math"},
                   {**PDF, "subject_type": "quiz_solved"})
        for source in sources:
            for items in (1, 2, 5, 10, 40, 150):
                for questions in (1, 5, 10, 20, 30, 60, 120):
                    for mode in ("Standard", "Over-Limit", "Super-PDF", "Super-Text", "Super-Images"):
                        result = eta(source, items, questions, mode)
                        self.assertTrue(5 <= result.typical <= result.slow <= result.ceiling,
                                        (source, items, questions, mode, result))

    def test_bad_inputs_do_not_raise(self):
        result = estimate_generation_eta({}, None, 0, "Standard")
        self.assertIsInstance(result, GenerationEta)

    def test_ceiling_falls_back_when_time_budget_is_unavailable(self):
        with patch.dict(sys.modules, {"services.ai.time_budget": None}):
            result = eta(IMAGES, 1, 10)
        self.assertEqual(result.ceiling, 600)
        self.assertLess(result.typical, result.slow)
        self.assertLessEqual(result.slow, result.ceiling)


class WordingTests(unittest.TestCase):
    ETA = GenerationEta(typical=40, slow=100, ceiling=240)

    def test_before_typical_time_shows_countdown_and_bar(self):
        text = build_processing_text(10, self.ETA)
        self.assertIn("المتبقي تقريباً", text)
        self.assertIn("▰", text)
        self.assertIn("▱", text)
        self.assertIn("دقيقتين", text)  # slow=100s is shown as the upper bound

    def test_bar_never_completes_before_the_work_is_done(self):
        self.assertIn("▱", build_processing_text(39, self.ETA))

    def test_after_typical_time_there_is_no_negative_countdown(self):
        text = build_processing_text(55, self.ETA)
        self.assertIn("أكتر من المعتاد", text)
        self.assertNotIn("المتبقي", text)
        self.assertNotIn("-", text)

    def test_long_delay_mentions_refund_deadline(self):
        text = build_processing_text(150, self.ETA)
        self.assertIn("4 دقائق", text)
        self.assertIn("نقاطك", text)

    def test_without_estimate_the_old_generic_wording_is_used(self):
        text = build_processing_text(65, None)
        self.assertIn("لسا عم نعالج المستند", text)
        self.assertIn("دقيقة و5 ثواني", text)

    def test_negative_elapsed_is_clamped(self):
        self.assertIn("المتبقي", build_processing_text(-5, self.ETA))

    def test_text_changes_as_time_passes(self):
        self.assertNotEqual(build_processing_text(5, self.ETA), build_processing_text(25, self.ETA))
        self.assertNotEqual(build_processing_text(60, self.ETA), build_processing_text(75, self.ETA))

    def test_arabic_number_wording(self):
        self.assertEqual(format_elapsed(1), "ثانية")
        self.assertEqual(format_elapsed(2), "ثانيتين")
        self.assertEqual(format_elapsed(7), "7 ثواني")
        self.assertEqual(format_elapsed(45), "45 ثانية")
        self.assertEqual(format_elapsed(60), "دقيقة")
        self.assertEqual(format_elapsed(120), "دقيقتين")
        self.assertEqual(format_elapsed(125), "دقيقتين و5 ثواني")
        self.assertEqual(format_remaining(5), "أقل من 10 ثواني")
        self.assertEqual(format_remaining(33), "حوالي 35 ثانية")
        self.assertEqual(format_remaining(61), "حوالي دقيقتين")


if __name__ == "__main__":
    unittest.main()
