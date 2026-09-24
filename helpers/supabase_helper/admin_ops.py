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
        # 🆕 يُنفَّذ عبر RPC ذري (UPDATE ... SET x_points = x_points + amount) بدل
        # قراءة الرصيد ثم كتابته من بايثون - نفس نمط award_referral_bonus_atomic /
        # refund_user_points_atomic. القراءة-ثم-الكتابة كانت عرضة لفقدان تحديثات
        # (lost update) لو أضاف الأدمن نقاطاً بنفس اللحظة اللي فيها البوت بيخصم
        # نقاط من نفس المستخدم (توليد كويز) أو بيسترجعها له.
        rpc_response = await supabase.rpc("admin_add_points_atomic", {
            "target_user_id": target_id,
            "amount": float(amount),
            "balance_type": balance_type,
        }).execute()
        if rpc_response.data:
            row = rpc_response.data[0] if isinstance(rpc_response.data, list) else rpc_response.data
            return int(row["total_points"])
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

