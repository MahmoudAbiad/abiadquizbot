"""Persistent inbox = match history. Push notifications are bounded, at-most-once hints.
Presence means recent activity in the game, NOT Telegram's online indicator.
"""
from html import escape


def notification_text(kind, match):
    labels = match["labels"]
    attacker, subject = escape(labels["attacker"]), escape(labels["subject"])
    if kind == "attack_started":
        return f"🚨 تتعرض أرضك للهجوم!\n⚔️ {attacker} يهاجم برج {subject}.\nيمكنك مراجعة النتيجة لاحقًا من سجل الدفاعات."
    r = match.get("result") or {}
    stars = "⭐" * r.get("stars", 0) or "دون نجوم"
    return (f"🏕️ نتيجة دفاع أرضك\n⚔️ {attacker} — برج {subject}\n"
            f"{r.get('correct', 0)}/{r.get('total', 0)} — {stars}\n"
            "التفاصيل في «أرضي ← سجل الدفاعات».")
