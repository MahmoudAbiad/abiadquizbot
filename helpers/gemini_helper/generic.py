"""Compatibility entry points backed by the provider-independent runtime."""
import time

from services.ai.contracts import AIRequest, Attachment
from helpers.gemini_helper.cascade import _execute_cascade
from helpers.gemini_helper.state import (
    _last_model_used_var, _last_token_usage_var, _last_generation_metadata_var,
)


def _normalize_contents(contents):
    """Bridge existing callers using inline Google Parts; new code uses Attachment."""
    result = []
    for part in contents:
        if isinstance(part, (str, Attachment)):
            result.append(part)
        elif getattr(part, "inline_data", None):
            result.append(Attachment(mime_type=part.inline_data.mime_type, data=part.inline_data.data))
        else:
            raise ValueError("Pass local attachments, not vendor-specific uploaded file references")
    return tuple(result)


async def generate_structured_with_cascade(contents, response_schema, prompt_instruction=None):
    result = await _execute_cascade(
        AIRequest(_normalize_contents(contents), response_schema,
                  system_instruction=prompt_instruction)
    )
    return (result.parsed, result.total_tokens) if result else None


async def generate_text_with_cascade(contents, prompt_instruction=None):
    _last_model_used_var.set(None)
    _last_token_usage_var.set(None)
    _last_generation_metadata_var.set(None)
    started_at = time.monotonic()
    result = await _execute_cascade(
        AIRequest(_normalize_contents(contents), system_instruction=prompt_instruction)
    )
    if result:
        metadata = dict(_last_model_used_var.get() or {})
        metadata.update(_last_token_usage_var.get() or {})
        metadata.update(duration_seconds=time.monotonic() - started_at, mode="text")
        _last_generation_metadata_var.set(metadata)
        return result.text, result.total_tokens, result.truncated
    return None
