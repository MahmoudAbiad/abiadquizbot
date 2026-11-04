import unittest

from services.latex_text import latex_to_plain


class LatexPlainTextTests(unittest.TestCase):
    def test_fill_in_blanks_are_preserved(self):
        for blank in ("__", "___", "____", "________", "____________"):
            for sentence in (
                f"I've been working here {blank} five years.",
                f"Kate has lived here {blank}2012.",
                f"It has been raining {blank}a long time.",
                f"أكمل الجملة: عملت هنا {blank} خمس سنوات.",
                f"({blank})",
            ):
                with self.subTest(sentence=sentence):
                    self.assertEqual(latex_to_plain(sentence), sentence)

    def test_single_standalone_underscore_is_preserved(self):
        self.assertEqual(latex_to_plain("Choose _ or (_)."), "Choose _ or (_).")

    def test_translated_english_question_preserves_blank_and_parentheses(self):
        question = (
            "Complete the sentence: I've been working at this company ____ five years.\n"
            "أكمل الجملة: أنا أعمل في هذه الشركة لمدة خمس سنوات."
        )
        self.assertEqual(latex_to_plain(question), question)
        self.assertEqual(latex_to_plain("for (لمدة)"), "for (لمدة)")

    def test_math_scripts_still_convert(self):
        for source, expected in (
            ("$x_1 + x_{12}$", "x₁ + x₁₂"),
            ("$S_n + H_2O$", "Sₙ + H₂O"),
            ("$x_z$", "x_(z)"),
            ("$x_{foo}$", "x_(foo)"),
            ("$x^2 + x^{12}$", "x² + x¹²"),
            (r"$\frac{x_1}{x^2}$", "((x₁)/(x²))"),
            ("Choose ____ for $x_1$.", "Choose ____ for x₁."),
        ):
            with self.subTest(source=source):
                self.assertEqual(latex_to_plain(source), expected)


if __name__ == "__main__":
    unittest.main()
