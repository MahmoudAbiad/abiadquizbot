"""Telegram UI for live (real-time) student challenges (3C). Private chats only.

PostgreSQL (0010 RPCs via helpers.supabase_helper.challenge_live) is the ONLY source of truth: this module
keeps no game state. The in-process dicts below are cosmetic caches (which Telegram message shows what), so a
restart / Redis reset can at worst cause one extra edit or one new message, never a lost or altered match.
Identity is always CallbackQuery.from_user.id; callback_data carries ids only (see services.challenge_live_ui).
"""
import asyncio
import logging
from collections import OrderedDict
from uuid import UUID
from weakref import WeakValueDictionary

from aiogram import F, Router, types
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError

from helpers.supabase_helper import challenges as content
from helpers.supabase_helper import challenge_game as game_repo
from helpers.supabase_helper import challenge_live as live
from helpers.supabase_helper.feature_flags import is_feature_enabled
from services import challenge_live_ui as ui
from helpers.challenge_delivery import deliver_screen
from services.challenge_diagnostics import log_failure

router = Router(name="challenge_live")
router.message.filter(F.chat.type == "private")
router.callback_query.filter(F.message.chat.type == "private")
logger = logging.getLogger(__name__)

_search = {}                # user_id -> {"message_id", "tok", "unit_hex", "labels"}   (search screen being shown)
_cache = OrderedDict()      # (match_id, user_id) -> {"key", "sig", "message_id"}      (what is on screen)
_pending = {}               # (match_id, user_id) -> beats seen without any known message (see render_for)
_status = {}                # match_id -> last status seen by the worker (for match_started logging only)
_seen = set()               # match ids that were active on the previous beat
_labels = {}                # unit_id -> {"subject", "unit"}
_MAX_CACHE = 5000
_wake = asyncio.Event()
_present_locks = WeakValueDictionary()


def to_markup(rows):
    return types.InlineKeyboardMarkup(inline_keyboard=[[
        types.InlineKeyboardButton(text=b.text, url=b.url) if b.url else
        types.InlineKeyboardButton(text=b.text, callback_data=b.data) for b in row] for row in rows])


async def deliver(bot, chat_id, message_id, screen):
    text, rows = screen
    return await deliver_screen(bot, chat_id, message_id, text, to_markup(rows))


def wake_live():
    _wake.set()


async def wait_for_work(delay):
    try:
        await asyncio.wait_for(_wake.wait(), timeout=delay)
    except asyncio.TimeoutError:
        pass
    finally:
        _wake.clear()


async def push_opponent(bot, state):
    """Notify the other player immediately, even while the background worker is idle.
    Its own authorized state is read again; never reuse the caller's answer payload.
    """
    match_id, opponent_id = state["id"], state["opponent"]["id"]
    pointers = await live.player_messages([match_id]) or {}
    info = _search.get(opponent_id)
    message_id = pointers.get((match_id, opponent_id)) or (info or {}).get("message_id")
    other = await live.match_state(match_id, opponent_id)
    entry = _cache.get((match_id, opponent_id))
    page = entry.get("page", 0) if entry and entry["key"] == ui.state_key(other) else 0
    await present(bot, opponent_id, message_id, other, page)
    _search.pop(opponent_id, None)


async def notify_opponent(bot, state):
    try:
        await push_opponent(bot, state)
    except Exception as exc:
        log_failure(logger, "live_opponent_push_failed", exc, match=state["id"])
    finally:
        _seen.add(state["id"])
        wake_live()


def remember(match_id, user_id, key, sig, message_id):
    _cache[(match_id, user_id)] = {"key": key, "sig": sig, "message_id": message_id}
    _cache.move_to_end((match_id, user_id))
    while len(_cache) > _MAX_CACHE:
        _cache.popitem(last=False)


async def present(bot, user_id, message_id, state, page=0):
    key = (state["id"], user_id)
    lock = _present_locks.setdefault(key, asyncio.Lock())
    async with lock:
        return await present_locked(bot, user_id, message_id, state, page)


