"""
Supabase database operations for user management and statistics.
Handles user registration, points management, and database queries.
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
    """
    Check if user exists, add if not, handle daily renewal and referrals.
    
    Args:
        user_id: Telegram user ID
        username: Telegram username
        referrer_id: ID of user who referred this user
        
    Returns:
        Dict with keys: points, status, referrer
            - points: Current user points
            - status: "new", "renewed", or "normal"
            - referrer: Referrer ID if bonus was given
    """
    try:
        is_valid, error = validate_user_id(user_id)
        if not is_valid:
            log_warning(logger, f"Invalid user ID: {user_id}")
            return {"points": 0, "status": "error", "referrer": None}
        
        today = datetime.date.today().isoformat()
        response = supabase.table("users").select("*").eq("user_id", user_id).execute()
        
        if not response.data:
            # New user - initialize with welcome points
            return _add_new_user(user_id, username, referrer_id, today)
        
        # Existing user - check daily renewal
        return _check_daily_renewal(user_id, response.data[0], today)
        
    except Exception as e:
        log_error(logger, f"Error in check_or_add_user: {e}", exception=e)
        return {"points": 0, "status": "error", "referrer": None}

def _add_new_user(user_id: int, username: str, referrer_id: Optional[int], today: str) -> Dict[str, Any]:
    """
    Add a new user to the database.
    
    Args:
        user_id: User ID
        username: Username
        referrer_id: Referrer ID (optional)
        today: Current date in ISO format
        
    Returns:
        Dict with user info and bonus details
    """
    try:
        actual_referrer = None
        
        # Validate referrer
        if referrer_id and str(referrer_id) != str(user_id):
            ref_check = supabase.table("users").select("points").eq("user_id", referrer_id).execute()
            if ref_check.data:
                actual_referrer = referrer_id
                # Give referrer bonus
                new_ref_points = ref_check.data[0]['points'] + REFERRAL_BONUS_POINTS
                supabase.table("users").update({"points": new_ref_points}).eq("user_id", referrer_id).execute()
                log_info(logger, f"Referrer {referrer_id} rewarded {REFERRAL_BONUS_POINTS} points")
        
        # Create new user
        supabase.table("users").insert({
            "user_id": user_id,
            "username": username,
            "points": WELCOME_POINTS,
            "total_questions": 0,
            "referred_by": actual_referrer,
            "last_renewal": today
        }).execute()
        
        log_info(logger, f"New user created: {user_id} with {WELCOME_POINTS} welcome points")
        return {"points": WELCOME_POINTS, "status": "new", "referrer": actual_referrer}
        
    except Exception as e:
        log_error(logger, f"Error adding new user: {e}", exception=e)
        return {"points": 0, "status": "error", "referrer": None}

def _check_daily_renewal(user_id: int, user_data: Dict, today: str) -> Dict[str, Any]:
    """
    Check if user needs daily renewal and update if needed.
    
    Args:
        user_id: User ID
        user_data: User data from database
        today: Current date in ISO format
        
    Returns:
        Dict with user info and renewal status
    """
    try:
        current_points = user_data['points']
        last_renewal = user_data.get('last_renewal')
        
        if last_renewal != today:
            # Daily renewal - add free points
            current_points += DAILY_RENEWAL_POINTS
            supabase.table("users").update({
                "points": current_points,
                "last_renewal": today
            }).eq("user_id", user_id).execute()
            
            log_info(logger, f"Daily renewal for user {user_id}: +{DAILY_RENEWAL_POINTS} points")
            return {"points": current_points, "status": "renewed", "referrer": None}
        
        return {"points": current_points, "status": "normal", "referrer": None}
        
    except Exception as e:
        log_error(logger, f"Error checking daily renewal: {e}", exception=e)
        return {"points": user_data.get('points', 0), "status": "error", "referrer": None}

def update_user_stats(user_id: int, questions_generated: int) -> Optional[int]:
    """
    Deduct points after quiz generation and update question count.
    
    Args:
        user_id: User ID
        questions_generated: Number of questions generated
        
    Returns:
        New points balance or None on error
    """
    try:
        is_valid, error = validate_user_id(user_id)
        if not is_valid:
            log_warning(logger, f"Invalid user ID for stats update: {user_id}")
            return None
        
        user = supabase.table("users").select("points, total_questions").eq("user_id", user_id).execute()
        if user.data:
            current_points = user.data[0]['points']
            current_total = user.data[0]['total_questions']
            
            points_to_deduct = questions_generated * POINTS_PER_QUESTION
            new_points = max(0, current_points - points_to_deduct)
            new_total = current_total + questions_generated
            
            supabase.table("users").update({
                "points": new_points,
                "total_questions": new_total
            }).eq("user_id", user_id).execute()
            
            log_info(logger, f"User {user_id} points updated: -{points_to_deduct}, total questions: {new_total}")
            return new_points
        
        return None
        
    except Exception as e:
        log_error(logger, f"Error updating user stats: {e}", exception=e)
        return None

# ==================== Admin Operations ====================
def admin_add_points(target_id: int, amount: int) -> Optional[int]:
    """
    Admin function to charge points to a user.
    
    Args:
        target_id: User ID to charge
        amount: Points to add
        
    Returns:
        New points balance or None on error
    """
    try:
        is_valid, error = validate_user_id(target_id)
        if not is_valid:
            log_warning(logger, f"Invalid target user ID: {target_id}")
            return None
        
        is_valid, error = validate_points_amount(amount)
        if not is_valid:
            log_warning(logger, f"Invalid points amount: {amount} - {error}")
            return None
        
        user = supabase.table("users").select("points").eq("user_id", target_id).execute()
        if user.data:
            new_points = user.data[0]['points'] + amount
            supabase.table("users").update({"points": new_points}).eq("user_id", target_id).execute()
            
            log_info(logger, f"Admin charged {amount} points to user {target_id}, new balance: {new_points}")
            return new_points
        
        log_warning(logger, f"User not found: {target_id}")
        return None
        
    except Exception as e:
        log_error(logger, f"Error in admin_add_points: {e}", exception=e)
        return None

def admin_get_global_stats() -> Dict[str, int]:
    """
    Get global database statistics.
    
    Returns:
        Dict with total_users and total_questions
    """
    try:
        response = supabase.table("users").select("user_id, total_questions").execute()
        
        if response.data:
            total_users = len(response.data)
            total_questions = sum(user['total_questions'] for user in response.data)
            
            log_info(logger, f"Global stats: {total_users} users, {total_questions} total questions")
            return {"total_users": total_users, "total_questions": total_questions}
        
        return {"total_users": 0, "total_questions": 0}
        
    except Exception as e:
        log_error(logger, f"Error getting global stats: {e}", exception=e)
        return {"total_users": 0, "total_questions": 0}

def admin_search_user(query: str) -> Optional[Dict[str, Any]]:
    """
    Search for a user by ID or username.
    
    Args:
        query: User ID (numeric string) or username
        
    Returns:
        User data dict or None if not found
    """
    try:
        if str(query).isdigit():
            res = supabase.table("users").select("*").eq("user_id", int(query)).execute()
        else:
            res = supabase.table("users").select("*").eq("username", query).execute()
        
        if res.data:
            log_info(logger, f"User found: {query}")
            return res.data[0]
        
        log_warning(logger, f"User not found: {query}")
        return None
        
    except Exception as e:
        log_error(logger, f"Error searching user: {e}", exception=e)
        return None
