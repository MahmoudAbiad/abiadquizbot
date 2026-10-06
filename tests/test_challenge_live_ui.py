"""3C — Telegram live-challenge UI tests.

aiogram is not required: production handler/worker code is executed through tests/reliability_support.load_functions
with injected fakes (bot, types, Telegram exceptions, backend). PostgreSQL semantics are covered by tests/sql/*
(34/34 + 10/10 from 3B.5) and are not re-tested here.
"""
import asyncio
import logging
import sys
import unittest
from collections import OrderedDict
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock
from uuid import UUID
from weakref import WeakValueDictionary
from services.challenge_diagnostics import log_failure
from helpers.challenge_delivery import deliver_screen

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from reliability_support import load_functions  # noqa: E402
from services import challenge_live_ui as ui  # noqa: E402

MID = "11111111-1111-1111-1111-111111111111"
UNIT = "22222222-2222-2222-2222-222222222222"
SUBJ = "33333333-3333-3333-3333-333333333333"
NOW = "2026-10-05T10:00:10+00:00"
HANDLER = "handlers/challenge_live.py"
NAMES = ["to_markup", "deliver", "remember", "present", "present_locked", "push_opponent", "notify_opponent", "wake_live", "unit_labels", "identity", "live_open", "edit", "menu",
         "units_menu", "start_search", "route_queue", "cancel", "resume", "refresh", "answer", "options_page",
         "ask_leave", "leave", "live_callback", "notify_expired", "render_for", "_log_transition", "live_beat",
         "challenge_live_loop"]


from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError


class BadRequest(TelegramBadRequest):
    def __init__(self, message):
        super().__init__(method=None, message=message)


class Forbidden(TelegramForbiddenError):
    def __init__(self, message):
        super().__init__(method=None, message=message)


class Btn:
    def __init__(self, text, callback_data=None, url=None):
        self.text, self.callback_data, self.url = text, callback_data, url


class Markup:
    def __init__(self, inline_keyboard):
        self.inline_keyboard = inline_keyboard


class Logs:
    def __init__(self):
        self.lines = []

    def _add(self, msg, *args):
        self.lines.append(msg % args if args else msg)

    info = warning = debug = _add


class FakeLive:
    """Scripted backend: script[name] may be a value, a callable, an Exception, or a tuple (consumed in order)."""

    def __init__(self, **script):
        self.script, self.calls = dict(script), []

    def __getattr__(self, name):
        async def fn(*a, **k):
            self.calls.append((name, a, k))
            v = self.script.get(name)
            if isinstance(v, tuple):
                v = v[0] if len(v) == 1 else (self.script.__setitem__(name, v[1:]) or v[0])
            if callable(v):
                v = v(*a, **k)
            if isinstance(v, BaseException):
                raise v
            return v
        return fn

    def names(self):
        return [c[0] for c in self.calls]


class FakeBot:
    def __init__(self):
        self.edits, self.sends, self.edit_error, self.send_error, self.next_id = [], [], None, None, 500

    async def edit_message_text(self, text, chat_id, message_id, reply_markup, parse_mode):
        if self.edit_error:
            raise self.edit_error
        self.edits.append(SimpleNamespace(text=text, chat_id=chat_id, message_id=message_id, markup=reply_markup))

    async def send_message(self, chat_id, text, reply_markup, parse_mode):
        if self.send_error:
            raise self.send_error
        self.next_id += 1
        self.sends.append(SimpleNamespace(text=text, chat_id=chat_id, message_id=self.next_id, markup=reply_markup))
        return SimpleNamespace(message_id=self.next_id)

    def shown(self):
        return [e.text for e in self.edits] + [s.text for s in self.sends]


class FakeContent:
    async def get_node(self, kind, node_id):
        return {"unit": {"id": node_id, "name": "الحركة", "subject_id": SUBJ},
                "subject": {"id": node_id, "name": "الفيزياء"}}[kind]

    async def get_playable_branches(self):
        return [{"id": "b1", "name": "علمي"}]

    async def get_playable_subjects(self, branch_id):
        return [{"id": SUBJ, "name": "الفيزياء"}]

    async def get_playable_units(self, subject_id):
        return [{"id": UNIT, "name": "الحركة"}]


class FakeGame:
    def __init__(self, branch="b1"):
        self.branch = branch

    async def profile(self, user_id, name, branch=None):
        return {"branch_id": self.branch, "trophies": 100}

    async def get_profile(self, user_id):
        return {"win_streak": 3, "trophies": 108}


def make_env(live, bot=None, flags=None, game=None):
    flags = {"challenge_system_enabled": True, "live_matches_enabled": True} if flags is None else flags

    async def enabled(key, default=False):
        return flags.get(key, default)

    env = load_functions(HANDLER, NAMES, live=live, content=FakeContent(), game_repo=game or FakeGame(),
                         is_feature_enabled=enabled, ui=ui, types=SimpleNamespace(InlineKeyboardMarkup=Markup, InlineKeyboardButton=Btn),
                         TelegramBadRequest=BadRequest, TelegramForbiddenError=Forbidden, logger=Logs(), asyncio=asyncio,
                         UUID=UUID, OrderedDict=OrderedDict, _search={}, _cache=OrderedDict(), _pending={}, _status={},
                         _seen=set(), _labels={}, _MAX_CACHE=5000)
    async def wait_for_work(delay):
        await env["asyncio"].sleep(delay)
    env.update(deliver_screen=deliver_screen, log_failure=log_failure,
               _wake=asyncio.Event(), _present_locks=WeakValueDictionary(), wait_for_work=wait_for_work)
    env["bot"] = bot or FakeBot()
    return env


def user(uid=7):
    return SimpleNamespace(id=uid, full_name="أحمد", username=None)


def callback(data, uid=7, cid="cb1", message_id=100):
    async def ack():
        return None
    return SimpleNamespace(id=cid, data=data, from_user=user(uid), message=SimpleNamespace(message_id=message_id, chat=SimpleNamespace(id=uid)),
                           answer=ack, bot=None)


def mstate(status="in_progress", index=0, total=10, me_answered=False, opp_answered=False, previous=None, **extra):
    state = {"id": MID, "kind": "live", "status": status, "index": index, "total": total,
             "labels": {"subject": "الفيزياء", "unit": "الحركة", "test": "اختبار"},
             "me": {"id": 7, "name": "أحمد", "score": 0, "answered": me_answered, "option_id": None},
             "opponent": {"id": 8, "name": "سارة", "score": 0, "answered": opp_answered},
             "question": {"text": "ما هو؟", "options": ["أ", "ب", "ج", "د"], "image_url": None} if status == "in_progress" else None,
             "deadline": "2026-10-05T10:00:30+00:00", "countdown_until": "2026-10-05T10:00:13+00:00",
             "server_now": NOW, "previous": previous, "result": None}
    state.update(extra)
    return state


def result(winner=7, draw=False, forfeit=None, reason="completed", mine=None, theirs=None):
    return {"reason": reason, "total": 10, "winner_id": winner, "draw": draw, "forfeit_by": forfeit, "players": [
        {"user_id": 7, "score": 800, "correct": 8, "xp": 80, "trophies": 8, "average_response_ms": 4200, **(mine or {})},
        {"user_id": 8, "score": 500, "correct": 5, "xp": 50, "trophies": 0, "average_response_ms": 6100, **(theirs or {})}]}


def run(coro):
    return asyncio.run(coro)


def code_identifiers(path):
    """Names, attributes and imported modules actually used by the code (comments/docstrings excluded)."""
    import ast
    tree = ast.parse((Path(__file__).resolve().parents[1] / path).read_text())
    out = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            out.add(node.id)
        elif isinstance(node, ast.Attribute):
            out.add(node.attr)
        elif isinstance(node, ast.ImportFrom):
            out.add(node.module or "")
            out.update(a.name for a in node.names)
        elif isinstance(node, ast.Import):
            out.update(a.name for a in node.names)
    return out


