"""
supabase_helper.favorites
============================
Favorite Quiz Operations: أقسام المفضلة والكويزات المحفوظة فيها.
"""

import uuid
from typing import Optional, Dict, List, Any

from ._client import supabase, logger, log_error
from constants import DEFAULT_FAVORITE_SECTION_TITLE, MAX_FAVORITE_SECTIONS

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
                "section_id": row["section_id"],
                # 🆕 quiz_id/creator_id: مأخوذان مباشرة من quizzes(*) (مُحمَّلة أصلاً بهذا
                # الاستعلام) - يُستخدمان لإظهار زر "حذف الكويز نهائياً" فقط للأدمن أو
                # لمالك الكويز الفعلي (راجع services/quiz_permissions.py).
                "quiz_id": quiz_info.get("id"),
                "creator_id": quiz_info.get("creator_id"),
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

