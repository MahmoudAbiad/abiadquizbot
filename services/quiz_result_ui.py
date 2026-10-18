"""Presentation helpers for the end-of-quiz review.

Reviews contain only incorrectly answered questions and are stored separately from
the current FSM session, so old result buttons never show a newer quiz's errors.
"""
from html import escape


def build_wrong_answer_review(questions, answered_questions):
    """Return compact, HTML-safe-to-render snapshots of explicitly wrong answers."""
    answers = answered_questions if isinstance(answered_questions, dict) else {}
    results = []
    for index, question in enumerate(questions or []):
        if not isinstance(question, dict):
            continue
        chosen = answers.get(str(index), answers.get(index))
        options = question.get("options") or []
        if not isinstance(options, (list, tuple)):
            continue
        try:
            chosen = int(chosen)
            correct = int(question.get("correct_option_id"))
        except (ValueError, TypeError):
            continue
        if chosen == correct or min(chosen, correct) < 0 or max(chosen, correct) >= len(options):
            continue
        results.append({
            "number": index + 1,
            "question": str(question.get("question") or "سؤال مصوّر أو غير نصي")[:1800],
            "chosen": str(options[chosen])[:700],
            "correct": str(options[correct])[:700],
        })
    return results


def review_page_text(entries, index):
    """Render a single review entry; callers must validate the page index."""
    item = entries[index]
    number = len(entries)
    return (
        f"📚 <b>مراجعة الأخطاء · {index + 1} من {number}</b>\n\n"
        f"❓ <b>السؤال {item['number']}:</b>\n{escape(item['question'])}\n\n"
        f"🔴 <b>إجابتك:</b> {escape(item['chosen'])}\n"
        f"🟢 <b>الإجابة الصحيحة:</b> {escape(item['correct'])}\n\n"
        "يمكنك العودة إلى النتيجة الأصلية في المحادثة."
    )


def review_inline_button(token, wrong_count):
    """Rich-message inline button markup (callback names use a random review token)."""
    return (
        f'<tg-button type="callback_data" style="link" data="qrev:{token}:0">'
        f"❌ مراجعة الأخطاء ({wrong_count})</tg-button>"
    )