async def present_locked(bot, user_id, message_id, state, page=0):
    """Render a backend match state for `user_id` into `message_id` (private chat id == user id), keep the
    per-player message pointer in PostgreSQL, and remember what is displayed. Returns the shown message id."""
    match_id = state["id"]
    entry = _cache.get((match_id, user_id))
    stamp = ui.parse_ts(state.get("server_now"))
    if entry and stamp and entry.get("server_now") and stamp < entry["server_now"]:
        return entry["message_id"]  # a slower callback must not replace a newer worker screen
    profile = None
    if state["status"] in ui.TERMINAL:
        try:
            profile = await game_repo.get_profile(user_id)
        except Exception:
            profile = None          # streak line is optional; never block the result screen
    screen = ui.match_screen(state, page, profile)
    sig = ui.signature(screen)
    entry = _cache.get((match_id, user_id))
    unchanged = entry and entry["sig"] == sig and entry["message_id"] == message_id
    shown = message_id if unchanged else await deliver(bot, user_id, message_id, screen)
    if shown is None:
        remember(match_id, user_id, ui.state_key(state), "unreachable", None)
        return None
    attached = bool(entry and entry.get("attached") and entry["message_id"] == shown)
    if not attached:
        try:
            await live.attach_message(match_id, user_id, shown)
            attached = True
        except Exception as exc:
            log_failure(logger, "live_attach_failed", exc, match=match_id)
    remember(match_id, user_id, ui.state_key(state), sig, shown)
    _cache[(match_id, user_id)].update(server_now=stamp, page=page, attached=attached)
    return shown


async def unit_labels(unit_id):
    if unit_id not in _labels:
        unit = await content.get_node("unit", unit_id)
        subject = await content.get_node("subject", unit["subject_id"]) if unit else None
        _labels[unit_id] = {"subject": (subject or {}).get("name", "—"), "unit": (unit or {}).get("name", "—")}
        while len(_labels) > 500:
            _labels.pop(next(iter(_labels)))
    return _labels[unit_id]


async def identity(user):
    # Deliberately never reads phone, email, payment or paid-points tables (same rule as the async game).
    return await game_repo.profile(user.id, user.full_name or user.username or "طالب")


async def live_open():
    return (await is_feature_enabled("challenge_system_enabled", default=False)
            and await is_feature_enabled("live_matches_enabled", default=False))


async def edit(bot, user_id, message_id, screen):
    return await deliver(bot, user_id, message_id, screen)


# ----------------------------------------------------------------------------- flow: menu -> search
async def menu(bot, user, message_id, page=0):
    q = await live.queue_state(user.id)
    if q.get("status") in ("waiting", "matched"):
        return await resume(bot, user, message_id)        # never start a second search
    if not await live_open():
        return await edit(bot, user.id, message_id, (ui.DISABLED, [[ui.Btn("↩️ أرضي", "cg:home:0")]]))
    p = await identity(user)
    if not p.get("branch_id"):
        return await edit(bot, user.id, message_id, ("📚 اختر فرعك الدراسي أولًا.", [[ui.Btn("📚 اختيار الفرع", "cg:branches")]]))
    branches = await content.get_playable_branches()
    branch = next((b["name"] for b in branches if b["id"] == p["branch_id"]), None)
    subjects = await content.get_playable_subjects(p["branch_id"]) if branch else []
    return await edit(bot, user.id, message_id, ui.menu_subjects(branch or "—", subjects, page))


async def units_menu(bot, user, message_id, subject_hex, page):
    subject_id = ui.fullid(subject_hex)
    units = await content.get_playable_units(subject_id)
    subject = await content.get_node("subject", subject_id)
    return await edit(bot, user.id, message_id, ui.menu_units((subject or {}).get("name", "—"), subject_id, units, page))


async def start_search(bot, c, unit_hex):
    user, message_id = c.from_user, c.message.message_id
    unit_id = ui.fullid(unit_hex)
    key = ui.request_key(user.id, c.id)
    tok = ui.token_of(key)
    labels = await unit_labels(unit_id)
    previous = _search.get(user.id)
    # Registered BEFORE joining so the worker can adopt this message if the opponent's heartbeat matches us first.
    _search[user.id] = {"message_id": message_id, "tok": tok, "unit_hex": unit_hex, "labels": labels}
    try:
        state = await live.join_queue(user.id, unit_id, key)
    except Exception as exc:
        if previous:
            _search[user.id] = previous        # a rejected duplicate press must not replace the live search's key
        else:
            _search.pop(user.id, None)
        if ui.error_code(exc) in ("already_queued", "in_live_match"):
            return await resume(bot, user, message_id)   # double tap: show the real state instead of an error
        raise
    wake_live()
    return await route_queue(bot, user, message_id, state)


