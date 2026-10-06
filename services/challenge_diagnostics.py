"""Useful stack locations without exception messages, credentials or question data."""
import traceback
from pathlib import Path


def log_failure(logger, event, exc, **context):
    stack = " > ".join(f"{Path(f.filename).name}:{f.lineno}:{f.name}"
                       for f in traceback.extract_tb(exc.__traceback__)[-8:])
    details = " ".join(f"{k}={v}" for k, v in context.items())
    logger.warning("%s %s type=%s stack=%s", event, details, type(exc).__name__, stack)
