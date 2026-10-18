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
        is_math = bool(question.get("is_math"))
        results.append({
            "index": index,
            "is_math": is_math,
            "image_url": str(question.get("image_url") or "")[:2048] if is_math else "",
            "explanation": str(question.get("explanation") or "")[:3500],
            "math_question": {
                key: question.get(key) for key in
                ("question", "options", "table", "matrices", "is_math")
            } if is_math else None,
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
        f"🔴 <b>إجابتك:</b> {escape(item['chosen'])}\n\n"
        f"🟢 <b>الإجابة الصحيحة:</b> {escape(item['correct'])}\n\n"
        "يمكنك العودة إلى النتيجة الأصلية في المحادثة."
    )


def review_inline_button(token, wrong_count):
    """Rich-message inline button markup (callback names use a random review token)."""
    return (
        '<tg-button-row align="center">'
        f'<tg-button type="callback_data" style="primary" data="qrev:{token}:0">'
        f"📚 مراجعة الأخطاء ({wrong_count})</tg-button>"
        '</tg-button-row>'
    )


def review_choice_label(entry, key):
    """Match the original letter-only poll for math, not the full LaTeX option."""
    if not entry.get("is_math"):
        return entry[key]
    question = entry.get("math_question") or {}
    options = question.get("options") or []
    try:
        option_index = options.index(entry[key])
        from services.image_quiz_renderer import looks_arabic, letters_for
        letters = letters_for(looks_arabic(str(question.get("question") or "")), len(options))
        return letters[option_index]
    except (ValueError, TypeError, IndexError, KeyError):
        return entry[key]


def review_rich_button(callback_data, title):
    return (
        '<tg-button-row align="center">'
        f'<tg-button type="callback_data" style="primary" data="{escape(callback_data, quote=True)}">'
        f'{escape(title)}</tg-button></tg-button-row>'
    )


def review_question_rich_html(entry, page, total, token, image_url=""):
    parts = [
        f"<p>📚 <b>مراجعة الأخطاء · {page + 1} من {total}</b></p>",
        f"<p>❓ <b>السؤال {entry['number']}:</b></p>",
    ]
    if entry.get("is_math") and image_url:
        parts.append(f'<img src="{escape(image_url, quote=True)}"/>')
    else:
        question_text = entry["question"]
        if entry.get("is_math"):
            from services.latex_text import latex_to_plain
            question_text = latex_to_plain(question_text)
        parts.append(f"<p>{escape(question_text)}</p>")
    chosen = review_choice_label(entry, "chosen")
    correct = review_choice_label(entry, "correct")
    parts.extend([
        f"<p>🔴 <b>إجابتك:</b> {escape(chosen)}</p>",
        f"<p>🟢 <b>الإجابة الصحيحة:</b> {escape(correct)}</p>",
        review_rich_button(f"qrevexp:{token}:{page}", "💡 انقر لعرض الشرح"),
    ])
    return "".join(parts)


def review_explanation_rich_html(entry, page, total, token):
    from services.latex_text import latex_to_plain
    explanation = entry.get("explanation") or "لا يوجد شرح محفوظ لهذا السؤال."
    if entry.get("is_math"):
        explanation = latex_to_plain(explanation)
    explanation = escape(explanation).replace("\n", "<br>")
    return (
        f"<p>💡 <b>شرح السؤال {entry['number']} · {page + 1} من {total}</b></p>"
        f"<blockquote>{explanation}</blockquote>"
        + review_rich_button(f"qrevback:{token}:{page}", "↩️ العودة للسؤال")
    )


def review_question_fallback_text(entry, page, total):
    if entry.get("is_math"):
        return (
            f"📚 <b>مراجعة الأخطاء · {page + 1} من {total}</b>\n\n"
            f"❓ <b>السؤال {entry['number']}</b> (شاهد الصورة)\n\n"
            f"🔴 <b>إجابتك:</b> {escape(review_choice_label(entry, 'chosen'))}\n\n"
            f"🟢 <b>الإجابة الصحيحة:</b> {escape(review_choice_label(entry, 'correct'))}"
        )
    return review_page_text([entry], 0)


def review_explanation_fallback_text(entry):
    from services.latex_text import latex_to_plain
    explanation = entry.get("explanation") or "لا يوجد شرح محفوظ لهذا السؤال."
    if entry.get("is_math"):
        explanation = latex_to_plain(explanation)
    return f"💡 <b>شرح السؤال {entry['number']}</b>\n\n{escape(explanation)}"
