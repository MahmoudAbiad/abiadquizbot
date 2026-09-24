"""
supabase_helper.analytics
============================
Usage Analytics & Tracking: تسجيل أحداث الاستخدام والأخطاء، تتبّع محاولات
الكويز، ولوحات تحليلات الأدمن (نظرة عامة، مستخدمون نشطون، سجل الأخطاء،
سجل التوليد، لوحة الإحالات، تنظيف البيانات القديمة).
"""

import asyncio
import datetime
import os
import traceback
import uuid
from typing import Optional, Dict, List, Any

from config import redis_client
from constants import to_syria_datetime, format_syria_time
from ._client import supabase, logger, log_error, log_warning, log_info, _is_valid_uuid
from .classification import (
    _get_safe_to_delete_quiz_ids,
    _get_file_hashes_for_quiz_ids,
    _cleanup_classification_for_hashes,
)

# ==================== Usage Analytics & Tracking (Fixed & Complete) ====================

import json

try:
    ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))
except (ValueError, TypeError):
    ADMIN_ID = 0
    
# 1️⃣ تسجيل الأحداث (تعريف واحد موحد: حفظ مباشر في الداتابيز مع مسار احتياطي لـ Redis)
async def log_usage_event(user_id: int, event_type: str, metadata: Optional[Dict[str, Any]] = None) -> None:
    """تسجيل حدث استخدام آمن (يستثني الآدمن لعدم التأثير على التحليلات)."""
    # 🚫 تجنب تسجيل نشاط الآدمن في التحليلات
    if ADMIN_ID and user_id == ADMIN_ID:
        return

    try:
        payload = {
            "user_id": user_id,
            "event_type": event_type,
            "metadata": metadata or {},
            "created_at": datetime.datetime.now(datetime.timezone.utc).isoformat()
        }
        await supabase.table("usage_events").insert(payload).execute()
    except Exception as e:
        try:
            await redis_client.rpush("analytics_queue", json.dumps(payload))
        except Exception as redis_err:
            # ⚠️ نستخدم logger.error مباشرة هنا (وليس log_error) عن قصد: log_error تحاول
            # ربط أي خطأ بالمستخدم الحالي عبر log_error_event، والتي بدورها تنادي
            # log_usage_event — فلو استُخدمت log_error هنا لدخلنا بحلقة استدعاء ذاتية
            # لا نهائية عند فشل التسجيل. هذا الفشل بالذات (فشل تسجيل + فشل احتياطي Redis)
            # نادر جداً وغير حرج لتجربة الطالب، فيكفي تسجيله بالـ logger فقط.
            logger.error(f"Error logging usage event for user {user_id}: {e} | Redis fallback failed: {redis_err}")

# 1️⃣.5 تسجيل الأخطاء التي يواجهها الطالب فعلياً (خطأ = حدث بنوع 'error_occurred')
async def log_error_event(user_id: int, error_message: str, exception: Optional[Exception] = None,
                           update_type: Optional[str] = None, context: Optional[str] = None,
                           unhandled: bool = False) -> None:
    """
    تسجيل خطأ واجهه طالب فعلياً أثناء استخدام البوت، كحدث تحليلات عادي بجدول usage_events
    (event_type='error_occurred') — بنفس آلية log_usage_event تماماً (صامتة عند الفشل،
    مع مسار احتياطي عبر Redis)، فيظهر تلقائياً بلوحة الأدمن (قائمة الأحداث، تصدير CSV،
    وقسم "🐞 آخر الأخطاء" المخصص).

    تُستدعى تلقائياً من logger.log_error()/log_critical() لأي استدعاء بأي مكان بالمشروع
    طالما هناك سياق مستخدم حالي (راجع error_context.py)، بالإضافة لاستدعاء صريح من
    ErrorTrackingMiddleware عند حدوث استثناء غير متوقع بالكامل لم يلتقطه أي try/except
    محلي (unhandled=True).
    """
    tb_str = None
    if exception is not None:
        try:
            tb_str = "".join(
                traceback.format_exception(type(exception), exception, exception.__traceback__)
            )[-2000:]  # آخر 2000 حرف كافية عادةً لمعرفة مكان الخطأ دون تضخيم الصف بالداتابيز
        except Exception:
            tb_str = None

    metadata: Dict[str, Any] = {
        "message": (error_message or "")[:500],
        "error_type": type(exception).__name__ if exception else None,
        "update_type": update_type,
        "context": (context or "")[:200] if context else None,
        "unhandled": unhandled,
    }
    if tb_str:
        metadata["traceback"] = tb_str

    await log_usage_event(user_id, "error_occurred", metadata)


