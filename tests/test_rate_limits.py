import sys
import unittest
from pathlib import Path
from datetime import datetime, timezone
from email.utils import format_datetime

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from services.ai.rate_limits import RateLimitInfo, classify, decide_cooldown


class RateLimitTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 10, 18, 12, 0, 0, tzinfo=timezone.utc).timestamp()

    def test_explicit_daily_quota_stops_at_next_midnight_not_24h(self):
        info = classify(429, {"error": {"message": "Rate limit exceeded: free-models-per-day-high-balance"}}, now=self.now)
        self.assertEqual(info.reason, "daily_quota")
        policy = decide_cooldown(429, info, is_openrouter_free=True, now=self.now)
        self.assertEqual(policy, type(policy)(43202, "openrouter_free_models", "utc_midnight"))

    def test_minute_limit_and_retry_after(self):
        info = classify(429, {"error": {"message": "free-models-per-min"}}, {"Retry-After": "17"}, now=self.now)
        self.assertEqual(info, RateLimitInfo("minute_rate_limit", 17, None))
        policy = decide_cooldown(429, info, is_openrouter_free=True, now=self.now)
        self.assertEqual((policy.seconds, policy.scope, policy.basis), (17, "openrouter_free_models", "retry_after"))

    def test_upstream_provider_metadata_stays_model_scoped(self):
        response = {"error": {"message": "Provider returned error", "metadata": {"provider_code": 429,
                "raw": "temporarily rate-limited upstream SECRET user text"}}}
        info = classify(429, response, now=self.now)
        self.assertEqual(info.reason, "upstream_rate_limit")
        policy = decide_cooldown(429, info, is_openrouter_free=True, now=self.now)
        self.assertEqual((policy.scope, policy.seconds), ("model_credential", 45))

    def test_unknown_429_is_short_model_scoped(self):
        info = classify(429, {"error": {"code": 429, "message": "Rate limit exceeded"}}, now=self.now)
        self.assertEqual(info.reason, "rate_limited_unknown")
        self.assertEqual(decide_cooldown(429, info, is_openrouter_free=True, now=self.now).seconds, 30)
        self.assertEqual(decide_cooldown(429, info, is_openrouter_free=True, now=self.now).scope, "model_credential")

    def test_reset_epoch_milliseconds_and_mirrored_headers(self):
        reset_ms = int((self.now + 75) * 1000)
        payload = {"error": {"message": "rate limited", "metadata": {"headers": {
            "X-RateLimit-Limit": "20", "X-RateLimit-Remaining": "0", "X-RateLimit-Reset": str(reset_ms)}}}}
        info = classify(429, payload, now=self.now)
        self.assertEqual(info.reason, "minute_rate_limit")
        self.assertEqual(info.reset_after_s, 75)
        self.assertEqual(decide_cooldown(429, info, is_openrouter_free=True, now=self.now).seconds, 77)

    def test_daily_headers_reset_to_actual_limit_not_midnight(self):
        headers = {"x-ratelimit-limit": "50", "x-ratelimit-remaining": "0",
                   "x-ratelimit-reset": str(int((self.now + 4900) * 1000))}
        info = classify(429, headers=headers, now=self.now)
        self.assertEqual(info.reason, "daily_quota")
        self.assertEqual(decide_cooldown(429, info, is_openrouter_free=True, now=self.now).seconds, 4902)

    def test_retry_after_http_date_and_invalid_input(self):
        date = format_datetime(datetime.fromtimestamp(self.now + 35, timezone.utc), usegmt=True)
        info = classify(429, headers={"retry-after": date}, now=self.now)
        self.assertEqual(info.retry_after_s, 35)
        junk = classify(429, headers={"retry-after": "private-key"}, now=self.now)
        self.assertIsNone(junk.retry_after_s)

    def test_non_429_no_account_wide_block(self):
        info = classify(401, {"error": {"message": "bad auth"}}, now=self.now)
        self.assertEqual(info.reason, "invalid_api_key")
        policy = decide_cooldown(401, info, is_openrouter_free=True, now=self.now)
        self.assertEqual((policy.seconds, policy.scope), (60, "model_credential"))

    def test_provider_code_overrides_generic_message(self):
        info = classify(429, {"error": {"message": "Rate limit exceeded",
                    "metadata": {"error_type": "rate_limit_exceeded", "provider_code": "429"}}}, now=self.now)
        self.assertEqual(info.reason, "upstream_rate_limit")

    def test_upstream_rate_limited_string_not_daily(self):
        info = classify(429, {"error": {"metadata": {
            "raw": "google/gemma-4-31b-it:free temporarily rate-limited upstream"},
            "message": "Provider returned error"}}, now=self.now)
        self.assertEqual(info.reason, "upstream_rate_limit")

    def test_generic_429_with_headers_no_window_still_platform(self):
        info = classify(429, headers={"x-ratelimit-limit": "42", "x-ratelimit-remaining": "0",
                                  "x-ratelimit-reset": str(int((self.now + 600) * 1000))}, now=self.now)
        self.assertEqual(info.reason, "platform_rate_limit")
        policy = decide_cooldown(429, info, is_openrouter_free=True, now=self.now)
        self.assertEqual(policy.scope, "openrouter_free_models")

    def test_rate_limit_reset_does_not_accept_relative_guess(self):
        info = classify(429, headers={"x-ratelimit-reset": "999", "retry-after": "in one hour"}, now=self.now)
        self.assertIsNone(info.reset_after_s)
        self.assertIsNone(info.retry_after_s)


if __name__ == "__main__":
    unittest.main()
