"""Tolerant generation timing and content identities shared with the runner."""
import hashlib
import json
import math

TIMING_INSTRUCTION = ('\nFor EVERY question return estimated_time_seconds: an integer estimate of the '
    'seconds a student needs to read and solve it (10–300). Consider reading length, '
    'calculation steps and difficulty. Estimate in this same response; retain exactly four options.')


def valid_time(value):
    if isinstance(value, bool):
        return None
    try:
        value = float(value)
        if math.isfinite(value) and 10 <= value <= 300:
            return round(value)
    except (TypeError, ValueError, OverflowError):
        pass
    return None


def fallback_time(question):
    words = len(str(question.get('question', '')).split())
    words += sum(len(str(o).split()) for o in question.get('options', []))
    math_question = question.get('is_math') or question.get('matrices') or '$' in str(question.get('question', ''))
    return min(180, max(25, round(words / 2.5) + (55 if math_question else 15)))


def question_key(question):
    # Excludes transient image URLs and timing, includes options and answer mapping.
    value = {k: question.get(k) for k in ('question', 'options', 'correct_option_id', 'table', 'matrices')}
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
