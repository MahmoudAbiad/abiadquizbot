"""Runner-only currency. Spending/credit transactions live in service-only SQL RPCs."""
import hashlib
import hmac
import json
import re


def mastery_key(question):
    """Content identity survives option reordering, quiz copies and timing/image changes."""
    clean = lambda value: re.sub(r'\s+', ' ', str(value)).strip()
    options = question['options']
    value = {'question': clean(question['question']), 'options': sorted(clean(o) for o in options),
             'answer': clean(options[question['correct_option_id']]),
             'table': question.get('table'), 'matrices': question.get('matrices')}
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def track_objects(round_id, segment, secret):
    """Stable bounded track per question; no currency amount supplied by the browser."""
    digest = hmac.new(secret.encode(), f'runner-track:{round_id}:{segment}'.encode(), hashlib.sha256).digest()
    return [{'id': i, 'at': round(.7 * (i + 1), 2), 'lane': digest[i*2] % 3,
             'kind': 'coin' if digest[i*2+1] < 166 else 'barrier'} for i in range(10)]


async def wallet(db, uid):
    return (await db.rpc('runner_wallet_state', {'p_user': uid}).execute()).data
