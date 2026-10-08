"""Check real rendered option placement, ordering and long-content fallback."""
import io
import unittest
from unittest.mock import patch
from matplotlib.axes import Axes
from PIL import Image
from services import image_quiz_renderer as renderer


class OptionLayoutTests(unittest.TestCase):
    def render(self, options, is_ar=True):
        recorded = []
        original = Axes.text
        def capture(ax, x, y, text, *args, **kwargs):
            if kwargs.get('color') == '#2c2c2c':
                recorded.append((x, y, text))
            return original(ax, x, y, text, *args, **kwargs)
        with patch.object(Axes, 'text', capture):
            png = renderer.render_question_image(
                {'question': 'اختر الإجابة الصحيحة' if is_ar else 'Choose the correct answer',
                 'options': options}, 0, 1, is_ar)
        image = Image.open(io.BytesIO(png))
        return recorded, image.size

    def test_short_arabic_options_rtl_two_rows_without_font_shrink(self):
        positions, size = self.render(['واحد', 'اثنان', 'ثلاثة', 'أربعة'])
        self.assertEqual(len(positions), 4)
        self.assertGreater(positions[0][0], positions[1][0])
        self.assertAlmostEqual(positions[0][1], positions[1][1], delta=10)
        self.assertEqual(positions[0][0], positions[2][0])
        self.assertGreater(positions[0][1], positions[2][1])
        self.assertTrue(all(0 < y < size[1] for _, y, _ in positions))

    def test_short_english_options_ltr_and_odd_last_row(self):
        positions, size = self.render(['one', 'two', 'three'], False)
        self.assertLess(positions[0][0], positions[1][0])
        self.assertEqual(positions[0][0], positions[2][0])
        self.assertGreater(positions[0][1], positions[2][1])
        self.assertTrue(all(0 < y < size[1] for _, y, _ in positions))

    def test_one_long_option_keeps_all_options_stacked(self):
        positions, _ = self.render(['خيار طويل يحتاج إلى مساحة كبيرة ' * 6, 'اثنان', 'ثلاثة', 'أربعة'])
        self.assertGreater(len(positions), 4)
        self.assertEqual(len({x for x, _, _ in positions}), 1)

    def test_short_math_fits_and_wide_unbroken_math_stays_stacked(self):
        positions, _ = self.render([r'$x^2$', r'$\frac{1}{2}$', '$-1$', '$2$'])
        self.assertEqual(len({x for x, _, _ in positions}), 2)
        positions, _ = self.render(['$' + '+'.join(['x'] * 18) + '$', '$2$'])
        self.assertEqual(len({x for x, _, _ in positions}), 1)
