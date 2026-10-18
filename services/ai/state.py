"""Request-local AI identity and usage, isolated across concurrent asyncio tasks."""
from contextvars import ContextVar

_last_model_used_var = ContextVar("_last_model_used_var", default=None)
_last_token_usage_var = ContextVar("_last_token_usage_var", default=None)
_last_generation_metadata_var = ContextVar("_last_generation_metadata_var", default=None)
_groq_skip_reason_var = ContextVar("_groq_skip_reason_var", default=None)


def get_last_generation_metadata():
    return _last_generation_metadata_var.get()


def _record_token_usage(response):
    """Compatibility helper for older callers passing a Google SDK response."""
    usage = getattr(response, "usage_metadata", None)
    values = {
        "input_tokens": int(getattr(usage, "prompt_token_count", 0) or 0),
        "output_tokens": int(getattr(usage, "candidates_token_count", 0) or 0),
        "thoughts_tokens": int(getattr(usage, "thoughts_token_count", 0) or 0),
        "total_tokens": int(getattr(usage, "total_token_count", 0) or 0),
    }
    _last_token_usage_var.set(values)
    return values["total_tokens"]
