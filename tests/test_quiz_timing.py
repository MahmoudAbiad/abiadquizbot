import asyncio
import unittest
from unittest.mock import patch
from services.quiz_timing import measured


class TimingTests(unittest.IsolatedAsyncioTestCase):
    async def test_result_and_monotonic_duration(self):
        async def work(): return 42
        with patch('services.quiz_timing.time.perf_counter', side_effect=[10, 10.025]), patch('services.quiz_timing.logger.info') as log:
            self.assertEqual(await measured('render', work()), 42)
        self.assertAlmostEqual(log.call_args.args[3], 25)
        self.assertEqual(log.call_args.args[4], 'ok')

    async def test_original_error_and_cancellation_propagate(self):
        for error in (ValueError('private payload'), asyncio.CancelledError()):
            async def work(): raise error
            with patch('services.quiz_timing.logger.info') as log:
                with self.assertRaises(type(error)) as caught:
                    await measured('send', work())
            self.assertIs(caught.exception, error)
            self.assertEqual(log.call_args.args[4], type(error).__name__)
            self.assertNotIn('private payload', str(log.call_args))
