"""Safe, structured rate-limit diagnostics and bounded cooldown decisions.

Raw upstream messages are used only for classification and never retained/logged.
"""
import math
import re
import time
from dataclasses import dataclass
from datetime import timezone
from email.utils import parsedate_to_datetime


@dataclass(frozen=True)
class RateLimitInfo:
    reason: str
    retry_after_s: int | None = None
    reset_after_s: int | None = None


@dataclass(frozen=True)
class Cooldown:
    seconds: int
    scope: str  # model_credential | openrouter_free_models
    basis: str  # provider_reset | retry_after | utc_midnight | policy_default


def _error_metadata(payload):
    error = payload.get("error") if isinstance(payload, dict) else None
    return error.get("metadata") if isinstance(error, dict) and isinstance(error.get("metadata"), dict) else {}


def _safe_message(payload):
    error = payload.get("error") if isinstance(payload, dict) else None
    metadata = _error_metadata(payload)
    # Inspect only bounded strings for specific known rate-limit terms.
    parts = []
    for value in (error.get("message") if isinstance(error, dict) else error,
                  metadata.get("error_type"), metadata.get("limit_source"), metadata.get("raw")):
        if isinstance(value, str):
            parts.append(value[:2048].casefold())
    return " ".join(parts)


def _header(headers, metadata, name):
    """Use HTTP headers first, then the optional sanitized metadata.headers mirror."""
    if headers:
        for key, val in headers.items():
            if isinstance(key, str) and key.casefold() == name:
                return val
    mirrored = metadata.get("headers")
    if isinstance(mirrored, dict):
        for key, val in mirrored.items():
            if isinstance(key, str) and key.casefold() == name:
                return val
    return None


def _parse_retry_after(value, now):
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        seconds = value
    elif isinstance(value, str):
        raw = value.strip()
        if len(raw) > 90:
            return None
        try:
            seconds = float(raw) if re.fullmatch(r"\d+(?:\.\d{1,3})?", raw) else None
            if seconds is None:
                dt = parsedate_to_datetime(raw)
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                seconds = dt.timestamp() - now
        except (TypeError, ValueError, OverflowError):
            return None
    else:
        return None
    if not math.isfinite(seconds) or not 0 <= seconds <= 172800:
        return None
    return max(1, math.ceil(seconds))


def _parse_reset(value, now):
    """Accept seconds or milliseconds Unix timestamps, not guessed relative units."""
    if not isinstance(value, (str, int, float)) or isinstance(value, bool):
        return None
    raw = str(value).strip()
    if not re.fullmatch(r"\d{10,13}(?:\.\d{1,3})?", raw):
        return None
    stamp = float(raw)
    if stamp >= 1e12:
        stamp /= 1000
    delta = stamp - now
    if not 0 <= delta <= 172800:
        return None
    return max(1, math.ceil(delta))


def classify(status, payload=None, headers=None, *, now=None):
    """Return fixed labels using upstream metadata, body hints, and reset headers."""
    now = time.time() if now is None else now
    metadata = _error_metadata(payload)
    hint = _safe_message(payload)
    retry = _parse_retry_after(_header(headers, metadata, "retry-after"), now)
    reset = _parse_reset(_header(headers, metadata, "x-ratelimit-reset"), now)
    if status != 429:
        codes = {400: "invalid_request_or_parameters", 401: "invalid_api_key",
                 402: "insufficient_credits", 403: "access_forbidden",
                 404: "model_or_route_not_found", 408: "upstream_timeout",
                 422: "invalid_request_or_parameters", 500: "upstream_server_error",
                 502: "upstream_unavailable", 503: "upstream_unavailable",
                 504: "upstream_timeout", 529: "provider_capacity"}
        return RateLimitInfo(codes.get(status, "upstream_error"), retry, reset)

    # Explicit platform errors take precedence over more general metadata markers.
    if ("free-models-per-day" in hint or "free_tier_daily" in hint or
            re.search(r"(?:per[- ]day|daily (?:request|rate|quota|limit)|requests?/day)", hint)):
        reason = "daily_quota"
    elif ("free-models-per-min" in hint or "free_tier_minute" in hint or
          re.search(r"(?:per[- ]min(?:ute)?|minute (?:request|rate|quota|limit)|requests?/min|\brpm\b)", hint)):
        reason = "minute_rate_limit"
    elif (metadata.get("provider_code") is not None or
          any(s in hint for s in ("provider_overloaded", "provider_rate_limit", "upstream", "provider returned error", "provider busy", "capacity"))):
        reason = "upstream_rate_limit"
    else:
        # OpenRouter documents X-RateLimit-* headers on its platform-side 429s.
        limit = _header(headers, metadata, "x-ratelimit-limit")
        remaining = _header(headers, metadata, "x-ratelimit-remaining")
        if reset is not None and str(limit).strip().isdecimal() and str(remaining).strip() == "0":
            # Only infer the window when the documented free-model caps are recognizable.
            if int(limit) == 20 and reset <= 180:
                reason = "minute_rate_limit"
            elif int(limit) in (50, 1000):
                reason = "daily_quota"
            else:
                reason = "platform_rate_limit"
        elif reset is not None and remaining is not None:
            reason = "platform_rate_limit"
        else:
            reason = "rate_limited_unknown"
    return RateLimitInfo(reason, retry, reset)


def decide_cooldown(status, info, *, is_openrouter_free=False, now=None):
    """Choose bounded duration, and widen to the free-model pool only when proven platform-wide."""
    now = time.time() if now is None else now
    reason = info.reason
    shared = is_openrouter_free and reason in {"daily_quota", "minute_rate_limit", "platform_rate_limit"}
    scope = "openrouter_free_models" if shared else "model_credential"
    retry = info.retry_after_s
    reset = info.reset_after_s
    if status != 429:
        return Cooldown(60, "model_credential", "policy_default")
    if reason == "daily_quota":
        if reset is not None:
            return Cooldown(min(reset + 2, 86400), scope, "provider_reset")
        if retry is not None:
            return Cooldown(min(retry, 86400), scope, "retry_after")
        # OpenRouter free-model daily budgets reset at midnight UTC.
        remaining = 86400 - (now % 86400)
        return Cooldown(max(2, min(math.ceil(remaining) + 2, 86400)), scope, "utc_midnight")
    if reason in ("minute_rate_limit", "platform_rate_limit"):
        # For an unlabelled platform limit, honor a trusted reset header even
        # when its window is longer than one hour.
        maximum = 86400 if reason == "platform_rate_limit" else 3600
        if retry is not None:
            return Cooldown(min(retry, maximum), scope, "retry_after")
        if reset is not None:
            return Cooldown(min(reset + 2, maximum), scope, "provider_reset")
        return Cooldown(65, scope, "policy_default")
    if retry is not None:
        return Cooldown(min(retry, 3600), scope, "retry_after")
    if reset is not None:
        return Cooldown(min(reset + 2, 3600), scope, "provider_reset")
    return Cooldown(45 if reason == "upstream_rate_limit" else 30,
                    scope, "policy_default")