def all_data(rows):
    return [b.data for row in rows for b in row if b.data]


# ======================================================================================= pure UI
class CallbackDataTests(unittest.TestCase):
    def test_every_callback_fits_telegram_and_carries_no_user_or_answer(self):
        mid = ui.hexid(MID)
        samples = [ui.cb("a", mid, 9, 9), ui.cb("p", mid, 9, 2), ui.cb("r", mid), ui.cb("x", mid), ui.cb("z", mid),
                   ui.cb("j", ui.hexid(UNIT)), ui.cb("u", ui.hexid(SUBJ), 12), ui.cb("c", "a1b2c3d4e5f6"), ui.cb("q"), ui.cb("m", 3)]
        for data in samples:
            self.assertLessEqual(len(data.encode()), 64, data)
            self.assertTrue(data.startswith("lv:"))
        with self.assertRaises(ValueError):
            ui.cb("a", "x" * 70)

    def test_request_key_is_idempotent_per_callback_and_distinct_across_presses(self):
        self.assertEqual(ui.request_key(7, "cbA"), ui.request_key(7, "cbA"))
        self.assertNotEqual(ui.request_key(7, "cbA"), ui.request_key(7, "cbB"))
        self.assertNotEqual(ui.request_key(7, "cbA"), ui.request_key(8, "cbA"))
        key = ui.request_key(7, "cbA")
        self.assertEqual(ui.key_of(ui.token_of(key)), key)
        self.assertIsNone(ui.key_of("-"))

    def test_backend_errors_become_clean_arabic_without_internal_codes(self):
        for code in ui.ERRORS:
            text, rows = ui.error_screen(Exception(f'{{"message":"{code}"}}'))
            self.assertNotIn(code, text)
            self.assertTrue(rows)
        self.assertIn("متابعة", [b.text for r in ui.error_screen(Exception("already_queued"))[1] for b in r][0])
        text, _ = ui.error_screen(Exception("connection reset"))
        self.assertEqual(text, ui.GENERIC_ERROR)


class RenderTests(unittest.TestCase):
    def test_countdown_uses_server_timestamps_only(self):
        text, rows = ui.match_screen(mstate("created"))
        self.assertIn("تم العثور على خصم", text)
        self.assertIn("سارة", text)
        self.assertIn("تبدأ المباراة بعد 3", text)
        text2, _ = ui.match_screen(mstate("created", server_now="2026-10-05T10:00:12+00:00"))
        self.assertIn("بعد 1", text2)
        text3, _ = ui.match_screen(mstate("created", server_now="2026-10-05T10:00:13+00:00"))
        self.assertIn("ابدأ", text3)
        text4, _ = ui.match_screen(mstate("created", server_now="2026-10-05T10:09:00+00:00"))   # missed the countdown: current state
        self.assertIn("ابدأ", text4)
        self.assertNotIn("بعد 5", text4)

    def test_open_question_never_exposes_answer_data(self):
        state = mstate(question={"text": "س", "options": ["أ", "ب"], "image_url": None, "correct_option_id": 7777, "SECRET": "LEAK-1"},
                       opponent={"id": 8, "name": "سارة", "score": 0, "answered": True, "option_id": 8888, "SECRET": "LEAK-2"},
                       snapshot=[{"correct_option_id": 6666}], correct_option_id=5555)
        text, rows = ui.question_screen(state)
        blob = text + " ".join(all_data(rows))
        for marker in ("LEAK", "7777", "8888", "6666", "5555"):
            self.assertNotIn(marker, blob, marker)
        self.assertNotIn("الإجابة الصحيحة", text)                       # no `previous` => nothing revealed
        self.assertIn("سارة أجاب بالفعل", text)                          # boolean only
        self.assertEqual(all_data(rows)[:2], [ui.cb("a", ui.hexid(MID), 0, 0), ui.cb("a", ui.hexid(MID), 0, 1)])

    def test_previous_round_is_shown_only_from_the_resolved_block(self):
        prev = {"index": 1, "correct_option_id": 2,
                "mine": {"option_id": 2, "is_correct": True, "score": 97, "timed_out": False},
                "opponent": {"option_id": 0, "is_correct": False, "score": 0, "timed_out": False}}
        text, _ = ui.question_screen(mstate(index=2, previous=prev))
        self.assertIn("نتيجة السؤال السابق", text)
        self.assertIn("أنت: ✅ صحيحة (+97)", text)
        self.assertIn("سارة: ❌ خاطئة", text)
        self.assertIn("الإجابة الصحيحة: الخيار 3", text)
        timeout = dict(prev, mine={"option_id": None, "is_correct": False, "score": 0, "timed_out": True})
        self.assertIn("⌛", ui.question_screen(mstate(index=2, previous=timeout))[0])

    def test_answered_player_gets_no_option_buttons(self):
        text, rows = ui.question_screen(mstate(me_answered=True))
        self.assertIn("تم تسجيل إجابتك", text)
        self.assertEqual([d for d in all_data(rows) if d.startswith("lv:a:")], [])
        self.assertTrue(any(d.startswith("lv:r:") for d in all_data(rows)))

    def test_source_text_is_complete_and_image_is_embedded(self):
        state = mstate(question={"text": "س" * 2000, "options": ["خ" * 500] * 7})
        for page in range(4):
            text, rows = ui.question_screen(state, page=page)
            self.assertLessEqual(len(text), 4096)
            self.assertIn(state["question"]["text"], text)
            self.assertIn("خ" * 500, text)
        state["question"]["image_url"] = "https://x/y.png"
        text, rows = ui.question_screen(state)
        self.assertEqual(text.image_url, "https://x/y.png")
        self.assertLessEqual(len(text), 1024)
        self.assertEqual(len([d for d in all_data(rows) if d.startswith("lv:a:")]), 7)
        self.assertFalse(any(b.url for r in rows for b in r))

    def test_completed_screen_win_draw_loss(self):
        win, rows = ui.final_screen(mstate("completed", index=10, result=result()), {"win_streak": 4, "trophies": 108})
        for part in ("الفائز: أنت", "800 نقطة", "✅ 8/10", "متوسط زمن إجابتك: 4.2", "+80 XP", "+8 كأس", "سلسلة الفوز: 4"):
            self.assertIn(part, win)
        self.assertEqual([b.text for r in rows for b in r], ["🔁 تحدي جديد", "📊 التفاصيل", "🏕️ أرضي"])
        self.assertIn("🤝 تعادل", ui.final_screen(mstate("completed", result=result(winner=None, draw=True)))[0])
        loss = ui.final_screen(mstate("completed", result=result(winner=8, mine={"xp": 0, "trophies": 0})))[0]
        self.assertIn("الفائز: سارة", loss)
        self.assertIn("لا مكافأة", loss)

    def test_abandoned_expired_cancelled_screens(self):
        quit_me = ui.final_screen(mstate("abandoned", result=result(winner=8, forfeit=7, reason="abandoned", mine={"xp": 0, "trophies": 0})))[0]
        self.assertIn("انسحبت", quit_me)
        quit_opp = ui.final_screen(mstate("abandoned", result=result(winner=7, forfeit=8, reason="abandoned", mine={"xp": 0}, theirs={"xp": 0})))[0]
        self.assertIn("فزت", quit_opp)
        self.assertIn("+0 XP", quit_opp)
        exp = ui.final_screen(mstate("expired", result={"reason": "expired", "players": []}))[0]
        self.assertIn("انتهت مهلة المباراة", exp)
        self.assertIn("لا مكافآت", exp)
        self.assertIn("ألغيت المباراة", ui.final_screen(mstate("cancelled", result={"forfeit_by": 7, "players": []}))[0])
        self.assertIn("ألغى خصمك", ui.final_screen(mstate("cancelled", result={"forfeit_by": 8, "players": []}))[0])

    def test_overdue_and_signature(self):
        self.assertTrue(ui.is_overdue(mstate("created", server_now="2026-10-05T10:00:13+00:00")))
        self.assertFalse(ui.is_overdue(mstate("created")))
        self.assertTrue(ui.is_overdue(mstate(server_now="2026-10-05T10:00:30+00:00")))
        self.assertFalse(ui.is_overdue(mstate()))
        self.assertFalse(ui.is_overdue(mstate("completed")))
        a, b = ui.match_screen(mstate()), ui.match_screen(mstate(index=1))
        self.assertEqual(ui.signature(a), ui.signature(ui.match_screen(mstate())))
        self.assertNotEqual(ui.signature(a), ui.signature(b))

    def test_menus_show_only_what_backend_returned(self):
        self.assertEqual(ui.menu_subjects("علمي", [])[0], ui.EMPTY)
        self.assertEqual(ui.menu_units("الفيزياء", SUBJ, [])[0], ui.EMPTY)
        text, rows = ui.menu_units("الفيزياء", SUBJ, [{"id": UNIT, "name": "الحركة"}])
        self.assertEqual(all_data(rows)[0], ui.cb("j", ui.hexid(UNIT)))


