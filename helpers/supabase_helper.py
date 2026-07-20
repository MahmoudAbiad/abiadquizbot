"""
Supabase database operations for user management and statistics.
Handles user registration, points management, database queries, centralized quiz caching, and community ratings.
"""

import asyncio
import os
import datetime
import uuid
from typing import Optional, Dict, List, Any
from dotenv import load_dotenv, find_dotenv
from supabase import create_async_client
from logger import get_logger, log_error, log_warning, log_info
from constants import (
    WELCOME_POINTS, DAILY_RENEWAL_POINTS, REFERRAL_BONUS_POINTS,
    DEFAULT_FAVORITE_SECTION_TITLE, MAX_FAVORITE_SECTIONS,
)
from validators import validate_user_id, validate_points_amount

dotenv_path = find_dotenv()
load_dotenv(dotenv_path)

logger = get_logger(__name__)


def _balance_payload(free_points: Any = 0, paid_points: Any = 0, **extra: Any) -> Dict[str, Any]:
    """Expose split balances while retaining ``points`` for older callers."""
    free = float(free_points or 0)
    paid = float(paid_points or 0)
    return {"free_points": free, "paid_points": paid, "points": free + paid, **extra}

# ==================== إعداد واقلاع عميل قاعدة البيانات بشكل آمن ====================
try:
    client_or_coro = create_async_client(os.getenv("SUPABASE_URL"), os.getenv("SUPABASE_KEY"))
    
    if asyncio.iscoroutine(client_or_coro):
        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                supabase = loop.run_until_complete(client_or_coro)
            else:
                supabase = asyncio.run(client_or_coro)
        except RuntimeError:
            supabase = asyncio.run(client_or_coro)
    else:
        supabase = client_or_coro

    log_info(logger, "Supabase Async client initialized successfully with centralized schema mapping")
except Exception as e:
    log_error(logger, f"Failed to initialize Supabase Async: {e}", exception=e)
    raise
# ==================================================================================

# ==================== User Management ====================
async def check_or_add_user(user_id: int, username: str, first_name: str, last_name: str, referrer_id: Optional[int] = None) -> Dict[str, Any]:
    try:
        is_valid, error = validate_user_id(user_id)
        if not is_valid:
            return _balance_payload(status="error", referrer=None)
        
        today = datetime.date.today().isoformat()
        response = await supabase.table("users").select("*").eq("user_id", user_id).execute()
        
        if not response.data:
            return await _add_new_user(user_id, username, first_name, last_name, referrer_id, today)
        
        return await _check_daily_renewal(user_id, response.data[0], today)
    except Exception as e:
        log_error(logger, f"Error in check_or_add_user: {e}", exception=e)
        return _balance_payload(status="error", referrer=None)

async def _add_new_user(user_id: int, username: str, first_name: str, last_name: str, referrer_id: Optional[int], today: str) -> Dict[str, Any]:
    try:
        actual_referrer = None
        if referrer_id and str(referrer_id) != str(user_id):
            ref_check = await supabase.table("users").select("paid_points").eq("user_id", referrer_id).execute()
            if ref_check.data:
                actual_referrer = referrer_id
                new_ref_points = float(ref_check.data[0].get('paid_points') or 0) + REFERRAL_BONUS_POINTS
                await supabase.table("users").update({"paid_points": new_ref_points}).eq("user_id", referrer_id).execute()
                
        await supabase.table("users").insert({
            "user_id": user_id,
            "username": username,
            "first_name": first_name,
            "last_name": last_name or "Unknown",
            "free_points": float(WELCOME_POINTS),
            "paid_points": 0.0,
            "total_questions": 0,
            "referred_by": actual_referrer,
            "last_renewal": today
        }).execute()
        
        return _balance_payload(WELCOME_POINTS, 0, status="new", referrer=actual_referrer)
    except Exception as e:
        log_error(logger, f"Error adding new user: {e}", exception=e)
        return _balance_payload(status="error", referrer=None)

