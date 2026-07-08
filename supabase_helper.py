"""
Supabase database operations for user management and statistics.
Handles user registration, points management, database queries, and quiz token caching.
"""

import os
import datetime
import uuid
from typing import Optional, Dict, List, Any
from dotenv import load_dotenv, find_dotenv
from supabase import create_client
from logger import get_logger, log_error, log_warning, log_info
# الاستيراد الآمن والمتطابق
from constants import (
    WELCOME_POINTS, DAILY_RENEWAL_POINTS, REFERRAL_BONUS_POINTS,
    POINTS_PER_QUESTION,
    DEFAULT_FAVORITE_SECTION_TITLE,
    MAX_FAVORITE_SECTIONS,
)
from validators import validate_user_id, validate_points_amount

dotenv_path = find_dotenv()
load_dotenv(dotenv_path)

logger = get_logger(__name__)

try:
    supabase = create_client(os.getenv("SUPABASE_URL"), os.getenv("SUPABASE_KEY"))
    log_info(logger, "Supabase client initialized successfully")
except Exception as e:
    log_error(logger, f"Failed to initialize Supabase: {e}", exception=e)
    raise

# ==================== User Management ====================
def check_or_add_user(user_id: int,username: str, first_name: str, last_name: str,  referrer_id: Optional[int] = None) -> Dict[str, Any]:
    try:
        is_valid, error = validate_user_id(user_id)
        if not is_valid:
            return {"points": 0, "status": "error", "referrer": None}
        
        today = datetime.date.today().isoformat()
        response = supabase.table("users").select("*").eq("user_id", user_id).execute()
        
        if not response.data:
            return _add_new_user(user_id,username, first_name, last_name,  referrer_id, today)
        
        return _check_daily_renewal(user_id, response.data[0], today)
    except Exception as e:
        log_error(logger, f"Error in check_or_add_user: {e}", exception=e)
        return {"points": 0, "status": "error", "referrer": None}

def _add_new_user(user_id: int, username: str, first_name: str, last_name: str, referrer_id: Optional[int], today: str) -> Dict[str, Any]:
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
            "first_name": first_name,
            "last_name": last_name or "Unknown",
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

def update_user_stats(
    user_id: int,
    points_to_deduct: int,
    questions_generated: Optional[int] = None,
) -> Optional[int]:
    try:
        is_valid, error = validate_user_id(user_id)
        if not is_valid: return None

        if questions_generated is None:
            questions_generated = points_to_deduct
        
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

# ==================== Shared Quiz Operations ====================
def create_shared_quiz_id() -> str:
    """Create a short share identifier safe for Telegram callback/deep links."""
    return uuid.uuid4().hex[:12]

def save_shared_quiz(share_id: str, owner_id: int, title: str, quiz_data: List[Dict[str, Any]]) -> bool:
    try:
        supabase.table("shared_quizzes").upsert({
            "share_id": share_id,
            "owner_id": owner_id,
            "title": title,
            "quiz_data": quiz_data,
            "created_at": datetime.datetime.utcnow().isoformat()
        }).execute()
        log_info(logger, f"Saved shared quiz: {share_id}")
        return True
    except Exception as e:
        log_error(logger, f"Error saving shared quiz: {e}", exception=e)
        return False

def get_shared_quiz(share_id: str) -> Optional[Dict[str, Any]]:
    try:
        res = supabase.table("shared_quizzes").select("*").eq("share_id", share_id).execute()
        if res.data:
            return res.data[0]
        return None
    except Exception as e:
        log_error(logger, f"Error loading shared quiz: {e}", exception=e)
        return None

# ==================== Favorite Quiz Operations ====================
def count_favorite_sections(user_id: int) -> int:
    try:
        res = supabase.table("favorite_quiz_sections").select("section_id", count="exact").eq("user_id", user_id).execute()
        return int(res.count or 0)
    except Exception as e:
        log_error(logger, f"Error counting favorite sections: {e}", exception=e)
        return 0


def list_favorite_sections(user_id: int) -> List[Dict[str, Any]]:
    try:
        res = supabase.table("favorite_quiz_sections").select("section_id, title, created_at").eq("user_id", user_id).order("created_at", desc=False).execute()
        return res.data or []
    except Exception as e:
        log_error(logger, f"Error listing favorite sections: {e}", exception=e)
        return []


def create_favorite_section(user_id: int, title: str) -> Optional[str]:
    try:
        section_id = uuid.uuid4().hex[:12]
        supabase.table("favorite_quiz_sections").insert({
            "section_id": section_id,
            "user_id": user_id,
            "title": title,
            "created_at": datetime.datetime.utcnow().isoformat(),
        }).execute()
        log_info(logger, f"Created favorite section: {section_id} for user {user_id}")
        return section_id
    except Exception as e:
        log_error(logger, f"Error creating favorite section: {e}", exception=e)
        return None


def save_favorite_quiz(
    user_id: int,
    title: str,
    quiz_data: List[Dict[str, Any]],
    section_id: Optional[str] = None,
    source_title: Optional[str] = None,
) -> Optional[str]:
    try:
        favorite_id = uuid.uuid4().hex[:12]
        payload = {
            "favorite_id": favorite_id,  # Fixed: Added identifier string directly into payload
            "user_id": user_id,
            "title": title,
            "source_title": source_title or title,
            "quiz_data": quiz_data,
            "created_at": datetime.datetime.utcnow().isoformat()
        }
        if section_id is not None:
            payload["section_id"] = section_id

        try:
            supabase.table("favorite_quizzes").insert(payload).execute()
        except Exception as insert_error:
            # Safer parsing for APIError objects or standard string errors
            error_text = getattr(insert_error, 'message', str(insert_error))
            if section_id is not None and "section_id" in error_text and "favorite_quizzes" in error_text:
                payload.pop("section_id", None)
                supabase.table("favorite_quizzes").insert(payload).execute()
                log_warning(logger, "Saved favorite quiz without section_id because the deployed schema does not expose that column yet.")
            else:
                raise

        log_info(logger, f"Saved favorite quiz: {favorite_id} for user {user_id}")
        return favorite_id
    except Exception as e:
        log_error(logger, f"Error saving favorite quiz: {e}", exception=e)
        return None

