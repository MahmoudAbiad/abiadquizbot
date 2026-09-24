"""
supabase_helper.admin_ops
============================
Admin Operations: إضافة نقاط يدوياً، إحصائيات عامة، والبحث عن مستخدم.
"""

from typing import Optional, Dict

from ._client import supabase, logger

# ==================== Admin Operations ====================
async def admin_add_points(target_id: int, amount: int, balance_type: str = "paid") -> Optional[int]:
    try:
        if balance_type not in ("free", "paid"):
            return None
        user = await supabase.table("users").select("free_points, paid_points").eq("user_id", target_id).execute()
        if user.data:
            paid_points = float(user.data[0].get('paid_points') or 0)
            free_points = float(user.data[0].get('free_points') or 0)
            if balance_type == "free":
                free_points += amount
            else:
                paid_points += amount
            await supabase.table("users").update({
                "free_points": free_points,
                "paid_points": paid_points,
            }).eq("user_id", target_id).execute()
            return int(free_points + paid_points)
        return None
    except Exception as e:
        logger.error(f"Error in admin_add_points: {e}")
        return None

async def admin_get_global_stats() -> Dict[str, int]:
    try:
        response = await supabase.table("users").select("user_id, total_questions").execute()
        if response.data:
            return {"total_users": len(response.data), "total_questions": sum(u['total_questions'] for u in response.data)}
        return {"total_users": 0, "total_questions": 0}
    except Exception as e:
        logger.error(f"Error getting global stats: {e}")
        return {"total_users": 0, "total_questions": 0}

async def admin_search_user(query: str) -> Optional[list]:
    try:
        query = query.strip()
        if query.isdigit():
            res = await supabase.table("users").select("*").eq("user_id", int(query)).execute()
        else:
            clean_username = query.lstrip('@')
            res = await supabase.table("users").select("*").ilike("username", f"%{clean_username}%").execute()
        return res.data
    except Exception as e:
        return None

