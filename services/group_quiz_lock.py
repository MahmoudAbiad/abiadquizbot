"""Durable permission journal for opt-in timed group quizzes.

Write the original permissions BEFORE Telegram. Retain the journal until Telegram
confirms restoration. A crash at either boundary is retried by the heartbeat.
Redis serializes external Telegram changes across webhook workers; PostgreSQL is
source of truth for recovery and has no TTL on the permission snapshot.
"""
import datetime
import secrets
import time
from contextlib import asynccontextmanager

from aiogram.types import ChatPermissions
from config import bot, redis_client
from helpers.supabase_helper import supabase
from logger import get_logger

logger = get_logger(__name__)
_TABLE = "group_quiz_chat_locks"
_RELEASE = """-- release_owned
if redis.call('get', KEYS[1]) == ARGV[1] then
 return redis.call('del', KEYS[1])
end
return 0
"""


_SCHEMA_CACHE = {"ready": False, "until": 0.0}


async def quiz_lock_schema_ready():
    """Rollout gate: legacy schemas keep existing quiz setup fully functional."""
    if time.monotonic() < _SCHEMA_CACHE["until"]:
        return _SCHEMA_CACHE["ready"]
    try:
        await supabase.table("group_quiz_sessions").select("lock_chat").limit(1).execute()
        await supabase.table(_TABLE).select("chat_id").limit(1).execute()
        ready = True
    except Exception:
        ready = False
    _SCHEMA_CACHE.update(ready=ready, until=time.monotonic() + 60)
    return ready


@asynccontextmanager
async def chat_permission_mutex(chat_id):
    key, token = f"gq:permission_mutex:{chat_id}", secrets.token_hex(16)
    if not await redis_client.set(key, token, nx=True, ex=120):
        raise RuntimeError("Group permission operation already running")
    try:
        # All Telegram calls below have a bounded timeout, below this lease.
        yield
    finally:
        try:
            await redis_client.eval(_RELEASE, 1, key, token)
        except Exception:
            logger.warning("Group permission lease release deferred chat=%s", chat_id)


async def _load_lock(chat_id):
    res = await supabase.table(_TABLE).select("*").eq("chat_id", chat_id).limit(1).execute()
    return res.data[0] if res.data else None


async def _live_session(session_id):
    res = await supabase.table("group_quiz_sessions").select("*").eq("id", session_id).limit(1).execute()
    return res.data[0] if res.data else None


async def _restore_locked(row):
    result = await bot.set_chat_permissions(
        chat_id=int(row["chat_id"]), permissions=ChatPermissions(**row["original_permissions"]),
        use_independent_chat_permissions=True, request_timeout=20,
    )
    if result is not True:
        raise RuntimeError("Telegram did not confirm permission restoration")
    # A lost DB acknowledgement causes another identical restoration, never loss
    # of the original snapshot. No cascade-delete can remove a pending journal.
    await supabase.table(_TABLE).delete().eq("chat_id", row["chat_id"]).eq("session_id", row["session_id"]).execute()
    logger.info("GROUP_QUIZ_UNLOCK_SUCCESS chat=%s session=%s", row["chat_id"], row["session_id"])


async def acquire_quiz_lock(session):
    if not session.get("lock_chat"):
        return
    chat_id, sid = int(session["chat_id"]), str(session["id"])
    async with chat_permission_mutex(chat_id):
        fresh = await _live_session(sid)
        if not fresh or fresh["status"] != "active":
            raise RuntimeError("Session already ended")
        chat = await bot.get_chat(chat_id, request_timeout=15)
        if chat.type not in {"group", "supergroup"}:
            raise RuntimeError("Only groups can be locked")
        if not fresh.get("question_timer_seconds") and fresh.get("pacing_mode") != "fixed_interval":
            raise RuntimeError("A timed quiz is required")
        member = await bot.get_chat_member(chat_id, bot.id, request_timeout=15)
        if member.status != "administrator" or not member.can_restrict_members:
            raise RuntimeError("Bot needs restrict-members permission")
        row = await _load_lock(chat_id)
        if row and str(row["session_id"]) != sid:
            raise RuntimeError("A previous group unlock is still pending")
        if row and row["state"] == "locked":
            return
        if not row:
            if chat.permissions is None:
                raise RuntimeError("Original permissions unavailable")
            original = chat.permissions.model_dump(exclude_none=True)
            # Never touch a group that was already closed before this quiz.
            if not original.get("can_send_messages", False):
                return
            locked = dict(original)
            for field in set(ChatPermissions.model_fields) | set(original):
                if field.startswith("can_send_") or field == "can_add_web_page_previews":
                    locked[field] = False
            # Voting on an existing poll does not require can_send_polls, which
            # controls creating NEW polls. Admins retain Telegram privileges.
            res = await supabase.table(_TABLE).insert({
                "chat_id": chat_id, "session_id": sid,
                "original_permissions": original, "locked_permissions": locked,
                "state": "prepared",
            }).execute()
            if not res.data:
                raise RuntimeError("Cannot durably save original permissions")
            row = res.data[0]
        result = await bot.set_chat_permissions(
            chat_id=chat_id, permissions=ChatPermissions(**row["locked_permissions"]),
            use_independent_chat_permissions=True, request_timeout=20,
        )
        if result is not True:
            raise RuntimeError("Telegram did not confirm group locking")
        await supabase.table(_TABLE).update({"state": "locked"}).eq("chat_id", chat_id).eq("session_id", sid).execute()
        # Finish may have won while Telegram was applying the restriction.
        fresh = await _live_session(sid)
        if not fresh or fresh["status"] != "active":
            await _restore_locked(row)
            raise RuntimeError("Session ended while locking")
        logger.info("GROUP_QUIZ_LOCK_SUCCESS chat=%s session=%s", chat_id, sid)


async def restore_quiz_lock(session_id, chat_id):
    """One immediate attempt; failure stays durable and is retried every tick."""
    if not await quiz_lock_schema_ready():
        return False
    try:
        async with chat_permission_mutex(chat_id):
            row = await _load_lock(chat_id)
            if row and str(row["session_id"]) == str(session_id):
                await _restore_locked(row)
        return True
    except Exception:
        # Do not log Telegram payloads or permission snapshots.
        logger.warning("GROUP_QUIZ_UNLOCK_RETRY chat=%s session=%s", chat_id, session_id)
        return False


async def recover_quiz_locks():
    """Recover ended sessions and interrupted prepare/apply on every restart."""
    if not await quiz_lock_schema_ready():
        return
    try:
        rows, offset = [], 0
        while True:
            res = await supabase.table(_TABLE).select("*").order("chat_id").range(offset, offset + 199).execute()
            rows.extend(res.data or [])
            if len(res.data or []) < 200:
                break
            offset += 200
        for row in rows:
            try:
                session = await _live_session(row["session_id"])
                if session and session["status"] == "active":
                    if row["state"] == "prepared":
                        created = datetime.datetime.fromisoformat(row["created_at"].replace("Z", "+00:00"))
                        if (datetime.datetime.now(datetime.timezone.utc) - created).total_seconds() >= 120:
                            await acquire_quiz_lock(session)
                    continue
                await restore_quiz_lock(row["session_id"], int(row["chat_id"]))
            except Exception:
                logger.warning("GROUP_QUIZ_LOCK_RECOVERY_RETRY session=%s", row["session_id"])
    except Exception:
        logger.warning("GROUP_QUIZ_LOCK_RECOVERY_UNAVAILABLE")