# ======================================================================================= search flow
class SearchTests(unittest.TestCase):
    def test_start_search_joins_with_telegram_identity_and_shows_searching(self):
        live = FakeLive(join_queue={"status": "waiting", "unit_id": UNIT, "waited_seconds": 0,
                                    "expires_at": "2026-10-05T10:02:10+00:00", "server_now": NOW})
        e = make_env(live)
        c = callback(ui.cb("j", ui.hexid(UNIT)), uid=7, cid="CB-9")
        c.bot = e["bot"]
        run(e["live_callback"](c))
        name, args, _ = live.calls[0]
        self.assertEqual((name, args[0], args[1], args[2]), ("join_queue", 7, UNIT, ui.request_key(7, "CB-9")))
        self.assertIn("جارٍ البحث عن خصم", e["bot"].edits[-1].text)
        self.assertIn("الفيزياء", e["bot"].edits[-1].text)
        self.assertIn(ui.cb("c", ui.token_of(args[2])), [x.callback_data for row in e["bot"].edits[-1].markup.inline_keyboard for x in row])
        self.assertEqual(e["_search"][7]["message_id"], 100)

    def test_user_id_in_callback_data_is_never_trusted(self):
        live = FakeLive(join_queue={"status": "waiting", "unit_id": UNIT, "server_now": NOW}, answer=mstate())
        e = make_env(live)
        c = callback(ui.cb("j", ui.hexid(UNIT)), uid=999)       # real Telegram identity is from_user.id
        c.bot = e["bot"]
        run(e["live_callback"](c))
        self.assertEqual(live.calls[0][1][0], 999)

    def test_backend_rejections_show_clean_messages(self):
        for code in ("already_queued", "in_live_match", "active_attack", "game_disabled", "no_content", "branch_mismatch",
                     "profile_missing", "invalid_request"):
            live = FakeLive(join_queue=Exception(code), queue_state={"status": "idle", "server_now": NOW})
            e = make_env(live)
            c = callback(ui.cb("j", ui.hexid(UNIT)))
            c.bot = e["bot"]
            run(e["live_callback"](c))
            shown = " ".join(e["bot"].shown())
            if code in ("already_queued", "in_live_match"):
                continue                                              # resumed, covered below
            self.assertNotIn(code, shown, code)
            self.assertIn(ui.ERRORS[code], shown)

    def test_double_press_keeps_original_search_and_does_not_show_an_error(self):
        live = FakeLive(join_queue=({"status": "waiting", "unit_id": UNIT, "server_now": NOW, "waited_seconds": 0},
                                    Exception("already_queued")),
                        queue_state={"status": "waiting", "unit_id": UNIT, "server_now": NOW, "waited_seconds": 1})
        e = make_env(live)
        c1, c2 = callback(ui.cb("j", ui.hexid(UNIT)), cid="first"), callback(ui.cb("j", ui.hexid(UNIT)), cid="second")
        c1.bot = c2.bot = e["bot"]
        run(e["live_callback"](c1))
        run(e["live_callback"](c2))
        self.assertEqual(e["_search"][7]["tok"], ui.token_of(ui.request_key(7, "first")))     # cancel must still target the live key
        self.assertNotIn("بالفعل", " ".join(e["bot"].shown()))
        self.assertIn("جارٍ البحث", e["bot"].edits[-1].text)

    def test_menu_with_active_match_resumes_instead_of_new_search(self):
        live = FakeLive(queue_state={"status": "matched", "match_id": MID, "server_now": NOW}, match_state=mstate())
        e = make_env(live)
        run(e["menu"](e["bot"], user(), 100))
        self.assertIn("السؤال 1 / 10", e["bot"].edits[-1].text)
        self.assertNotIn("join_queue", live.names())

    def test_cancel_waiting_search(self):
        live = FakeLive(cancel_search={"status": "cancelled", "server_now": NOW})
        e = make_env(live)
        e["_search"][7] = {"message_id": 100, "tok": "abc123", "unit_hex": ui.hexid(UNIT), "labels": {}}
        run(e["cancel"](e["bot"], user(), 100, "abc123"))
        self.assertEqual(live.calls[0][1:], ((7, "lv:abc123"), {}))
        self.assertIn("تم إلغاء البحث", e["bot"].edits[-1].text)
        self.assertNotIn(7, e["_search"])

    def test_cancel_after_match_exists_shows_match_and_never_leaves_it(self):
        live = FakeLive(cancel_search={"status": "matched", "match_id": MID, "server_now": NOW}, match_state=mstate("created"))
        e = make_env(live)
        run(e["cancel"](e["bot"], user(), 100, "abc123"))
        self.assertIn("تم العثور على خصم", e["bot"].edits[-1].text)
        self.assertNotIn("leave", live.names())

    def test_unknown_token_cancel_targets_own_waiting_search(self):
        live = FakeLive(cancel_search={"status": "cancelled", "server_now": NOW})
        e = make_env(live)
        run(e["cancel"](e["bot"], user(), 100, "-"))
        self.assertIsNone(live.calls[0][1][1])

    def test_queue_expiry_in_worker_notifies_with_options(self):
        live = FakeLive(waiting_users=[], sweep={"expired_users": [7], "matches": []}, active_matches=[])
        e = make_env(live)
        e["_search"][7] = {"message_id": 100, "tok": "t", "unit_hex": ui.hexid(UNIT), "labels": {}}
        run(e["live_beat"](e["bot"]))
        edit = e["bot"].edits[-1]
        self.assertEqual((edit.chat_id, edit.message_id), (7, 100))
        self.assertIn("لم نجد خصمًا مناسبًا", edit.text)
        data = [x.callback_data for row in edit.markup.inline_keyboard for x in row]
        self.assertEqual(data, [ui.cb("j", ui.hexid(UNIT)), "cg:l:-1:0", "cg:home:0"])
        self.assertNotIn(7, e["_search"])

    def test_queue_screens_via_resume(self):
        for status, expect in (("expired", "لم نجد خصمًا"), ("cancelled", "تم إلغاء البحث"), ("idle", "لا توجد مباراة"), ("done", "لا توجد مباراة")):
            live = FakeLive(queue_state={"status": status, "server_now": NOW, "match_id": MID if status == "done" else None})
            e = make_env(live)
            run(e["resume"](e["bot"], user(), 100))
            self.assertIn(expect, e["bot"].edits[-1].text, status)

    def test_flags_off_blocks_new_search_ui_but_not_inflight_actions(self):
        live = FakeLive(queue_state={"status": "idle", "server_now": NOW})
        e = make_env(live, flags={"challenge_system_enabled": True, "live_matches_enabled": False})
        run(e["menu"](e["bot"], user(), 100))
        self.assertEqual(e["bot"].edits[-1].text, ui.DISABLED)
        e2 = make_env(FakeLive(queue_state={"status": "idle", "server_now": NOW}), flags={"challenge_system_enabled": False, "live_matches_enabled": True})
        run(e2["menu"](e2["bot"], user(), 100))
        self.assertEqual(e2["bot"].edits[-1].text, ui.DISABLED)
        # answering / refreshing / leaving never consult the flags: an in-flight match keeps working
        live3 = FakeLive(answer=mstate(index=1), match_state=mstate(), leave=mstate("abandoned", result=result(winner=8, forfeit=7, reason="abandoned")))
        e3 = make_env(live3, flags={})
        c = callback(ui.cb("a", ui.hexid(MID), 0, 1))
        c.bot = e3["bot"]
        run(e3["live_callback"](c))
        self.assertIn("السؤال 2 / 10", e3["bot"].edits[-1].text)

    def test_backend_game_disabled_on_join_is_friendly(self):
        e = make_env(FakeLive(join_queue=Exception("game_disabled")))
        c = callback(ui.cb("j", ui.hexid(UNIT)))
        c.bot = e["bot"]
        run(e["live_callback"](c))
        self.assertIn(ui.DISABLED, e["bot"].shown())


