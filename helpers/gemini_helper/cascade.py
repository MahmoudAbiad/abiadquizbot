"""Compatibility import: execution policy lives outside the Gemini package."""
from services.ai.runtime import execute as _execute_cascade  # noqa: F401