async def route_queue(bot, user, message_id, queue):
    status = queue.get("status")
    if status == "matched" and queue.get("match_id"):
        _search.pop(user.id, None)
        state = await live.match_state(queue["match_id"], user.id)
        logger.info("match_found match=%s", queue["match_id"])
        _seen.add(state["id"])
        # Both deliveries run concurrently; an unavailable peer cannot prevent the caller's screen.
        shown, _ = await asyncio.gather(present(bot, user.id, message_id, state), notify_opponent(bot, state))
        return shown
    if status == "waiting":
        info = _search.get(user.id)
        if not info:                                   # recovered screen (restart): labels from the unit
            labels = await unit_labels(queue["unit_id"])
            info = _search[user.id] = {"message_id": message_id, "tok": "-", "unit_hex": ui.hexid(queue["unit_id"]), "labels": labels}
        info["message_id"] = message_id
        return await edit(bot, user.id, message_id, ui.searching(info["labels"], queue, info["tok"]))
    info = _search.pop(user.id, None)
    if status == "expired":
        return await edit(bot, user.id, message_id, ui.queue_ended("expired", info and info["unit_hex"]))
    if status == "cancelled":
        return await edit(bot, user.id, message_id, ui.queue_ended("cancelled"))
    return await edit(bot, user.id, message_id, ui.no_live_match())


async def cancel(bot, user, message_id, token):
    # Matched => the backend returns 'matched' and the match is untouched; route_queue then shows the real match.
    state = await live.cancel_search(user.id, ui.key_of(token))
    return await route_queue(bot, user, message_id, state)


async def resume(bot, user, message_id):
    """Recovery entry: what does PostgreSQL say about this user right now?"""
    return await route_queue(bot, user, message_id, await live.queue_state(user.id))


# ----------------------------------------------------------------------------- flow: match
async def refresh(bot, user, message_id, match_id, page=0):
    """Re-read PostgreSQL and re-render. If server time already passed a countdown end / deadline, run the same
    idempotent `tick` the worker would (so a missed heartbeat cannot leave a stuck screen); never alters results."""
    state = await live.match_state(match_id, user.id)
    if ui.is_overdue(state):
        await live.tick(match_id)
        state = await live.match_state(match_id, user.id)
    return await present(bot, user.id, message_id, state, page)


async def answer(bot, c, match_id, index, option):
    user, message_id = c.from_user, c.message.message_id
    state = await live.answer(match_id, user.id, index, option, f"lv:{c.id}")
    shown, _ = await asyncio.gather(present(bot, user.id, message_id, state), notify_opponent(bot, state))
    return shown


async def options_page(bot, user, message_id, match_id, index, page):
    state = await live.match_state(match_id, user.id)
    return await present(bot, user.id, message_id, state, page if index == state.get("index") else 0)


async def ask_leave(bot, user, message_id, match_id):
    state = await live.match_state(match_id, user.id)
    if state["status"] in ui.TERMINAL:
        return await present(bot, user.id, message_id, state)
    return await edit(bot, user.id, message_id, ui.leave_confirm(match_id, state["status"] == "created"))


async def leave(bot, user, message_id, match_id):
    state = await live.leave(match_id, user.id)
    shown, _ = await asyncio.gather(present(bot, user.id, message_id, state), notify_opponent(bot, state))
    return shown


@router.callback_query(F.data.startswith("lv:"))
async def live_callback(c: types.CallbackQuery):
    try:
        await c.answer()                      # ack first: nothing below may delay it
    except TelegramBadRequest:
        pass                                  # expired ack must not block durable handling
    parts, user, bot = c.data.split(":"), c.from_user, c.bot
    action, message_id = parts[1], c.message.message_id
    try:
        if action == "m":
            await menu(bot, user, message_id, max(0, int(parts[2])) if len(parts) > 2 else 0)
        elif action == "u":
            await units_menu(bot, user, message_id, parts[2], max(0, int(parts[3])))
        elif action == "j":
            await start_search(bot, c, parts[2])
        elif action == "c":
            await cancel(bot, user, message_id, parts[2] if len(parts) > 2 else "-")
        elif action == "q":
            await resume(bot, user, message_id)
        elif action == "a":
            await answer(bot, c, str(UUID(parts[2])), int(parts[3]), int(parts[4]))
        elif action == "p":
            await options_page(bot, user, message_id, str(UUID(parts[2])), int(parts[3]), max(0, int(parts[4])))
        elif action == "r":
            await refresh(bot, user, message_id, str(UUID(parts[2])))
        elif action == "x":
            await ask_leave(bot, user, message_id, str(UUID(parts[2])))
        elif action == "z":
            await leave(bot, user, message_id, str(UUID(parts[2])))
    except Exception as exc:
        log_failure(logger, "live_callback_failed", exc, user=user.id, action=action, code=ui.error_code(exc))
        await edit(bot, user.id, message_id, ui.error_screen(exc))