# 2️⃣ تفريغ طابور Redis بأمان دون فقدان البيانات (Transactional Pop)
async def flush_analytics_queue() -> None:
    """تفريغ الأحداث الاحتياطية من Redis ورفعها دفعة واحدة إلى Supabase مع ضمان عدم الفقدان."""
    try:
        events = []
        raw_items = []
        for _ in range(500):
            raw = await redis_client.lpop("analytics_queue")
            if not raw:
                break
            raw_items.append(raw)
            events.append(json.loads(raw))

        if events:
            try:
                await supabase.table("usage_events").insert(events).execute()
                log_info(logger, f"Successfully flushed {len(events)} analytics events to Supabase.")
            except Exception as db_err:
                # إرجاع البيانات إلى طابور Redis في حال فشل الإدراج لعدم ضياعها
                for item in reversed(raw_items):
                    await redis_client.lpush("analytics_queue", item)
                log_error(logger, f"Failed to insert flushed events into Supabase, re-queued items: {db_err}")
    except Exception as e:
        log_error(logger, f"Error flushing analytics queue: {e}")


# 3️⃣ إدارة محاولات الكويز مع تفادي الـ Race Conditions
def start_quiz_attempt(user_id: int, quiz_id: Optional[str], source_type: str, total_questions: int) -> str:
    """توليد معرف فريد وبدء المحاولة (تستثني الآدمن)."""
    client_ref = uuid.uuid4().hex
    # 🚫 تجنب تسجيل محاولات الآدمن بجدول المحاولات
    if ADMIN_ID and user_id == ADMIN_ID:
        return client_ref
        
    asyncio.create_task(_insert_quiz_attempt(client_ref, user_id, quiz_id, source_type, total_questions))
    return client_ref


async def _insert_quiz_attempt(client_ref: str, user_id: int, quiz_id: Optional[str], source_type: str, total_questions: int) -> None:
    try:
        clean_quiz_id = str(quiz_id) if _is_valid_uuid(quiz_id) else None

        await supabase.table("quiz_attempts").insert({
            "client_ref": client_ref,
            "user_id": user_id,
            "quiz_id": clean_quiz_id,
            "source_type": source_type,
            "total_questions": total_questions,
            "started_at": datetime.datetime.now(datetime.timezone.utc).isoformat()
        }).execute()
    except Exception as e:
        log_error(logger, f"Error inserting quiz attempt tracking row ({client_ref}): {e}")


async def complete_quiz_attempt(attempt_ref: Optional[str], score: int) -> None:
    """إغلاق المحاولة المكتملة وحساب الوقت بدقة مع معالجة تأخير السجلات."""
    if not attempt_ref:
        return
    try:
        row = None
        for _ in range(3):
            res = await supabase.table("quiz_attempts").select("started_at").eq("client_ref", attempt_ref).limit(1).execute()
            if res.data:
                row = res.data[0]
                break
            await asyncio.sleep(0.4)  # انتظار 400ms في حال وجود تأخير في الشبكة

        duration = None
        if row and row.get("started_at"):
            try:
                started_str = str(row["started_at"]).replace("Z", "+00:00")
                started = datetime.datetime.fromisoformat(started_str)
                duration = int((datetime.datetime.now(datetime.timezone.utc) - started).total_seconds())
            except Exception as dt_err:
                log_warning(logger, f"Duration calculation issue for {attempt_ref}: {dt_err}")

        await supabase.table("quiz_attempts").update({
            "score": score,
            "is_completed": True,
            "completed_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "duration_seconds": duration
        }).eq("client_ref", attempt_ref).execute()
    except Exception as e:
        log_error(logger, f"Error completing quiz attempt {attempt_ref}: {e}")


