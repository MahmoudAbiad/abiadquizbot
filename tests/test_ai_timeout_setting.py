import unittest
from unittest.mock import AsyncMock, patch

from helpers import settings_helper as sh


class AiTimeoutSettingTests(unittest.IsolatedAsyncioTestCase):
    async def test_default_comes_from_constants(self):
        self.assertEqual(sh._DEFAULTS["ai_request_timeout"], 120.0)

    async def test_value_is_clamped_to_safe_bounds(self):
        for stored, expected in [(5, 30), (45, 45), (120, 120), (99999, 600)]:
            with patch.object(sh, "get_setting", AsyncMock(return_value=stored)):
                self.assertEqual(await sh.get_ai_request_timeout(), expected)

    async def test_setting_not_listed_in_points_panel(self):
        self.assertIn("ai_request_timeout", sh.SETTING_LABELS)
        self.assertNotIn("ai_request_timeout", sh.POINTS_SETTING_LABELS)


if __name__ == "__main__":
    unittest.main()
