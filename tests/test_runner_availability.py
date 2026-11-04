import ast
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from services.runner_availability import accepting_new_rounds


class AdmissionTests(unittest.IsolatedAsyncioTestCase):
    async def test_environment_is_required_even_when_admin_switch_is_on(self):
        with patch('services.runner_core.enabled', return_value=False):
            self.assertFalse(await accepting_new_rounds())

    async def test_explicit_switch_and_read_failure(self):
        for value in (True, False, ConnectionError('offline')):
            reader = AsyncMock(**({'side_effect': value} if isinstance(value, Exception) else {'return_value': value}))
            module = types.SimpleNamespace(is_feature_enabled=reader)
            with patch('services.runner_core.enabled', return_value=True), patch.dict(sys.modules, {
                'helpers.supabase_helper.feature_flags': module,
            }):
                self.assertEqual(await accepting_new_rounds(), value is True)
                reader.assert_awaited_once_with('runner_enabled', default=False)

    def test_actual_registry_defaults_off_for_single_and_batch_reads(self):
        tree = ast.parse(Path('helpers/supabase_helper/feature_flags.py').read_text(encoding='utf-8'))
        flags = next(ast.literal_eval(n.value) for n in tree.body if isinstance(n, ast.Assign)
                     and any(isinstance(t, ast.Name) and t.id == '_GAME_FLAGS' for t in n.targets))
        self.assertIn('runner_enabled', flags)
