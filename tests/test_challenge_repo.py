"""اختبارات طبقة الـ Repository بعميل Supabase مزيّف (بلا شبكة). السلوك الذري/الفريد بقاعدة
البيانات نفسها مغطّى بـ tests/sql/challenge_content_test.sql."""
import asyncio
import re
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from reliability_support import load_functions  # noqa: E402

SRC = "helpers/supabase_helper/challenges.py"
NAMES = ["_now", "_is_unique_violation", "set_test_status", "get_test", "import_test", "pick_active_test",
         "get_test_questions_public", "find_quiz_for_import", "ChallengeRepoError", "_distinct",
         "get_playable_units", "_playable_rows", "get_playable_branches"]


class Res:
    def __init__(self, data):
        self.data = data


class Q:
    def __init__(self, db, table):
        self.db, self.table, self.filters, self.cols, self.patch = db, table, [], None, None

    def select(self, cols="*"):
        self.cols = cols
        self.db.selects.append((self.table, cols))
        return self

    def update(self, patch):
        self.patch = patch
        return self

    def _f(self, kind, k, v):
        self.filters.append((kind, k, v))
        return self

    def eq(self, k, v): return self._f("eq", k, v)
    def neq(self, k, v): return self._f("neq", k, v)
    def gt(self, k, v): return self._f("gt", k, v)
    def limit(self, n): return self
    def order(self, *a, **k): return self

    async def execute(self):
        rows = [r for r in self.db.tables.get(self.table, []) if self._match(r)]
        if self.patch is not None:
            for r in rows:
                r.update(self.patch)
        return Res([dict(r) for r in rows])

    def _match(self, r):
        for kind, k, v in self.filters:
            if kind == "eq" and r.get(k) != v: return False
            if kind == "neq" and r.get(k) == v: return False
            if kind == "gt" and not r.get(k, 0) > v: return False
        return True


class FakeDB:
    def __init__(self):
        self.tables, self.selects, self.rpc_calls, self.rpc_result, self.rpc_error = {}, [], [], None, None

    def table(self, name): return Q(self, name)

    def rpc(self, name, params):
        self.rpc_calls.append((name, params))
        db = self

        class R:
            async def execute(self_):
                if db.rpc_error:
                    raise db.rpc_error
                return Res(db.rpc_result)
        return R()


UID = "11111111-1111-4111-8111-111111111111"


def _module_constant(name):
    """يقرأ ثابتاً بمستوى الملف من المصدر الفعلي (بدل نسخه بالاختبار فيتباعد عنه)."""
    import ast
    tree = ast.parse((Path(__file__).resolve().parents[1] / SRC).read_text())
    for node in tree.body:
        if isinstance(node, ast.Assign) and node.targets[0].id == name:
            return eval(compile(ast.Expression(node.value), SRC, "eval"))
    raise KeyError(name)


def build(db):
    logs = MagicMock()
    env = load_functions(SRC, NAMES, supabase=db, logger=logs, log_info=MagicMock(), log_error=MagicMock(),
                         _is_valid_uuid=lambda v: bool(v and re.fullmatch(r"[0-9a-f-]{36}", str(v))),
                         ParsedTest=object, to_import_payload=lambda qs: qs, random=__import__("random"),
                         datetime=__import__("datetime"), Any=object, Dict=dict, List=list, Optional=None, Sequence=tuple,
                         _TEST_COLS="*",
                         _ALLOWED_TRANSITIONS=_module_constant("_ALLOWED_TRANSITIONS"))
    return env


def run(coro):
    return asyncio.run(coro)