async def _check_daily_renewal(user_id: int, user_data: Dict, today: str) -> Dict[str, Any]:
    try:
        rpc_response = await supabase.rpc("check_and_apply_daily_renewal_atomic", {
            "target_user_id": user_id,
            "today_date": today,
            "renewal_amount": DAILY_RENEWAL_POINTS
        }).execute()
        
        if rpc_response.data:
            result = rpc_response.data[0] if isinstance(rpc_response.data, list) else rpc_response.data
            return {
                **_balance_payload(result.get("free_points"), result.get("paid_points")),
                "status": result["renewal_status"], 
                "referrer": None
            }
        
        return _balance_payload(user_data.get('free_points'), user_data.get('paid_points'), status="normal", referrer=None)
    except Exception as e:
        log_error(logger, f"Error checking daily renewal via RPC: {e}", exception=e)
        return _balance_payload(user_data.get('free_points'), user_data.get('paid_points'), status="error", referrer=None)
    
async def update_user_stats(user_id: int, points_to_deduct: float, questions_generated: Optional[int] = None) -> Optional[float]:
    try:
        is_valid, error = validate_user_id(user_id)
        if not is_valid: return None

        if questions_generated is None:
            questions_generated = int(points_to_deduct)
        
        rpc_response = await supabase.rpc("deduct_user_points_atomic", {
            "target_user_id": user_id,
            "points_to_deduct": points_to_deduct,
            "questions_generated": questions_generated
        }).execute()
        
        if rpc_response.data is not None:
            return float(rpc_response.data)
        return None
    except Exception as e:
        log_error(logger, f"Error updating user stats via RPC: {e}", exception=e)
        return None

async def refund_user_points(user_id: int, points_to_refund: float) -> bool:
    try:
        if points_to_refund <= 0:
            return True
        is_valid, error = validate_user_id(user_id)
        if not is_valid:
            return False
        response = await supabase.table("users").select("paid_points").eq("user_id", user_id).execute()
        if not response.data:
            return False
        current_paid = float(response.data[0].get("paid_points") or 0)
        new_paid = current_paid + float(points_to_refund)
        await supabase.table("users").update({"paid_points": new_paid}).eq("user_id", user_id).execute()
        log_info(logger, f"Refunded {points_to_refund} points to user {user_id}")
        return True
    except Exception as e:
        log_error(logger, f"Error refunding points for user {user_id}: {e}", exception=e)
        return False

# ==================== Central Quiz & Cache Operations ====================

async def get_file_quizzes(file_hash: str) -> list:
    """جلب كل الكويزات التابعة للملف مرتبة تلقائياً حسب التقييم الأعلى لزملائك الطلاب"""
    try:
        res = await supabase.table("quizzes").select("id, likes, dislikes, score, quiz_data").eq("file_hash", file_hash).order("score", desc=True).execute()
        return res.data or []
    except Exception as e:
        log_error(logger, f"Error getting file quizzes from central table: {e}")
        return []

async def save_file_quiz_multiple(file_hash: str, creator_id: int, source_title: str, quiz_data: list, total_tokens: int) -> Optional[str]:
    """حفظ كويز جديد مولد كلياً بالجدول المركزي وعزل التكرار لخدمة الدفعة الدراسية"""
    try:
        res = await supabase.table("quizzes").insert({
            "creator_id": creator_id,
            "file_hash": file_hash,
            "source_title": source_title,
            "quiz_data": quiz_data,
            "total_tokens": total_tokens
        }).execute()
        if res.data:
            return res.data[0]['id']
        return None
    except Exception as e:
        log_error(logger, f"Error saving central quiz data: {e}")
        return None

async def get_cached_quiz(file_hash: str) -> Optional[Dict[str, Any]]:
    """توجيه ذكي وفولباك (Backward Compatibility) لمحاذاة كود ملف البوت القديم مع الجدول المركزي الجديد"""
    try:
        res = await supabase.table("quizzes").select("quiz_data, total_tokens").eq("file_hash", file_hash).order("score", desc=True).limit(1).execute()
        if res.data:
            log_info(logger, f"Cache HIT (Central Table redirection) for hash: {file_hash}")
            row = res.data[0]
            return {
                "questions_data": row["quiz_data"],
                "total_tokens": row["total_tokens"]
            }
        return None
    except Exception as e:
        log_error(logger, f"Error reading fallback cache content: {e}")
        return None

