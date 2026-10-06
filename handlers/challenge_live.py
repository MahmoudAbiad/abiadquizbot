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

from aiogram import F, Router, types
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError

from helpers.supabase_helper import challenges as content
from helpers.supabase_helper import challenge_game as game_repo
from helpers.supabase_helper import challenge_live as live
from helpers.supabase_helper.feature_flags import is_feature_enabled
from services import challenge_live_ui as ui

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


def to_markup(rows):
    return types.InlineKeyboardMarkup(inline_keyboard=[[
        types.InlineKeyboardButton(text=b.text, url=b.url) if b.url else
        types.InlineKeyboardButton(text=b.text, callback_data=b.data) for b in row] for row in rows])


async def deliver(bot, chat_id, message_id, screen):
    """Edit the existing message; fall back to a NEW message if Telegram refuses (too old / deleted /
    inaccessible). 'message is not modified' is success. Returns the message id now showing the screen,
    or None when the user cannot be reached (blocked bot). Rate limits (TelegramRetryAfter) propagate."""
    text, rows = screen
    markup = to_markup(rows)
    if message_id:
        try:
            await bot.edit_message_text(text, chat_id=chat_id, message_id=message_id, reply_markup=markup, parse_mode=None)
            return message_id
        except TelegramBadRequest as exc:
            if "message is not modified" in str(exc).lower():
                return message_id
        except TelegramForbiddenError:
            return None
    try:
        sent = await bot.send_message(chat_id, text, reply_markup=markup, parse_mode=None)
    except (TelegramBadRequest, TelegramForbiddenError):
        return None
    return sent.message_id


def remember(match_id, user_id, key, sig, message_id):
    _cache[(match_id, user_id)] = {"key": key, "sig": sig, "message_id": message_id}
    _cache.move_to_end((match_id, user_id))
    while len(_cache) > _MAX_CACHE:
        _cache.popitem(last=False)


async def present(bot, user_id, message_id, state, page=0):
    """Render a backend match state for `user_id` into `message_id` (private chat id == user id), keep the
    per-player message pointer in PostgreSQL, and remember what is displayed. Returns the shown message id."""
    match_id = state["id"]
    profile = None
    if state["status"] in ui.TERMINAL:
        try:
            profile = await game_repo.get_profile(user_id)
        except Exception:
            profile = None          # streak line is optional; never block the result screen
    screen = ui.match_screen(state, page, profile)
    sig = ui.signature(screen)
    entry = _cache.get((match_id, user_id))
    if entry and entry["sig"] == sig and entry["message_id"] == message_id:
        return message_id
    shown = await deliver(bot, user_id, message_id, screen)
    if shown is None:
        remember(match_id, user_id, ui.state_key(state), "unreachable", None)   # blocked bot: stop retrying this state
        return None
    if not entry or entry["message_id"] != shown:
        try:
            await live.attach_message(match_id, user_id, shown)
        except Exception:
            logger.info("live_attach_failed match=%s", match_id)
    remember(match_id, user_id, ui.state_key(state), sig, shown)
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
    return await route_queue(bot, user, message_id, state)


async def route_queue(bot, user, message_id, queue):
    status = queue.get("status")
    if status == "matched" and queue.get("match_id"):
        _search.pop(user.id, None)
        state = await live.match_state(queue["match_id"], user.id)
        logger.info("match_found match=%s", queue["match_id"])
        return await present(bot, user.id, message_id, state)
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
    return await present(bot, user.id, message_id, state)


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
    return await present(bot, user.id, message_id, state)


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
        logger.warning("live_callback_failed user=%s action=%s type=%s code=%s", user.id, action,
                       type(exc).__name__, ui.error_code(exc))
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
    if entry and row["status"] != "created" and entry["key"] == key:
        return                                        # nothing new (e.g. the answering handler already rendered it)
    message_id, from_search = db_message_id, False
    if not message_id and _search.get(user_id):
        message_id, from_search = _search[user_id]["message_id"], True    # waiting player: adopt the search message
    if not message_id:
        # No pointer yet. The joiner's handler attaches within milliseconds; wait a few beats before sending a new one.
        _pending[(match_id, user_id)] = _pending.get((match_id, user_id), 0) + 1
        if _pending[(match_id, user_id)] < 3:
            return
    state = await live.match_state(match_id, user_id)
    shown = await present(bot, user_id, message_id, state)
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
    then push changed screens. A fixed number of aggregated queries regardless of user count."""
    global _seen
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
    finished = await live.matches_by_ids(sorted(_seen - ids)) if _seen - ids else []
    everything = rows + finished
    messages = await live.player_messages([r["id"] for r in everything]) if everything else {}
    for row in everything:
        _log_transition(row)
        for user_id in (row["attacker_id"], row["defender_id"]):
            try:
                await render_for(bot, row, user_id, messages.get((row["id"], user_id)))
            except asyncio.CancelledError:
                raise
            except Exception:                           # Telegram/transport trouble never changes the match
                logger.info("live_render_unavailable match=%s", row["id"])
    _seen = ids
    return 1 if any(r["status"] == "created" for r in rows) else 2


async def challenge_live_loop(bot):
    """Spawned beside challenge_heartbeat_loop in BOTH polling (main.py) and webhook (webhook_server.py) mode.
    Idle (no search/match anywhere) costs one tiny indexed query per 20 s; works before the 0010 migration exists
    (errors are swallowed and retried slowly), and keeps running for in-flight matches even if flags are turned off."""
    while True:
        delay = 20
        try:
            # `_seen` non-empty = a match was active on the last beat: one more beat renders its final screen
            # even though its queue rows are already 'done' (otherwise has_live_work() would hide the result).
            if _seen or await live.has_live_work():
                delay = await live_beat(bot)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.debug("live_heartbeat_unavailable_or_migrations_not_applied")
            delay = 30
        await asyncio.sleep(delay)