class RepoTests(unittest.TestCase):
    def setUp(self):
        self.db = FakeDB()
        self.env = build(self.db)

    def test_status_transitions(self):
        self.db.tables["challenge_tests"] = [{"id": UID, "status": "active", "unit_id": "u"}]
        self.assertTrue(run(self.env["set_test_status"](UID, "disabled", 1)))
        self.assertEqual(self.db.tables["challenge_tests"][0]["status"], "disabled")
        self.assertIsNotNone(self.db.tables["challenge_tests"][0]["disabled_at"])
        self.assertTrue(run(self.env["set_test_status"](UID, "active", 1)))
        self.assertIsNone(self.db.tables["challenge_tests"][0]["disabled_at"])
        self.assertTrue(run(self.env["set_test_status"](UID, "archived", 1)))
        # المؤرشف نهائي: لا عودة ولا تعطيل
        self.assertFalse(run(self.env["set_test_status"](UID, "active", 1)))
        self.assertFalse(run(self.env["set_test_status"](UID, "disabled", 1)))

    def test_status_unknown_test(self):
        self.assertFalse(run(self.env["set_test_status"]("bad-id", "disabled", 1)))

    def test_public_questions_never_select_answers(self):
        self.db.tables["challenge_questions"] = []
        run(self.env["get_test_questions_public"](UID))
        cols = [c for t, c in self.db.selects if t == "challenge_questions"][0]
        self.assertNotIn("correct_option_id", cols)
        self.assertNotIn("explanation", cols)

    def test_pick_active_test_avoids_recent_and_falls_back(self):
        self.db.tables["challenge_tests"] = [
            {"id": "a", "unit_id": "u", "status": "active", "question_count": 5, "title": "A"},
            {"id": "b", "unit_id": "u", "status": "active", "question_count": 5, "title": "B"},
            {"id": "c", "unit_id": "u", "status": "disabled", "question_count": 5, "title": "C"},
            {"id": "d", "unit_id": "u", "status": "active", "question_count": 0, "title": "D"},
            {"id": "e", "unit_id": "other", "status": "active", "question_count": 5, "title": "E"},
        ]
        picks = {run(self.env["pick_active_test"]("u", ["a"]))["id"] for _ in range(30)}
        self.assertEqual(picks, {"b"})  # لا disabled ولا فارغ ولا وحدة أخرى
        both = run(self.env["pick_active_test"]("u", ["a", "b"]))
        self.assertIn(both["id"], {"a", "b"})  # لا يبقى بلا اختبار لو كلها مستبعدة
        self.assertIsNone(run(self.env["pick_active_test"]("empty-unit")))

    def test_import_reports_duplicate_and_unit_not_active(self):
        self.db.rpc_result = {"created": False, "test_id": UID}
        parsed = MagicMock(title="t", source_type="json_upload", source_reference=None, content_hash="h",
                           question_count=1, questions=[], metadata={})
        out = run(self.env["import_test"](UID, parsed, 1))
        self.assertFalse(out["created"])
        self.db.rpc_error = Exception("P0001 unit_not_active")
        with self.assertRaises(self.env["ChallengeRepoError"]):
            run(self.env["import_test"](UID, parsed, 1))

    def test_playable_lists_derive_from_view(self):
        self.db.tables["challenge_playable_units"] = [
            {"branch_id": "b1", "branch_name": "ب", "branch_order": 20, "subject_id": "s1", "subject_name": "م",
             "subject_order": 10, "unit_id": "u1", "unit_name": "و2", "unit_order": 20, "active_test_count": 2},
            {"branch_id": "b1", "branch_name": "ب", "branch_order": 20, "subject_id": "s1", "subject_name": "م",
             "subject_order": 10, "unit_id": "u2", "unit_name": "و1", "unit_order": 10, "active_test_count": 1},
            {"branch_id": "b0", "branch_name": "أ", "branch_order": 10, "subject_id": "s9", "subject_name": "ز",
             "subject_order": 10, "unit_id": "u9", "unit_name": "ح", "unit_order": 10, "active_test_count": 1},
        ]
        self.assertEqual([b["id"] for b in run(self.env["get_playable_branches"]())], ["b0", "b1"])
        self.assertEqual([u["id"] for u in run(self.env["get_playable_units"]("s1"))], ["u2", "u1"])

    def test_find_quiz_by_uuid_or_share_code(self):
        self.db.tables["quizzes"] = [{"id": UID, "share_code": "XY12"}]
        self.assertEqual(run(self.env["find_quiz_for_import"](UID))["id"], UID)
        self.assertEqual(run(self.env["find_quiz_for_import"]("XY12"))["id"], UID)
        self.assertIsNone(run(self.env["find_quiz_for_import"]("nope")))
        self.assertIsNone(run(self.env["find_quiz_for_import"]("  ")))


if __name__ == "__main__":
    unittest.main()
