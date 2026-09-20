# services/group_quiz_store.py
"""
==============================================================================
MODULE: طبقة الوصول لقاعدة البيانات الخاصة بجلسات الكويز الجماعية
==============================================================================
كل تعامل مع جداول الكويز الجماعي (`group_quiz_sessions`, `group_quiz_participants`,
`group_quiz_answers`) + جدول عضويات البوت بالمحادثات (`bot_chat_memberships`، خاص
بدعم القنوات) محصور هون. `handlers/group_quiz.py` ما بيلمس supabase
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
_ABANDONED_WAITING_TIMEOUT_SECONDS = 600  # 10 دقايق - راجع get_open_session_for_chat


# ==================== الجلسات ====================
async def create_session(
    quiz_id: str,
    chat_id: int,
    started_by: int,
    total_questions: int,
    message_thread_id: Optional[int] = None,
) -> Optional[Dict[str, Any]]:
    """ينشئ جلسة جديدة بحالة `waiting` (لسا ما بلشت - المعلّم عم يختار الإعدادات).

    `message_thread_id`: موضوع الغروب (Forum supergroup topic) يلي انبلشت منه
    الجلسة، إن وجد. بينحفظ لحظة الإنشاء ويتم تمريره بعدين لكل نداءات الإرسال
    (send_poll/send_message/send_photo) عشان الكويز يضل بنفس التوبيك يلي بلش
    فيه الأدمن، مش يروح لمكان افتراضي بالغروب.
    """
    try:
        fields: Dict[str, Any] = {
            "quiz_id": quiz_id,
            "chat_id": chat_id,
            "started_by": started_by,
            "status": "waiting",
            "total_questions": total_questions,
        }
        if message_thread_id is not None:
            fields["message_thread_id"] = message_thread_id
        res = await supabase.table("group_quiz_sessions").insert(fields).execute()
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
    الوقت بنفس الغروب (تداخل أسئلة + مضاعفة استهلاك rate limit).

    ⚠️ جلسة بحالة `waiting` أقدم من `_ABANDONED_WAITING_TIMEOUT_SECONDS` (أدمن
    بلش الإعداد - اختار كويز مثلاً - وما كمّل شاشات الإعداد) بتتجاهل هون وكأنها
    مش موجودة. بدون هيك، جلسة معلّقة هيك كانت بتقفل الغروب/القناة عن أي كويز
    جماعي جديد للأبد (`create_session` الجديد ما بينعمل إلا لو هالدالة رجّعت
    `None`). ما منلمس صف الجلسة القديمة هون (بس قراءة/تجاهل) - قرار مقصود
    الأبسط والأسرع تنفيذاً (البديل: تنظيف دوري/TTL فعلي، مؤجّل).
    """
    try:
        res = (await supabase.table("group_quiz_sessions").select("*")
               .eq("chat_id", chat_id).in_("status", list(ACTIVE_STATUSES))
               .order("created_at", desc=True).limit(1).execute())
        if not res.data:
            return None
        session = res.data[0]
        if session.get("status") == "waiting":
            created_at_raw = session.get("created_at")
            if created_at_raw:
                try:
                    created_at = datetime.datetime.fromisoformat(created_at_raw.replace("Z", "+00:00"))
                    age_seconds = (datetime.datetime.now(datetime.timezone.utc) - created_at).total_seconds()
                    if age_seconds > _ABANDONED_WAITING_TIMEOUT_SECONDS:
                        return None  # مهجورة - نعامل الغروب/القناة كأنه بلا جلسة مفتوحة
                except Exception:
                    pass  # fail-open - صيغة تاريخ غير متوقعة، ما منقفل الأدمن بلا داعي
        return session
    except Exception as e:
        log_error(logger, f"Error checking open session for chat {chat_id}: {e}")
        return None


