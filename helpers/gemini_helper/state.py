"""Compatibility exports for request-local AI audit state."""
from services.ai.state import (
    _last_model_used_var, _last_token_usage_var, _last_generation_metadata_var,
    _groq_skip_reason_var, _record_token_usage, get_last_generation_metadata,
)