# ======================================================================================= answers
class AnswerTests(unittest.TestCase):
    def go(self, live, data, uid=7, cid="CBX"):
        e = make_env(live)
        c = callback(data, uid=uid, cid=cid)
        c.bot = e["bot"]
        run(e["live_callback"](c))
        return e

    def test_answer_goes_to_backend_with_from_user_and_update_key(self):
        live = FakeLive(answer=mstate(me_answered=True))
        e = self.go(live, ui.cb("a", ui.hexid(MID), 4, 2), uid=7, cid="UPD-1")
        self.assertEqual(live.calls[0], ("answer", (MID, 7, 4, 2, "lv:UPD-1"), {}))
        self.assertIn("تم تسجيل إجابتك", e["bot"].edits[-1].text)

    def test_duplicate_callback_reuses_update_key_and_renders_safely(self):
        live = FakeLive(answer=mstate(me_answered=True))
        e = make_env(live)
        for _ in range(2):
            c = callback(ui.cb("a", ui.hexid(MID), 0, 1), cid="SAME")
            c.bot = e["bot"]
            run(e["live_callback"](c))
        keys = [call[1][4] for call in live.calls if call[0] == "answer"]
        self.assertEqual(keys, ["lv:SAME", "lv:SAME"])
        self.assertEqual(len(e["bot"].edits), 1)                      # identical screen => no second edit

    def test_stale_question_callback_shows_current_state_and_records_nothing_here(self):
        live = FakeLive(answer=mstate(index=5))                        # backend ignored index 1, returned the live question 5
        e = self.go(live, ui.cb("a", ui.hexid(MID), 1, 0))
        self.assertIn("السؤال 6 / 10", e["bot"].edits[-1].text)
        self.assertNotIn("خطأ", e["bot"].edits[-1].text)

    def test_wrong_user_and_invalid_option_and_unknown_match(self):
        for err, text in (("not_your_match", ui.ERRORS["not_your_match"]), ("invalid_option", ui.ERRORS["invalid_option"]),
                          ("not_found", ui.ERRORS["not_found"])):
            e = self.go(FakeLive(answer=Exception(err)), ui.cb("a", ui.hexid(MID), 0, 99), uid=555)
            self.assertIn(text, e["bot"].edits[-1].text)
            self.assertNotIn(err, e["bot"].edits[-1].text)

    def test_timeout_and_finished_match_renderings(self):
        timeout = {"index": 0, "correct_option_id": 1, "mine": {"is_correct": False, "score": 0, "timed_out": True},
                   "opponent": {"is_correct": True, "score": 90, "timed_out": False}}
        e = self.go(FakeLive(answer=mstate(index=1, previous=timeout)), ui.cb("a", ui.hexid(MID), 0, 0))
        self.assertIn("⌛", e["bot"].edits[-1].text)
        e = self.go(FakeLive(answer=mstate("completed", index=10, result=result())), ui.cb("a", ui.hexid(MID), 9, 0))
        self.assertIn("انتهت المواجهة", e["bot"].edits[-1].text)
        self.assertIn("سلسلة الفوز: 3", e["bot"].edits[-1].text)     # streak comes from the profile, not invented

    def test_malformed_callback_is_contained(self):
        e = self.go(FakeLive(), "lv:a:not-a-uuid:x:y")
        self.assertIn(ui.GENERIC_ERROR, e["bot"].edits[-1].text)

    def test_option_page_ignored_for_old_question(self):
        live = FakeLive(match_state=mstate(index=2, question={"text": "س", "options": ["1", "2", "3", "4", "5"], "image_url": None}))
        e = make_env(live)
        run(e["options_page"](e["bot"], user(), 100, MID, 1, 1))     # page for question 1, but live question is 2
        self.assertIn("الإجابة 1", [b.text for row in e["bot"].edits[-1].markup.inline_keyboard for b in row])
        e2 = make_env(live)
        run(e2["options_page"](e2["bot"], user(), 100, MID, 2, 1))
        self.assertEqual([b.text for row in e2["bot"].edits[-1].markup.inline_keyboard for b in row][0], "الإجابة 3")

    def test_leave_requires_confirmation_then_uses_backend(self):
        live = FakeLive(match_state=mstate(), leave=mstate("abandoned", result=result(winner=8, forfeit=7, reason="abandoned")))
        e = self.go(live, ui.cb("x", ui.hexid(MID)))
        self.assertIn("تأكيد الانسحاب", e["bot"].edits[-1].text)
        self.assertNotIn("leave", live.names())
        e = self.go(live, ui.cb("z", ui.hexid(MID)))
        self.assertIn("leave", live.names())
        self.assertIn("انسحبت", e["bot"].edits[-1].text)
        e = self.go(FakeLive(match_state=mstate("created")), ui.cb("x", ui.hexid(MID)))
        self.assertIn("لم تبدأ بعد", e["bot"].edits[-1].text)


# ======================================================================================= message handling
class MessageHandlingTests(unittest.TestCase):
    def test_edit_success_reuses_message_and_attaches_pointer_once(self):
        live = FakeLive(attach_message=None)
        e = make_env(live)
        run(e["present"](e["bot"], 7, 100, mstate()))
        run(e["present"](e["bot"], 7, 100, mstate(index=1)))
        self.assertEqual(len(e["bot"].edits), 2)
        self.assertEqual(e["bot"].sends, [])
        self.assertEqual([c[0] for c in live.calls].count("attach_message"), 1)

    def test_message_not_modified_is_not_an_error(self):
        live = FakeLive(attach_message=None)
        e = make_env(live)
        e["bot"].edit_error = BadRequest("Bad Request: message is not modified: specified new message content ...")
        self.assertEqual(run(e["present"](e["bot"], 7, 100, mstate())), 100)
        self.assertEqual(e["bot"].sends, [])

    def test_edit_failure_falls_back_to_new_message_and_attaches_it(self):
        live = FakeLive(attach_message=None)
        e = make_env(live)
        e["bot"].edit_error = BadRequest("Bad Request: message to edit not found")
        shown = run(e["present"](e["bot"], 7, 100, mstate()))
        self.assertEqual(shown, 501)
        self.assertIn("السؤال 1 / 10", e["bot"].sends[0].text)
        self.assertIn(("attach_message", (MID, 7, 501), {}), live.calls)

    def test_old_message_cannot_be_edited_is_also_a_fallback(self):
        e = make_env(FakeLive(attach_message=None))
        e["bot"].edit_error = BadRequest("Bad Request: message can't be edited")
        self.assertEqual(run(e["present"](e["bot"], 7, 100, mstate())), 501)

    def test_blocked_user_does_not_break_or_retry_forever(self):
        live = FakeLive(match_state=mstate(), attach_message=None)
        e = make_env(live)
        e["bot"].edit_error = Forbidden("bot was blocked")
        e["bot"].send_error = Forbidden("bot was blocked")
        row = {"id": MID, "status": "in_progress", "question_index": 0, "attacker_id": 7, "defender_id": 8}
        run(e["render_for"](e["bot"], row, 7, 100))
        run(e["render_for"](e["bot"], row, 7, 100))
        self.assertEqual(live.names().count("match_state"), 1)         # second beat skipped: state remembered as unreachable

    def test_attach_failure_never_changes_what_the_user_sees(self):
        live = FakeLive(attach_message=Exception("db down"))
        e = make_env(live)
        e["bot"].edit_error = BadRequest("message to edit not found")
        self.assertEqual(run(e["present"](e["bot"], 7, 100, mstate())), 501)


