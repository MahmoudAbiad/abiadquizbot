import os
import datetime
import asyncio
from dotenv import load_dotenv, find_dotenv
from supabase import create_client

dotenv_path = find_dotenv()
load_dotenv(dotenv_path)

supabase = create_client(os.getenv("SUPABASE_URL"), os.getenv("SUPABASE_KEY"))

def _parse_datetime_value(value):
    if not value:
        return None

    if isinstance(value, datetime.datetime):
        return value

    if isinstance(value, datetime.date):
        return datetime.datetime.combine(value, datetime.time.min)

    if isinstance(value, str):
        normalized = value.strip().replace("Z", "+00:00")
        try:
            return datetime.datetime.fromisoformat(normalized)
        except ValueError:
            for fmt in ("%Y-%m-%d", "%Y-%m-%d %H:%M:%S"):
                try:
                    return datetime.datetime.strptime(value.strip(), fmt)
                except ValueError:
                    continue
    return None

def _extract_gift_info(user_data):
    gift_start_raw = (
        user_data.get("gift_start_date")
        or user_data.get("gift_start")
        or user_data.get("gift_from")
    )
    gift_end_raw = (
        user_data.get("gift_end_date")
        or user_data.get("gift_end")
        or user_data.get("gift_until")
        or user_data.get("gift_expires_at")
    )

    gift_start = _parse_datetime_value(gift_start_raw)
    gift_end = _parse_datetime_value(gift_end_raw)

    gift_points = user_data.get("gift_points")
    if gift_points is None:
        gift_points = user_data.get("bonus_points")

    return {
        "start": gift_start,
        "end": gift_end,
        "points": gift_points,
        "has_gift": bool(gift_start or gift_end or gift_points),
    }

def _check_or_add_user_sync(user_id, username, referrer_id=None):
    """المنطق المتزامن الأساسي للتحقق من المستخدم"""
    today = datetime.date.today().isoformat()
    response = supabase.table("users").select("*").eq("user_id", user_id).execute()
    
    if not response.data:
        actual_referrer = None
        if referrer_id and str(referrer_id) != str(user_id):
            ref_check = supabase.table("users").select("points").eq("user_id", referrer_id).execute()
            if ref_check.data:
                actual_referrer = referrer_id
                new_ref_points = ref_check.data[0]['points'] + 10
                supabase.table("users").update({"points": new_ref_points}).eq("user_id", referrer_id).execute()
        
        supabase.table("users").insert({
            "user_id": user_id, 
            "username": username, 
            "points": 20, 
            "total_questions": 0,
            "referred_by": actual_referrer,
            "last_renewal": today
        }).execute()
        
        return {
            "points": 20,
            "status": "new",
            "referrer": actual_referrer,
            "gift_info": {"start": None, "end": None, "points": None, "has_gift": False}
        }
    
    user_data = response.data[0]
    current_points = user_data['points']
    last_renewal = user_data.get('last_renewal')
    gift_info = _extract_gift_info(user_data)
    
    if last_renewal != today:
        current_points += 15 
        supabase.table("users").update({
            "points": current_points,
            "last_renewal": today
        }).eq("user_id", user_id).execute()
        return {"points": current_points, "status": "renewed", "referrer": None, "gift_info": gift_info}
        
    return {"points": current_points, "status": "normal", "referrer": None, "gift_info": gift_info}

async def check_or_add_user(user_id, username, referrer_id=None):
    return await asyncio.to_thread(_check_or_add_user_sync, user_id, username, referrer_id)

def _update_user_stats_sync(user_id, questions_generated):
    user = supabase.table("users").select("points, total_questions").eq("user_id", user_id).execute()
    if user.data:
        current_points = user.data[0]['points']
        current_total = user.data[0]['total_questions']
        new_points = max(0, current_points - questions_generated)
        new_total = current_total + questions_generated
        supabase.table("users").update({"points": new_points, "total_questions": new_total}).eq("user_id", user_id).execute()
        return new_points
    return None

async def update_user_stats(user_id, questions_generated):
    return await asyncio.to_thread(_update_user_stats_sync, user_id, questions_generated)

def _admin_add_points_sync(target_id, amount):
    user = supabase.table("users").select("points").eq("user_id", target_id).execute()
    if user.data:
        new_points = user.data[0]['points'] + amount
        supabase.table("users").update({"points": new_points}).eq("user_id", target_id).execute()
        return new_points
    return None

async def admin_add_points(target_id, amount):
    return await asyncio.to_thread(_admin_add_points_sync, target_id, amount)

def _admin_get_global_stats_sync():
    response = supabase.table("users").select("user_id, total_questions").execute()
    if response.data:
        total_users = len(response.data)
        total_questions = sum(user['total_questions'] for user in response.data)
        return {"total_users": total_users, "total_questions": total_questions}
    return {"total_users": 0, "total_questions": 0}

async def admin_get_global_stats():
    return await asyncio.to_thread(_admin_get_global_stats_sync)

def _admin_search_user_sync(query):
    if str(query).isdigit():
        res = supabase.table("users").select("*").eq("user_id", int(query)).execute()
    else:
        res = supabase.table("users").select("*").eq("username", query).execute()
    return res.data[0] if res.data else None

async def admin_search_user(query):
    return await asyncio.to_thread(_admin_search_user_sync, query)
