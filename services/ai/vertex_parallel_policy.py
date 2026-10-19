"""Admin-controlled Vertex AI participation in multi-part quiz processing.

The default is the pre-existing production policy: exclude Vertex for the
three-part PDF and super-album paths unless the admin explicitly enables it.
The feature flag persists in Supabase and uses the existing Redis/local TTL.
"""

from helpers.supabase_helper import is_feature_enabled

VERTEX_PARALLEL_FLAG = "ai_parallel_vertex_enabled"


async def should_exclude_vertex_for_parallel(part_count: int) -> bool:
    """Return the Vertex exclusion for new multi-part work.

    One- and two-part requests have always allowed Vertex. Three-part jobs
    honor the admin setting; a missing/unavailable setting remains OFF.
    """
    if part_count < 3:
        return False
    return not await is_feature_enabled(VERTEX_PARALLEL_FLAG, default=False)