# ======================================================================================= recovery & refresh
class RecoveryTests(unittest.TestCase):
    def test_active_match_restored_from_postgres_after_restart(self):
        live = FakeLive(queue_state={"status": "matched", "match_id": MID, "server_now": NOW}, match_state=mstate(index=3, me_answered=False),
                        attach_message=None)
        e = make_env(live)                                              # brand-new process: every cache is empty
        self.assertEqual((e["_search"], len(e["_cache"]), e["_seen"]), ({}, 0, set()))
        c = callback(ui.cb("q"))
        c.bot = e["bot"]
        run(e["live_callback"](c))
        self.assertIn("السؤال 4 / 10", e["bot"].edits[-1].text)
        self.assertEqual(live.names()[:2], ["queue_state", "match_state"])

    def test_recovered_waiting_search_can_still_be_cancelled(self):
        live = FakeLive(queue_state={"status": "waiting", "unit_id": UNIT, "waited_seconds": 12, "expires_at": "2026-10-05T10:02:10+00:00", "server_now": NOW})
        e = make_env(live)
        run(e["resume"](e["bot"], user(), 100))
        self.assertIn("12 ث", e["bot"].edits[-1].text)
        self.assertIn(ui.cb("c", "-"), [x.callback_data for row in e["bot"].edits[-1].markup.inline_keyboard for x in row])

    def test_worker_after_restart_renders_both_players_once(self):
        live = FakeLive(waiting_users=[], sweep={"expired_users": [], "matches": []},
                        active_matches=[{"id": MID, "status": "in_progress", "question_index": 4, "attacker_id": 7, "defender_id": 8}],
                        player_messages={(MID, 7): 70, (MID, 8): 80},
                        match_state=lambda mid, uid: mstate(index=4), attach_message=None)
        e = make_env(live)
        run(e["live_beat"](e["bot"]))
        self.assertEqual(sorted((x.chat_id, x.message_id) for x in e["bot"].edits), [(7, 70), (8, 80)])
        run(e["live_beat"](e["bot"]))
        self.assertEqual(len(e["bot"].edits), 2)                        # unchanged state => no spam

    def test_refresh_only_rerenders_when_not_overdue(self):
        live = FakeLive(match_state=mstate(), attach_message=None)
        e = make_env(live)
        run(e["refresh"](e["bot"], user(), 100, MID))
        self.assertNotIn("tick", live.names())

    def test_refresh_ticks_when_server_time_already_passed_a_deadline(self):
        overdue = mstate(server_now="2026-10-05T10:00:31+00:00")
        live = FakeLive(match_state=(overdue, mstate(index=1)), tick={"status": "in_progress"}, attach_message=None)
        e = make_env(live)
        run(e["refresh"](e["bot"], user(), 100, MID))
        self.assertEqual(live.names()[:3], ["match_state", "tick", "match_state"])
        self.assertIn("السؤال 2 / 10", e["bot"].edits[-1].text)

    def test_refresh_on_old_message_adopts_it_as_the_live_message(self):
        live = FakeLive(match_state=mstate(), attach_message=None)
        e = make_env(live)
        run(e["present"](e["bot"], 7, 100, mstate(index=0)))
        run(e["refresh"](e["bot"], user(), 250, MID))                  # user pressed a button on an older message
        self.assertIn(("attach_message", (MID, 7, 250), {}), live.calls)

    def test_no_redis_dependency_in_live_ui(self):
        for path in (HANDLER, "services/challenge_live_ui.py"):
            self.assertFalse([n for n in code_identifiers(path) if "redis" in n.lower()], path)


