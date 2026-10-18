"""Best-effort background tracking without making user flows depend on analytics."""
import logging


def track_usage(user_id, event_type, metadata=None):
    coroutine = None
    try:
        from helpers.supabase_helper import log_usage_event
        from task_utils import spawn
        coroutine = log_usage_event(user_id, event_type, metadata)
        spawn(coroutine)
    except Exception:
        if coroutine is not None:
            coroutine.close()
        logging.getLogger(__name__).warning("Could not schedule usage event %s", event_type)
