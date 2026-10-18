"""Live-match Python tests: pure rules + repository behaviour with a fake Supabase client (no network).
SQL semantics (locks, deadlines, settlement) live in tests/sql/run_challenge_live.mjs."""
import asyncio
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock
from services.challenge_diagnostics import log_failure

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from reliability_support import load_functions  # noqa: E402
from services.challenge_game import load_rules  # noqa: E402
from services.challenge_live import allowed_range, can_match, outcome  # noqa: E402

SRC = "helpers/supabase_helper/challenge_live.py"
NAMES = ["rpc", "_log_terminal", "join_queue", "try_match", "queue_state", "cancel_search", "match_state", "answer",
         "leave", "tick", "due_matches", "expire_queue", "sweep", "pair_waiting"]


class RuleTests(unittest.TestCase):
    def setUp(self):
        self.r = load_rules()

    def test_range_starts_narrow_widens_and_is_capped(self):
        self.assertEqual(allowed_range(0, self.r), 100)
        self.assertEqual(allowed_range(9.9, self.r), 100)
        self.assertEqual(allowed_range(10, self.r), 200)
        self.assertEqual(allowed_range(30, self.r), 400)
        self.assertEqual(allowed_range(10_000, self.r), self.r["matchmaking_range"])
        self.assertEqual(allowed_range(-5, self.r), 100)

    def test_match_is_symmetric_and_uses_the_wider_range(self):
        self.assertFalse(can_match(0, 0, 450, 0, self.r))
        self.assertFalse(can_match(0, 35, 450, 0, self.r))   # 35 s => range 400
        self.assertTrue(can_match(0, 40, 450, 0, self.r))
        self.assertTrue(can_match(450, 0, 0, 40, self.r))
        self.assertTrue(can_match(0, 0, 100, 0, self.r))
        self.assertFalse(can_match(0, 0, 101, 0, self.r))

    def test_outcome_tie_is_a_draw(self):
        self.assertEqual(outcome(10, 5), "a")
        self.assertEqual(outcome(5, 10), "b")
        self.assertEqual(outcome(7, 7), "draw")

    def test_live_rules_validated(self):
        import json
        from services import challenge_game as g
        base = json.loads((Path(g.__file__).with_name("challenge_rules.json")).read_text())
        for key, bad in [("live_questions", 0), ("live_countdown_seconds", 61), ("live_queue_seconds", 5),
                         ("live_range_step_seconds", 0), ("live_pair_daily_limit", 0), ("live_range_initial", 9999)]:
            orig = Path(g.__file__).with_name("challenge_rules.json").read_text()
            try:
                Path(g.__file__).with_name("challenge_rules.json").write_text(json.dumps({**base, key: bad}))
                with self.assertRaises(ValueError, msg=key):
                    load_rules()
            finally:
                Path(g.__file__).with_name("challenge_rules.json").write_text(orig)
        load_rules()


class FakeSupabase:
    def __init__(self, responses):
        self.responses, self.calls = responses, []

    def rpc(self, name, params):
        self.calls.append((name, params))
        data = self.responses[name]
        data = self.responses.__setitem__(name, data[1:]) or data[0] if isinstance(data, tuple) else data
        q = MagicMock()

        async def execute():
            if isinstance(data, Exception):
                raise data
            return MagicMock(data=data)
        q.execute = execute
        return q