async def has_completed_any_quiz_before(user_id: int) -> bool:
    """
    🆕 UX: تُستخدم فقط لتحديد ما إذا كان المستخدم قد أنهى أي اختبار من قبل (لتأجيل ظهور
    القائمة الرئيسية عن مستخدم جديد إلى ما بعد أول اختبار كامل له - راجع
    handlers/quiz_runner.py::_handle_quiz_completion). فحص بسيط بـ count("exact")
    بدون جلب صفوف فعلية. أي خطأ هنا يُعامل بتحفّظ كـ "نعم أكمل من قبل" (True) - أي
    نُفضّل عدم إزعاج مستخدم قديم بقائمة إضافية غير متوقعة على خطر إخفاء القائمة عن
    مستخدم جديد فعلاً بسبب عطل مؤقت بالاستعلام.
    """
    try:
        res = await supabase.table("quiz_attempts").select(
            "id", count="exact"
        ).eq("user_id", user_id).eq("is_completed", True).limit(1).execute()
        total = res.count if res.count is not None else len(res.data or [])
        return total > 0
    except Exception as e:
        log_error(logger, f"Error checking prior completed quizzes for user {user_id}: {e}")
        return True


async def mark_quiz_attempt_stopped(attempt_ref: Optional[str]) -> None:
    """تسجيل توقف الطالب المبكر."""
    if not attempt_ref:
        return
    try:
        await supabase.table("quiz_attempts").update({
            "is_completed": False,
            "completed_at": datetime.datetime.now(datetime.timezone.utc).isoformat()
        }).eq("client_ref", attempt_ref).execute()
    except Exception as e:
        log_error(logger, f"Error marking quiz attempt {attempt_ref} as stopped: {e}")


# 4️⃣ دوال الاستعلامات الخاصة بـ لوحة التحكم والإدارة (Admin Analytics Queries)

async def admin_get_usage_overview(days: int = 7) -> Dict[str, Any]:
    """ملخص شامل لسلوك الاستخدام لـ الطلاب حصراً."""
    empty = {
        "days": days, "active_users": 0, "event_counts": {}, "total_attempts": 0,
        "completed_attempts": 0, "completion_rate": 0.0, "avg_duration_seconds": 0,
        "source_breakdown": {}, "avg_score_percentage": 0.0, "error_count": 0, "users_with_errors": 0,
    }
    try:
        since = (datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=days)).isoformat()

        # استعلام الأحداث مع استبعاد الآدمن
        events_query = supabase.table("usage_events").select("user_id, event_type").gte("created_at", since)
        if ADMIN_ID:
            events_query = events_query.neq("user_id", ADMIN_ID)
        events_res = await events_query.execute()
        events = events_res.data or []

        active_users = len({e["user_id"] for e in events})
        event_counts: Dict[str, int] = {}
        for e in events:
            event_counts[e["event_type"]] = event_counts.get(e["event_type"], 0) + 1

        error_events = [e for e in events if e["event_type"] == "error_occurred"]
        error_count = len(error_events)
        users_with_errors = len({e["user_id"] for e in error_events})

        # استعلام المحاولات مع استبعاد الآدمن
        attempts_query = supabase.table("quiz_attempts").select(
            "is_completed, source_type, duration_seconds, score, total_questions"
        ).gte("started_at", since)
        if ADMIN_ID:
            attempts_query = attempts_query.neq("user_id", ADMIN_ID)
        attempts_res = await attempts_query.execute()
        attempts = attempts_res.data or []

        total_attempts = len(attempts)
        completed_attempts = sum(1 for a in attempts if a.get("is_completed"))
        completion_rate = (completed_attempts / total_attempts * 100) if total_attempts else 0.0

        durations = [a["duration_seconds"] for a in attempts if a.get("duration_seconds")]
        avg_duration = (sum(durations) / len(durations)) if durations else 0

        source_breakdown: Dict[str, int] = {}
        for a in attempts:
            src = a.get("source_type") or "unknown"
            source_breakdown[src] = source_breakdown.get(src, 0) + 1

        scored = [a for a in attempts if a.get("total_questions")]
        pct_list = [(a["score"] / a["total_questions"]) * 100 for a in scored if a["total_questions"] > 0]
        avg_score_pct = (sum(pct_list) / len(pct_list)) if pct_list else 0.0

        return {
            "days": days,
            "active_users": active_users,
            "event_counts": event_counts,
            "total_attempts": total_attempts,
            "completed_attempts": completed_attempts,
            "completion_rate": completion_rate,
            "avg_duration_seconds": avg_duration,
            "source_breakdown": source_breakdown,
            "avg_score_percentage": avg_score_pct,
            "error_count": error_count,
            "users_with_errors": users_with_errors,
        }
    except Exception as e:
        log_error(logger, f"Error building usage overview: {e}")
        return empty


