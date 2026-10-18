"""Pure, Telegram-independent game rules. SQL uses the same snapshotted JSON config.
No paid-points imports or dependency on the existing users table.
"""
import json
import math
from pathlib import Path


def load_rules():
    rules = json.loads(Path(__file__).with_name("challenge_rules.json").read_text())
    if not 1 <= rules["questions_per_attack"] <= 50:
        raise ValueError("questions_per_attack must be 1..50")
    if not 5 <= rules["question_seconds"] <= 300:
        raise ValueError("question_seconds must be 5..300")
    if rules["attack_seconds"] < rules["question_seconds"]:
        raise ValueError("attack_seconds too short")
    ts = rules["star_thresholds"]
    if len(ts) != 3 or not 0 < ts[0] < ts[1] < ts[2] <= 1:
        raise ValueError("invalid star thresholds")
    if not 0 <= rules["speed_bonus"] < rules["accuracy_points"]:
        raise ValueError("accuracy must dominate speed")
    for key, val in rules.items():
        if key != "star_thresholds" and (not isinstance(val, int) or isinstance(val, bool) or val < 0):
            raise ValueError(f"invalid rule: {key}")
    if min(rules["tower_level_base_xp"], rules["max_tower_level"],
           rules["target_cooldown_seconds"], rules["repeat_content_seconds"]) <= 0:
        raise ValueError("invalid zero rule")
    # Live (real-time) rules: same file, same pinned-per-match snapshot as async rules.
    if not 1 <= rules["live_questions"] <= 50:
        raise ValueError("live_questions must be 1..50")
    if not 0 <= rules["live_countdown_seconds"] <= 60:
        raise ValueError("live_countdown_seconds must be 0..60")
    if rules["live_queue_seconds"] < 10 or rules["live_range_step_seconds"] < 1 or rules["live_pair_daily_limit"] < 1:
        raise ValueError("invalid live matchmaking rule")
    if rules["live_range_initial"] > rules["matchmaking_range"]:
        raise ValueError("live_range_initial exceeds matchmaking_range")
    return rules


def question_score(correct, elapsed_seconds, rules):
    deadline = rules["question_seconds"]
    if not correct or elapsed_seconds < 0 or elapsed_seconds >= deadline:
        return 0
    return rules["accuracy_points"] + math.floor(rules["speed_bonus"] * (1 - elapsed_seconds / deadline))


def stars_for(correct, total, rules):
    if total <= 0:
        return 0
    return sum(correct / total >= threshold for threshold in rules["star_thresholds"])


def tower_progress(xp, correct, questions, rules):
    """XP progression capped by lifetime demonstrated accuracy; empty actions add nothing."""
    base, maximum = rules["tower_level_base_xp"], rules["max_tower_level"]
    earned = min(maximum, 1 + math.floor(math.sqrt(max(0, xp) / base)))
    mastery = 1 if questions <= 0 else 1 + math.floor((maximum - 1) * correct / questions)
    level = min(earned, mastery)
    return {"level": level, "xp": xp, "next_level_xp": base * level * level,
            "accuracy_cap": mastery}


def subject_icon(name):
    for fragment, icon in (("كيمي", "🧪"), ("فيزي", "⚡"), ("رياض", "➗"),
                           ("عربي", "📚"), ("علوم", "🧬"), ("إنكلي", "🔤"), ("انكلي", "🔤")):
        if fragment in name:
            return icon
    return "🏰"
