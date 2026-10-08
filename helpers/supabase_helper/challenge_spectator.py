"""Trusted bot only. Actor must come from Telegram from_user.id, never callback data."""
from .challenge_game import rpc


async def state(match_id, user_id):
    return await rpc('challenge_spectator_state', p_match=match_id, p_user=user_id)


async def incoming(user_id, page=0):
    return await rpc('challenge_spectator_incoming', p_user=user_id, p_offset=max(0, page)*5) or []