# ----------------------------------------------------------------------------- worker (heartbeat)
async def notify_expired(bot, user_id):
    info = _search.pop(user_id, None)
    await deliver(bot, user_id, info and info["message_id"], ui.queue_ended("expired", info and info["unit_hex"]))


async def render_for(bot, row, user_id, db_message_id):
    """Push the latest state of one match to one player, only when it changed since what is on screen."""
    match_id = row["id"]
    key = (row["status"], row["question_index"])
    entry = _cache.get((match_id, user_id))
    if entry and entry["sig"] == "unreachable" and entry["key"] == key:
        return
    # Read live state even within one question: scores and answered flags can change.
    # present() still deduplicates identical screens, so this does not spam Telegram.
    message_id, from_search = (entry or {}).get("message_id") or db_message_id, False
    if not message_id and _search.get(user_id):
        message_id, from_search = _search[user_id]["message_id"], True    # waiting player: adopt the search message
    state = await live.match_state(match_id, user_id)
    page = entry.get("page", 0) if entry and entry["key"] == key else 0
    shown = await present(bot, user_id, message_id, state, page)
    if shown is not None:
        _pending.pop((match_id, user_id), None)
        if from_search:
            _search.pop(user_id, None)


def _log_transition(row):
    previous = _status.get(row["id"])
    if row["status"] == "in_progress" and previous == "created":
        logger.info("match_started match=%s", row["id"])
    if row["status"] in ui.TERMINAL:
        _status.pop(row["id"], None)
        for k in [k for k in _pending if k[0] == row["id"]]:
            _pending.pop(k, None)
    else:
        _status[row["id"]] = row["status"]


async def live_beat(bot):
    """One heartbeat: second-chance pairing, DB-driven sweep (countdown end, timeouts, next question, expiry),
    then read both player views (bounded concurrency) and push only changed screens."""
    global _seen
    seen_before = set(_seen)
    waiting = await live.waiting_users()
    if len(waiting) >= 2:                              # one waiter cannot be paired; range widens with time
        await live.pair_waiting(waiting[:50])
    swept = await live.sweep()
    for user_id in swept.get("expired_users", []):
        try:
            await notify_expired(bot, user_id)
        except Exception:
            logger.info("live_expiry_notice_unavailable user=%s", user_id)
    rows = await live.active_matches()
    ids = {r["id"] for r in rows}
    settled = {m["id"] for m in swept.get("matches", []) if m.get("status") in ui.TERMINAL}
    finished_ids = (_seen | settled) - ids
    finished = await live.matches_by_ids(sorted(finished_ids)) if finished_ids else []
    everything = rows + finished
    messages = await live.player_messages([r["id"] for r in everything]) if everything else {}
    retry_ids = set()
    slots = asyncio.Semaphore(8)

    async def render_player(row, user_id):
        async with slots:
            try:
                await render_for(bot, row, user_id, messages.get((row["id"], user_id)))
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                retry_ids.add(row["id"])
                log_failure(logger, "live_render_unavailable", exc, match=row["id"], user=user_id)

    for row in everything:
        _log_transition(row)
    await asyncio.gather(*(render_player(row, user_id) for row in everything
                           for user_id in (row["attacker_id"], row["defender_id"])))
    _seen = ids | retry_ids | (_seen - seen_before)
    return 1 if any(r["status"] == "created" for r in rows) else 2


async def challenge_live_loop(bot):
    """Spawned beside challenge_heartbeat_loop in BOTH polling (main.py) and webhook (webhook_server.py) mode.
    Idle (no search/match anywhere) costs one indexed query per 2 s; works before the 0010 migration exists
    (errors are swallowed and retried slowly), and keeps running for in-flight matches even if flags are turned off."""
    while True:
        delay = 2
        try:
            # `_seen` non-empty = a match was active on the last beat: one more beat renders its final screen
            # even though its queue rows are already 'done' (otherwise has_live_work() would hide the result).
            if _seen or await live.has_live_work():
                delay = await live_beat(bot)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log_failure(logger, "live_heartbeat_failed", exc)
            delay = 5
        await wait_for_work(delay)
