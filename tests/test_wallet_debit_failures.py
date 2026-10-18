"""An RPC error must never masquerade as a confirmed insufficient balance."""
import types
import unittest
from unittest.mock import AsyncMock, Mock

from aiogram import types as tg
import constants
from reliability_support import load_functions
from services.wallet_errors import WalletDebitError, WALLET_DEBIT_ERROR_TEXT


class WalletDebitFailures(unittest.IsolatedAsyncioTestCase):
    def helper(self, **kwargs):
        self.debit=AsyncMock(**kwargs)
        self.log=Mock()
        return load_functions('helpers/supabase_helper/users.py', {'update_user_stats'},
            debit=self.debit, validate_user_id=lambda _: (True,''), log_error=self.log,
            logger=Mock(), WalletDebitError=WalletDebitError)['update_user_stats']

    async def test_constraint_error_is_a_technical_failure(self):
        update=self.helper(side_effect=ValueError('users_free_points_check'))
        with self.assertRaises(WalletDebitError):
            await update(123,6,5)
        self.debit.assert_awaited_once_with(123,6,5)
        self.log.assert_called_once()

    async def test_unknown_network_outcome_is_not_reported_as_low_funds(self):
        update=self.helper(side_effect=ConnectionError('Response lost'))
        with self.assertRaises(WalletDebitError):
            await update(123,6,5)
        self.assertEqual(self.debit.await_count,1)  # no automatic double debit

    async def test_malformed_receipt_is_not_a_balance_rejection(self):
        update=self.helper(return_value={})
        with self.assertRaises(WalletDebitError):
            await update(123,6,5)

    async def test_only_explicit_null_means_confirmed_rejection(self):
        update=self.helper(return_value=None)
        self.assertIsNone(await update(123,6,5))
        self.log.assert_not_called()

    def balance_environment(self):
        deps={key:getattr(constants,key) for key in dir(constants) if key.isupper()}
        deps.update(types=tg, WALLET_DEBIT_ERROR_TEXT=WALLET_DEBIT_ERROR_TEXT,
            get_setting=AsyncMock(return_value=25), log_usage_event=AsyncMock(),
            spawn=lambda coroutine:coroutine.close())
        return load_functions('handlers/files.py', {'_insufficient_balance'}, **deps)

    async def test_funded_student_gets_no_recharge_button_or_low_balance_event(self):
        env=self.balance_environment()
        message=types.SimpleNamespace(answer=AsyncMock())
        await env['_insufficient_balance'](message,{'points':9684.1,'user_id':123},6)
        message.answer.assert_awaited_once_with(WALLET_DEBIT_ERROR_TEXT)
        env['get_setting'].assert_not_awaited()
        env['log_usage_event'].assert_not_called()

    async def test_real_shortfall_still_shows_correct_balance_and_deficit(self):
        env=self.balance_environment()
        message=types.SimpleNamespace(answer=AsyncMock())
        await env['_insufficient_balance'](message,{'points':3,'free_points':1,'paid_points':2},6)
        text=message.answer.await_args.args[0]
        self.assertIn('لا يكفي',text)
        self.assertIn('3.00 نقطة',text)
        self.assertIsNotNone(message.answer.await_args.kwargs['reply_markup'])

    async def test_cached_quiz_rpc_failure_preserves_selection_and_does_not_start(self):
        data={'items_count':1,'available_quizzes':[{'id':'cached','quiz_data':[{}]}]}
        state=types.SimpleNamespace(get_data=AsyncMock(return_value=data))
        call=types.SimpleNamespace(data='use_multi_cached',from_user=types.SimpleNamespace(id=123),
            answer=AsyncMock(),message=types.SimpleNamespace(answer=AsyncMock()))
        env=load_functions('handlers/files.py', {'handle_multi_cache_selection'},
            _current_user=AsyncMock(return_value={'points':9684.1}),_renewal_notice=AsyncMock(),
            calculate_cached_points_cost=AsyncMock(return_value=6),
            update_user_stats=AsyncMock(side_effect=WalletDebitError('Database error')),
            WalletDebitError=WalletDebitError,WALLET_DEBIT_ERROR_TEXT=WALLET_DEBIT_ERROR_TEXT,
            _insufficient_balance=AsyncMock(),start_loaded_quiz=AsyncMock(),
            log_error=Mock(),logger=Mock())
        await env['handle_multi_cache_selection'](call,state)
        call.message.answer.assert_awaited_once_with(WALLET_DEBIT_ERROR_TEXT)
        env['start_loaded_quiz'].assert_not_awaited()
        env['_insufficient_balance'].assert_not_awaited()
        self.assertEqual(data['available_quizzes'][0]['id'],'cached')
