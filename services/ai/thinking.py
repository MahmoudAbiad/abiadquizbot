"""Translate the detection thinking policy without inventing vendor settings."""
import re

from logger import get_logger

logger = get_logger(__name__)


def google_thinking(model, effort, types):
    if not effort:
        return None
    name = model.rsplit("/", 1)[-1].lower()
    # These families expose MEDIUM. Image models and Pro variants differ.
    if (re.match(r"gemini-3(?:\.\d+)?-flash(?:-|$)", name)
            and "image" not in name) or name == "gemini-4-argon":
        logger.info("AI_THINKING model=%s requested=%s applied=level", model, effort)
        return types.ThinkingConfig(thinking_level=effort.upper())
    if re.match(r"gemini-2\.5-(?:flash|pro)(?:-|$)", name):
        # Application mapping, not a Google-defined medium level. 4096 is
        # inside the supported budget ranges of 2.5 Flash, Flash-Lite and Pro.
        logger.info("AI_THINKING model=%s requested=%s applied=budget_4096", model, effort)
        return types.ThinkingConfig(thinking_budget=4096)
    logger.warning("AI_THINKING_UNSUPPORTED model=%s requested=%s applied=provider_default", model, effort)
    return None


def chat_thinking(service, model, effort):
    if not effort:
        return {}
    if service == "openrouter":
        # OpenRouter translates the unified effort for its reasoning endpoints.
        logger.info("AI_THINKING model=%s requested=%s applied=openrouter_effort", model, effort)
        return {"reasoning": {"effort": effort}}
    name = model.lower()
    if (service == "generic"
            and re.match(r"(?:o[134](?:-|$)|gpt-5(?:[.\-]|$))", name)
            and not name.startswith(("o1-mini", "o1-preview")) and "chat" not in name):
        logger.info("AI_THINKING model=%s requested=%s applied=reasoning_effort", model, effort)
        return {"reasoning_effort": effort}
    logger.warning("AI_THINKING_UNSUPPORTED model=%s requested=%s applied=provider_default", model, effort)
    return {}
