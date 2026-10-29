import unittest

import fitz

from services.export_service import build_quiz_pdf


class ExportBlankTests(unittest.TestCase):
    def test_english_and_translated_blanks_survive_every_pdf_style(self):
        for translation in ("", "\nأكمل الجملة: أنا أعمل في هذه الشركة لمدة خمس سنوات."):
            question = {
                "question": "I've worked here ____ five years." + translation,
                "options": ["for (لمدة)", "since (منذ)", "_______ years"],
                "correct_option_id": 0,
                "explanation": "Use for with a duration.",
            }
            for style in ("simple", "modern", "academic"):
                for answers in (False, True):
                    with self.subTest(translation=bool(translation), style=style, answers=answers):
                        data = build_quiz_pdf("For and since rule", [question], style, include_answers=answers)
                        with fitz.open(stream=data, filetype="pdf") as pdf:
                            text = "\n".join(page.get_text() for page in pdf)
                        self.assertRegex(text, r"(?<!_)_{4}(?!_)")
                        self.assertRegex(text, r"(?<!_)_{7}(?!_)")
                        self.assertNotIn("_(_)", text)


if __name__ == "__main__":
    unittest.main()
