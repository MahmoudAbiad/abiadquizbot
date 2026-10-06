"""Service-role repository. All game-critical writes are one transactional RPC.
Only this backend accepts Telegram actor IDs; never expose these RPCs to public clients.
"""
from ._client import supabase
from . import challenges as content
from services.challenge_game import load_rules


async def rpc(name, **params):
    result = await supabase.rpc(name, params).execute()
    return result.data


async def profile(user_id, name, branch_id=None):
    return await rpc("challenge_touch_profile", p_user=user_id, p_name=name, p_branch=branch_id)


async def get_profile(user_id):
    rows = (await supabase.table("challenge_profiles").select("*").eq("user_id", user_id).limit(1).execute()).data
    return rows[0] if rows else None


async def towers(user_id, branch_id):
    subjects = await content.get_playable_subjects(branch_id)
    rows = (await supabase.table("challenge_towers").select("subject_id,xp,correct,questions")
            .eq("user_id", user_id).execute()).data or []
    by_id = {row["subject_id"]: row for row in rows}
    return [{**subject, **by_id.get(subject["id"], {"xp": 0, "correct": 0, "questions": 0})} for subject in subjects]


async def find_land(user_id, after_trophies=-1, after_user=0):
    return await rpc("challenge_find_land", p_user=user_id, p_after_trophies=after_trophies,
                     p_after_user=after_user, p_range=load_rules()["matchmaking_range"])


async def start_attack(user_id, target, unit_id, request_key):
    existing = (await supabase.table("challenge_matches").select("id")
                .eq("attacker_id", user_id).eq("request_key", request_key).limit(1).execute()).data or []
    if existing:
        return await advance(existing[0]["id"], user_id)
    # Reuse Phase 1's selection API. The RPC revalidates and locks every active ancestor.
    recent = (await supabase.table("challenge_matches").select("test_id").eq("attacker_id", user_id)
              .order("created_at", desc=True).limit(30).execute()).data or []
    chosen = await content.pick_active_test(unit_id, [r["test_id"] for r in recent])
    if not chosen:
        raise ValueError("no_content")
    return await rpc("challenge_start_attack", p_user=user_id, p_target=target, p_unit=unit_id,
                     p_test=chosen["id"], p_request=request_key, p_rules=load_rules())


async def advance(match_id, user_id, action="resume", index=None, option=None, update_key=None):
    return await rpc("challenge_advance_attack", p_match=match_id, p_user=user_id,
                     p_action=action, p_index=index, p_option=option, p_update=update_key)


async def active_attack(user_id):
    rows = (await supabase.table("challenge_matches").select("id").eq("attacker_id", user_id)
            .eq("kind", "async_attack").eq("status", "in_progress").limit(1).execute()).data or []
    return rows[0]["id"] if rows else None


async def attach_message(match_id, user_id, message_id):
    await supabase.table("challenge_matches").update({"telegram_message_id": message_id}) \
        .eq("id", match_id).eq("attacker_id", user_id).execute()


async def history(user_id, direction="defense", page=0):
    column = "defender_id" if direction == "defense" else "attacker_id"
    return (await supabase.table("challenge_matches").select("id,status,labels,result,created_at,finished_at")
            .eq(column, user_id).eq("kind", "async_attack").order("created_at", desc=True).range(page * 5, page * 5 + 5).execute()).data or []


async def unit_stats(user_id):
    return (await supabase.table("challenge_unit_stats").select(
        "challenges,wins,correct,questions,response_ms,responses,challenge_units(name,challenge_subjects(name))")
        .eq("user_id", user_id).execute()).data or []


async def toggle_notifications(user_id, enabled):
    await supabase.table("challenge_profiles").update({"notifications_enabled": enabled}).eq("user_id", user_id).execute()


async def due_attacks(now):
    return (await supabase.table("challenge_matches").select("id,attacker_id").eq("status", "in_progress")
            .eq("kind", "async_attack").lte("question_deadline", now).order("question_deadline").limit(50).execute()).data or []


async def claim_notifications():
    return await rpc("challenge_claim_notifications", p_limit=20) or []


async def notification_summary(match_id):
    rows = (await supabase.table("challenge_matches").select("labels,result,status").eq("id", match_id).limit(1).execute()).data
    return rows[0] if rows else None


async def has_active_attacks():
    rows = (await supabase.table("challenge_matches").select("id").eq("status", "in_progress")
            .eq("kind", "async_attack").limit(1).execute()).data
    return bool(rows)


async def subject_stats(user_id):
    return (await supabase.table("challenge_subject_stats").select("*").eq("user_id", user_id).execute()).data or []
