"""Live (real-time) challenge repository. Service-role only; every game-critical write is ONE RPC (one
PostgreSQL transaction) from db/migrations/0010_challenge_live.sql. PostgreSQL is the source of truth:
nothing here keeps match state in memory, so a worker restart or a Redis reset loses nothing.
Telegram actor ids are passed by the trusted bot backend only; never expose these RPCs to public clients.
Logs carry ids and event names only (no answers, option text, tokens or payment data).
"""
from ._client import supabase, log_info
from services.challenge_game import load_rules

_TERMINAL = {"completed", "expired", "abandoned", "cancelled"}
_LOG_EVENT = {"completed": "match_completed", "abandoned": "match_abandoned",
              "expired": "match_timeout", "cancelled": "match_cancelled"}


async def rpc(name, **params):
    return (await supabase.rpc(name, params).execute()).data


def _log_terminal(state):
    status = (state or {}).get("status")
    if status in _TERMINAL:
        log_info(f"{_LOG_EVENT[status]} match={state.get('id')}")


async def join_queue(user_id, unit_id, request_key):
    """Enter the queue for ONE unit (the unit is never changed automatically) and try to pair at once.
    Idempotent per request_key. Raises on: already_queued, in_live_match, active_attack, game_disabled,
    no_content, branch_mismatch, profile_missing."""
    state = await rpc("challenge_live_join", p_user=user_id, p_unit=unit_id, p_request=request_key, p_rules=load_rules())
    if (state or {}).get("status") == "waiting":
        state = await try_match(user_id)
    return state


async def try_match(user_id, attempts=3):
    """Pair `user_id` with the closest compatible waiting player. One SQL call per attempt; retries only
    when the candidate was lost to a race (retry=true). Returns the player's queue state; when
    state['status']=='matched' use state['match_id']."""
    rules, state = load_rules(), None
    for _ in range(max(1, attempts)):
        state = await rpc("challenge_live_try_match", p_user=user_id, p_rules=rules)
        if state and state.get("created"):
            log_info(f"match_created match={state.get('match_id')}")
        if not state or not state.get("retry"):
            break
    return state


async def queue_state(user_id):
    return await rpc("challenge_live_queue_state", p_user=user_id)


async def cancel_search(user_id, request_key=None):
    """Idempotent. Only cancels a *waiting* search (pass the request_key to avoid cancelling a newer one).
    If a match already exists the returned status is 'matched' and the match is untouched."""
    return await rpc("challenge_live_cancel", p_user=user_id, p_request=request_key)


async def match_state(match_id, user_id):
    """Player-safe view: no snapshot, no correct answer for the open question, no opponent option."""
    return await rpc("challenge_live_state", p_match=match_id, p_user=user_id)


async def answer(match_id, user_id, index, option, update_key=None):
    """Record the player's answer once. update_key = Telegram update/callback id (duplicate-safe)."""
    state = await rpc("challenge_live_answer", p_match=match_id, p_user=user_id, p_index=index,
                      p_option=option, p_update=update_key)
    log_info(f"answer_received match={match_id} index={index}")
    _log_terminal(state)
    return state


async def leave(match_id, user_id):
    state = await rpc("challenge_live_leave", p_match=match_id, p_user=user_id)
    _log_terminal(state)
    return state


async def tick(match_id):
    """Start/resolve/expire one match if due. Safe from any number of workers (idempotent)."""
    result = await rpc("challenge_live_tick", p_match=match_id)
    _log_terminal(result)
    return result


async def due_matches(limit=50):
    return await rpc("challenge_live_due", p_limit=limit) or []


async def expire_queue(limit=100):
    """Marks overdue searches 'expired'; returns the user ids so the UI layer can tell them."""
    return await rpc("challenge_live_expire_queue", p_limit=limit) or []


async def sweep(limit=50):
    """One heartbeat for a worker: expire stale searches, then tick every due match. Recovery after a
    restart is simply running this again (all state is in PostgreSQL)."""
    expired = await expire_queue()
    ticked = []
    for match_id in await due_matches(limit):
        ticked.append(await tick(match_id))
    return {"expired_users": expired, "matches": ticked}


async def pair_waiting(user_ids):
    """Second chance for waiting players (range widens with time); call from the same heartbeat."""
    return [await try_match(u, attempts=1) for u in user_ids]


async def waiting_users(limit=200):
    rows = (await supabase.table("challenge_live_queue").select("user_id").eq("status", "waiting")
            .order("joined_at").limit(limit).execute()).data or []
    return [r["user_id"] for r in rows]


async def attach_message(match_id, user_id, message_id):
    await rpc("challenge_live_attach_message", p_match=match_id, p_user=user_id, p_message=message_id)


async def has_live_work():
    """True when any search or live match exists (lets the worker idle cheaply)."""
    q = (await supabase.table("challenge_live_queue").select("user_id").in_("status", ["waiting", "matched"])
         .limit(1).execute()).data
    return bool(q)


# ---- read-only helpers for the Telegram heartbeat (3C). One aggregated query each; never per-user polling.
_MATCH_COLS = "id,status,question_index,attacker_id,defender_id"


async def active_matches(limit=200):
    """Live matches currently counting down or being played (served by the partial live-match indexes)."""
    return (await supabase.table("challenge_matches").select(_MATCH_COLS).eq("kind", "live")
            .in_("status", ["created", "in_progress"]).order("created_at").limit(limit).execute()).data or []


async def matches_by_ids(match_ids):
    """Primary-key lookup used to render the final screen of matches that just left the active set."""
    if not match_ids:
        return []
    return (await supabase.table("challenge_matches").select(_MATCH_COLS).eq("kind", "live")
            .in_("id", list(match_ids)[:200]).execute()).data or []


async def player_messages(match_ids):
    """{(match_id, user_id): message_id|None} for the given matches in a single query."""
    if not match_ids:
        return {}
    rows = (await supabase.table("challenge_match_players").select("match_id,user_id,message_id")
            .in_("match_id", list(match_ids)).execute()).data or []
    return {(r["match_id"], r["user_id"]): r.get("message_id") for r in rows}
