"""
tests/test_study_schedule.py
==============================================================================
اختبارات شاملة لميزة الجداول الدراسية وتنظيم الوقت:
- التحقق من تكامل البيانات والخطة السنوية والأسبوعية لكل فئة (علمي/أدبي/جامعي).
- التحقق من صحة لوحات المفاتيح والأزرار التفاعلية.
- التحقق من توليد الصور فائقة الدقة بصيغة PNG وملفات الـ PDF لجميع السمات الأربعة.
- التحقق من تسجيل معالجات الـ Router بدون أي تعارض.
==============================================================================
"""

import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

# متغيرات بيئة وهمية للمرور من فحوصات التهيئة
os.environ.setdefault("BOT_TOKEN", "123456:TEST_TOKEN_FOR_STUDY_SCHEDULE")
os.environ.setdefault("ADMIN_ID", "1")

from services.study_schedule_data import (
    CATEGORIES,
    CATEGORY_BAC_SCI,
    CATEGORY_BAC_LIT,
    CATEGORY_UNIV,
    UNIVERSITY_TRACKS,
    TRACK_MEDICAL,
    TRACK_ENGINEERING,
    TRACK_HUMANITIES,
    TRACK_GENERAL,
    THEMES,
    DEFAULT_THEME,
    BAC_SCI_WEEKLY,
    BAC_SCI_ANNUAL,
    BAC_LIT_WEEKLY,
    BAC_LIT_ANNUAL,
    UNIV_ANNUAL,
    get_univ_weekly,
)
from services.study_schedule_renderer import (
    render_weekly_schedule,
    render_annual_schedule,
    has_arabic,
    _shape,
)
from keyboards import (
    get_study_schedule_menu_keyboard,
    get_study_schedule_categories_keyboard,
    get_study_schedule_university_tracks_keyboard,
    get_study_schedule_theme_keyboard,
    get_study_schedule_result_keyboard,
)
from handlers.study_schedule import router as study_schedule_router


class TestStudyScheduleData(unittest.TestCase):
    """اختبارات تكامل البيانات والخطط الدراسية للمناهج السورية والجامعات."""

    def test_categories_presence(self):
        self.assertIn(CATEGORY_BAC_SCI, CATEGORIES)
        self.assertIn(CATEGORY_BAC_LIT, CATEGORIES)
        self.assertIn(CATEGORY_UNIV, CATEGORIES)
        self.assertEqual(CATEGORIES[CATEGORY_BAC_SCI]["total_marks"], 2900)
        self.assertEqual(CATEGORIES[CATEGORY_BAC_LIT]["total_marks"], 2800)

    def test_university_tracks(self):
        self.assertIn(TRACK_MEDICAL, UNIVERSITY_TRACKS)
        self.assertIn(TRACK_ENGINEERING, UNIVERSITY_TRACKS)
        self.assertIn(TRACK_HUMANITIES, UNIVERSITY_TRACKS)
        self.assertIn(TRACK_GENERAL, UNIVERSITY_TRACKS)

    def test_themes_configuration(self):
        for theme_id in ("minimalist", "academic", "pastel", "dark"):
            self.assertIn(theme_id, THEMES)
            t = THEMES[theme_id]
            for field in ("bg", "card_bg", "card_border", "header_bg", "header_text", "primary", "text"):
                self.assertIn(field, t)
                self.assertTrue(t[field].startswith("#"), f"Theme {theme_id} field {field} must be hex")

    def test_bac_sci_weekly_data(self):
        self.assertEqual(len(BAC_SCI_WEEKLY["days"]), 7)
        for day in BAC_SCI_WEEKLY["days"]:
            self.assertIn("day", day)
            self.assertIn("morning", day)
            self.assertIn("afternoon", day)
            self.assertIn("evening", day)
            self.assertGreater(len(day.get("checkboxes", [])), 0)

    def test_bac_sci_annual_data(self):
        self.assertEqual(len(BAC_SCI_ANNUAL["phases"]), 4)
        for phase in BAC_SCI_ANNUAL["phases"]:
            self.assertIn("period", phase)
            self.assertGreater(len(phase["goals"]), 0)
            self.assertTrue(len(phase["strategy"]) > 0)
        self.assertGreater(len(BAC_SCI_ANNUAL["syria_tips"]), 0)

    def test_bac_lit_weekly_and_annual(self):
        self.assertEqual(len(BAC_LIT_WEEKLY["days"]), 7)
        self.assertEqual(len(BAC_LIT_ANNUAL["phases"]), 4)

    def test_university_weekly_tracks(self):
        for track in (TRACK_MEDICAL, TRACK_ENGINEERING, TRACK_HUMANITIES, TRACK_GENERAL):
            plan = get_univ_weekly(track)
            self.assertEqual(len(plan["days"]), 7)
            self.assertIn(track, plan["track_info"]["id"])