# ======================================================================================= worker
class WorkerTests(unittest.TestCase):
    ROW = {"id": MID, "status": "created", "question_index": 0, "attacker_id": 7, "defender_id": 8}

    def test_beat_cost_is_fixed_regardless_of_match_count_and_pairs_only_two_or_more(self):
        rows = [{"id": f"{i:08d}-1111-1111-1111-111111111111", "status": "in_progress", "question_index": 1, "attacker_id": 100 + i, "defender_id": 200 + i} for i in range(30)]
        msgs = {(r["id"], u): 9 for r in rows for u in (r["attacker_id"], r["defender_id"])}
        live = FakeLive(waiting_users=[1], sweep={"expired_users": []}, active_matches=rows, player_messages=msgs,
                        match_state=lambda m, u: mstate(index=1), attach_message=None)
        e = make_env(live)
        for r in rows:                                                  # already on screen
            for u in (r["attacker_id"], r["defender_id"]):
                e["_cache"][(r["id"], u)] = {"key": ("in_progress", 1), "sig": "x", "message_id": 9}
        run(e["live_beat"](e["bot"]))
        self.assertEqual(live.names().count("match_state"), 60)  # refresh same-question answer flags
        live2 = FakeLive(waiting_users=[1, 2], sweep={"expired_users": []}, active_matches=[], pair_waiting=[])
        e2 = make_env(live2)
        run(e2["live_beat"](e2["bot"]))
        self.assertIn("pair_waiting", live2.names())

    def test_beat_interval_is_fast_only_during_countdown(self):
        live = FakeLive(waiting_users=[], sweep={"expired_users": []}, active_matches=[self.ROW], player_messages={(MID, 7): 1, (MID, 8): 2},
                        match_state=lambda m, u: mstate("created"), attach_message=None)
        e = make_env(live)
        self.assertEqual(run(e["live_beat"](e["bot"])), 1)
        live2 = FakeLive(waiting_users=[], sweep={"expired_users": []}, active_matches=[dict(self.ROW, status="in_progress")],
                         player_messages={(MID, 7): 1, (MID, 8): 2}, match_state=lambda m, u: mstate(), attach_message=None)
        e2 = make_env(live2)
        self.assertEqual(run(e2["live_beat"](e2["bot"])), 2)

    def test_countdown_renders_each_second_without_duplicate_edits(self):
        states = iter([mstate("created"), mstate("created"), mstate("created", server_now="2026-10-05T10:00:11+00:00"),
                       mstate("created", server_now="2026-10-05T10:00:11+00:00")])
        live = FakeLive(match_state=lambda m, u: next(states), attach_message=None)
        e = make_env(live)
        for _ in range(2):
            run(e["render_for"](e["bot"], self.ROW, 7, 1))
            run(e["render_for"](e["bot"], self.ROW, 8, 2))
        texts = [x.text for x in e["bot"].edits]
        self.assertEqual(len(texts), 4)
        self.assertIn("بعد 3", texts[0])
        self.assertIn("بعد 2", texts[2])

    def test_waiting_player_adopts_search_message_when_opponent_created_the_match(self):
        live = FakeLive(match_state=mstate("created"), attach_message=None)
        e = make_env(live)
        e["_search"][8] = {"message_id": 777, "tok": "t", "unit_hex": "u", "labels": {}}
        run(e["render_for"](e["bot"], self.ROW, 8, None))
        self.assertEqual((e["bot"].edits[-1].chat_id, e["bot"].edits[-1].message_id), (8, 777))
        self.assertEqual(e["bot"].sends, [])
        self.assertIn(("attach_message", (MID, 8, 777), {}), live.calls)
        self.assertNotIn(8, e["_search"])

    def test_player_without_message_pointer_is_not_delayed_by_grace_beats(self):
        live = FakeLive(match_state=mstate("created"), attach_message=None)
        e = make_env(live)
        run(e["render_for"](e["bot"], self.ROW, 8, None))
        self.assertEqual(len(e["bot"].sends), 1)
        self.assertIn(("attach_message", (MID, 8, 501), {}), live.calls)

    def test_finished_match_is_rendered_for_both_even_after_queue_rows_are_done(self):
        live = FakeLive(has_live_work=False, waiting_users=[], sweep={"expired_users": []},
                        active_matches=[], matches_by_ids=[dict(self.ROW, status="completed", question_index=10)],
                        player_messages={(MID, 7): 70, (MID, 8): 80},
                        match_state=lambda m, u: mstate("completed", index=10, result=result()), attach_message=None)
        e = make_env(live)
        e["_seen"].add(MID)
        loops = []

        async def one_loop():
            async def stop_sleep(delay):
                loops.append(delay)
                raise asyncio.CancelledError
            e["asyncio"] = SimpleNamespace(sleep=stop_sleep, CancelledError=asyncio.CancelledError, Lock=asyncio.Lock, gather=asyncio.gather, Semaphore=asyncio.Semaphore)
            e["challenge_live_loop"].__globals__["asyncio"] = e["asyncio"]
            await e["challenge_live_loop"](e["bot"])
        with self.assertRaises(asyncio.CancelledError):
            run(one_loop())
        self.assertEqual(sorted(x.chat_id for x in e["bot"].edits), [7, 8])
        self.assertTrue(all("انتهت المواجهة" in x.text for x in e["bot"].edits))
        self.assertEqual(e["_seen"], set())

    def test_idle_loop_is_cheap_and_errors_back_off(self):
        delays = []

        def loop_env(live):
            e = make_env(live)

            async def sleep(delay):
                delays.append(delay)
                raise asyncio.CancelledError
            e["asyncio"] = SimpleNamespace(sleep=sleep, CancelledError=asyncio.CancelledError, Lock=asyncio.Lock, gather=asyncio.gather, Semaphore=asyncio.Semaphore)
            e["challenge_live_loop"].__globals__["asyncio"] = e["asyncio"]
            return e
        e = loop_env(FakeLive(has_live_work=False))
        with self.assertRaises(asyncio.CancelledError):
            run(e["challenge_live_loop"](e["bot"]))
        e = loop_env(FakeLive(has_live_work=Exception('relation "challenge_live_queue" does not exist')))
        with self.assertRaises(asyncio.CancelledError):
            run(e["challenge_live_loop"](e["bot"]))
        self.assertEqual(delays, [2, 5])

    def test_match_started_is_logged_once_on_the_transition(self):
        e = make_env(FakeLive())
        for status in ("created", "created", "in_progress", "in_progress", "completed"):
            e["_log_transition"](dict(self.ROW, status=status))
        self.assertEqual([x for x in e["logger"].lines if x.startswith("match_started")], [f"match_started match={MID}"])

    def test_render_error_for_one_player_does_not_stop_the_other_or_the_match(self):
        def state_for(m, u):
            if u == 7:
                raise RuntimeError("telegram flood")
            return mstate("created")
        live = FakeLive(waiting_users=[], sweep={"expired_users": []}, active_matches=[self.ROW],
                        player_messages={(MID, 7): 1, (MID, 8): 2}, match_state=state_for, attach_message=None)
        e = make_env(live)
        run(e["live_beat"](e["bot"]))
        self.assertEqual([x.chat_id for x in e["bot"].edits], [8])
        self.assertNotIn("leave", live.names())


# ======================================================================================= repo + wiring
class FakeTable:
    def __init__(self, rows, log):
        self.rows, self.log = rows, log

    def __getattr__(self, name):
        def call(*a):
            self.log.append((name, a))
            return self
        return call

    async def execute(self):
        return MagicMock(data=self.rows)


class RepoReadHelperTests(unittest.TestCase):
    def env(self, rows):
        log = []
        sb = SimpleNamespace(table=lambda name: (log.append(("table", name)) or FakeTable(rows, log)))
        return load_functions("helpers/supabase_helper/challenge_live.py", ["active_matches", "matches_by_ids", "player_messages"],
                              supabase=sb, _MATCH_COLS="id,status"), log

    def test_active_matches_is_one_filtered_query(self):
        f, log = self.env([{"id": "m"}])
        self.assertEqual(run(f["active_matches"]()), [{"id": "m"}])
        self.assertIn(("eq", ("kind", "live")), log)
        self.assertIn(("in_", ("status", ["created", "in_progress"])), log)

    def test_player_messages_maps_pairs_and_skips_empty_input(self):
        f, log = self.env([{"match_id": "m", "user_id": 7, "message_id": 70}, {"match_id": "m", "user_id": 8, "message_id": None}])
        self.assertEqual(run(f["player_messages"](["m"])), {("m", 7): 70, ("m", 8): None})
        f2, log2 = self.env([])
        self.assertEqual((run(f2["player_messages"]([])), run(f2["matches_by_ids"]([])), log2), ({}, [], []))


