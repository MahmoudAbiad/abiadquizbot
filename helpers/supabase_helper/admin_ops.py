"""
supabase_helper.admin_ops
============================
Admin Operations: إضافة نقاط يدوياً، إحصائيات عامة، والبحث عن مستخدم.
"""

from typing import Optional, Dict

from ._client import supabase, logger
from .gifts import enrich_balances, gift_balances

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
            gifts = await gift_balances([target_id])
            return float(row["total_points"]) + gifts.get(target_id, 0)
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
        return await enrich_balances(res.data or [])
    except Exception as e:
        return None


def _admin_users_query(query: str = ""):
    request = supabase.table("users").select("*", count="exact")
    query = query.strip().lstrip("@")
    if query:
        if query.isdigit():
            request = request.eq("user_id", int(query))
        else:
            # Telegram underscores are literal username characters, not SQL wildcards.
            escaped = query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            request = request.ilike("username", f"%{escaped}%")
    return request.order("joined_at", desc=True).order("user_id", desc=True)


async def admin_get_users_page(page: int = 1, query: str = "", page_size: int = 5):
    """Fetch one database page with an exact count; never preload the full registry."""
    page = max(1, int(page))
    page_size = max(1, min(int(page_size), 100))
    start = (page - 1) * page_size
    response = await _admin_users_query(query).range(start, start + page_size - 1).execute()
    total = response.count or 0
    pages = max(1, (total + page_size - 1) // page_size)
    if page > pages:
        return await admin_get_users_page(pages, query, page_size)
    return await enrich_balances(response.data or []), total, page, pages

