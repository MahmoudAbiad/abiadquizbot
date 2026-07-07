"""
Supabase database operations for user management and statistics.
Handles user registration, points management, database queries, and quiz caching.
"""

import os
import datetime
from typing import Optional, Dict, List, Any
from dotenv import load_dotenv, find_dotenv
from supabase import create_client
from logger import get_logger, log_error, log_warning, log_info
from constants import (
    WELCOME_POINTS, DAILY_RENEWAL_POINTS, REFERRAL_BONUS_POINTS,
    POINTS_PER_QUESTION
)
from validators import validate_user_id, validate_points_amount

dotenv_path = find_dotenv()
load_dotenv(dotenv_path)

logger = get_logger(__name__)

# ==================== Supabase Configuration ====================
try:
    supabase = create_client(os.getenv("SUPABASE_URL"), os.getenv("SUPABASE_KEY"))
    log_info(logger, "Supabase client initialized successfully")
except Exception as e:
    log_error(logger, f"Failed to initialize Supabase: {e}", exception=e)
    raise

# ==================== User Management ====================
def check_or_add_user(user_id: int, username: str, referrer_id: Optional[int] = None) -> Dict[str, Any]:
    try:
        is_valid, error = validate_user_id(user_id)
        if not is_valid:
            log_warning(logger, f"Invalid user ID: {user_id}")
            return {"points": 0, "status": "error", "referrer": None}
        
        today = datetime.date.today().isoformat()
        response = supabase.table("users").select("*").eq("user_id", user_id).execute()
        
        if not response.data:
            return _add_new_user(user_id, username, referrer_id, today)
        
        return _check_daily_renewal(user_id, response.data[0], today)
        
    except Exception as e:
        log_error(logger, f"Error in check_or_add_user: {e}", exception=e)
        return {"points": 0, "status": "error", "referrer": None}

def _add_new_user(user_id: int, username: str, referrer_id: Optional[int], today: str) -> Dict[str, Any]:
    try:
        actual_referrer = None
        if referrer_id and str(referrer_id) != str(user_id):
            ref_check = supabase.table("users").select("points").eq("user_id", referrer_id).execute()
            if ref_check.data:
                actual_referrer = referrer_id
                new_ref_points = ref_check.data[0]['points'] + REFERRAL_BONUS_POINTS
                supabase.table("users").update({"points": new_ref_points}).eq("user_id", referrer_id).execute()
                
        supabase.table("users").insert({
            "user_id": user_id,
            "username": username,
            "points": WELCOME_POINTS,
            "total_questions": 0,
            "referred_by": actual_referrer,
            "last_renewal": today
        }).execute()
        
        return {"points": WELCOME_POINTS, "status": "new", "referrer": actual_referrer}
    except Exception as e:
        log_error(logger, f"Error adding new user: {e}", exception=e)
        return {"points": 0, "status": "error", "referrer": None}

def _check_daily_renewal(user_id: int, user_data: Dict, today: str) -> Dict[str, Any]:
    try:
        current_points = user_data['points']
        last_renewal = user_data.get('last_renewal')
        
        if last_renewal != today:
            current_points += DAILY_RENEWAL_POINTS
            supabase.table("users").update({
                "points": current_points,
                "last_renewal": today
            }).eq("user_id", user_id).execute()
            return {"points": current_points, "status": "renewed", "referrer": None}
        
        return {"points": current_points, "status": "normal", "referrer": None}
    except Exception as e:
        log_error(logger, f"Error checking daily renewal: {e}", exception=e)
        return {"points": user_data.get('points', 0), "status": "error", "referrer": None}

def update_user_stats(user_id: int, points_to_deduct: int, questions_generated: int) -> Optional[int]:
    try:
        is_valid, error = validate_user_id(user_id)
        if not is_valid:
            return None
        
        user = supabase.table("users").select("points, total_questions").eq("user_id", user_id).execute()
        if user.data:
            current_points = user.data[0]['points']
            current_total = user.data[0]['total_questions']
            
            new_points = max(0, current_points - points_to_deduct)
            new_total = current_total + questions_generated
            
            supabase.table("users").update({
                "points": new_points,
                "total_questions": new_total
            }).eq("user_id", user_id).execute()
            
            return new_points
        return None
    except Exception as e:
        log_error(logger, f"Error updating user stats: {e}", exception=e)
        return None

# ==================== Cache Operations ====================
def get_cached_quiz(file_hash: str) -> Optional[List[Dict[str, Any]]]:
    """البحث عن الكويز في قاعدة البيانات باستخدام بصمة الملف"""
    try:
        res = supabase.table("files_cache").select("questions_data").eq("file_hash", file_hash).execute()
        if res.data:
            log_info(logger, f"Cache HIT for hash: {file_hash}")
            return res.data[0]['questions_data']
        return None
    except Exception as e:
        log_error(logger, f"Error reading from cache: {e}", exception=e)
        return None

def save_quiz_to_cache(file_hash: str, quiz_data: List[Dict[str, Any]]) -> bool:
    """حفظ الكويز المولد حديثاً في قاعدة البيانات"""
    try:
        supabase.table("files_cache").insert({
            "file_hash": file_hash,
            "questions_data": quiz_data
        }).execute()
        log_info(logger, f"Saved new quiz to cache with hash: {file_hash}")
        return True
    except Exception as e:
        log_error(logger, f"Error saving to cache: {e}", exception=e)
        return False

# ==================== Admin Operations ====================
def admin_add_points(target_id: int, amount: int) -> Optional[int]:
    try:
        user = supabase.table("users").select("points").eq("user_id", target_id).execute()
        if user.data:
            new_points = user.data[0]['points'] + amount
            supabase.table("users").update({"points": new_points}).eq("user_id", target_id).execute()
            return new_points
        return None
    except Exception as e:
        log_error(logger, f"Error in admin_add_points: {e}", exception=e)
        return None

def admin_get_global_stats() -> Dict[str, int]:
    try:
        response = supabase.table("users").select("user_id, total_questions").execute()
        if response.data:
            total_users = len(response.data)
            total_questions = sum(user['total_questions'] for user in response.data)
            return {"total_users": total_users, "total_questions": total_questions}
        return {"total_users": 0, "total_questions": 0}
    except Exception as e:
        log_error(logger, f"Error getting global stats: {e}", exception=e)
        return {"total_users": 0, "total_questions": 0}

def admin_search_user(query: str) -> Optional[List[Dict[str, Any]]]:
    """البحث المتقدم: يدعم الآيدي الرقمي، والمعرف الكامل، والبحث الجزئي بالمعرف"""
    try:
        query = query.strip()
        
        # إذا كان البحث برقم الآيدي
        if query.isdigit():
            res = supabase.table("users").select("*").eq("user_id", int(query)).execute()
            return res.data
            
        # إذا كان البحث باليوزرنيم (بحث جزئي)
        clean_username = query.lstrip('@')
        res = supabase.table("users").select("*").ilike("username", f"%{clean_username}%").execute()
        return res.data
        
    except Exception as e:
        log_error(logger, f"Error searching user: {e}", exception=e)
        return None