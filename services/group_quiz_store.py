# services/group_quiz_store.py
"""
==============================================================================
MODULE: طبقة الوصول لقاعدة البيانات الخاصة بجلسات الكويز الجماعية
==============================================================================
كل تعامل مع الجداول الثلاثة (`group_quiz_sessions`, `group_quiz_participants`,
`group_quiz_answers`) محصور هون. `handlers/group_quiz.py` ما بيلمس supabase
مباشرة إطلاقاً - بيتعامل مع دوال بأسماء واضحة بس.

**لماذا مش داخل `helpers/supabase_helper.py`؟** نفس سبب فصل
`services/group_permissions.py` عن `quiz_permissions.py`: `supabase_helper.py`
صار god-file (+2000 سطر)، وإضافة ميزة كاملة جديدة عليه بيزيد الطين بلة.

⚠️ ملاحظة على التزامن: `claim_question_slot()` هي نقطة التسلسل الوحيدة لإرسال
الأسئلة - منستخدمها بدل أي قفل بالذاكرة لأنه البوت بيشتغل بأكتر من نسخة/عملية
(والنبضة الدورية + معالج `poll` المغلق ممكن ينادوا نفس الجلسة بنفس اللحظة).
"""

import datetime
import json
from typing import Any, Dict, List, Optional

from config import redis_client
from logger import get_logger, log_error
from supabase_helper import supabase

logger = get_logger(__name__)

ACTIVE_STATUSES = ("waiting", "active")
_QUESTIONS_CACHE_PREFIX = "gq:questions:"
_QUESTIONS_CACHE_TTL = 10800  # 3 ساعات - أطول من أي جلسة واقعية


# ==================== الجلسات ====================
async def create_session(
    quiz_id: str,
    chat_id: int,
    started_by: int,
    total_questions: int,
) -> Optional[Dict[str, Any]]:
    """ينشئ جلسة جديدة بحالة `waiting` (لسا ما بلشت - المعلّم عم يختار الإعدادات)."""
    try:
        res = await supabase.table("group_quiz_sessions").insert({
            "quiz_id": quiz_id,
            "chat_id": chat_id,
            "started_by": started_by,
            "status": "waiting",
            "total_questions": total_questions,
        }).execute()
        return res.data[0] if res.data else None
    except Exception as e:
        log_error(logger, f"Error creating group quiz session (chat={chat_id}): {e}")
        return None


async def get_session(session_id: str) -> Optional[Dict[str, Any]]:
    try:
        res = await supabase.table("group_quiz_sessions").select("*").eq("id", session_id).limit(1).execute()
        return res.data[0] if res.data else None
    except Exception as e:
        log_error(logger, f"Error loading group quiz session {session_id}: {e}")
        return None


async def get_open_session_for_chat(chat_id: int) -> Optional[Dict[str, Any]]:
    """يرجّع أي جلسة لسا مفتوحة (waiting أو active) بهالغروب - منمنع جلستين بنفس
    الوقت بنفس الغروب (تداخل أسئلة + مضاعفة استهلاك rate limit)."""
    try:
        res = (await supabase.table("group_quiz_sessions").select("*")
               .eq("chat_id", chat_id).in_("status", list(ACTIVE_STATUSES))
               .order("created_at", desc=True).limit(1).execute())
        return res.data[0] if res.data else None
    except Exception as e:
        log_error(logger, f"Error checking open session for chat {chat_id}: {e}")
        return None


async def get_due_sessions(limit: int = 50) -> List[Dict[str, Any]]:
    """يجلب كل جلسات `fixed_interval` النشطة اللي حان وقت سؤالها التالي - أساس
    النبضة الدورية بمستوى التطبيق كله (وليس مهمة بالذاكرة لكل جلسة، راجع
    السبب بالخطة: انقطاع الاستضافة بمنتصف جلسة بيفقد أي حالة بالذاكرة)."""
    try:
        now_iso = datetime.datetime.now(datetime.timezone.utc).isoformat()
        res = (await supabase.table("group_quiz_sessions").select("*")
               .eq("status", "active").eq("pacing_mode", "fixed_interval")
               .lte("next_question_due_at", now_iso)
               .limit(limit).execute())
        return res.data or []
    except Exception as e:
        log_error(logger, f"Error fetching due group quiz sessions: {e}")
        return []