async def save_quiz_to_cache(file_hash: str, quiz_data: List[Dict[str, Any]], total_tokens: int) -> bool:
    """دالة فولباك للتخزين السريع في المسار المركزي الافتراضي"""
    try:
        # استخدام معرف الإدارة كمنشئ افتراضي في حال عدم تمريره من السيرفر القديم
        admin_id = int(os.getenv("ADMIN_ID", "0"))
        res = await save_file_quiz_multiple(file_hash, admin_id, "كويز مخزن تلقائياً", quiz_data, total_tokens)
        return res is not None
    except Exception as e:
        log_error(logger, f"Error routing fallback cache saving: {e}")
        return False

# ==================== Shared Quiz Operations (Deep Linking) ====================
def create_shared_quiz_id() -> str:
    return uuid.uuid4().hex[:12]

async def save_shared_quiz(share_id: str, owner_id: int, title: str, quiz_data: List[Dict[str, Any]]) -> bool:
    """تفعيل ميزة المشاركة بدمج كود الرابط مباشرة بالخلية المركزية لمنع تكرار الـ JSONB وهدر المساحة"""
    try:
        # البحث إن كان هذا الكويز موجود بالفعل لنقوم فقط بحقن رمز المشاركة داخله دون إنشاء صف جديد مكرر
        check_res = await supabase.table("quizzes").select("id").eq("creator_id", owner_id).eq("source_title", title).order("created_at", desc=True).limit(1).execute()
        
        if check_res.data:
            target_id = check_res.data[0]["id"]
            await supabase.table("quizzes").update({"share_code": share_id}).eq("id", target_id).execute()
            log_info(logger, f"Injected share code {share_id} into existing central quiz {target_id}")
        else:
            # إذا كان كويز نصي مباشر أو لم يعثر عليه، ننشئ له سجلاً مركزياً مخصصاً برمز مشاركة فريد
            await supabase.table("quizzes").insert({
                "creator_id": owner_id,
                "source_title": title,
                "quiz_data": quiz_data,
                "share_code": share_id
            }).execute()
            log_info(logger, f"Created new central row with share code: {share_id}")
        return True
    except Exception as e:
        log_error(logger, f"Error linking shared quiz code: {e}")
        return False

async def get_shared_quiz(share_id: str) -> Optional[Dict[str, Any]]:
    try:
        res = await supabase.table("quizzes").select("*").eq("share_code", share_id).execute()
        if res.data:
            return res.data[0]
        return None
    except Exception as e:
        log_error(logger, f"Error loading shared quiz from code: {e}")
        return None

# ==================== Favorite Quiz Operations ====================
async def count_favorite_sections(user_id: int) -> int:
    try:
        res = await supabase.table("favorite_quiz_sections").select("id", count="exact").eq("user_id", user_id).execute()
        return int(res.count or 0)
    except Exception as e:
        log_error(logger, f"Error counting favorite sections: {e}")
        return 0

async def list_favorite_sections(user_id: int) -> List[Dict[str, Any]]:
    try:
        res = await supabase.table("favorite_quiz_sections").select("id, title, created_at").eq("user_id", user_id).order("created_at", desc=False).execute()
        # إعادة تعيين المسميات لتطابق السير القديم في البوت (id -> section_id)
        return [{"section_id": r["id"], "title": r["title"], "created_at": r["created_at"]} for r in (res.data or [])]
    except Exception as e:
        log_error(logger, f"Error listing favorite sections: {e}")
        return []

async def create_favorite_section(user_id: int, title: str) -> Optional[str]:
    try:
        res = await supabase.table("favorite_quiz_sections").insert({
            "user_id": user_id,
            "title": title
        }).execute()
        if res.data:
            return res.data[0]["id"]
        return None
    except Exception as e:
        log_error(logger, f"Error creating favorite section: {e}")
        return None

