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


def track_upload_rejection(
    user_id,
    media_type,
    reason,
    input_channel,
    *,
    file_size_bytes=None,
    max_size_bytes=None,
    item_count=None,
    max_item_count=None,
    page_count=None,
    max_page_count=None,
    duration_minutes=None,
    max_duration_minutes=None,
    text_length=None,
    max_text_length=None,
    failure_stage=None,
    exception_type=None,
):
    """Record a rejected upload using safe, structured metadata only.

    Callers must pass stable reason codes; filenames, paths, file contents, and
    raw exception messages are intentionally excluded from analytics.
    """
    normalized_media_type = {"images": "image", "files": "document"}.get(str(media_type), str(media_type))
    metadata = {
        "media_type": normalized_media_type[:24],
        "reason": str(reason)[:48],
        "input_channel": str(input_channel)[:24],
    }
    optional = {
        "file_size_bytes": file_size_bytes,
        "max_size_bytes": max_size_bytes,
        "item_count": item_count,
        "max_item_count": max_item_count,
        "page_count": page_count,
        "max_page_count": max_page_count,
        "duration_minutes": duration_minutes,
        "max_duration_minutes": max_duration_minutes,
        "text_length": text_length,
        "max_text_length": max_text_length,
        "failure_stage": failure_stage,
        "exception_type": exception_type,
    }
    metadata.update({key: value for key, value in optional.items() if value is not None})
    track_usage(user_id, "upload_rejected", metadata)