async def admin_get_daily_active_users(days: int = 14) -> List[Dict[str, Any]]:
    """عدد المستخدمين النشطين يومياً (استبعاد الآدمن).

    ⚡ بيستعلم مباشرة من الـ View الجاهز `daily_active_users` (توقيت سوريا مطبّق مسبقاً
    داخل الـ View نفسه على مستوى قاعدة البيانات)، بدل جلب كل صفوف usage_events الخام على
    دفعات (pagination) وإعادة حساب اليوم/التجميع يدوياً في بايثون كما كان سابقاً. الـ View
    بيرجع صف واحد لكل (يوم، مستخدم نشط) بعد الدمج، فحجم البيانات المنقولة أصغر بكثير ولا
    داعي لأي pagination عملياً ضمن نطاق الأيام المطلوب.

    تعريف الـ View المتوقّع بقاعدة البيانات (Postgres):
        CREATE OR REPLACE VIEW daily_active_users AS
        SELECT
            ((created_at AT TIME ZONE 'UTC') + INTERVAL '3 hours')::date AS day,
            user_id
        FROM usage_events
        GROUP BY day, user_id;
    """
    try:
        since_day = (
            datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=days)
        ).strftime("%Y-%m-%d")

        query = supabase.table("daily_active_users").select("day, user_id").gte("day", since_day)
        if ADMIN_ID:
            query = query.neq("user_id", ADMIN_ID)
        res = await query.execute()
        rows = res.data or []

        by_day: Dict[str, set] = {}
        for r in rows:
            day = r.get("day")
            if not day:
                continue
            by_day.setdefault(str(day), set()).add(r["user_id"])

        return sorted(
            [{"day": d, "active_users": len(u)} for d, u in by_day.items()],
            key=lambda x: x["day"]
        )
    except Exception as e:
        log_error(logger, f"Error computing daily active users: {e}")
        return []

async def admin_get_user_activity(user_id: int, event_limit: int = 15) -> Dict[str, Any]:
    """سجل نشاط تفصيلي لطالب محدد."""
    empty = {"recent_events": [], "total_attempts": 0, "completed_attempts": 0, "avg_score_percentage": 0.0, "recent_attempts": []}
    try:
        events_res = await supabase.table("usage_events").select("event_type, metadata, created_at") \
            .eq("user_id", user_id).order("created_at", desc=True).limit(event_limit).execute()

        attempts_res = await supabase.table("quiz_attempts").select(
            "source_type, score, total_questions, is_completed, duration_seconds, started_at"
        ).eq("user_id", user_id).order("started_at", desc=True).execute()
        attempts = attempts_res.data or []

        total_attempts = len(attempts)
        completed = sum(1 for a in attempts if a.get("is_completed"))
        scored = [a for a in attempts if a.get("total_questions")]
        pct_list = [(a["score"] / a["total_questions"]) * 100 for a in scored if a["total_questions"] > 0]
        avg_pct = (sum(pct_list) / len(pct_list)) if pct_list else 0.0

        return {
            "recent_events": events_res.data or [],
            "total_attempts": total_attempts,
            "completed_attempts": completed,
            "avg_score_percentage": avg_pct,
            "recent_attempts": attempts[:10],
        }
    except Exception as e:
        log_error(logger, f"Error fetching user activity for {user_id}: {e}")
        return empty


