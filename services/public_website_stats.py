"""Small, read-only website snapshot. HTTP requests never query the database.

Opt in with PUBLIC_WEBSITE_STATS_ENABLED=true on the production bot only.
No change to database grants, RLS, credentials, or existing API routes.
"""
import asyncio
import logging
import os
from datetime import datetime, timezone

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

LOG = logging.getLogger(__name__)
REFRESH_SECONDS = 900
MAX_AGE_SECONDS = 86400
MAX_VALUE = 10**12
ALLOWED_ORIGINS = frozenset({"https://abiad.me", "https://www.abiad.me"})


def utcnow():
    return datetime.now(timezone.utc)


def valid_count(value):
    return type(value) is int and 0 <= value < MAX_VALUE


async def collect_counts(client, cutoff):
    """Aggregate one numeric column server-side; never load names or IDs.

    Same counter as the admin dashboard, without its first-page truncation.
    A fixed registration cutoff and stable ordering avoid pagination drift from
    new registrations. Updates/deletions can still give eventual consistency.
    """
    users, questions, offset = 0, 0, 0
    for _ in range(2000):
        response = await (client.table("users").select("total_questions")
                          .lte("joined_at", cutoff.isoformat()).order("user_id")
                          .range(offset, offset + 499).execute())
        rows = response.data
        if not isinstance(rows, list):
            raise ValueError("Invalid aggregate source")
        if not rows:
            return {"students": users, "questions": questions}
        for row in rows:
            value = row.get("total_questions")
            value = 0 if value is None else value
            if not valid_count(value):
                raise ValueError("Invalid question counter")
            questions += value
        users += len(rows)
        offset += len(rows)  # Honor a server row limit smaller than our page.
        if not valid_count(users) or not valid_count(questions):
            raise ValueError("Aggregate out of range")
    raise ValueError("Aggregate scan limit exceeded")


class PublicStatsCache:
    def __init__(self):
        self._snapshot = None

    def publish(self, counts, updated_at):
        if not all(valid_count(counts.get(key)) for key in ("students", "questions")):
            raise ValueError("Invalid public aggregate")
        # Explicit projection: additional fields must never reach the browser.
        self._snapshot = {"students": counts["students"], "questions": counts["questions"],
                          "updated_at": updated_at.isoformat()}

    def read(self, now=None):
        if self._snapshot is None:
            return None
        now = now or utcnow()
        updated = datetime.fromisoformat(self._snapshot["updated_at"])
        if not 0 <= (now - updated).total_seconds() <= MAX_AGE_SECONDS:
            return None
        return dict(self._snapshot)

    async def refresh(self, client):
        cutoff = utcnow()
        counts = await asyncio.wait_for(collect_counts(client, cutoff), timeout=45)
        self.publish(counts, cutoff)  # Publish only after the entire scan succeeds.

    async def run(self):
        from helpers.supabase_helper._client import supabase
        while True:
            try:
                await self.refresh(supabase)
            except Exception:
                # Do not put provider responses, IDs or credentials in public errors.
                LOG.warning("Website aggregate refresh failed; retaining previous snapshot")
            await asyncio.sleep(REFRESH_SECONDS)


def build_router(cache, enabled):
    router = APIRouter()

    @router.get("/api/public-stats", include_in_schema=False)
    async def public_stats(request: Request):
        headers = {"X-Content-Type-Options": "nosniff", "Vary": "Origin",
                   "Cache-Control": "no-store"}
        origin = request.headers.get("origin")
        if origin in ALLOWED_ORIGINS:
            headers["Access-Control-Allow-Origin"] = origin
        if not enabled:
            return JSONResponse({"detail": "Not found"}, status_code=404, headers=headers)
        if request.query_params:
            return JSONResponse({"detail": "No parameters supported"}, status_code=400, headers=headers)
        snapshot = cache.read()
        if snapshot is None:
            headers["Retry-After"] = "60"
            return JSONResponse({"detail": "Statistics temporarily unavailable"}, status_code=503, headers=headers)
        headers["Cache-Control"] = "public, max-age=60, s-maxage=300"
        # CORS is a browser policy, not authentication: these aggregates are public.
        # Every request reads this in-memory snapshot, never Postgres or Redis.
        return JSONResponse(snapshot, headers=headers)

    return router


cache = PublicStatsCache()
enabled = os.getenv("PUBLIC_WEBSITE_STATS_ENABLED", "").strip().lower() == "true"
router = build_router(cache, enabled)