async def save_favorite_quiz(user_id: int, title: str, quiz_data: List[Dict[str, Any]], section_id: Optional[str] = None, source_title: Optional[str] = None, quiz_id: Optional[str] = None) -> Optional[str]:
    try:
        target_quiz_uuid = None
        
        # التحقق إذا كان الآيدي الممرر عبارة عن UUID صحيح وجاهز للربط في السكيما المركزية
        if quiz_id:
            try:
                uuid.UUID(str(quiz_id))
                target_quiz_uuid = str(quiz_id)
            except ValueError:
                pass
                
        # إذا لم يتوفر UUID (مثل الكويزات القديمة أو النصية)، نضمن حقنها بالجدول المركزي أولاً لتوليد معرف فريد لها
        if not target_quiz_uuid:
            q_res = await supabase.table("quizzes").insert({
                "creator_id": user_id,
                "source_title": source_title or title,
                "quiz_data": quiz_data
            }).execute()
            if q_res.data:
                target_quiz_uuid = q_res.data[0]["id"]
                
        if not target_quiz_uuid:
            return None
            
        fav_id = str(uuid.uuid4())
        await supabase.table("favorite_quizzes").insert({
            "favorite_id": fav_id,
            "user_id": user_id,
            "quiz_id": target_quiz_uuid,
            "section_id": section_id if section_id else None,
            "custom_title": title
        }).execute()
        return fav_id
    except Exception as e:
        log_error(logger, f"Error saving favorite junction entity: {e}")
        return None

async def list_favorite_quizzes(user_id: int, search_query: Optional[str] = None, sort_by: str = "latest") -> List[Dict[str, Any]]:
    try:
        res = await supabase.table("favorite_quizzes").select("favorite_id, section_id, custom_title, created_at, quizzes(id, source_title, quiz_data)").eq("user_id", user_id).execute()
        
        sections_res = await supabase.table("favorite_quiz_sections").select("id, title").eq("user_id", user_id).execute()
        section_map = {s["id"]: s["title"] for s in (sections_res.data or [])}
        
        items = []
        for row in (res.data or []):
            quiz_info = row.get("quizzes") or {}
            item = {
                "favorite_id": row["favorite_id"],
                "quiz_id": quiz_info.get("id"),
                "title": row["custom_title"] or quiz_info.get("source_title") or "كويز",
                "source_title": quiz_info.get("source_title") or "محتوى مستخرج",
                "section_id": row["section_id"],
                "section_title": section_map.get(row["section_id"]) or DEFAULT_FAVORITE_SECTION_TITLE,
                "created_at": row["created_at"],
                "quiz_data": quiz_info.get("quiz_data", [])
            }
            items.append(item)
            
        if search_query:
            query = search_query.strip().lower()
            items = [
                i for i in items
                if query in i["title"].lower() or query in i["source_title"].lower() or query in i["section_title"].lower()
            ]

        if sort_by == "section":
            items.sort(key=lambda x: x["created_at"] or "", reverse=True)
            items.sort(key=lambda x: x["section_title"].lower())
        else:
            items.sort(key=lambda x: x["created_at"] or "", reverse=True)

        return items
    except Exception as e:
        log_error(logger, f"Error listing favorite central junction row: {e}")
        return []

async def can_create_more_favorite_sections(user_id: int) -> bool:
    return await count_favorite_sections(user_id) < MAX_FAVORITE_SECTIONS

async def get_favorite_quiz(user_id: int, favorite_id: str) -> Optional[Dict[str, Any]]:
    try:
        res = await supabase.table("favorite_quizzes").select("favorite_id, custom_title, section_id, quizzes(*)").eq("user_id", user_id).eq("favorite_id", favorite_id).execute()
        if res.data:
            row = res.data[0]
            quiz_info = row.get("quizzes") or {}
            return {
                "favorite_id": row["favorite_id"],
                "title": row["custom_title"] or quiz_info.get("source_title"),
                "quiz_data": quiz_info.get("quiz_data"),
                "section_id": row["section_id"]
            }
        return None
    except Exception as e:
        log_error(logger, f"Error loading specific favorite quiz join row: {e}")
        return None