async def admin_get_all_usage_events(limit: int = 5000) -> List[Dict[str, Any]]:
    """جلب سجل الأحداث الخام لـ الطلاب حصراً كملف CSV."""
    try:
        query = supabase.table("usage_events").select("user_id, event_type, metadata, created_at")
        if ADMIN_ID:
            query = query.neq("user_id", ADMIN_ID)
        res = await query.order("created_at", desc=True).limit(limit).execute()
        return res.data or []
    except Exception as e:
        log_error(logger, f"Error exporting usage events: {e}")
        return []


async def admin_get_recent_errors(limit: int = 20, days: int = 7) -> List[Dict[str, Any]]:
    """جلب آخر الأخطاء التي واجهها الطلاب فعلياً (event_type='error_occurred') مع بيانات الطالب."""
    try:
        since = (datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=days)).isoformat()
        query = supabase.table("usage_events") \
            .select("user_id, metadata, created_at") \
            .eq("event_type", "error_occurred") \
            .gte("created_at", since)
        if ADMIN_ID:
            query = query.neq("user_id", ADMIN_ID)
        res = await query.order("created_at", desc=True).limit(limit).execute()
        errors = res.data or []
        if not errors:
            return []

        user_ids = list({e["user_id"] for e in errors if e.get("user_id")})
        users_map = {}
        if user_ids:
            users_res = await supabase.table("users") \
                .select("user_id, first_name, last_name, username") \
                .in_("user_id", user_ids) \
                .execute()
            users_map = {u["user_id"]: u for u in (users_res.data or [])}

        for e in errors:
            e["user"] = users_map.get(e.get("user_id"), {})
            e["time_str"] = format_syria_time(e.get("created_at"), fmt="%I:%M %p (%Y-%m-%d)")

        return errors
    except Exception as e:
        log_error(logger, f"Error fetching recent errors: {e}")
        return []


async def admin_get_quiz_generation_log(limit: int = 200, days: int = 7) -> List[Dict[str, Any]]:
    """🆕 جلب سجل توليد الكويزات (event_type='quiz_generated') لآخر عدة أيام مع بيانات
    الطالب - يغذّي لوحة الأدمن الجديدة "📊 سجل توليد الكويزات" (handlers/admin/ai_control.py):
    لكل كويز مولَّد، الوقت المستغرق (generation_seconds) واسم/مزوّد الموديل المستخدم
    (ai_model/ai_provider) - كلها مُرفقة أصلاً بـ metadata الحدث من handlers/files.py.
    نفس نمط admin_get_recent_errors تماماً (تصفح محلي، صفحة الطالب مُرفقة)."""
    try:
        since = (datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=days)).isoformat()
        query = supabase.table("usage_events") \
            .select("user_id, metadata, created_at") \
            .eq("event_type", "quiz_generated") \
            .gte("created_at", since)
        if ADMIN_ID:
            query = query.neq("user_id", ADMIN_ID)
        res = await query.order("created_at", desc=True).limit(limit).execute()
        rows = res.data or []
        if not rows:
            return []

        user_ids = list({r["user_id"] for r in rows if r.get("user_id")})
        users_map = {}
        if user_ids:
            users_res = await supabase.table("users") \
                .select("user_id, first_name, last_name, username") \
                .in_("user_id", user_ids) \
                .execute()
            users_map = {u["user_id"]: u for u in (users_res.data or [])}

        for r in rows:
            r["user"] = users_map.get(r.get("user_id"), {})
            r["time_str"] = format_syria_time(r.get("created_at"), fmt="%I:%M %p (%Y-%m-%d)")

        return rows
    except Exception as e:
        log_error(logger, f"Error fetching quiz generation log: {e}")
        return []


