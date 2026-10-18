"""Gift ledger and atomic wallet operations (server-side service role only)."""
import secrets
import uuid
from ._client import supabase


def new_code():
    return 'ABIAD-' + secrets.token_hex(10).upper()


async def gift_balances(user_ids):
    if not user_ids:
        return {}
    result = await supabase.rpc('wallet_gift_balances', {'p_user_ids': user_ids}).execute()
    return {int(row['user_id']): float(row['gift_points']) for row in (result.data or [])}


async def enrich_balances(users):
    balances = await gift_balances([int(u['user_id']) for u in users])
    for user in users:
        user['gift_points'] = balances.get(int(user['user_id']), 0.0)
    return users


async def list_gifts(user_id, page=0, page_size=5):
    # Inclusive range: fetch one extra row to detect a following page. Keep the
    # admin's existing five-row default; the student wallet requests three.
    result = await supabase.table('point_gifts').select('*').eq('user_id', user_id).order(
        'granted_at', desc=True).order('id').range(page*page_size, page*page_size+page_size).execute()
    return result.data or []


async def redeem(user_id, code):
    result = await supabase.rpc('redeem_gift_code', {'p_user_id': user_id, 'p_code': code}).execute()
    return result.data


async def debit(user_id, amount, questions):
    result = await supabase.rpc('wallet_debit', {
        'p_user_id': user_id, 'p_amount': round(amount, 2), 'p_questions': questions,
        'p_debit_id': str(uuid.uuid4()),
    }).execute()
    return result.data


async def refund(user_id, debit_id, total_ratio=1):
    result = await supabase.rpc('wallet_refund', {
        'p_user_id': user_id, 'p_debit_id': debit_id, 'p_total_ratio': total_ratio,
    }).execute()
    if result.data is not True:
        raise RuntimeError("Wallet refund receipt was not found")
    return True


async def create_code(values):
    # The draft UUID/code are generated once before confirmation: duplicate callbacks
    # cannot create a second code. A retry after a lost response reads the same record.
    try:
        result = await supabase.table('gift_codes').insert(values).execute()
        return result.data[0]
    except Exception as exc:
        if getattr(exc, 'code', None) != '23505':
            raise
        result = await supabase.table('gift_codes').select('*').eq('id', values['id']).single().execute()
        if not result.data or result.data['code'] != values['code']:
            raise
        return result.data
