"""Admin-facing settings for async challenge land attacks.

The limits themselves are enforced transactionally inside PostgreSQL by
`challenge_start_attack`; this module only reads/writes the `app_settings`
values exposed in the admin panel.
"""
from __future__ import annotations

import datetime
from typing import Dict

from ._client import supabase

COOLDOWN_SECONDS_KEY = "challenge_target_cooldown_seconds"
COOLDOWN_RESET_EPOCH_KEY = "challenge_cooldown_reset_epoch"
DAILY_ATTACK_LIMIT_KEY = "challenge_daily_attack_limit"

DEFAULT_COOLDOWN_SECONDS = 21600  # 6 hours; matches services/challenge_rules.json
DEFAULT_DAILY_ATTACK_LIMIT = 20   # matches services/challenge_rules.json

MAX_COOLDOWN_SECONDS = 30 * 24 * 60 * 60  # 30 days, admin guardrail only
MAX_DAILY_ATTACK_LIMIT = 1000


async def get_land_attack_settings() -> Dict[str, int]:
    rows = (
        await supabase.table("app_settings")
        .select("key,value")
        .in_("key", [COOLDOWN_SECONDS_KEY, COOLDOWN_RESET_EPOCH_KEY, DAILY_ATTACK_LIMIT_KEY])
        .execute()
    ).data or []
    values = {row["key"]: row.get("value") for row in rows}

    try:
        seconds = int(float(values.get(COOLDOWN_SECONDS_KEY, DEFAULT_COOLDOWN_SECONDS)))
    except (TypeError, ValueError):
        seconds = DEFAULT_COOLDOWN_SECONDS
    try:
        reset_epoch = int(float(values.get(COOLDOWN_RESET_EPOCH_KEY, 0)))
    except (TypeError, ValueError):
        reset_epoch = 0
    try:
        daily_limit = int(float(values.get(DAILY_ATTACK_LIMIT_KEY, DEFAULT_DAILY_ATTACK_LIMIT)))
    except (TypeError, ValueError):
        daily_limit = DEFAULT_DAILY_ATTACK_LIMIT

    return {
        "cooldown_seconds": max(0, min(MAX_COOLDOWN_SECONDS, seconds)),
        "reset_epoch": max(0, reset_epoch),
        "daily_attack_limit": max(0, min(MAX_DAILY_ATTACK_LIMIT, daily_limit)),
    }


# Backward-compatible name used by the first cooldown admin implementation.
async def get_land_attack_cooldown_settings() -> Dict[str, int]:
    return await get_land_attack_settings()


async def set_land_attack_cooldown_seconds(seconds: int) -> int:
    seconds = int(seconds)
    if seconds < 0 or seconds > MAX_COOLDOWN_SECONDS:
        raise ValueError("cooldown_out_of_range")
    await supabase.table("app_settings").upsert({
        "key": COOLDOWN_SECONDS_KEY,
        "value": float(seconds),
        "label": "مهلة إعادة مهاجمة نفس الأرض (ثانية)",
        "updated_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
    }, on_conflict="key").execute()
    return seconds


async def set_land_attack_daily_limit(limit: int) -> int:
    limit = int(limit)
    if limit < 0 or limit > MAX_DAILY_ATTACK_LIMIT:
        raise ValueError("daily_attack_limit_out_of_range")
    await supabase.table("app_settings").upsert({
        "key": DAILY_ATTACK_LIMIT_KEY,
        "value": float(limit),
        "label": "عدد هجمات الأرض اليومية لكل طالب",
        "updated_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
    }, on_conflict="key").execute()
    return limit


async def reset_all_land_attack_cooldowns() -> int:
    """Expire all cooldowns that started before this instant without deleting history."""
    epoch = int(datetime.datetime.now(datetime.timezone.utc).timestamp())
    await supabase.table("app_settings").upsert({
        "key": COOLDOWN_RESET_EPOCH_KEY,
        "value": float(epoch),
        "label": "وقت آخر تصفير عالمي لمهل إعادة الهجوم (Unix epoch)",
        "updated_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
    }, on_conflict="key").execute()
    return epoch