async def get_due_sessions(limit: int = 50) -> List[Dict[str, Any]]:
    """يجلب كل الجلسات النشطة (أي وضع) اللي حان وقت سؤالها التالي.

    ⚠️ صارت تشمل `chain_to_timer` كمان، مش `fixed_interval` بس. السبب: تيليجرام
    ما بيبعت تحديث `poll` للبوت لما الاستفتاء يسكّر لحاله بانتهاء `open_period` -
    التوثيق الرسمي بيقول حرفياً "Bots receive only updates about **manually**
    stopped polls" (يعني `bot.stop_poll` بس، مش الإغلاق التلقائي). فـ
    `@router.poll()` بـ handlers/group_quiz.py ما بينفّذ أبداً بهالحالة، وكان
    هاد سبب توقّف `chain_to_timer` عند السؤال الأول بالتجربة الفعلية (تأكّدنا
    من `group_quiz_sessions.questions_sent` عالقة على 1 بكل الجلسات).
    الحل: النبضة الدورية هي المسؤولة عن التقدّم بكلا الوضعين الآن -
    `send_next_group_question` بتحسب `next_question_due_at` من
    `question_timer_seconds` بوضع `chain_to_timer` بدل `question_interval_seconds`.
    معالج `@router.poll()` تركناه كمسار تسريع اختياري بلا ضرر (idempotent عبر
    `claim_question_slot`) بالحالات النادرة يلي تيليجرام بيبعت فيها تحديث."""
    try:
        now_iso = datetime.datetime.now(datetime.timezone.utc).isoformat()
        res = (await supabase.table("group_quiz_sessions").select("*")
               .eq("status", "active").in_("pacing_mode", ["fixed_interval", "chain_to_timer"])
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


async def claim_session_finish(session_id: str) -> bool:
    """يحجز حق إنهاء الجلسة ونشر رسالة النتيجة - قبل أي شغل تاني.

    نفس فكرة `claim_question_slot()` بالضبط (optimistic concurrency)، بس هون
    الشرط على `status IN ('waiting', 'active')` بدل رقم `questions_sent` محدد:
    3 نقاط دخول ممكن توصل لنفس الجلسة بنفس اللحظة تقريباً (نهاية طبيعية عبر
    `send_next_group_question`، زر "إنهاء الآن" من أكتر من أدمن بنفس اللحظة،
    ومسار التسريع `@router.poll()`) - أول واحد بيوصل هون بيربح السباق (`status`
    بيصير `finished` فعلياً، الصف المتأثر `1`)، أي منادي متزامن تاني بيلاقي
    الشرط `IN (...)` غير محقق (الصف صار `finished` أصلاً) فبيرجع `False`
    وبينسحب بصمت بلا ما ينشر رسالة نهاية مكرّرة.

    ⚠️ الفحص المحلي القديم (`session.get("status") == "finished"`) كان بيفحص
    نسخة `session` بالذاكرة (ممكن تكون stale لحظة السباق) - هاد بيفحص قاعدة
    البيانات فعلياً بنفس لحظة التحديث، مو نسخة قديمة محمولة بالكود.
    """
    try:
        fields: Dict[str, Any] = {
            "status": "finished",
            "finished_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "next_question_due_at": None,
        }
        res = (await supabase.table("group_quiz_sessions").update(fields)
               .eq("id", session_id).in_("status", list(ACTIVE_STATUSES)).execute())
        return bool(res.data)
    except Exception as e:
        log_error(logger, f"Error claiming session finish {session_id}: {e}")
        return False


async def claim_session_cancel(session_id: str) -> bool:
    """يلغي الجلسة (`cancelled`) **بشرط** إنها لسا `waiting`/`active` لحظة التنفيذ.

    نفس نمط `claim_session_finish()` (optimistic concurrency): لو الأدمن ضغط
    "إنهاء الآن" بنفس اللحظة اللي فشل فيها إرسال أول سؤال، واحد بس بيربح
    (الصف بيصير `finished`/`cancelled` والتاني بيلاقي الشرط غير محقق فبيرجع `False`
    وبينسحب بصمت) - بدل ما نكتب `cancelled` فوق جلسة خلصت أصلاً.
    مستخدمة بـ handlers/group_quiz.py::_cancel_session_on_first_send_failure (بند 5).
    """
    try:
        fields: Dict[str, Any] = {
            "status": "cancelled",
            "finished_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "next_question_due_at": None,
        }
        res = (await supabase.table("group_quiz_sessions").update(fields)
               .eq("id", session_id).in_("status", list(ACTIVE_STATUSES)).execute())
        return bool(res.data)
    except Exception as e:
        log_error(logger, f"Error claiming session cancel {session_id}: {e}")
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


# ==================== عضويات البوت بالمحادثات (دعم القنوات) ====================
# جدول `bot_chat_memberships` بيتغذّى من تحديثات `my_chat_member` (راجع
# handlers/group_quiz.py::track_bot_chat_membership). هو مصدر معرفة "بأي قنوات
# البوت أدمن وعنده صلاحية نشر" - تيليجرام ما بتقدّم `getMyChats` ولا شي مشابه،
# فالتتبّع لحظة التغيّر هو الطريقة الوحيدة.
_CHANNEL_CANDIDATES_LIMIT = 100


async def upsert_chat_membership(
    chat_id: int,
    chat_type: str,
    chat_title: Optional[str],
    status: str,
    can_post_messages: Optional[bool],
    added_by: Optional[int] = None,
) -> bool:
    """upsert لآخر حالة عضوية معروفة للبوت بمحادثة معيّنة.

    `added_by` **اختياري ومقصود**: بيُمرَّر (ويُكتب) بس لحظة الترقية الفعلية لأدمن
    (من لم يكن أدمن -> أدمن). أي تحديث تاني (تعديل صلاحيات، طرد...) بيمرّر `None`
    فبيتحذف المفتاح من الـ payload - PostgREST بيحدّث بس الأعمدة الموجودة
    بالـ payload، فالقيمة القديمة بتضل محفوظة بدل ما تنكتب فوقها هوية شخص تاني
    عدّل الصلاحيات أو طرد البوت.
    """
    try:
        payload: Dict[str, Any] = {
            "chat_id": chat_id,
            "chat_type": chat_type,
            "chat_title": chat_title,
            "status": status,
            "can_post_messages": can_post_messages,
            "updated_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        }
        if added_by is not None:
            payload["added_by"] = added_by
        await supabase.table("bot_chat_memberships").upsert(payload, on_conflict="chat_id").execute()
        return True
    except Exception as e:
        log_error(logger, f"Error upserting bot chat membership (chat={chat_id}): {e}")
        return False


async def get_chat_membership(chat_id: int) -> Optional[Dict[str, Any]]:
    try:
        res = (await supabase.table("bot_chat_memberships").select("*")
               .eq("chat_id", chat_id).limit(1).execute())
        return res.data[0] if res.data else None
    except Exception as e:
        log_error(logger, f"Error loading bot chat membership {chat_id}: {e}")
        return None


async def list_postable_channels(limit: int = _CHANNEL_CANDIDATES_LIMIT) -> List[Dict[str, Any]]:
    """كل القنوات اللي البوت أدمن فيها **وعنده صلاحية نشر** (`can_post_messages`).

    ⚠️ هاي لائحة *كل* قنوات البوت (لكل المستخدمين) - مش مفلترة بمستخدم معيّن.
    المستدعي (`handlers/group_quiz.py::list_channels_for_share`) لازم يفلترها
    بفحص `is_group_admin` لايف قبل ما يعرض أي شي للمستخدم.
    """
    try:
        res = (await supabase.table("bot_chat_memberships").select("*")
               .eq("chat_type", "channel").eq("status", "administrator")
               .eq("can_post_messages", True)
               .order("updated_at", desc=True).limit(limit).execute())
        return res.data or []
    except Exception as e:
        log_error(logger, f"Error listing postable channels: {e}")
        return []