class RepoTests(unittest.TestCase):
    def env(self, responses):
        self.logs = []
        sb = FakeSupabase(responses)
        f = load_functions(SRC, NAMES, supabase=sb, load_rules=load_rules, log_info=load_functions("logger.py", ["log_info"])["log_info"],
                           logger=MagicMock(info=self.logs.append), log_failure=log_failure,
                           _TERMINAL={"completed", "expired", "abandoned", "cancelled"},
                           _LOG_EVENT={"completed": "match_completed", "abandoned": "match_abandoned",
                                       "expired": "match_timeout", "cancelled": "match_cancelled"})
        return f, sb

    def run_(self, coro):
        return asyncio.run(coro)

    def test_join_then_pairs_immediately_and_keeps_the_chosen_unit(self):
        f, sb = self.env({"challenge_live_join": {"status": "waiting"},
                          "challenge_live_try_match": {"status": "matched", "match_id": "m1", "created": True}})
        st = self.run_(f["join_queue"](7, "unit-1", "req-1"))
        self.assertEqual(st["match_id"], "m1")
        name, params = sb.calls[0]
        self.assertEqual((name, params["p_unit"], params["p_request"]), ("challenge_live_join", "unit-1", "req-1"))
        self.assertEqual(params["p_rules"]["live_questions"], 10)
        self.assertEqual(self.logs, ["match_created match=m1"])

    def test_join_of_cancelled_retry_does_not_attempt_matching(self):
        f, sb = self.env({"challenge_live_join": {"status": "cancelled"}})
        self.assertEqual(self.run_(f["join_queue"](7, "u", "r"))["status"], "cancelled")
        self.assertEqual([c[0] for c in sb.calls], ["challenge_live_join"])

    def test_try_match_retries_only_when_candidate_lost_to_a_race(self):
        f, sb = self.env({"challenge_live_try_match": ({"status": "waiting", "retry": True},
                                                       {"status": "waiting", "retry": True},
                                                       {"status": "matched", "match_id": "m", "created": True})})
        self.assertEqual(self.run_(f["try_match"](1))["status"], "matched")
        self.assertEqual(len(sb.calls), 3)
        f, sb = self.env({"challenge_live_try_match": {"status": "waiting"}})
        self.run_(f["try_match"](1))
        self.assertEqual(len(sb.calls), 1)
        f, sb = self.env({"challenge_live_try_match": {"status": "waiting", "retry": True}})
        self.run_(f["try_match"](1, attempts=2))
        self.assertEqual(len(sb.calls), 2)

    def test_cancel_passes_request_key_and_never_touches_matches(self):
        f, sb = self.env({"challenge_live_cancel": {"status": "matched", "match_id": "m"}})
        self.run_(f["cancel_search"](3, "req-9"))
        self.assertEqual(sb.calls, [("challenge_live_cancel", {"p_user": 3, "p_request": "req-9"})])

    def test_answer_forwards_update_key_and_logs_without_answer_content(self):
        f, sb = self.env({"challenge_live_answer": {"id": "m", "status": "completed"}})
        self.run_(f["answer"]("m", 3, 4, 2, "upd-1"))
        self.assertEqual(sb.calls[0][1], {"p_match": "m", "p_user": 3, "p_index": 4, "p_option": 2, "p_update": "upd-1"})
        self.assertEqual(self.logs, ["answer_received match=m index=4", "match_completed match=m"])
        self.assertFalse(any("option" in line or "upd-1" in line for line in self.logs))

    def test_terminal_status_log_names(self):
        for status, event in [("expired", "match_timeout"), ("cancelled", "match_cancelled"), ("abandoned", "match_abandoned")]:
            f, _ = self.env({"challenge_live_tick": {"id": "x", "status": status}})
            self.run_(f["tick"]("x"))
            self.assertEqual(self.logs, [f"{event} match=x"])

    def test_sweep_expires_queue_then_ticks_every_due_match_from_the_database(self):
        f, sb = self.env({"challenge_live_expire_queue": [5, 6], "challenge_live_due": ["a", "b"],
                          "challenge_live_tick": ({"id": "a", "status": "in_progress"}, {"id": "b", "status": "completed"})})
        out = self.run_(f["sweep"]())
        self.assertEqual(out["expired_users"], [5, 6])
        self.assertEqual([c[0] for c in sb.calls], ["challenge_live_expire_queue", "challenge_live_due", "challenge_live_tick", "challenge_live_tick"])
        self.assertEqual([m["status"] for m in out["matches"]], ["in_progress", "completed"])

    def test_db_errors_propagate_for_the_ui_layer(self):
        f, _ = self.env({"challenge_live_join": RuntimeError("already_queued")})
        with self.assertRaises(RuntimeError):
            self.run_(f["join_queue"](1, "u", "r"))


class AsyncIsolationTests(unittest.TestCase):
    def test_async_repo_queries_filter_kind(self):
        src = (Path(__file__).resolve().parents[1] / "helpers/supabase_helper/challenge_game.py").read_text()
        self.assertEqual(src.count('"kind", "async_attack"'), 4)   # active_attack, history, due_attacks, has_active_attacks

    def test_migration_guards_async_rpcs_and_declares_lock_order(self):
        sql = (Path(__file__).resolve().parents[1] / "db/migrations/0010_challenge_live.sql").read_text()
        self.assertGreaterEqual(sql.count("kind='async_attack'"), 3)
        self.assertNotIn("ORDER BY RANDOM", sql.upper().replace("RANDOM()", "RANDOM"))
        self.assertNotIn("random()", sql.lower())
        self.assertNotIn("users.points", sql)
        self.assertNotIn("UPDATE users", sql)


if __name__ == "__main__":
    unittest.main()