# ======================================================================================= end-to-end UI simulation
class SimBackend:
    """In-memory stand-in with the SAME state shapes as the 0010 RPCs (rules simplified: 3 questions, option 1 is
    correct, 5 s countdown, 20 s per question, 100 points per correct answer). It exists only to exercise the
    handler/worker coordination; SQL semantics are proven by tests/sql/*."""
    N, COUNT, QSEC = 3, 5, 20

    def __init__(self):
        self.t, self.queue, self.match, self.msg, self.requests = 0, {}, None, {}, {}

    def iso(self, t):
        return f"2026-10-05T10:{int(t) // 60:02d}:{int(t) % 60:02d}+00:00"

    # ---- queue
    async def join_queue(self, uid, unit, key):
        if key in self.requests:
            return await self.queue_state(uid)
        self.requests[key] = uid
        self.queue[uid] = {"status": "waiting", "unit_id": unit, "joined": self.t, "match_id": None}
        return await self.try_match(uid)

    async def try_match(self, uid):
        mine = self.queue.get(uid)
        others = [u for u, q in self.queue.items() if u != uid and q["status"] == "waiting"]
        if mine and mine["status"] == "waiting" and others and not self.match:
            self.match = {"id": MID, "status": "created", "q": 0, "players": sorted([uid, others[0]]), "answers": {},
                          "countdown_until": self.t + self.COUNT, "deadline": self.t + self.COUNT + self.QSEC, "result": None}
            for u in self.match["players"]:
                self.queue[u].update(status="matched", match_id=MID)
        return await self.queue_state(uid)

    async def queue_state(self, uid):
        q = self.queue.get(uid)
        base = {"server_now": self.iso(self.t)}
        if not q:
            return {"status": "idle", **base}
        return {"status": q["status"], "unit_id": q["unit_id"], "match_id": q["match_id"], "waited_seconds": int(self.t - q["joined"]),
                "expires_at": self.iso(q["joined"] + 120), **base}

    async def cancel_search(self, uid, key=None):
        q = self.queue.get(uid)
        if q and q["status"] == "waiting":
            q["status"] = "cancelled"
        return await self.queue_state(uid)

    async def waiting_users(self):
        return [u for u, q in self.queue.items() if q["status"] == "waiting"]

    async def pair_waiting(self, ids):
        return [await self.try_match(u) for u in ids]

    async def has_live_work(self):
        return any(q["status"] in ("waiting", "matched") for q in self.queue.values())

    # ---- match
    def progress(self):
        m = self.match
        if not m or m["status"] in ("completed", "abandoned", "expired", "cancelled"):
            return
        if m["status"] == "created" and self.t >= m["countdown_until"]:
            m["status"] = "in_progress"
        if m["status"] != "in_progress":
            return
        both = all((u, m["q"]) in m["answers"] for u in m["players"])
        if both or self.t >= m["deadline"]:
            for u in m["players"]:
                m["answers"].setdefault((u, m["q"]), {"option": None, "ok": False, "score": 0, "timed_out": True})
            m["q"] += 1
            m["deadline"] = self.t + self.QSEC
            if m["q"] >= self.N:
                self.finish("completed")

    def finish(self, status, loser=None):
        m = self.match
        m["status"] = status
        score = {u: sum(a["score"] for (uu, _), a in m["answers"].items() if uu == u) for u in m["players"]}
        hi = max(score.values())
        winner = None if list(score.values()).count(hi) > 1 else [u for u in score if score[u] == hi][0]
        if status == "abandoned":
            winner = [u for u in m["players"] if u != loser][0]
        m["result"] = {"reason": status, "total": self.N, "winner_id": winner, "draw": status == "completed" and winner is None,
                       "forfeit_by": loser, "players": [{"user_id": u, "score": score[u], "correct": sum(1 for (uu, _), a in m["answers"].items() if uu == u and a["ok"]),
                                                          "xp": 10 * (status == "completed"), "trophies": 8 if u == winner else 0, "average_response_ms": 3000} for u in m["players"]]}
        for u in m["players"]:
            self.queue[u]["status"] = "done"

    async def match_state(self, mid, uid):
        m = self.match
        if not m or uid not in m["players"]:
            raise Exception("not_your_match")
        opp = [u for u in m["players"] if u != uid][0]
        done = lambda u, i: (u, i) in m["answers"]
        resolved = lambda u: sum(a["score"] for (uu, i), a in m["answers"].items() if uu == u and i < m["q"])
        prev = None
        if m["q"] > 0:
            side = lambda u: {k: m["answers"][(u, m["q"] - 1)][k2] for k, k2 in (("option_id", "option"), ("is_correct", "ok"), ("score", "score"), ("timed_out", "timed_out"))}
            prev = {"index": m["q"] - 1, "correct_option_id": 1, "mine": side(uid), "opponent": side(opp)}
        live_q = m["status"] == "in_progress" and m["q"] < self.N
        return {"id": MID, "kind": "live", "status": m["status"], "index": m["q"], "total": self.N,
                "labels": {"subject": "الفيزياء", "unit": "الحركة", "test": "t"},
                "me": {"id": uid, "name": f"U{uid}", "score": resolved(uid), "answered": done(uid, m["q"]), "option_id": None},
                "opponent": {"id": opp, "name": f"U{opp}", "score": resolved(opp), "answered": done(opp, m["q"])},
                "question": {"text": f"سؤال {m['q'] + 1}", "options": ["أ", "ب", "ج"], "image_url": None} if live_q else None,
                "deadline": self.iso(m["deadline"]) if m["status"] in ("created", "in_progress") else None,
                "countdown_until": self.iso(m["countdown_until"]), "server_now": self.iso(self.t), "previous": prev, "result": m["result"]}

    async def answer(self, mid, uid, index, option, key=None):
        m = self.match
        self.progress()
        if m["status"] == "in_progress" and index == m["q"] and (uid, index) not in m["answers"]:
            ok = option == 1
            m["answers"][(uid, index)] = {"option": option, "ok": ok, "score": 100 if ok else 0, "timed_out": False}
            self.progress()
        return await self.match_state(mid, uid)

    async def leave(self, mid, uid):
        self.progress()
        if self.match["status"] in ("created", "in_progress"):
            self.finish("abandoned" if self.match["status"] == "in_progress" else "cancelled", uid)
        return await self.match_state(mid, uid)

    async def tick(self, mid):
        self.progress()
        return {"id": mid, "status": self.match["status"], "index": self.match["q"]}

    async def sweep(self, limit=50):
        self.progress()
        return {"expired_users": [], "matches": []}

    async def active_matches(self):
        m = self.match
        return [] if not m or m["status"] not in ("created", "in_progress") else [
            {"id": MID, "status": m["status"], "question_index": m["q"], "attacker_id": m["players"][0], "defender_id": m["players"][1]}]

    async def matches_by_ids(self, ids):
        m = self.match
        return [{"id": MID, "status": m["status"], "question_index": m["q"], "attacker_id": m["players"][0], "defender_id": m["players"][1]}] if m and MID in ids else []

    async def player_messages(self, ids):
        return {(MID, u): self.msg.get(u) for u in (self.match["players"] if self.match else [])}

    async def attach_message(self, mid, uid, message_id):
        self.msg[uid] = message_id


