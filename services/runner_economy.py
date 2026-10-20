"""Runner-only currency. Spending/credit transactions live in service-only SQL RPCs."""
import hashlib
import hmac


def track_objects(round_id, segment, secret):
    """Stable bounded track per question; no currency amount supplied by the browser."""
    digest = hmac.new(secret.encode(), f'runner-track:{round_id}:{segment}'.encode(), hashlib.sha256).digest()
    return [{'id': i, 'at': round(.7 * (i + 1), 2), 'lane': digest[i*2] % 3,
             'kind': 'coin' if digest[i*2+1] < 166 else 'barrier'} for i in range(10)]


async def wallet(db, uid):
    rows = (await db.table('runner_wallets').select('coins,jetpack_owned,version').eq('user_id', uid).limit(1).execute()).data
    return rows[0] if rows else {'coins': 0, 'jetpack_owned': False, 'version': 0}