def list_favorite_quizzes(
    user_id: int,
    search_query: Optional[str] = None,
    sort_by: str = "latest",
) -> List[Dict[str, Any]]:
    try:
        sections_res = supabase.table("favorite_quiz_sections").select("section_id, title").eq("user_id", user_id).execute()
        section_map = {
            item["section_id"]: item["title"]
            for item in (sections_res.data or [])
            if item.get("section_id")
        }

        try:
            res = supabase.table("favorite_quizzes").select("favorite_id, title, source_title, section_id, created_at").eq("user_id", user_id).execute()
        except Exception as select_error:
            error_text = getattr(select_error, 'message', str(select_error))
            if "section_id" in error_text and "favorite_quizzes" in error_text:
                res = supabase.table("favorite_quizzes").select("favorite_id, title, source_title, created_at").eq("user_id", user_id).execute()
            else:
                raise

        items = []
        for row in (res.data or []):
            item = dict(row)
            # Safe tracking fallback options
            item["favorite_id"] = item.get("favorite_id") or item.get("created_at")
            section_title = section_map.get(item.get("section_id")) or DEFAULT_FAVORITE_SECTION_TITLE
            item["section_title"] = section_title
            items.append(item)

        if search_query:
            query = search_query.strip().lower()
            items = [
                item for item in items
                if query in (item.get("title") or "").lower()
                or query in (item.get("source_title") or "").lower()
                or query in (item.get("section_title") or "").lower()
            ]

        if sort_by == "section":
            items.sort(key=lambda item: item.get("created_at") or "", reverse=True)
            items.sort(key=lambda item: (item.get("section_title") or "").lower())
        else:
            items.sort(key=lambda item: item.get("created_at") or "", reverse=True)

        return items
    except Exception as e:
        log_error(logger, f"Error listing favorite quizzes: {e}", exception=e)
        return []


def can_create_more_favorite_sections(user_id: int) -> bool:
    return count_favorite_sections(user_id) < MAX_FAVORITE_SECTIONS

def get_favorite_quiz(user_id: int, favorite_id: str) -> Optional[Dict[str, Any]]:
    try:
        # Fixed: Query via favorite_id column instead of created_at timestamp
        res = supabase.table("favorite_quizzes").select("*").eq("user_id", user_id).eq("favorite_id", favorite_id).execute()
        if res.data:
            return res.data[0]
        return None
    except Exception as e:
        log_error(logger, f"Error loading favorite quiz: {e}", exception=e)
        return None

def remove_favorite_quiz(user_id: int, favorite_id: str) -> bool:
    try:
        # Fixed: Query via favorite_id column instead of created_at timestamp
        supabase.table("favorite_quizzes").delete().eq("user_id", user_id).eq("favorite_id", favorite_id).execute()
        log_info(logger, f"Removed favorite quiz: {favorite_id} for user {user_id}")
        return True
    except Exception as e:
        log_error(logger, f"Error removing favorite quiz: {e}", exception=e)
        return False

# ==================== Token Cache Operations ====================
def get_cached_quiz(file_hash: str) -> Optional[Dict[str, Any]]:
    """البحث عن المستند في الكاش وجلب بيانات الأسئلة والتوكينات الأصلية معاً"""
    try:
        res = supabase.table("files_cache").select("questions_data, total_tokens").eq("file_hash", file_hash).execute()
        if res.data:
            log_info(logger, f"Cache HIT for hash: {file_hash}")
            return res.data[0]
        return None
    except Exception as e:
        log_error(logger, f"Error reading from cache: {e}", exception=e)
        return None

def save_quiz_to_cache(file_hash: str, quiz_data: List[Dict[str, Any]], total_tokens: int) -> bool:
    """حفظ الكويز المولد حديثاً مع تسجيل عدد التوكينات الفعلية المستهلكة"""
    try:
        supabase.table("files_cache").insert({
            "file_hash": file_hash,
            "questions_data": quiz_data,
            "total_tokens": total_tokens
        }).execute()
        log_info(logger, f"Saved to cache with hash: {file_hash} | Tokens: {total_tokens}")
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
        logger.error(f"Error in admin_add_points: {e}")
        return None

def admin_get_global_stats() -> Dict[str, int]:
    try:
        response = supabase.table("users").select("user_id, total_questions").execute()
        if response.data:
            return {"total_users": len(response.data), "total_questions": sum(u['total_questions'] for u in response.data)}
        return {"total_users": 0, "total_questions": 0}
    except Exception as e:
        logger.error(f"Error getting global stats: {e}")
        return {"total_users": 0, "total_questions": 0}

# تأكد من استيراد كل ما يلزم في الأعلى
# لا حاجة لتغييرات كبيرة، فقط تأكد أن admin_search_user تعمل هكذا:

def admin_search_user(query: str) -> Optional[list]:
    try:
        query = query.strip()
        if query.isdigit():
            res = supabase.table("users").select("*").eq("user_id", int(query)).execute()
        else:
            clean_username = query.lstrip('@')
            res = supabase.table("users").select("*").ilike("username", f"%{clean_username}%").execute()
        return res.data
    except Exception as e:
        return None