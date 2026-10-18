"""Build isolated practice sessions from recorded mistakes, without generation or DB writes."""
from copy import deepcopy

from aiogram import BaseMiddleware

from services.quiz_result_ui import build_wrong_answer_review


class PracticeCallbackGuard(BaseMiddleware):
    """Old result/save/feedback buttons must not turn a subset into a saved quiz."""

    async def __call__(self, handler, event, data):
        callback = event.data or ""
        unsafe = (callback in {"quiz_replay", "quiz_share", "quiz_favorite", "save_quiz"}
                  or callback.startswith(("save_", "rate_feedback_")))
        state = data.get("state")
        if unsafe and state and (await state.get_data()).get("is_mistake_practice"):
            await event.answer("هذا الزر يخص الاختبار الأصلي. استخدم أزرار محاولة التدريب الحالية.", show_alert=True)
            return
        return await handler(event, data)


def practice_snapshot(data, session):
    questions = data.get("questions") or []
    wrong = build_wrong_answer_review(questions, data.get("answered_questions"))
    if not wrong:
        return None
    return {
        "session": session,
        "questions": [deepcopy(questions[item["index"]]) for item in wrong],
        "source_title": data.get("source_title") or "اختبار",
        "round": int(data.get("practice_round", 0)) + 1,
        "skipped_before": practice_skipped_count(data) if data.get("is_mistake_practice") else 0,
    }


def practice_skipped_count(data):
    """Skipped questions never become mistakes or imply mastery of the whole chain."""
    answered = data.get("answered_questions") or {}
    skipped = sum(str(i) not in answered and i not in answered
                  for i in range(len(data.get("questions") or [])))
    return int(data.get("practice_skipped_before", 0)) + skipped


def practice_session(snapshot, identity):
    questions = deepcopy(snapshot["questions"])
    # No original quiz_id/attempt_id: subset indices must never update the source
    # quiz's images, scores, analytics rows, or favorite/share records.
    return {
        "questions": questions, "current_index": 0, "total_count": len(questions),
        "score": 0, "answered_questions": {}, "quiz_completed": False,
        "stopped_early": False, "is_switching_question": False,
        "is_mistake_practice": True, "practice_round": snapshot["round"],
        "practice_skipped_before": snapshot["skipped_before"],
        "source_title": snapshot["source_title"], "quiz_origin": "mistake_practice",
        "quiz_id": None, "attempt_id": None, "share_id": None,
        "navigation_session_id": identity,
    }
