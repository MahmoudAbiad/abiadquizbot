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
        # استدعاء الدالة الذرية من قاعدة البيانات مباشرة لمنع حالة السباق
        rpc_response = supabase.rpc("check_and_apply_daily_renewal_atomic", {
            "target_user_id": user_id,
            "today_date": today,
            "renewal_amount": DAILY_RENEWAL_POINTS
        }).execute()
        
        if rpc_response.data:
            result = rpc_response.data[0] if isinstance(rpc_response.data, list) else rpc_response.data
            return {
                "points": result["current_points"], 
                "status": result["renewal_status"], 
                "referrer": None
            }
        
        return {"points": user_data.get('points', 0), "status": "normal", "referrer": None}
    except Exception as e:
        log_error(logger, f"Error checking daily renewal via RPC: {e}", exception=e)
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
        
        # تنفيذ عملية الخصم الذرية بطلب واحد آمن بنسبة 100%
        rpc_response = supabase.rpc("deduct_user_points_atomic", {
            "target_user_id": user_id,
            "points_to_deduct": points_to_deduct,
            "questions_generated": questions_generated
        }).execute()
        
        if rpc_response.data is not None:
            return int(rpc_response.data)
        return None
    except Exception as e:
        log_error(logger, f"Error updating user stats via RPC: {e}", exception=e)
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
    quiz_id: Optional[str] = None,  # 🆕 أضفنا هذا المعامل لاستقبال معرف الكويز الفريد
) -> Optional[str]:
    try:
        favorite_id = uuid.uuid4().hex[:12]
        payload = {
            "favorite_id": favorite_id,
            "user_id": user_id,
            "title": title,
            "source_title": source_title or title,
            "quiz_data": quiz_data,
            "created_at": datetime.datetime.utcnow().isoformat()
        }
        if section_id is not None:
            payload["section_id"] = section_id
            
        if quiz_id is not None:  # 🆕 تخزين المعرف في قاعدة البيانات إن وجد
            payload["quiz_id"] = quiz_id

        try:
            supabase.table("favorite_quizzes").insert(payload).execute()
        except Exception as insert_error:
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
            # 🆕 قمنا بإضافة quiz_id هنا لجلبها من قاعدة البيانات
            res = supabase.table("favorite_quizzes").select("favorite_id, quiz_id, title, source_title, section_id, created_at").eq("user_id", user_id).execute()
        except Exception as select_error:
            error_text = getattr(select_error, 'message', str(select_error))
            if "section_id" in error_text and "favorite_quizzes" in error_text:
                # 🆕 قمنا بإضافة quiz_id هنا أيضاً في حالة الفولباك (Fallback)
                res = supabase.table("favorite_quizzes").select("favorite_id, quiz_id, title, source_title, created_at").eq("user_id", user_id).execute()
            else:
                raise

        items = []
        for row in (res.data or []):
            item = dict(row)
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

# 🆕 تم التحديث: جلب الكويز المفضّل باستخدام الـ UUID الفريد عالمياً (لملف start.py)
def get_favorite_quiz_by_global_id(favorite_id: str) -> Optional[Dict[str, Any]]:
    try:
        res = supabase.table("favorite_quizzes").select("*").eq("favorite_id", favorite_id).execute()
        if res.data:
            return res.data[0]
        return None
    except Exception as e:
        log_error(logger, f"Error loading global favorite quiz: {e}", exception=e)
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
    
    # ==================== Quiz Scores & Leaderboard Operations ====================

def get_or_update_high_score(user_id: int, quiz_id: str, current_score: int, total_questions: int) -> Dict[str, Any]:
    """
    جلب النتيجة السابقة للطالب (إن وجدت) وتحديثها إذا كانت النتيجة الحالية أعلى.
    تعيد قاموساً يحتوي على النتيجة السابقة وأعلى نتيجة حالية.
    """
    try:
        # البحث عن النتيجة السابقة
        res = supabase.table("quiz_scores").select("*").eq("quiz_id", quiz_id).eq("user_id", user_id).execute()
        
        previous_score = None
        new_highest = current_score
        is_public = False
        
        if res.data:
            existing = res.data[0]
            previous_score = existing["highest_score"]
            is_public = existing["is_public"]
            
            # التحديث فقط إذا كانت النتيجة الجديدة أعلى
            if current_score > previous_score:
                supabase.table("quiz_scores").update({
                    "highest_score": current_score,
                    "total_questions": total_questions,
                    "updated_at": datetime.datetime.utcnow().isoformat()
                }).eq("score_id", existing["score_id"]).execute()
            else:
                new_highest = previous_score # الاحتفاظ بالنتيجة القديمة لأنها أعلى
        else:
            # إدخال سجل جديد إذا كانت هذه أول مرة يحل فيها الكويز
            supabase.table("quiz_scores").insert({
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


def publish_score_to_leaderboard(user_id: int, quiz_id: str) -> bool:
    """تغيير حالة النتيجة لتصبح عامة وتظهر في لوحة الشرف"""
    try:
        supabase.table("quiz_scores").update({"is_public": True}).eq("quiz_id", quiz_id).eq("user_id", user_id).execute()
        return True
    except Exception as e:
        log_error(logger, f"Error publishing score: {e}", exception=e)
        return False


def get_top_5_leaderboard(quiz_id: str) -> List[Dict[str, Any]]:
    """جلب أعلى 5 نتائج علنية لهذا الكويز مع أسماء الطلاب"""
    try:
        # استخدام Join في Supabase لجلب بيانات المستخدم مع النتيجة
        res = supabase.table("quiz_scores") \
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