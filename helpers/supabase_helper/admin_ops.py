"""
supabase_helper.admin_ops
============================
Admin Operations: إضافة نقاط يدوياً، منح هدايا مباشرة، إحصائيات عامة، والبحث عن مستخدم.
"""

from datetime import datetime, timedelta, timezone
from typing import Optional, Dict
import uuid

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


async def admin_grant_direct_gift(
    target_id: int,
    amount: float,
    valid_days: int = 30,
    title: str = "هدية مستخدم جديد",
    source_key: Optional[str] = None,
) -> Optional[Dict]:
    """منح هدية نقاط مباشرة لمستخدم محدد عبر RPC ذري مع مسار احتياطي مباشر."""
    try:
        clean_amount = round(float(amount), 2)
        clean_days = int(valid_days)
        clean_title = (title or "هدية من الإدارة").strip()[:100]
        if clean_amount <= 0 or clean_amount > 1000000 or clean_days < 0 or clean_days > 3650 or not clean_title:
            return None
        key = (source_key or f"direct:{uuid.uuid4()}").strip()[:120]
        try:
            rpc_response = await supabase.rpc("admin_grant_user_gift", {
                "p_user_id": int(target_id),
                "p_source_key": key,
                "p_title": clean_title,
                "p_points": clean_amount,
                "p_valid_days": clean_days,
            }).execute()
            data = rpc_response.data
            if isinstance(data, list) and data:
                data = data[0]
            if isinstance(data, dict) and data.get("status") == "ok":
                return {
                    "status": "ok",
                    "gift_id": data.get("gift_id"),
                    "title": data.get("title") or clean_title,
                    "points": float(data.get("points", clean_amount)),
                    "expires_at": data.get("expires_at"),
                    "gift_points": float(data.get("gift_points", 0)),
                    "total_points": float(data.get("total_points", 0)),
                }
            if isinstance(data, dict) and data.get("status") == "missing_user":
                return None
        except Exception as rpc_exc:
            if str(getattr(rpc_exc, "code", "")) not in {"42883", "PGRST202"}:
                raise
            now_utc = datetime.now(timezone.utc)
            expires_at = None if clean_days == 0 else (now_utc + timedelta(days=clean_days)).isoformat()
            ins = await supabase.table("point_gifts").insert({
                "user_id": int(target_id),
                "source": "admin",
                "source_key": key,
                "title": clean_title,
                "original_amount": clean_amount,
                "remaining_amount": clean_amount,
                "expires_at": expires_at,
            }).execute()
            gift_row = (ins.data or [{}])[0]
            user_res = await supabase.table("users").select("free_points, paid_points").eq("user_id", int(target_id)).limit(1).execute()
            if not user_res.data:
                return None
            u_row = user_res.data[0]
            gifts_map = await gift_balances([int(target_id)])
            gift_pts = float(gifts_map.get(int(target_id), 0.0))
            total_pts = float(u_row.get("free_points") or 0) + float(u_row.get("paid_points") or 0) + gift_pts
            return {
                "status": "ok",
                "gift_id": gift_row.get("id"),
                "title": clean_title,
                "points": clean_amount,
                "expires_at": gift_row.get("expires_at", expires_at),
                "gift_points": gift_pts,
                "total_points": total_pts,
            }
        return None
    except Exception as e:
        logger.error(f"Error in admin_grant_direct_gift: {e}")
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
