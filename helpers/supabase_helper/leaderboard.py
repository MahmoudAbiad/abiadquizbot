"""
supabase_helper.leaderboard
==============================
Quiz Scores & Leaderboard Operations: أعلى نتيجة، النشر/الإخفاء من لوحة
الشرف، وأفضل 5 نتائج.
"""

import datetime
from typing import Optional, Dict, List, Any

from ._client import supabase, logger, log_error, log_warning, _is_valid_uuid

# ==================== Quiz Scores & Leaderboard Operations ====================
async def get_or_update_high_score(user_id: int, quiz_id: str, current_score: int, total_questions: int) -> Dict[str, Any]:
    if not _is_valid_uuid(quiz_id):
        log_warning(logger, f"Skipping high score update: invalid quiz_id '{quiz_id}' (not a real UUID)")
        return {"previous_score": None, "highest_score": current_score, "is_public": False}
    try:
        res = await supabase.table("quiz_scores").select("*").eq("quiz_id", quiz_id).eq("user_id", user_id).execute()
        
        previous_score = None
        new_highest = current_score
        is_public = False
        
        if res.data:
            existing = res.data[0]
            previous_score = existing["highest_score"]
            is_public = existing["is_public"]
            
            if current_score > previous_score:
                await supabase.table("quiz_scores").update({
                    "highest_score": current_score,
                    "total_questions": total_questions,
                    "updated_at": datetime.datetime.utcnow().isoformat()
                }).eq("id", existing["id"]).execute()
            else:
                new_highest = previous_score
        else:
            # 🆕 أول محاولة على هالكويز: النتيجة تنحفظ خاصة افتراضياً، وشاشة النتيجة
            # (handlers/quiz_runner.py) بتسأل الطالب صراحة نعم/لا قبل ما تصير عامة.
            is_public = False
            await supabase.table("quiz_scores").insert({
                "quiz_id": quiz_id,
                "user_id": user_id,
                "highest_score": current_score,
                "total_questions": total_questions,
                "is_public": False
            }).execute()
            
        return {
            "previous_score": previous_score,
            "highest_score": new_highest,
            "is_public": is_public
        }
    except Exception as e:
        log_error(logger, f"Error updating high score: {e}", exception=e)
        return {"previous_score": None, "highest_score": current_score, "is_public": False}

async def publish_score_to_leaderboard(user_id: int, quiz_id: str) -> bool:
    if not _is_valid_uuid(quiz_id):
        log_warning(logger, f"Skipping leaderboard publish: invalid quiz_id '{quiz_id}' (not a real UUID)")
        return False
    try:
        await supabase.table("quiz_scores").update({"is_public": True}).eq("quiz_id", quiz_id).eq("user_id", user_id).execute()
        return True
    except Exception as e:
        log_error(logger, f"Error publishing score: {e}", exception=e)
        return False

async def hide_score_from_leaderboard(user_id: int, quiz_id: str) -> bool:
    """🆕 إخفاء نتيجة الطالب من لوحة الشرف (النتائج تُنشر فقط بعد موافقة صريحة،
    فهاي الدالة تسمح للطالب بالتراجع لاحقاً من زر الإخفاء تحت لوحة الشرف)."""
    if not _is_valid_uuid(quiz_id):
        log_warning(logger, f"Skipping leaderboard hide: invalid quiz_id '{quiz_id}' (not a real UUID)")
        return False
    try:
        await supabase.table("quiz_scores").update({"is_public": False}).eq("quiz_id", quiz_id).eq("user_id", user_id).execute()
        return True
    except Exception as e:
        log_error(logger, f"Error hiding score: {e}", exception=e)
        return False

async def get_my_leaderboard_status(user_id: int, quiz_id: str) -> Optional[bool]:
    """🆕 حالة نشر نتيجة الطالب الحالية لهالكويز - تُستخدم لعرض زر
    الإخفاء/الإظهار الصحيح تحت لوحة الشرف. ترجع None إذا الطالب ما أخد
    هالكويز أصلاً (ما في صف بجدول quiz_scores)."""
    if not _is_valid_uuid(quiz_id):
        return None
    try:
        res = await supabase.table("quiz_scores").select("is_public").eq("quiz_id", quiz_id).eq("user_id", user_id).execute()
        if res.data:
            return bool(res.data[0]["is_public"])
        return None
    except Exception as e:
        log_error(logger, f"Error getting leaderboard status: {e}", exception=e)
        return None

async def get_top_5_leaderboard(quiz_id: str) -> List[Dict[str, Any]]:
    if not _is_valid_uuid(quiz_id):
        log_warning(logger, f"Skipping leaderboard fetch: invalid quiz_id '{quiz_id}' (not a real UUID)")
        return []
    try:
        res = await supabase.table("quiz_scores") \
            .select("highest_score, total_questions, users(first_name, last_name)") \
            .eq("quiz_id", quiz_id) \
            .eq("is_public", True) \
            .order("highest_score", desc=True) \
            .limit(5) \
            .execute()
            
        return res.data or []
    except Exception as e:
        log_error(logger, f"Error getting leaderboard: {e}", exception=e)
        return []

