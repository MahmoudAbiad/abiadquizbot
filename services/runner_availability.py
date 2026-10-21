"""Admission switch; active rounds retain their existing authorization and expiry."""

RUNNER_FLAG = 'runner_enabled'


async def accepting_new_rounds():
    from services.runner_core import enabled
    if not enabled():
        return False
    try:
        from helpers.supabase_helper.feature_flags import is_feature_enabled
        return await is_feature_enabled(RUNNER_FLAG, default=False)
    except Exception:
        return False