class FlowSimulationTests(unittest.TestCase):
    def press(self, e, sim, data, uid, message_id, cid):
        c = callback(data, uid=uid, cid=cid, message_id=message_id)
        c.bot = e["bot"]
        run(e["live_callback"](c))

    def beat(self, e, sim, seconds=1):
        sim.t += seconds
        run(e["live_beat"](e["bot"]))

    def screen(self, bot, chat, message_id):
        last = [x for x in bot.edits if x.chat_id == chat and x.message_id == message_id]
        return last[-1].text if last else ""

    def test_two_players_full_match_one_message_each_no_spam(self):
        sim = SimBackend()
        e = make_env(sim)
        bot, A, B = e["bot"], 7, 8
        unit = ui.cb("j", ui.hexid(UNIT))
        self.press(e, sim, unit, A, 100, "a-join")                      # A searches on message 100
        self.assertIn("جارٍ البحث", self.screen(bot, A, 100))
        self.press(e, sim, unit, B, 200, "b-join")                      # B joins: instantly matched
        self.assertIn("تم العثور على خصم", self.screen(bot, B, 200))
        self.assertIn("تم العثور على خصم", self.screen(bot, A, 100))  # no worker beat needed
        self.beat(e, sim)                                                # worker: A's SEARCH message becomes the match
        self.assertIn("تم العثور على خصم", self.screen(bot, A, 100))
        self.assertIn("بعد 4", self.screen(bot, A, 100))
        self.beat(e, sim, 4)                                             # countdown over -> worker ticks + opens Q1
        for chat, mid_ in ((A, 100), (B, 200)):
            self.assertIn("السؤال 1 / 3", self.screen(bot, chat, mid_))
            self.assertIn("ابدأ", self.screen(bot, chat, mid_))
        data = lambda chat, mid_: [x.callback_data for row in [x for x in bot.edits if x.chat_id == chat][-1].markup.inline_keyboard for x in row]
        # Q1: A correct (option 1), then B wrong (option 0) => resolved by B's answer
        self.press(e, sim, ui.cb("a", ui.hexid(MID), 0, 1), A, 100, "a-q1")
        self.assertIn("تم تسجيل إجابتك", self.screen(bot, A, 100))
        self.assertIn("أجاب بالفعل", self.screen(bot, B, 200))
        self.press(e, sim, ui.cb("a", ui.hexid(MID), 0, 0), B, 200, "b-q1")
        self.assertIn("السؤال 2 / 3", self.screen(bot, B, 200))
        self.assertIn("أنت: ❌ خاطئة", self.screen(bot, B, 200))
        self.assertIn("السؤال 2 / 3", self.screen(bot, A, 100))          # immediate opponent push
        self.beat(e, sim)                                                # ...the worker pushes it within one beat
        self.assertIn("السؤال 2 / 3", self.screen(bot, A, 100))
        self.assertIn("أنت: ✅ صحيحة (+100)", self.screen(bot, A, 100))
        # stale button from Q1 pressed during Q2: silently shows the CURRENT question, records nothing
        self.press(e, sim, ui.cb("a", ui.hexid(MID), 0, 1), A, 100, "a-stale")
        self.assertIn("السؤال 2 / 3", self.screen(bot, A, 100))
        self.assertEqual(len([k for k in sim.match["answers"] if k[0] == A]), 1)   # the stale press recorded nothing
        # Q2: nobody answers -> server deadline resolves it for both via the worker alone
        self.beat(e, sim, 21)
        for chat, mid_ in ((A, 100), (B, 200)):
            self.assertIn("السؤال 3 / 3", self.screen(bot, chat, mid_))
            self.assertIn("⌛", self.screen(bot, chat, mid_))
        # Q3: both answer correctly (B first, A second) => completed
        self.press(e, sim, ui.cb("a", ui.hexid(MID), 2, 1), B, 200, "b-q3")
        self.press(e, sim, ui.cb("a", ui.hexid(MID), 2, 1), A, 100, "a-q3")
        self.assertIn("انتهت المواجهة", self.screen(bot, A, 100))
        self.assertIn("الفائز: أنت", self.screen(bot, A, 100))
        self.assertFalse(run(sim.has_live_work()))                       # queue rows are 'done' — worker must STILL finish B
        self.beat(e, sim)
        self.assertIn("انتهت المواجهة", self.screen(bot, B, 200))
        self.assertIn("الفائز: U7", self.screen(bot, B, 200))
        self.assertEqual(bot.sends, [])                                  # ONE message per player for the whole match
        self.assertEqual(sim.msg, {A: 100, B: 200})
        n = len(bot.edits)
        self.beat(e, sim)
        self.beat(e, sim)
        self.assertEqual(len(bot.edits), n)                              # finished: the worker goes quiet

    def test_restart_mid_match_resumes_from_postgres_only(self):
        sim = SimBackend()
        e = make_env(sim)
        unit = ui.cb("j", ui.hexid(UNIT))
        self.press(e, sim, unit, 7, 100, "a-join")
        self.press(e, sim, unit, 8, 200, "b-join")
        self.beat(e, sim, 6)
        self.press(e, sim, ui.cb("a", ui.hexid(MID), 0, 1), 7, 100, "a-q1")
        e2 = make_env(sim, bot=FakeBot())                                # process restart: every in-memory cache gone
        self.assertEqual(len(e2["_cache"]) + len(e2["_search"]) + len(e2["_seen"]), 0)
        self.beat(e2, sim, 1)                                            # worker alone rebuilds both screens from PG
        self.assertEqual(sorted((x.chat_id, x.message_id) for x in e2["bot"].edits), [(7, 100), (8, 200)])
        self.assertIn("تم تسجيل إجابتك", self.screen(e2["bot"], 7, 100))
        self.press(e2, sim, ui.cb("a", ui.hexid(MID), 0, 1), 8, 200, "b-q1")
        self.assertIn("السؤال 2 / 3", self.screen(e2["bot"], 8, 200))

    def test_leave_in_progress_ends_both_screens(self):
        sim = SimBackend()
        e = make_env(sim)
        unit = ui.cb("j", ui.hexid(UNIT))
        self.press(e, sim, unit, 7, 100, "a-join")
        self.press(e, sim, unit, 8, 200, "b-join")
        self.beat(e, sim, 6)
        self.press(e, sim, ui.cb("x", ui.hexid(MID)), 7, 100, "a-x")
        self.assertIn("تأكيد الانسحاب", self.screen(e["bot"], 7, 100))
        self.press(e, sim, ui.cb("z", ui.hexid(MID)), 7, 100, "a-z")
        self.assertIn("انسحبت", self.screen(e["bot"], 7, 100))
        self.beat(e, sim)
        self.assertIn("انسحب U7", self.screen(e["bot"], 8, 200))
        self.assertEqual(e["bot"].sends, [])


class HomeEntryTests(unittest.TestCase):
    """The REAL challenge_game.home with injected fakes: entry button + live recovery on the existing land screen."""

    def home(self, live_flag, queue):
        captured = {}

        async def render(message, text, rows):
            captured["rows"] = rows
            return message

        async def flag(key, default=False):
            return live_flag if key == "live_matches_enabled" else default

        async def queue_state(uid):
            if isinstance(queue, Exception):
                raise queue
            return queue
        game = SimpleNamespace(
            profile=None, towers=None, active_attack=None)

        async def identity(user, branch=None):
            return {"branch_id": "b1", "trophies": 5, "xp": 1, "notifications_enabled": True}

        async def towers(uid, branch):
            return []

        async def active_attack(uid):
            return None
        repo = SimpleNamespace(towers=towers, active_attack=active_attack)

        def button(text, data):
            return (text, data)
        f = load_functions("handlers/challenge_game.py", ["home"], identity=identity, repo=repo, load_rules=lambda: {},
                           tower_progress=None, subject_icon=None, button=button, UUID=UUID, EMPTY="x", render=render,
                           branches=None, is_feature_enabled=flag, live_repo=SimpleNamespace(queue_state=queue_state))
        run(f["home"](object(), user()))
        return [b for row in captured["rows"] for b in row]

    def test_entry_button_hidden_while_live_flag_is_off(self):
        self.assertNotIn(("⚔️ تحدي طالب", "lv:m:0"), self.home(False, {"status": "idle"}))

    def test_entry_button_shown_when_live_flag_is_on(self):
        self.assertIn(("⚔️ تحدي طالب", "lv:m:0"), self.home(True, {"status": "idle"}))

    def test_resume_button_appears_first_for_a_waiting_or_matched_user(self):
        for status in ("waiting", "matched"):
            self.assertEqual(self.home(True, {"status": status})[0], ("⚔️ متابعة المباراة", "lv:q"))

    def test_home_still_works_before_migration_0010_exists(self):
        buttons = self.home(False, Exception('function challenge_live_queue_state does not exist'))
        self.assertIn(("🗺️ البحث عن أرض", "cg:l:-1:0"), buttons)


class WiringTests(unittest.TestCase):
    ROOT = Path(__file__).resolve().parents[1]

    def test_router_and_worker_registered_in_polling_and_webhook_modes(self):
        self.assertIn("challenge_live_router", (self.ROOT / "handlers/__init__.py").read_text())
        main = (self.ROOT / "main.py").read_text()
        self.assertIn("challenge_live_router", main)
        self.assertIn("spawn(challenge_live_loop(bot))", main)
        self.assertIn("spawn(challenge_live_loop(bot))", (self.ROOT / "webhook_server.py").read_text())

    def test_home_entry_button_is_flag_gated_and_recovery_is_failsafe(self):
        src = (self.ROOT / "handlers/challenge_game.py").read_text()
        self.assertIn('is_feature_enabled("live_matches_enabled", default=False)', src)
        self.assertIn('"lv:m:0"', src)
        self.assertIn('"lv:q"', src)

    def test_handler_code_never_touches_paid_points_payments_or_snapshot(self):
        for path in (HANDLER, "services/challenge_live_ui.py"):
            names = code_identifiers(path)
            for forbidden in ("points", "payment", "snapshot"):
                self.assertFalse([n for n in names if forbidden in n.lower()], (path, forbidden))
            self.assertFalse(names & {"supabase", "table", "rpc"}, path)   # all DB access goes through the repositories


if __name__ == "__main__":
    unittest.main()
