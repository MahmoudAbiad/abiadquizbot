"""Pure runner rules. Browser never supplies an authoritative score or answer key."""
import base64
import hashlib
import hmac
import json
import os
import random
import time
from urllib.parse import urlparse
from services.quiz_timing import fallback_time, question_key, valid_time


def enabled():
    ref = os.getenv('RUNNER_TEST_SUPABASE_REF', '')
    return (os.getenv('RUNNER_ENABLED') == '1' and bool(ref)
            and os.getenv('RENDER_GIT_BRANCH', os.getenv('APP_BRANCH', '')) == 'test'
            and urlparse(os.getenv('SUPABASE_URL', '')).hostname == ref + '.supabase.co')


def sign_launch(quiz_id, user_id, secret, now=None):
    expires = int(now if now is not None else time.time()) + 7200
    body = f'{quiz_id}:{user_id}:{expires}'
    sig = hmac.new(secret.encode(), ('runner:'+body).encode(), hashlib.sha256).hexdigest()
    return base64.urlsafe_b64encode(f'{body}:{sig}'.encode()).decode().rstrip('=')


def verify_launch(token, quiz_id, user_id, secret, now=None):
    try:
        raw = base64.urlsafe_b64decode(token + '=' * (-len(token) % 4)).decode()
        qid, uid, expiry, sig = raw.split(':')
        body = f'{qid}:{uid}:{expiry}'
        expected = hmac.new(secret.encode(), ('runner:'+body).encode(), hashlib.sha256).hexdigest()
        return (qid == str(quiz_id) and uid == str(user_id)
                and (now if now is not None else time.time()) <= int(expiry)
                and hmac.compare_digest(expected, sig))
    except (ValueError, UnicodeError, TypeError):
        return False


def build_questions(questions, settings=None, rng=None):
    settings, rng = settings or {}, rng or random.SystemRandom()
    output = []
    for index, q in enumerate(questions or []):
        if not isinstance(q, dict):
            raise ValueError('سؤال غير صالح')
        options, correct = q.get('options'), q.get('correct_option_id')
        if (not isinstance(options, list) or len(options) < 3 or isinstance(correct, bool)
                or not isinstance(correct, int) or not 0 <= correct < len(options)
                or any(not isinstance(o, str) or not o.strip() for o in options)):
            raise ValueError(f'السؤال {index+1} غير مؤهل: يلزمه جواب صحيح وخياران خاطئان')
        wrong = [i for i in range(len(options)) if i != correct]
        selected = [correct] + rng.sample(wrong, 2)
        rng.shuffle(selected)
        key = question_key(q)
        original_ai = valid_time(q.get('ai_estimated_time_seconds', q.get('estimated_time_seconds')))
        ai = original_ai or valid_time(settings.get('ai_times', {}).get(key))
        seconds = (valid_time(settings.get('manual_times', {}).get(key))
                   or valid_time(settings.get('uniform_seconds')) or ai or fallback_time(q))
        output.append({'source_index': index, 'key': key, 'source': q,
                       'options': [{'id': i, 'text': options[i]} for i in selected],
                       'correct_id': correct, 'seconds': seconds})
    if not output or len(output) > 300:
        raise ValueError('اختر اختباراً بين سؤال واحد و300 سؤال')
    return output


def answer_transition(row, option_id, now=None):
    now = time.time() if now is None else now
    if row['status'] != 'running' or row.get('issued_at') is None:
        raise ValueError('السؤال غير نشط')
    q = row['questions'][row['cursor']]
    if option_id is not None and option_id not in [o['id'] for o in q['options']]:
        raise ValueError('الخيار غير موجود في الجولة')
    correct = option_id == q['correct_id'] and now - row['issued_at'] <= q['seconds'] + 2
    lives = row['lives'] - (0 if correct else 1)
    cursor = row['cursor'] + 1
    status = 'caught' if lives <= 0 else ('completed' if cursor == len(row['questions']) else 'running')
    response = {'correct': correct, 'correct_text': q['source']['options'][q['correct_id']],
                'correct_id': q['correct_id'], 'explanation': q['source'].get('explanation') or 'لا يوجد شرح محفوظ.',
                'lives': lives, 'score': row['score'] + (100 if correct else 0), 'status': status}
    return {'lives': lives, 'cursor': cursor, 'status': status, 'score': response['score'],
            'issued_at': None, 'version': row['version'] + 1, 'last_response': response}, response
