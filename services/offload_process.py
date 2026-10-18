"""Picklable process entry point; no bot, HTTP or database imports here."""

from services.offload_worker_core import BadPayload


def execute(work, payload):
    """Keep rendering imports, exceptions and memory cleanup inside the child."""
    from services.export_service import ExportError
    from mem_utils import release_memory_to_os

    try:
        data, media_type = work(payload)
        return "ok", data, media_type
    except BadPayload as exc:
        return "bad_payload", str(exc), ""
    except ExportError as exc:
        return "export_error", str(exc), ""
    finally:
        release_memory_to_os()