async def admin_get_referral_leaderboard(limit: int = 30) -> List[Dict[str, Any]]:
    """
    يبني ترتيب الطلاب الذين أحالوا غيرهم (الأكثر إحالة أولاً)، مع القائمة الكاملة لمن
    انضم عن طريق كل واحد منهم (لعرضها كقائمة منفردة عند الطلب، حتى لا تزدحم الواجهة
    الرئيسية بأسماء كل المُحالين دفعة واحدة).

    المصدر: عمود users.referred_by (مصدر رسمي وكامل تاريخياً لكل الإحالات، وليس فقط ما
    بعد إضافة حدث joined_via_referral)، لذا يشمل كل الإحالات القديمة والجديدة.
    """
    try:
        res = await supabase.table("users") \
            .select("user_id, first_name, last_name, username, referred_by") \
            .not_.is_("referred_by", "null") \
            .execute()
        referred_users = res.data or []
        if not referred_users:
            return []

        referrer_ids = list({u["referred_by"] for u in referred_users if u.get("referred_by")})
        referrers_res = await supabase.table("users") \
            .select("user_id, first_name, last_name, username") \
            .in_("user_id", referrer_ids) \
            .execute()
        referrers_map = {u["user_id"]: u for u in (referrers_res.data or [])}

        grouped: Dict[int, List[Dict[str, Any]]] = {}
        for u in referred_users:
            grouped.setdefault(u["referred_by"], []).append(u)

        def _display_name(row: Dict[str, Any]) -> str:
            return f"{row.get('first_name', '')} {row.get('last_name', '')}".strip() or row.get("username") or "غير معروف"

        leaderboard = []
        for ref_id, referred_list in grouped.items():
            referrer_row = referrers_map.get(ref_id, {})
            leaderboard.append({
                "referrer_id": ref_id,
                "referrer_name": _display_name(referrer_row),
                "referrer_username": referrer_row.get("username"),
                "referral_count": len(referred_list),
                "referred_users": [
                    {
                        "user_id": u["user_id"],
                        "name": _display_name(u),
                        "username": u.get("username"),
                    }
                    for u in referred_list
                ],
            })

        leaderboard.sort(key=lambda x: x["referral_count"], reverse=True)
        return leaderboard[:limit]
    except Exception as e:
        log_error(logger, f"Error building referral leaderboard: {e}")
        return []


async def admin_get_today_active_users() -> List[Dict[str, Any]]:
    """جلب قائمة الطلاب النشطين خلال الـ 24 ساعة الأخيرة حصراً (استبعاد الآدمن)."""
    try:
        now_utc = datetime.datetime.now(datetime.timezone.utc)
        twenty_four_hours_ago = (now_utc - datetime.timedelta(hours=24)).isoformat()

        query = supabase.table("usage_events") \
            .select("user_id, event_type, created_at") \
            .gte("created_at", twenty_four_hours_ago)
        if ADMIN_ID:
            query = query.neq("user_id", ADMIN_ID)
            
        res = await query.order("created_at", desc=True).execute()
        rows = res.data or []
        if not rows:
            return []

        latest_event_per_user: Dict[int, Dict[str, Any]] = {}
        for r in rows:
            uid = r["user_id"]
            if uid not in latest_event_per_user:
                latest_event_per_user[uid] = r

        user_ids = list(latest_event_per_user.keys())

        users_res = await supabase.table("users") \
            .select("user_id, first_name, last_name, username") \
            .in_("user_id", user_ids) \
            .execute()

        users_map = {u["user_id"]: u for u in (users_res.data or [])}
        active_list = []

        for uid, ev in latest_event_per_user.items():
            u_info = users_map.get(uid, {})
            syria_dt = to_syria_datetime(ev.get("created_at"))
            time_str = syria_dt.strftime("%I:%M %p (%Y-%m-%d)").replace("AM", "ص").replace("PM", "م") if syria_dt else "غير معروف"

            active_list.append({
                "user_id": uid,
                "first_name": u_info.get("first_name", "طالب"),
                "last_name": u_info.get("last_name", ""),
                "username": u_info.get("username", "Unknown"),
                "last_event": ev["event_type"],
                "time_str": time_str
            })

        return active_list
    except Exception as e:
        log_error(logger, f"Error fetching 24h active users: {e}")
        return []


