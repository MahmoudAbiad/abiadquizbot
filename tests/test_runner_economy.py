import types
import unittest
from unittest.mock import AsyncMock, patch
import pytest
from fastapi import HTTPException
from services.runner_economy import track_objects
from services import runner_api as api
from uuid import uuid4


def test_tracks_bounded_stable_and_distinct():
    plan = track_objects('r', 0, 'secret')
    assert plan == track_objects('r', 0, 'secret')
    assert plan != track_objects('r', 1, 'secret')
    assert plan != track_objects('other', 0, 'secret')
    assert len(plan) == 10
    assert [o['id'] for o in plan] == list(range(10))
    assert all(o['lane'] in (0, 1, 2) and o['kind'] in ('coin', 'barrier') for o in plan)


class EconomyAPITests(unittest.IsolatedAsyncioTestCase):
    async def test_claim_uses_authenticated_user_and_never_client_amount(self):
        request = api.CoinsRequest(init_data='x', quiz_id=uuid4(), round_id=uuid4(), segment=1, coin_ids=[0, 2])
        db = types.SimpleNamespace(rpc=lambda name, args: rpc(name, args))
        calls = []
        def rpc(name, args):
            calls.append((name, args))
            return types.SimpleNamespace(execute=AsyncMock(return_value=types.SimpleNamespace(data={'coins':2,'accepted':[0,2]})))
        with patch.object(api,'identity',AsyncMock(return_value=7)), patch.object(api,'round_row',AsyncMock()), patch.object(api,'db',return_value=db):
            result = await api.claim_coins(request)
            assert result['coins'] == 2
            assert calls == [('runner_claim_coins', {'p_user':7,'p_round':str(request.round_id),'p_segment':1,'p_ids':[0,2]})]
            request.coin_ids = [999]
            with pytest.raises(HTTPException): await api.claim_coins(request)
    
    
    async def test_purchase_checks_auth_and_quiz_access_before_rpc(self):
        request=api.Request(init_data='x',quiz_id=uuid4())
        db=types.SimpleNamespace(rpc=AsyncMock())
        with patch.object(api,'identity',AsyncMock(return_value=7)), patch.object(api,'access',AsyncMock(side_effect=HTTPException(403))), patch.object(api,'db',return_value=db):
            with pytest.raises(HTTPException): await api.buy_jetpack(request)
            db.rpc.assert_not_called()
