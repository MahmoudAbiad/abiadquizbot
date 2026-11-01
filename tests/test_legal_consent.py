"""Regression tests for the private-chat legal-consent cache and version logic."""

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from services import consent


class LegalConsentTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        consent._ok_cache.clear()
        consent._denied_cache.clear()

    def tearDown(self):
        consent._ok_cache.clear()
        consent._denied_cache.clear()

    def test_missing_or_invalid_version_uses_one(self):
        self.assertEqual(consent._as_version(None), 1)
        self.assertEqual(consent._as_version("NaN"), 1)
        self.assertEqual(consent._as_version(3.0), 3)

    def test_policy_documents_are_inline_links_and_keyboard_keeps_decision_buttons(self):
        for state in (consent.STATE_NEW, consent.STATE_UPDATE):
            text = consent.build_consent_text(state)
            self.assertIn(f'<a href="{consent.PRIVACY_URL}">سياسة الخصوصية</a>', text)
            self.assertIn(f'<a href="{consent.TERMS_URL}">شروط الخدمة</a>', text)
        declined = consent.build_declined_text()
        self.assertIn(f'<a href="{consent.PRIVACY_URL}">سياسة الخصوصية</a>', declined)
        self.assertIn(f'<a href="{consent.TERMS_URL}">شروط الخدمة</a>', declined)

        keyboard = consent.build_consent_keyboard()
        buttons = [button for row in keyboard.inline_keyboard for button in row]
        self.assertEqual([button.callback_data for button in buttons], [consent.CONSENT_ACCEPT, consent.CONSENT_DECLINE])
        self.assertTrue(all(button.url is None for button in buttons))

    async def test_acceptance_invalidates_local_negative_state(self):
        key = (100, 2, 1)
        consent._denied_cache[key] = consent.STATE_DECLINED
        redis = SimpleNamespace(set=AsyncMock(return_value=True))
        with patch.object(consent, "_redis", return_value=redis):
            await consent.remember_accepted(*key)
        self.assertIn(key, consent._ok_cache)
        self.assertNotIn(key, consent._denied_cache)
        redis.set.assert_awaited_once_with("consent:ok:100:2:1", "1", ex=consent._REDIS_OK_TTL)

    async def test_decline_revokes_local_and_redis_acceptance(self):
        key = (100, 1, 1)
        consent._ok_cache[key] = True
        redis = SimpleNamespace(delete=AsyncMock(return_value=1))
        with patch.object(consent, "_redis", return_value=redis):
            await consent.forget_denied(*key)
        self.assertNotIn(key, consent._ok_cache)
        self.assertEqual(consent._denied_cache[key], consent.STATE_DECLINED)
        redis.delete.assert_awaited_once_with("consent:ok:100:1:1")

    async def test_decline_still_blocks_if_redis_delete_fails(self):
        key = (100, 1, 1)
        consent._ok_cache[key] = True
        redis = SimpleNamespace(
            delete=AsyncMock(side_effect=ConnectionError("temporarily unavailable")),
            exists=AsyncMock(return_value=1),
        )
        with patch.object(consent, "_redis", return_value=redis), patch.object(
            consent, "current_versions", new=AsyncMock(return_value=(1, 1))
        ):
            await consent.forget_denied(*key)
            self.assertEqual(await consent.get_consent_state(100), consent.STATE_DECLINED)
        redis.exists.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