async def admin_get_today_quizzes() -> List[Dict[str, Any]]:
    """جلب الكويزات المُولَّدة خلال آخر 24 ساعة (نافذة متحركة من اللحظة الحالية للخلف)
    وليس اليوم التقويمي منذ منتصف الليل - كي لا يُفقَد أي نشاط حصل قبل الساعة 12
    صباحاً بتوقيت سوريا (كان يُستثنى بالكامل بالمنطق السابق رغم كونه حديثاً فعلياً)."""
    try:
        now_utc = datetime.datetime.now(datetime.timezone.utc)
        start_of_today_utc = (now_utc - datetime.timedelta(hours=24)).isoformat()

        # 1. جلب كويزات آخر 24 ساعة (باستثناء الآدمن، أسوة ببقية دوال التحليلات)
        query = supabase.table("quizzes") \
            .select("id, source_title, created_at, creator_id") \
            .gte("created_at", start_of_today_utc)
        if ADMIN_ID:
            query = query.neq("creator_id", ADMIN_ID)
        res = await query \
            .order("created_at", desc=True) \
            .execute()
            
        quizzes = res.data or []
        if not quizzes:
            return []

        # 2. جلب بيانات الطلاب المنشئين بطلب آمن (بدون الاعتماد على اسم Foreign Key صريح)
        creator_ids = list({q["creator_id"] for q in quizzes if q.get("creator_id")})
        users_map = {}
        if creator_ids:
            users_res = await supabase.table("users") \
                .select("user_id, username, first_name, last_name") \
                .in_("user_id", creator_ids) \
                .execute()
            users_map = {u["user_id"]: u for u in (users_res.data or [])}

        # 3. دمج البيانات وتحويل التوقيت إلى توقيت سوريا (UTC+3) — بشكل موحّد ومقاوم للأخطاء
        for q in quizzes:
            cid = q.get("creator_id")
            q["users"] = users_map.get(cid, {})
            q["time_str"] = format_syria_time(q.get("created_at"), fmt="%I:%M %p (%Y-%m-%d)")

        return quizzes
    except Exception as e:
        log_error(logger, f"Error fetching today quizzes: {e}")
        return []


USER_QUIZZES_FETCH_CAP = 200  # 🆕 سقف جلب كل مصدر (المُنشأة + المُستخدمة من الكاش) قبل الدمج والتصفح محلياً


