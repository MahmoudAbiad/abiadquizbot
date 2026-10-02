import sys
import unittest
import uuid
from pathlib import Path
from unittest.mock import Mock

from reliability_support import load_functions

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

ENDED = ("finished", "cancelled")


class Result:
    def __init__(self, data=None, count=None):
        self.data = data or []
        self.count = count


class Query:
    """Minimal PostgREST double: select/delete + in_/eq/limit over in-memory tables."""

    def __init__(self, db, table):
        self.db, self.table_name = db, table
        self.mode, self.filters, self.size = "select", [], 1000  # API default row cap

    def select(self, *a, **k):
        self.mode = "select"
        return self

    def delete(self):
        self.mode = "delete"
        return self

    def in_(self, col, values):
        self.filters.append(lambda r: r[col] in values)
        return self

    def eq(self, col, value):
        self.filters.append(lambda r: r[col] == value)
        return self

    def limit(self, n):
        self.size = n
        return self

    async def execute(self):
        rows = self.db.tables[self.table_name]
        matched = [r for r in rows if all(f(r) for f in self.filters)][: self.size]
        if self.mode == "delete":
            self.db.tables[self.table_name] = [r for r in rows if r not in matched]
        return Result(matched, len(matched))


class Database:
    def __init__(self, sessions, participants=(), answers=()):
        self.tables = {
            "group_quiz_sessions": list(sessions),
            "group_quiz_participants": list(participants),
            "group_quiz_answers": list(answers),
        }

    def table(self, name):
        return Query(self, name)


def make_sessions(finished=0, cancelled=0, active=0, waiting=0):
    out = []
    for status, n in (("finished", finished), ("cancelled", cancelled), ("active", active), ("waiting", waiting)):
        out += [{"id": str(uuid.uuid4()), "status": status} for _ in range(n)]
    return out


class PurgeTests(unittest.IsolatedAsyncioTestCase):
    def store(self, db):
        return load_functions(
            "services/group_quiz_store.py", {"purge_ended_group_sessions", "count_ended_group_sessions"},
            supabase=db, log_error=Mock(), log_info=Mock(), logger=Mock(),
            ENDED_STATUSES=ENDED, _PURGE_BATCH_SIZE=200, _PURGE_MAX_BATCHES=50,
            Dict=dict, Any=object,
        )

    async def test_purge_removes_all_ended_beyond_one_batch_and_spares_open_sessions(self):
        sessions = make_sessions(finished=450, cancelled=30, active=2, waiting=3)
        ended_ids = [s["id"] for s in sessions if s["status"] in ENDED]
        open_ids = [s["id"] for s in sessions if s["status"] not in ENDED]
        participants = [{"session_id": sid, "user_id": 1} for sid in ended_ids[:50] + open_ids]
        answers = [{"session_id": sid, "user_id": 1} for sid in ended_ids[:10] + open_ids]
        db = Database(sessions, participants, answers)

        result = await self.store(db)["purge_ended_group_sessions"]()

        self.assertFalse(result["error"])
        self.assertEqual(result["sessions"], 480)
        self.assertEqual(result["participants"], 50)
        self.assertEqual(result["answers"], 10)
        self.assertEqual({s["id"] for s in db.tables["group_quiz_sessions"]}, set(open_ids))
        self.assertEqual(len(db.tables["group_quiz_participants"]), len(open_ids))
        self.assertEqual(len(db.tables["group_quiz_answers"]), len(open_ids))

    async def test_purge_on_empty_is_noop(self):
        db = Database(make_sessions(active=1))
        result = await self.store(db)["purge_ended_group_sessions"]()
        self.assertEqual((result["sessions"], result["error"]), (0, False))
        self.assertEqual(len(db.tables["group_quiz_sessions"]), 1)

    async def test_purge_reports_error_without_raising(self):
        class Broken:
            def table(self, name):
                raise RuntimeError("db down")
        result = await self.store(Broken())["purge_ended_group_sessions"]()
        self.assertTrue(result["error"])


class DetailsTextTests(unittest.TestCase):
    def render(self, session, participants=(), live=None, membership=None, starter=None):
        env = load_functions(
            "handlers/admin/group_quiz_stats.py", {"_user_label", "_build_session_details_text"},
            html=__import__("html"),
            format_syria_time=lambda v, fmt=None: f"T({v})" if v else "غير معروف",
            STATUS_LABELS={"active": "🟢 نشطة الآن", "finished": "🏁 منتهية"},
            CHAT_TYPE_LABELS={"supergroup": "👥 غروب", "channel": "📢 قناة"},
            PACING_LABELS={"burst": "دفعة واحدة"}, EXACT_TIME_FMT="x",
            MAX_MESSAGE_CHARS=3900, MAX_PARTICIPANTS_SHOWN=15,
        )
        details = {
            "session": session, "membership": membership, "quiz": {"source_title": "Q", "creator_id": 5},
            "starter": starter, "added_by_user": None,
            "participants": list(participants), "total_answers": 0,
        }
        return env["_build_session_details_text"](details, live or {})

    def base_session(self, **kw):
        s = {"id": str(uuid.uuid4()), "quiz_id": str(uuid.uuid4()), "chat_id": -100123, "started_by": 77,
             "status": "active", "pacing_mode": "burst", "total_questions": 10, "questions_sent": 3,
             "created_at": "2026-10-02T10:00:00Z", "poll_is_anonymous": False, "show_names": True}
        s.update(kw)
        return s

    def test_contains_admin_chat_quiz_and_time(self):
        text = self.render(
            self.base_session(),
            membership={"chat_type": "supergroup", "chat_title": "Group <b>X</b>", "status": "administrator"},
            starter={"user_id": 77, "username": "teacher", "first_name": "Ali", "last_name": "Z"},
            live={"username": "grp", "members": 42},
        )
        self.assertIn("@teacher", text)
        self.assertIn("<code>77</code>", text)
        self.assertIn("-100123", text)
        self.assertIn("@grp", text)
        self.assertIn("T(2026-10-02T10:00:00Z)", text)
        self.assertNotIn("Group <b>X</b>", text)  # title is HTML-escaped
        self.assertIn("لا توجد بيانات مشاركين", text)

    def test_many_participants_stay_under_telegram_limit(self):
        parts = [{"user_id": i, "username": "u" * 30, "first_name": "N" * 60, "score": 9, "correct_count": 9,
                  "total_answer_time_ms": 9000, "_answers_count": 10, "joined_at": "2026-10-02T10:00:00Z"}
                 for i in range(200)]
        text = self.render(self.base_session(), participants=parts,
                           membership={"chat_type": "channel", "chat_title": "C", "status": "administrator"})
        self.assertLess(len(text), 4096)
        self.assertIn("غير معروض", text)


class KeyboardTests(unittest.TestCase):
    def test_callback_data_fits_telegram_limit(self):
        try:
            import keyboards
        except Exception as e:  # pragma: no cover - environment without bot deps
            self.skipTest(f"keyboards not importable here: {e}")
        sessions = [{"id": str(uuid.uuid4())} for _ in range(8)]
        for mode in ("a", "r"):
            kb = keyboards.get_group_quiz_sessions_keyboard(sessions, mode, 123, True)
            kb2 = keyboards.get_group_quiz_session_detail_keyboard(sessions[0]["id"], mode, 123)
            for markup in (kb, kb2):
                for row in markup.inline_keyboard:
                    for btn in row:
                        self.assertLessEqual(len(btn.callback_data.encode()), 64, btn.callback_data)


if __name__ == "__main__":
    unittest.main()