class TestStudyScheduleKeyboards(unittest.TestCase):
    """اختبارات لوحات المفاتيح والروابط."""

    def test_menu_keyboard(self):
        kb = get_study_schedule_menu_keyboard()
        buttons = [btn for row in kb.inline_keyboard for btn in row]
        callbacks = [btn.callback_data for btn in buttons if btn.callback_data]
        self.assertIn("study_sched:type:weekly", callbacks)
        self.assertIn("study_sched:type:annual", callbacks)
        self.assertIn("student_home", callbacks)

    def test_categories_keyboard(self):
        kb = get_study_schedule_categories_keyboard("weekly")
        callbacks = [btn.callback_data for row in kb.inline_keyboard for btn in row if btn.callback_data]
        self.assertIn(f"study_sched:cat:weekly:{CATEGORY_BAC_SCI}", callbacks)
        self.assertIn(f"study_sched:cat:weekly:{CATEGORY_BAC_LIT}", callbacks)
        self.assertIn(f"study_sched:cat:weekly:{CATEGORY_UNIV}", callbacks)

    def test_university_tracks_keyboard(self):
        kb = get_study_schedule_university_tracks_keyboard("annual")
        callbacks = [btn.callback_data for row in kb.inline_keyboard for btn in row if btn.callback_data]
        self.assertIn(f"study_sched:univ:annual:{TRACK_MEDICAL}", callbacks)
        self.assertIn(f"study_sched:univ:annual:{TRACK_ENGINEERING}", callbacks)

    def test_theme_keyboard(self):
        kb = get_study_schedule_theme_keyboard("weekly", CATEGORY_BAC_SCI)
        callbacks = [btn.callback_data for row in kb.inline_keyboard for btn in row if btn.callback_data]
        for theme_id in THEMES:
            self.assertTrue(any(f":{theme_id}" in c for c in callbacks))

    def test_result_keyboard(self):
        kb = get_study_schedule_result_keyboard("weekly", CATEGORY_BAC_SCI, current_theme="minimalist")
        callbacks = [btn.callback_data for row in kb.inline_keyboard for btn in row if btn.callback_data]
        # يجب أن يعرض سمات أخرى غير minimalist
        self.assertTrue(any("academic" in c for c in callbacks))
        self.assertIn("student_home", callbacks)


class TestStudyScheduleRenderer(unittest.TestCase):
    """اختبارات توليد الصور والـ PDF بجودة عالية ودعم اللغة العربية."""

    def test_arabic_shaping_helpers(self):
        self.assertTrue(has_arabic("جدول دراسي"))
        self.assertFalse(has_arabic("Weekly Schedule 123"))
        shaped = _shape("بكالوريا علمي")
        self.assertIsInstance(shaped, str)
        self.assertTrue(len(shaped) > 0)

    def test_render_weekly_all_themes(self):
        for theme_id in ("minimalist", "academic", "pastel", "dark"):
            png_bytes, pdf_bytes = render_weekly_schedule(
                BAC_SCI_WEEKLY,
                theme_id=theme_id,
                student_name="محمود",
            )
            # التحقق من أن صيغة PNG صالحة وتبدأ بالـ PNG magic header
            self.assertTrue(png_bytes.startswith(b"\x89PNG\r\n\x1a\n"))
            self.assertGreater(len(png_bytes), 10000, f"PNG too small for theme {theme_id}")
            # التحقق من أن صيغة PDF صالحة وتبدأ بـ %PDF-
            self.assertTrue(pdf_bytes.startswith(b"%PDF-"))
            self.assertGreater(len(pdf_bytes), 10000, f"PDF too small for theme {theme_id}")

    def test_render_annual_all_themes(self):
        for theme_id in ("minimalist", "academic", "pastel", "dark"):
            png_bytes, pdf_bytes = render_annual_schedule(
                BAC_SCI_ANNUAL,
                theme_id=theme_id,
                student_name="سارة",
            )
            self.assertTrue(png_bytes.startswith(b"\x89PNG\r\n\x1a\n"))
            self.assertGreater(len(png_bytes), 10000)
            self.assertTrue(pdf_bytes.startswith(b"%PDF-"))
            self.assertGreater(len(pdf_bytes), 10000)

    def test_router_registration(self):
        self.assertIsNotNone(study_schedule_router)
        self.assertTrue(len(study_schedule_router.message.handlers) > 0 or len(study_schedule_router.callback_query.handlers) > 0)


if __name__ == "__main__":
    unittest.main()