async def get_favorite_quiz_by_global_id(favorite_id: str) -> Optional[Dict[str, Any]]:
    try:
        res = await supabase.table("favorite_quizzes").select("favorite_id, custom_title, quizzes(*)").eq("favorite_id", favorite_id).execute()
        if res.data:
            row = res.data[0]
            quiz_info = row.get("quizzes") or {}
            return {
                "favorite_id": row["favorite_id"],
                "title": row["custom_title"] or quiz_info.get("source_title"),
                "quiz_data": quiz_info.get("quiz_data")
            }
        return None
    except Exception as e:
        log_error(logger, f"Error loading global id matching favorite element: {e}")
        return None

async def remove_favorite_quiz(user_id: int, favorite_id: str) -> bool:
    try:
        await supabase.table("favorite_quizzes").delete().eq("user_id", user_id).eq("favorite_id", favorite_id).execute()
        return True
    except Exception as e:
        log_error(logger, f"Error removing target favorite quiz connection: {e}")
        return False

# ==================== Rating, Feedbacks & Quality Control Operations ====================

async def submit_quiz_vote(quiz_id: str, user_id: int, vote_type: str) -> bool:
    """إرسال وحقن تصويت الطلاب (لايك/ديسلايك) عبر الـ RPC لضمان منع التكرار وحساب السكور لحظياً"""
    try:
        res = await supabase.rpc("vote_on_quiz", {
            "p_quiz_id": quiz_id,
            "p_user_id": user_id,
            "p_vote": vote_type
        }).execute()
        return bool(res.data)
    except Exception as e:
        log_error(logger, f"Error executing quiz atomic vote function: {e}")
        return False

async def save_quiz_feedback(quiz_id: str, user_id: int, comment: str) -> bool:
    """حفظ ملاحظات وإفادات الطلاب الأكاديمية لمراجعتها لاحقاً من قبل الإدارة"""
    try:
        await supabase.table("quiz_feedbacks").insert({
            "quiz_id": quiz_id,
            "user_id": user_id,
            "comment": comment
        }).execute()
        return True
    except Exception as e:
        log_error(logger, f"Error saving student feedback on quiz: {e}")
        return False

async def admin_get_recent_feedbacks() -> list:
    """جلب قائمة بأحدث الملاحظات لتقديمها للوحة الإدارة السرية"""
    try:
        res = await supabase.table("quiz_feedbacks").select("quiz_id, comment, created_at, user_id").order("created_at", desc=True).limit(15).execute()
        return res.data or []
    except Exception as e:
        log_error(logger, f"Error pulling admin statistics feedbacks report: {e}")
        return []

async def auto_cleanup_bad_quizzes():
    """تنظيف تلقائي شامل للكويزات المرفوضة من الطلاب (ديسلايكات عالية) والتي تجاوزت 48 ساعة"""
    try:
        threshold = (datetime.datetime.utcnow() - datetime.timedelta(days=2)).isoformat()
        # السياسة: حذف أي كويز قديم مجموعه سلبي (Score < 0) تلقائياً بفعل مجتمع الطلاب النشط
        await supabase.table("quizzes").delete().lt("created_at", threshold).lt("score", 0).execute()
        log_info(logger, "Automated database garbage cleanup loop executed successfully.")
    except Exception as e:
        log_error(logger, f"Error running the background auto cleanup query: {e}")

# ==================== Admin Operations ====================
async def admin_add_points(target_id: int, amount: int) -> Optional[int]:
    try:
        user = await supabase.table("users").select("free_points, paid_points").eq("user_id", target_id).execute()
        if user.data:
            paid_points = float(user.data[0].get('paid_points') or 0) + amount
            free_points = float(user.data[0].get('free_points') or 0)
            await supabase.table("users").update({"paid_points": paid_points}).eq("user_id", target_id).execute()
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

# ==================== Quiz Scores & Leaderboard Operations ====================
async def get_or_update_high_score(user_id: int, quiz_id: str, current_score: int, total_questions: int) -> Dict[str, Any]:
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
    try:
        await supabase.table("quiz_scores").update({"is_public": True}).eq("quiz_id", quiz_id).eq("user_id", user_id).execute()
        return True
    except Exception as e:
        log_error(logger, f"Error publishing score: {e}", exception=e)
        return False

async def get_top_5_leaderboard(quiz_id: str) -> List[Dict[str, Any]]:
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