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


HANDLER_NAMES = {"_user_label", "_build_session_details_text", "_build_participants_text", "_parse_session_callback"}


def handler_env():
    return load_functions(
        "handlers/admin/group_quiz_stats.py", HANDLER_NAMES,
        html=__import__("html"), uuid=uuid,
        format_syria_time=lambda v, fmt=None: f"T({v})" if v else "غير معروف",
        STATUS_LABELS={"active": "🟢 نشطة الآن", "finished": "🏁 منتهية"},
        CHAT_TYPE_LABELS={"supergroup": "👥 غروب", "channel": "📢 قناة"},
        PACING_LABELS={"burst": "دفعة واحدة"}, EXACT_TIME_FMT="x",
        MAX_MESSAGE_CHARS=3900, PARTICIPANTS_PAGE_SIZE=8, MAX_NAME_CHARS=40,
    )


def base_session(**kw):
    s = {"id": str(uuid.uuid4()), "quiz_id": str(uuid.uuid4()), "chat_id": -100123, "started_by": 77,
         "status": "active", "pacing_mode": "burst", "total_questions": 10, "questions_sent": 3,
         "created_at": "2026-10-02T10:00:00Z", "poll_is_anonymous": False, "show_names": True}
    s.update(kw)
    return s


class DetailsTextTests(unittest.TestCase):
    def render(self, session, total=0, live=None, membership=None, starter=None, answers=0):
        d = {"session": session, "membership": membership, "quiz": {"source_title": "Q", "creator_id": 5},
             "starter": starter, "added_by_user": None, "participants_total": total, "total_answers": answers}
        return handler_env()["_build_session_details_text"](d, live or {})

    def test_contains_admin_chat_quiz_and_time(self):
        text = self.render(
            base_session(),
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

    def test_details_show_participant_summary_only_not_the_list(self):
        text = self.render(base_session(), total=12, answers=340)
        self.assertIn("<code>12</code>", text)
        self.assertIn("<code>340</code>", text)
        self.assertNotIn("┣ <b>1.</b>", text)
        self.assertNotIn("لا توجد بيانات مشاركين", text)

    def test_details_without_participants_explains_anonymous_mode(self):
        self.assertIn("لا توجد بيانات مشاركين", self.render(base_session(), total=0))

    def test_worst_case_details_stay_under_telegram_limit(self):
        text = self.render(
            base_session(),
            membership={"chat_type": "channel", "chat_title": "C" * 128, "status": "administrator", "added_by": 5},
            starter={"user_id": 77, "username": "t" * 32, "first_name": "A" * 64, "last_name": "B" * 64},
            live={"username": "g" * 32, "members": 10**6}, total=10**6, answers=10**7,
        )
        self.assertLess(len(text), 4096)


class ParticipantsTextTests(unittest.TestCase):
    def data(self, n, page=0, total=None, **p_overrides):
        parts = []
        for i in range(n):
            p = {"user_id": i, "first_name": "P", "score": 1, "correct_count": 1,
                 "total_answer_time_ms": 1000, "_answers_count": 1, "joined_at": None}
            p.update(p_overrides)
            parts.append(p)
        session = {"_quiz_title": "Q", "_chat_title": "G", "_chat_type": "supergroup"}
        return {"session": session, "participants": parts, "total": total or n, "page": page}

    def test_rank_continues_across_pages(self):
        text = handler_env()["_build_participants_text"](self.data(8, page=1, total=20))
        self.assertIn("<b>9.</b>", text)
        self.assertIn("<b>16.</b>", text)
        self.assertIn("صفحة 2/3", text)

    def test_single_page_has_no_page_label(self):
        self.assertNotIn("صفحة", handler_env()["_build_participants_text"](self.data(3)))

    def test_worst_case_page_stays_under_telegram_limit(self):
        data = self.data(8, user_id=10**12, username="u" * 32, first_name="&<" * 60, last_name="N" * 60,
                         score=99, correct_count=99, _answers_count=100)
        data["session"] = {"_quiz_title": "Q" * 200, "_chat_title": "C" * 128, "_chat_type": "channel"}
        self.assertLess(len(handler_env()["_build_participants_text"](data)), 4096)


class CallbackParsingTests(unittest.TestCase):
    def test_parse(self):
        parse = handler_env()["_parse_session_callback"]
        sid = str(uuid.uuid4())
        self.assertEqual(parse(f"admin_gq_s:{sid}:r3"), (sid, "r", 3, 0))
        self.assertEqual(parse(f"admin_gq_s:{sid}:a0:7"), (sid, "a", 0, 0))  # old 4-part details ignored
        self.assertEqual(parse(f"admin_gq_p:{sid}:a2:5", extra_int=True), (sid, "a", 2, 5))
        for bad in (f"admin_gq_s:{sid}:x1", "admin_gq_s:not-a-uuid:r1", f"admin_gq_s:{sid}", f"admin_gq_p:{sid}:r1:zz"):
            self.assertIsNone(parse(bad, extra_int=True))


class KeyboardTests(unittest.TestCase):
    def kb_env(self):
        try:
            from aiogram import types
        except Exception as e:  # pragma: no cover
            self.skipTest(f"aiogram not installed: {e}")
        return load_functions(
            "keyboards.py",
            {"get_group_quiz_sessions_keyboard", "get_group_quiz_session_detail_keyboard",
             "get_group_quiz_participants_keyboard"},
            types=types,
        )

    def data(self, markup):
        return [b.callback_data for row in markup.inline_keyboard for b in row]

    def test_callback_data_fits_telegram_limit(self):
        env = self.kb_env()
        sessions = [{"id": str(uuid.uuid4())} for _ in range(8)]
        sid = sessions[0]["id"]
        for mode in ("a", "r"):
            markups = [
                env["get_group_quiz_sessions_keyboard"](sessions, mode, 12345, True),
                env["get_group_quiz_session_detail_keyboard"](sid, mode, 12345, participants_total=9, quiz_id=str(uuid.uuid4())),
                env["get_group_quiz_participants_keyboard"](sid, mode, 12345, 999, True, True),
            ]
            for m in markups:
                for cb in self.data(m):
                    self.assertLessEqual(len(cb.encode()), 64, cb)

    def test_active_list_has_pagination_and_detail_back_keeps_page(self):
        env = self.kb_env()
        sessions = [{"id": str(uuid.uuid4())} for _ in range(3)]
        data = self.data(env["get_group_quiz_sessions_keyboard"](sessions, "a", 2, True))
        self.assertIn("admin_gq_active:1", data)
        self.assertIn("admin_gq_active:3", data)
        self.assertIn("admin_gq_active:2", self.data(env["get_group_quiz_session_detail_keyboard"](sessions[0]["id"], "a", 2)))

    def test_detail_buttons_depend_on_participants_and_quiz(self):
        env = self.kb_env()
        sid, qid = str(uuid.uuid4()), str(uuid.uuid4())
        full = self.data(env["get_group_quiz_session_detail_keyboard"](sid, "r", 4, participants_total=3, quiz_id=qid))
        self.assertIn(f"admin_gq_p:{sid}:r4:0", full)
        self.assertIn(f"admin_gq_try:{qid}", full)
        empty = self.data(env["get_group_quiz_session_detail_keyboard"](sid, "r", 4))
        self.assertFalse(any(c.startswith(("admin_gq_p:", "admin_gq_try:")) for c in empty))

    def test_participants_keyboard_nav_and_back_to_details(self):
        env = self.kb_env()
        sid = str(uuid.uuid4())
        mid = self.data(env["get_group_quiz_participants_keyboard"](sid, "a", 2, 3, True, True))
        self.assertIn(f"admin_gq_p:{sid}:a2:2", mid)
        self.assertIn(f"admin_gq_p:{sid}:a2:4", mid)
        self.assertIn(f"admin_gq_s:{sid}:a2", mid)  # back keeps list mode + page
        first = self.data(env["get_group_quiz_participants_keyboard"](sid, "a", 2, 0, False, False))
        self.assertFalse(any(c.endswith(":-1") for c in first))
        self.assertEqual(len([c for c in first if c.startswith("admin_gq_p:")]), 1)  # refresh only


class DetailsStoreTests(unittest.IsolatedAsyncioTestCase):
    """get_group_session_details: صفحات المشاركين + أعداد دقيقة حتى لو تجاوزت سقف الـ 1000 صف."""

    class Q:
        def __init__(self, rows):
            self.rows, self.filters, self.orders = rows, [], []
            self.lo, self.hi, self.cap = 0, None, 1000

        def select(self, *a, **k):
            return self

        def eq(self, c, v):
            self.filters.append(lambda r: r.get(c) == v)
            return self

        def order(self, c, desc=False):
            self.orders.append((c, desc))
            return self

        def range(self, lo, hi):
            self.lo, self.hi = lo, hi
            return self

        def limit(self, n):
            self.cap = n
            return self

        async def execute(self):
            rows = [r for r in self.rows if all(f(r) for f in self.filters)]
            for c, desc in reversed(self.orders):
                rows.sort(key=lambda r: r[c], reverse=desc)
            total = len(rows)
            rows = rows[self.lo:self.hi + 1] if self.hi is not None else rows[: self.cap]
            return Result(rows, total)

    class DB:
        def __init__(self, tables, outer):
            self.tables, self.outer = tables, outer

        def table(self, name):
            return self.outer.Q(self.tables.get(name, []))

    def store(self, db, session):
        async def get_session(_):
            return session

        async def get_chat_membership(_):
            return None

        async def enrich(sessions):
            for x in sessions:
                x.update(_chat_title="G", _chat_type="supergroup", _quiz_title="Q")
            return sessions

        return load_functions(
            "services/group_quiz_store.py", {"get_group_session_details", "get_group_session_participants"},
            supabase=db, log_error=Mock(), logger=Mock(), asyncio=__import__("asyncio"),
            get_session=get_session, get_chat_membership=get_chat_membership, _enrich_sessions=enrich,
            Dict=dict, Any=object, Optional=object,
        )

    def tables(self, sid):
        participants = [{"session_id": sid, "user_id": i, "score": 100 - i, "total_answer_time_ms": i} for i in range(12)]
        answers = [{"session_id": sid, "user_id": i % 12, "question_index": i} for i in range(1500)]
        return {"users": [{"user_id": 7}], "group_quiz_participants": participants, "group_quiz_answers": answers}

    async def test_details_have_exact_totals_beyond_1000_and_no_participant_list(self):
        sid = str(uuid.uuid4())
        session = {"id": sid, "chat_id": 1, "started_by": 7, "quiz_id": None}
        d = await self.store(self.DB(self.tables(sid), self), session)["get_group_session_details"](sid)
        self.assertEqual(d["participants_total"], 12)
        self.assertEqual(d["total_answers"], 1500)  # not capped at 1000
        self.assertNotIn("participants", d)

    async def test_participants_pages_clamp_and_count_answers(self):
        sid = str(uuid.uuid4())
        session = {"id": sid, "chat_id": 1, "started_by": 7, "quiz_id": None}
        fn = self.store(self.DB(self.tables(sid), self), session)["get_group_session_participants"]
        d = await fn(sid, page=1, page_size=5)
        self.assertEqual((d["total"], d["page"]), (12, 1))
        self.assertEqual([p["user_id"] for p in d["participants"]], [5, 6, 7, 8, 9])
        self.assertTrue(all(p["_answers_count"] > 100 for p in d["participants"]))
        self.assertEqual(d["session"]["_quiz_title"], "Q")
        clamped = await fn(sid, page=99, page_size=5)
        self.assertEqual((clamped["page"], len(clamped["participants"])), (2, 2))

    async def test_missing_session_returns_none(self):
        sid = str(uuid.uuid4())
        fns = self.store(self.DB(self.tables(sid), self), None)
        self.assertIsNone(await fns["get_group_session_details"](sid))
        self.assertIsNone(await fns["get_group_session_participants"](sid))


if __name__ == "__main__":
    unittest.main()
