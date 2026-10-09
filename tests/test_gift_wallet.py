import asyncio
import types
import unittest
from unittest.mock import AsyncMock, Mock, patch
from datetime import datetime, timedelta, timezone
from reliability_support import load_functions
from services.gift_presentation import expiry_text, parse_expiry, gift_text


class Presentation(unittest.TestCase):
    def test_dates_are_damascus_and_zero_means_forever(self):
        self.assertEqual(parse_expiry('0'),None)
        self.assertEqual(parse_expiry('2099-10-08 18:00'),'2099-10-08T15:00:00+00:00')
        self.assertIn('18:00',expiry_text('2099-10-08T15:00:00Z'))
        with self.assertRaises(ValueError):parse_expiry('2000-01-01 00:00')

    def test_expired_revoked_and_spent_gifts_are_distinguishable(self):
        gift=dict(title='<هدية>',remaining_amount=5,original_amount=10,expires_at='2000-01-01T00:00:00Z')
        text=gift_text(gift);self.assertIn('منتهية',text);self.assertIn('&lt;',text)
        gift['revoked_at']='2020-01-01';self.assertIn('ملغاة',gift_text(gift))
        gift.update(revoked_at=None,expires_at=None,remaining_amount=0)
        self.assertIn('مستهلكة',gift_text(gift))

    def test_balances_include_gift_without_changing_legacy_total(self):
        env=load_functions('helpers/supabase_helper/users.py',{'_balance_payload'})
        self.assertEqual(env['_balance_payload'](5,20,30)['points'],55)
        self.assertEqual(env['_balance_payload'](5,20)['points'],25)

    def test_admin_filter_rejects_non_admin_and_missing_identity(self):
        BaseFilter = object
        env=load_functions('handlers/admin/dashboard.py',{'IsAdminFilter'},BaseFilter=BaseFilter,ADMIN_ID=99)
        guard=env['IsAdminFilter']()
        self.assertFalse(asyncio.run(guard(types.SimpleNamespace(from_user=None))))
        self.assertFalse(asyncio.run(guard(types.SimpleNamespace(from_user=types.SimpleNamespace(id=1)))))
        self.assertTrue(asyncio.run(guard(types.SimpleNamespace(from_user=types.SimpleNamespace(id=99)))))


class WalletFlow(unittest.IsolatedAsyncioTestCase):
    async def test_gift_only_generation_refunds_by_receipt(self):
        fn=load_functions('services/quiz_service.py',{'refund_user_on_failure'})['refund_user_on_failure']
        refund=AsyncMock(return_value=True)
        module=types.ModuleType('helpers.supabase_helper.gifts');module.refund=refund
        with patch.dict('sys.modules',{'helpers.supabase_helper.gifts':module}):
            await fn(123,{'debit_id':'abc','debited_gift':30,'debited_free':0,'debited_paid':0})
        refund.assert_awaited_once_with(123,'abc')

    async def test_old_pending_generation_keeps_old_split_refund(self):
        split=AsyncMock()
        fn=load_functions('services/quiz_service.py',{'refund_user_on_failure'},refund_user_points_split=split)['refund_user_on_failure']
        await fn(123,{'debited_free':2,'debited_paid':3})
        split.assert_awaited_once_with(123,2,3)

    async def test_renewal_exposes_three_buckets(self):
        client=Mock();client.rpc.return_value.execute=AsyncMock(return_value=types.SimpleNamespace(data=[dict(free_points=25,paid_points=9,renewal_status='renewed')]))
        env=load_functions('helpers/supabase_helper/users.py',{'_check_daily_renewal','_balance_payload'},supabase=client,
                           gift_balances=AsyncMock(return_value={12:40}),get_setting=AsyncMock(return_value=25),logger=Mock(),log_error=Mock())
        row=await env['_check_daily_renewal'](12,{},'2026-10-08')
        self.assertEqual(row['points'],74);self.assertEqual(row['gift_points'],40)

    async def test_new_debit_preserves_receipt_in_helper(self):
        expected=dict(remaining_points=8,debited_free=0,debited_paid=0,debited_gift=12,debit_id='r1')
        env=load_functions('helpers/supabase_helper/users.py',{'update_user_stats'},debit=AsyncMock(return_value=expected),
                           validate_user_id=lambda _: (True,''),log_error=Mock(),logger=Mock())
        self.assertEqual(await env['update_user_stats'](123,12),expected)

    async def test_code_creation_retry_returns_original_not_second_code(self):
        class Conflict(Exception):code='23505'
        row=dict(id='id',code='ABIAD-12345678901234567890')
        q=Mock();q.insert.return_value=q;q.select.return_value=q;q.eq.return_value=q;q.single.return_value=q
        q.execute=AsyncMock(side_effect=[Conflict(),types.SimpleNamespace(data=row)])
        env=load_functions('helpers/supabase_helper/gifts.py',{'create_code'},supabase=types.SimpleNamespace(table=lambda _:q))
        self.assertEqual(await env['create_code'](row),row)

    async def test_audio_balance_counts_gifts(self):
        fn=load_functions('services/audio_flow_service.py',{'compute_balance_and_deficit'})['compute_balance_and_deficit']
        self.assertEqual(fn({'free_points':1,'gift_points':5,'paid_points':2},10),(8,2))
