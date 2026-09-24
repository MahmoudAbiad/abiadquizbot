"""
supabase_helper.feedback
===========================
Rating, Feedbacks & Quality Control Operations: تقييمات/ملاحظات الطلاب،
موقع الكويز على اللوحة، الحذف الإداري، والتصويت.
"""

from typing import Optional, Dict, List, Any

from ._client import supabase, logger, log_error
from .quiz_cache import get_file_quizzes
from .classification import _get_file_hashes_for_quiz_ids, _cleanup_classification_for_hashes

# ==================== Rating, Feedbacks & Quality Control Operations ====================

async def admin_get_feedbacks_page(limit: int = 5, offset: int = 0) -> tuple[List[Dict[str, Any]], int]:
    """🆕 جلب صفحة من ملاحظات الطلاب مع معلومات الكويز (اسم الملف) والطالب (الاسم) المرتبطة بها،
    مع العدد الإجمالي لدعم التصفح بصفحات."""
    try:
        count_res = await supabase.table("quiz_feedbacks").select("id", count="exact").execute()
        total = count_res.count or 0

        res = await supabase.table("quiz_feedbacks").select(
            "id, comment, created_at, user_id, quiz_id, "
            "quizzes(id, source_title, file_hash)"
        ).order("created_at", desc=True).range(offset, offset + limit - 1).execute()
        rows = res.data or []
        if not rows:
            return [], total

        user_ids = list({row["user_id"] for row in rows})
        users_res = await supabase.table("users").select("user_id, first_name, last_name, username").in_("user_id", user_ids).execute()
        users_map = {u["user_id"]: u for u in (users_res.data or [])}
        for row in rows:
            row["student"] = users_map.get(row["user_id"])
        return rows, total
    except Exception as e:
        log_error(logger, f"Error fetching admin feedbacks page: {e}")
        return [], 0


async def admin_get_feedback_by_id(feedback_id: int) -> Optional[Dict[str, Any]]:
    """🆕 جلب ملاحظة واحدة بكامل تفاصيلها (الكويز + الطالب) لعرض شاشة التفاصيل الإدارية."""
    try:
        res = await supabase.table("quiz_feedbacks").select(
            "id, comment, created_at, user_id, quiz_id, "
            "quizzes(id, source_title, file_hash)"
        ).eq("id", feedback_id).limit(1).execute()
        if not res.data:
            return None
        row = res.data[0]
        user_res = await supabase.table("users").select("user_id, first_name, last_name, username").eq("user_id", row["user_id"]).limit(1).execute()
        row["student"] = user_res.data[0] if user_res.data else None
        return row
    except Exception as e:
        log_error(logger, f"Error fetching feedback {feedback_id}: {e}")
        return None


async def admin_get_quiz_board_position(file_hash: Optional[str], quiz_id: str) -> tuple[int, int]:
    """🆕 يرجع (رقم هذا الكويز، العدد الكلي) ضمن نفس لوحة/ملف الكويزات المخزّنة كاش،
    بنفس ترتيب الأفضلية (score) الذي يراه الطلاب فعلياً."""
    try:
        if not file_hash:
            return (0, 0)
        quizzes = await get_file_quizzes(file_hash)
        ids = [str(q["id"]) for q in quizzes]
        if str(quiz_id) in ids:
            return (ids.index(str(quiz_id)) + 1, len(ids))
        return (0, len(ids))
    except Exception as e:
        log_error(logger, f"Error computing quiz board position for {quiz_id}: {e}")
        return (0, 0)


async def admin_get_quiz_by_id(quiz_id: str) -> Optional[Dict[str, Any]]:
    """🆕 جلب بيانات كويز واحد كاملة من الجدول المركزي (تُستخدم لتجربة الكويز من لوحة الإدارة)."""
    try:
        res = await supabase.table("quizzes").select("id, source_title, quiz_data, file_hash, creator_id").eq("id", quiz_id).limit(1).execute()
        return res.data[0] if res.data else None
    except Exception as e:
        log_error(logger, f"Error fetching quiz {quiz_id}: {e}")
        return None


async def admin_delete_quiz(quiz_id: str) -> bool:
    """🆕 حذف كويز بالكامل من الجدول المركزي.

    ✅ تحقّقنا فعلياً من قيود الـ Foreign Key الحالية بقاعدة بيانات Supabase: الجداول
    favorite_quizzes وquiz_attempts وquiz_feedbacks وquiz_responses وquiz_scores
    وquiz_votes كلها مربوطة بـ quizzes.id بقيد ON DELETE CASCADE حقيقي، فتُحذف صفوفها
    المرتبطة تلقائياً بمجرد حذف صف الكويز بالأسفل - لا حاجة لحذفها يدوياً هنا.

    ⚠️ لكن جدولي التحقق المجتمعي من التصنيف (classification_votes وclassification_locks)
    مرتبطان بـ file_hash (نص الهاش) وليس بـ quiz_id، ولا يوجد أي قيد FK يربطهم بجدول
    quizzes أصلاً - فحذف صف الكويز وحده لا يمسحهم إطلاقاً، ويبقون يتيمين بقاعدة البيانات.
    لذلك: نجلب file_hash لهذا الكويز قبل حذفه، وبعد الحذف نتحقق هل ما زال هناك أي كويز
    آخر (لطالب مختلف مثلاً) يستخدم نفس الـ file_hash؛ فقط إذا لم يبقَ أي كويز آخر بنفس
    الهاش (يعني آخر نسخة فعلية منه انحذفت) نحذف تصويتات/تثبيت التصنيف المرتبطة به - حتى
    لا نمسح تصنيفاً ما زال يخدم كويزات أخرى حقيقية بنفس المحتوى بالخطأ.
    """
    try:
        file_hashes = await _get_file_hashes_for_quiz_ids([quiz_id])

        await supabase.table("quizzes").delete().eq("id", quiz_id).execute()

        if file_hashes:
            await _cleanup_classification_for_hashes(file_hashes)

        return True
    except Exception as e:
        log_error(logger, f"Error deleting quiz {quiz_id}: {e}")
        return False


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

