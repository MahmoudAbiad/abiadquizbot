"""Pure, Telegram-independent live-match helpers. SQL (0010) is the source of truth;
these mirror its matchmaking-range and outcome rules so they can be unit-tested offline.
Scoring is NOT duplicated: reuse services.challenge_game.question_score / SQL challenge_question_score.
"""
import math


def allowed_range(waited_seconds, rules):
    """Trophy range for one searcher: starts narrow, widens in steps, capped by matchmaking_range."""
    steps = math.floor(max(0, waited_seconds) / rules["live_range_step_seconds"])
    return min(rules["matchmaking_range"], rules["live_range_initial"] + rules["live_range_step"] * steps)


def can_match(trophies_a, waited_a, trophies_b, waited_b, rules):
    """Symmetric: a pair matches when the gap fits the wider of the two current ranges."""
    gap = abs(trophies_a - trophies_b)
    return gap <= max(allowed_range(waited_a, rules), allowed_range(waited_b, rules))


def outcome(score_a, score_b):
    """'a' | 'b' | 'draw' — equal total score is a draw (no hidden tie-breaker)."""
    return "a" if score_a > score_b else "b" if score_b > score_a else "draw"