async def admin_get_user_quizzes(creator_id: int, limit: int = 5, offset: int = 0) -> tuple[List[Dict[str, Any]], int]:
    """جلب الكويزات الخاصة بطالب محدد مرتبة مع التصفح.

    🆕 تشمل القائمة الآن مصدرين مدموجين:
      1) الكويزات التي أنشأها الطالب فعلياً (quizzes.creator_id).
      2) الكويزات التي "استخدمها" الطالب من الكاش المركزي دون أن ينشئها بنفسه
         (quiz_attempts.source_type == 'cached_file') - أي كويز أنشأه طالب آخر
         لكن تطابق محتواه مع ملف رفعه هذا الطالب فاستُخدم جاهزاً بدل توليد كويز
         جديد. كل عنصر من هذا النوع يحمل is_cached=True حتى يُعلَّم بالعرض، وتاريخه
         هو تاريخ الاستخدام الفعلي (وليس تاريخ إنشاء الكويز الأصلي بواسطة صاحبه).
    الدمج والترتيب والتصفح تتم محلياً (نفس نمط قائمة الإحالات بملف analytics.py)
    لأن المصدرين جدولان مختلفان بتوقيتين مختلفين يصعب دمجهما بأمر SQL واحد بسيط.
    """
    try:
        # 1) الكويزات التي أنشأها الطالب فعلياً
        owned_count_res = await supabase.table("quizzes") \
            .select("id", count="exact") \
            .eq("creator_id", creator_id) \
            .execute()
        owned_total = owned_count_res.count or 0

        owned_res = await supabase.table("quizzes") \
            .select("id, source_title, created_at, likes, dislikes") \
            .eq("creator_id", creator_id) \
            .order("created_at", desc=True) \
            .limit(USER_QUIZZES_FETCH_CAP) \
            .execute()
        owned_items = owned_res.data or []
        for q in owned_items:
            q["is_cached"] = False

        # 2) الكويزات التي استخدمها الطالب من الكاش المركزي
        cache_count_res = await supabase.table("quiz_attempts") \
            .select("id", count="exact") \
            .eq("user_id", creator_id) \
            .eq("source_type", "cached_file") \
            .execute()
        cache_total = cache_count_res.count or 0

        cache_attempts_res = await supabase.table("quiz_attempts") \
            .select("quiz_id, started_at") \
            .eq("user_id", creator_id) \
            .eq("source_type", "cached_file") \
            .order("started_at", desc=True) \
            .limit(USER_QUIZZES_FETCH_CAP) \
            .execute()
        cache_attempts = [a for a in (cache_attempts_res.data or []) if a.get("quiz_id")]

        cache_items: List[Dict[str, Any]] = []
        if cache_attempts:
            quiz_ids = list({a["quiz_id"] for a in cache_attempts})
            quizzes_res = await supabase.table("quizzes") \
                .select("id, source_title, likes, dislikes") \
                .in_("id", quiz_ids) \
                .execute()
            quizzes_map = {q["id"]: q for q in (quizzes_res.data or [])}

            for a in cache_attempts:
                q = quizzes_map.get(a["quiz_id"])
                if not q:
                    continue  # 🩹 الكويز الأصلي حُذف لاحقاً (تنظيف تلقائي دوري) - يُتجاهل بأمان
                cache_items.append({
                    "id": q["id"],
                    "source_title": q.get("source_title"),
                    "created_at": a.get("started_at"),  # تاريخ الاستخدام الفعلي من هذا الطالب
                    "likes": q.get("likes", 0),
                    "dislikes": q.get("dislikes", 0),
                    "is_cached": True,
                })

        # 3) دمج المصدرين وترتيبهما تنازلياً حسب التاريخ الفعلي لظهورهما عند الطالب، ثم تصفّح محلي
        combined = owned_items + cache_items
        combined.sort(key=lambda q: q.get("created_at") or "", reverse=True)

        total = owned_total + cache_total
        page_items = combined[offset:offset + limit]
        return page_items, total
    except Exception as e:
        log_error(logger, f"Error fetching user quizzes for {creator_id}: {e}")
        return [], 0


# 5️⃣ دالة تنظيف البيانات القديمة
async def auto_cleanup_old_analytics_data() -> None:
    """حذف سجلات الأحداث القديمة جداً والسيئة."""
    try:
        thirty_days_ago = (datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=30)).isoformat()
        three_days_ago = (datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=3)).isoformat()
        
        await supabase.table("usage_events").delete().lt("created_at", thirty_days_ago).execute()

        deletable_ids = await _get_safe_to_delete_quiz_ids(three_days_ago)
        if deletable_ids:
            # 🆕 نفس منطق تنظيف تصويتات/تثبيت التصنيف اليتيمة المطبَّق بـ admin_delete_quiz
            # (راجع تعليقها للتفاصيل) - لازم نجلب file_hash *قبل* الحذف الفعلي.
            file_hashes = await _get_file_hashes_for_quiz_ids(deletable_ids)
            await supabase.table("quizzes").delete().in_("id", deletable_ids).execute()
            if file_hashes:
                await _cleanup_classification_for_hashes(file_hashes)

        log_info(logger, f"Automated database cleanup executed successfully. Deleted {len(deletable_ids)} quizzes.")
    except Exception as e:
        log_error(logger, f"Error in database cleanup: {e}")