async def update_session(session_id: str, fields: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    try:
        res = await supabase.table("group_quiz_sessions").update(fields).eq("id", session_id).execute()
        return res.data[0] if res.data else None
    except Exception as e:
        log_error(logger, f"Error updating group quiz session {session_id}: {e}")
        return None


async def claim_question_slot(session_id: str, expected_sent: int, next_due_at: Optional[str]) -> bool:
    """يحجز حق إرسال السؤال رقم `expected_sent` **قبل** إرساله فعلياً.

    التحديث مشروط بـ `questions_sent = expected_sent` (optimistic concurrency):
    أول منادي بيربح ويرجع True، وأي منادي متزامن تاني بيلاقي 0 صفوف متأثرة
    فبيرجع False وبينسحب بصمت. هيك ما في احتمال إرسال نفس السؤال مرتين حتى لو
    النبضة الدورية ومعالج إغلاق الـ poll اشتغلوا بنفس اللحظة على نسختين
    مختلفتين من البوت.

    ⚠️ المقابل: لو فشل الإرسال بعد الحجز، السؤال بينحرق (بينعدّ كمُرسَل). مقبول
    - الخسارة سؤال واحد بدل تكرار/ازدواج يربك الطلاب، والفشل مُسجَّل باللوج.
    """
    try:
        fields: Dict[str, Any] = {"questions_sent": expected_sent + 1}
        fields["next_question_due_at"] = next_due_at  # None مقصودة بوضع chain_to_timer
        res = (await supabase.table("group_quiz_sessions").update(fields)
               .eq("id", session_id).eq("questions_sent", expected_sent)
               .eq("status", "active").execute())
        return bool(res.data)
    except Exception as e:
        log_error(logger, f"Error claiming question slot {expected_sent} for session {session_id}: {e}")
        return False


# ==================== أسئلة الجلسة (كاش) ====================
async def get_session_questions(session: Dict[str, Any]) -> List[Dict[str, Any]]:
    """يجلب أسئلة الكويز المرتبط بالجلسة، مع كاش Redis حتى ما نضرب قاعدة البيانات
    بكل سؤال (جلسة من 30 سؤال = 30 استعلام بلا داعٍ)."""
    session_id = str(session["id"])
    cache_key = f"{_QUESTIONS_CACHE_PREFIX}{session_id}"
    try:
        cached = await redis_client.get(cache_key)
        if cached:
            return json.loads(cached)
    except Exception as e:
        log_error(logger, f"Redis error reading questions cache for session {session_id}: {e}")

    try:
        res = await supabase.table("quizzes").select("quiz_data").eq("id", session["quiz_id"]).limit(1).execute()
        questions = (res.data[0].get("quiz_data") or []) if res.data else []
    except Exception as e:
        log_error(logger, f"Error loading questions for session {session_id}: {e}")
        return []

    try:
        await redis_client.set(cache_key, json.dumps(questions, ensure_ascii=False), ex=_QUESTIONS_CACHE_TTL)
    except Exception as e:
        log_error(logger, f"Redis error caching questions for session {session_id}: {e}")
    return questions


async def clear_session_questions_cache(session_id: str) -> None:
    try:
        await redis_client.delete(f"{_QUESTIONS_CACHE_PREFIX}{session_id}")
    except Exception as e:
        log_error(logger, f"Redis error clearing questions cache for session {session_id}: {e}")


# ==================== الإجابات والمشاركون ====================
async def record_answer(
    session_id: str,
    user_id: int,
    question_index: int,
    poll_id: Optional[str],
    selected_option: Optional[int],
    is_correct: bool,
) -> bool:
    """يسجّل إجابة عضو. القيد `unique (session_id, user_id, question_index)` هو
    الحارس ضد الازدواج - أي محاولة تانية بترجع خطأ ومنرجّع False بلا ضجيج."""
    try:
        res = await supabase.table("group_quiz_answers").insert({
            "session_id": session_id,
            "user_id": user_id,
            "question_index": question_index,
            "poll_id": poll_id,
            "selected_option": selected_option,
            "is_correct": is_correct,
        }).execute()
        return bool(res.data)
    except Exception as e:
        # متوقّع تماماً لو العضو بدّل صوته أو وصل التحديث مرتين
        log_error(logger, f"Answer not recorded (likely duplicate) session={session_id} user={user_id} q={question_index}: {e}")
        return False


async def bump_participant(
    session_id: str,
    user_id: int,
    username: Optional[str],
    first_name: Optional[str],
    is_correct: bool,
    answer_time_ms: int,
) -> None:
    """يحدّث سطر المشارك (أو ينشئه لو أول إجابة إله بالجلسة).

    ⚠️ read-modify-write: PostgREST ما بيدعم `score = score + 1` ذرّياً بلا RPC.
    النافذة الخطرة ضيقة جداً عملياً (نفس المستخدم بيجاوب على سؤالين بنفس
    الميلي ثانية)، بس لو صارت الجلسات كبيرة كتير فالحل النهائي دالة Postgres
    (RPC) بدل هالنمط - مُرشَّح واضح للخطوة 7.
    """
    try:
        res = (await supabase.table("group_quiz_participants").select("score, correct_count, total_answer_time_ms")
               .eq("session_id", session_id).eq("user_id", user_id).limit(1).execute())
        row = res.data[0] if res.data else None

        payload = {
            "session_id": session_id,
            "user_id": user_id,
            "username": username,
            "first_name": first_name,
            "score": int((row or {}).get("score", 0)) + (1 if is_correct else 0),
            "correct_count": int((row or {}).get("correct_count", 0)) + (1 if is_correct else 0),
            "total_answer_time_ms": int((row or {}).get("total_answer_time_ms", 0)) + max(0, int(answer_time_ms)),
        }
        await supabase.table("group_quiz_participants").upsert(
            payload, on_conflict="session_id,user_id"
        ).execute()
    except Exception as e:
        log_error(logger, f"Error updating participant session={session_id} user={user_id}: {e}")


async def get_leaderboard(session_id: str, limit: int = 20) -> List[Dict[str, Any]]:
    """ترتيب المشاركين: الأعلى score أولاً، وعند التساوي الأسرع (أقل زمن إجابة)."""
    try:
        res = (await supabase.table("group_quiz_participants").select("*")
               .eq("session_id", session_id)
               .order("score", desc=True).order("total_answer_time_ms", desc=False)
               .limit(limit).execute())
        return res.data or []
    except Exception as e:
        log_error(logger, f"Error loading leaderboard for session {session_id}: {e}")
        return []
