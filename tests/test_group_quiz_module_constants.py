"""Guard: module-level names used inside try/except-swallowed code must really exist.

The finished-marker constants were once referenced but never defined; the NameError was
swallowed by `except Exception`, silently disabling the late-answer guard. Unit tests that
inject these names by hand cannot catch that, so check the real module source."""
import ast
import unittest
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "handlers" / "group_quiz.py"


class ModuleConstantsTests(unittest.TestCase):
    def test_finished_marker_constants_are_defined_at_module_level(self):
        tree = ast.parse(SRC.read_text(encoding="utf-8"))
        defined = {t.id for n in tree.body if isinstance(n, ast.Assign)
                   for t in n.targets if isinstance(t, ast.Name)}
        for name in ("_FINISHED_SESSION_PREFIX", "_FINISHED_SESSION_TTL"):
            self.assertIn(name, defined)

    def test_setup_summary_constants_are_defined_at_module_level(self):
        tree = ast.parse(SRC.read_text(encoding="utf-8"))
        defined = {t.id for n in tree.body if isinstance(n, ast.Assign)
                   for t in n.targets if isinstance(t, ast.Name)}
        for name in ("_SETUP_HEADER_PREFIX", "_SETUP_HEADER_TTL", "_SETUP_FIELDS", "_SETUP_PACE_LABELS"):
            self.assertIn(name, defined)


if __name__ == "__main__":
    unittest.main()
