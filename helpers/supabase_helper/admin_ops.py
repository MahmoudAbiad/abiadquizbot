"""
supabase_helper.admin_ops
============================
Admin Operations: إضافة نقاط يدوياً، منح هدايا مباشرة، إحصائيات عامة، البحث عن مستخدم،
وخيارات حذف المستخدمين وبياناتهم.
"""

from datetime import datetime, timedelta, timezone
from typing import Optional, Dict, List, Any, Tuple
import uuid

from ._client import supabase, logger
from .gifts import enrich_balances, gift_balances
from .classification import _get_file_hashes_for_quiz_ids, _cleanup_classification_for_hashes

# حساب الاختبار المعتمد (QA) المسموح بتصفير موافقته القانونية منفردة دون حذف الحساب،
# وذلك لحماية حجية سجل الموافقات القانونية (user_consent_events) لجميع الطلاب الفعليين.
QA_TEST_USER_IDS: Tuple[int, ...] = (8959792818,)
QA_TEST_USERNAMES: Tuple[str, ...] = ("kp7771a",)


def is_qa_test_user(user_or_username: Any, user_id: Optional[int] = None) -> bool:
    """
    التحقق مما إذا كان الحساب هو حساب الاختبار المعتمد (@Kp7771a / ID: 8959792818).
    يُحظر تصفير سجل الموافقة القانونية منفرداً (consent_only) لأي حساب آخر غير حساب الاختبار.
    """
    uid = user_id
    raw_username = ""
    if isinstance(user_or_username, dict):
        if uid is None and user_or_username.get("user_id") is not None:
            try:
                uid = int(user_or_username["user_id"])
            except (ValueError, TypeError):
                uid = None
        raw_username = str(user_or_username.get("username") or "")
    elif isinstance(user_or_username, int):
        uid = user_or_username
    else:
        raw_username = str(user_or_username or "")

    if uid is not None:
        try:
            if int(uid) in QA_TEST_USER_IDS:
                return True
        except (ValueError, TypeError):
            pass

    clean = raw_username.strip().lstrip("@").lower()
    if not clean or clean == "unknown":
        return False
    return clean in QA_TEST_USERNAMES


# ==================== Admin Operations ====================
async def admin_add_points(target_id: int, amount: int, balance_type: str = "paid") -> Optional[int]:
    try:
        if balance_type not in ("free", "paid"):
            return None
        # 🆕 يُنفَّذ عبر RPC ذري (UPDATE ... SET x_points = x_points + amount) بدل
        # قراءة الرصيد ثم كتابته من بايثون - نفس نمط award_referral_bonus_atomic /
        # refund_user_points_atomic. القراءة-ثم-الكتابة كانت عرضة لفقدان تحديثات
        # (lost update) لو أضاف الأدمن نقاطاً بنفس اللحظة اللي فيها البوت بيخصم
        # نقاط من نفس المستخدم (توليد كويز) أو بيسترجعها له.
        rpc_response = await supabase.rpc("admin_add_points_atomic", {
            "target_user_id": target_id,
            "amount": float(amount),
            "balance_type": balance_type,
        }).execute()
        if rpc_response.data:
            row = rpc_response.data[0] if isinstance(rpc_response.data, list) else rpc_response.data
            gifts = await gift_balances([target_id])
            return float(row["total_points"]) + gifts.get(target_id, 0)
        return None
    except Exception as e:
        logger.error(f"Error in admin_add_points: {e}")
        return None


async def admin_grant_direct_gift(
    target_id: int,
    amount: float,
    valid_days: int = 30,
    title: str = "هدية مستخدم جديد",
    source_key: Optional[str] = None,
) -> Optional[Dict]:
    """منح هدية نقاط مباشرة لمستخدم محدد عبر RPC ذري مع مسار احتياطي مباشر."""
    try:
        clean_amount = round(float(amount), 2)
        clean_days = int(valid_days)
        clean_title = (title or "هدية من الإدارة").strip()[:100]
        if clean_amount <= 0 or clean_amount > 1000000 or clean_days < 0 or clean_days > 3650 or not clean_title:
            return None
        key = (source_key or f"direct:{uuid.uuid4()}").strip()[:120]
        try:
            rpc_response = await supabase.rpc("admin_grant_user_gift", {
                "p_user_id": int(target_id),
                "p_source_key": key,
                "p_title": clean_title,
                "p_points": clean_amount,
                "p_valid_days": clean_days,
            }).execute()
            data = rpc_response.data
            if isinstance(data, list) and data:
                data = data[0]
            if isinstance(data, dict) and data.get("status") == "ok":
                return {
                    "status": "ok",
                    "gift_id": data.get("gift_id"),
                    "title": data.get("title") or clean_title,
                    "points": float(data.get("points", clean_amount)),
                    "expires_at": data.get("expires_at"),
                    "gift_points": float(data.get("gift_points", 0)),
                    "total_points": float(data.get("total_points", 0)),
                }
            if isinstance(data, dict) and data.get("status") == "missing_user":
                return None
        except Exception as rpc_exc:
            if str(getattr(rpc_exc, "code", "")) not in {"42883", "PGRST202"}:
                raise
            now_utc = datetime.now(timezone.utc)
            expires_at = None if clean_days == 0 else (now_utc + timedelta(days=clean_days)).isoformat()
            ins = await supabase.table("point_gifts").insert({
                "user_id": int(target_id),
                "source": "admin",
                "source_key": key,
                "title": clean_title,
                "original_amount": clean_amount,
                "remaining_amount": clean_amount,
                "expires_at": expires_at,
            }).execute()
            gift_row = (ins.data or [{}])[0]
            user_res = await supabase.table("users").select("free_points, paid_points").eq("user_id", int(target_id)).limit(1).execute()
            if not user_res.data:
                return None
            u_row = user_res.data[0]
            gifts_map = await gift_balances([int(target_id)])
            gift_pts = float(gifts_map.get(int(target_id), 0.0))
            total_pts = float(u_row.get("free_points") or 0) + float(u_row.get("paid_points") or 0) + gift_pts
            return {
                "status": "ok",
                "gift_id": gift_row.get("id"),
                "title": clean_title,
                "points": clean_amount,
                "expires_at": gift_row.get("expires_at", expires_at),
                "gift_points": gift_pts,
                "total_points": total_pts,
            }
        return None
    except Exception as e:
        logger.error(f"Error in admin_grant_direct_gift: {e}")
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
        return await enrich_balances(res.data or [])
    except Exception as e:
        return None


def _admin_users_query(query: str = ""):
    request = supabase.table("users").select("*", count="exact")
    query = query.strip().lstrip("@")
    if query:
        if query.isdigit():
            request = request.eq("user_id", int(query))
        else:
            # Telegram underscores are literal username characters, not SQL wildcards.
            escaped = query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            request = request.ilike("username", f"%{escaped}%")
    return request.order("joined_at", desc=True).order("user_id", desc=True)


async def admin_get_users_page(page: int = 1, query: str = "", page_size: int = 5):
    """Fetch one database page with an exact count; never preload the full registry."""
    page = max(1, int(page))
    page_size = max(1, min(int(page_size), 100))
    start = (page - 1) * page_size
    response = await _admin_users_query(query).range(start, start + page_size - 1).execute()
    total = response.count or 0
    pages = max(1, (total + page_size - 1) // page_size)
    if page > pages:
        return await admin_get_users_page(pages, query, page_size)
    return await enrich_balances(response.data or []), total, page, pages


async def admin_get_user_deletion_summary(target_id: int) -> Optional[Dict[str, Any]]:
    """جلب ملخص شامل عن المستخدم قبل الحذف: بيانات الحساب، عدد الكويزات المولدة عن طريقه، وحالة الموافقة القانونية."""
    try:
        uid = int(target_id)
        users = await admin_search_user(str(uid))
        user_row = users[0] if users else None

        owned_count = 0
        try:
            q_res = await supabase.table("quizzes").select("id", count="exact").eq("creator_id", uid).execute()
            owned_count = int(q_res.count or 0)
        except Exception as exc:
            logger.warning(f"Could not count owned quizzes for user {uid}: {exc}")

        consent_row = None
        try:
            c_res = await (
                supabase.table("user_consents")
                .select("status, terms_version, privacy_version, first_prompted_at, decided_at, updated_at")
                .eq("user_id", uid)
                .limit(1)
                .execute()
            )
            if c_res.data:
                consent_row = c_res.data[0]
        except Exception as exc:
            logger.warning(f"Could not fetch consent status for user {uid}: {exc}")

        if not user_row and owned_count == 0 and not consent_row:
            return None

        return {
            "user": user_row,
            "owned_quizzes_count": owned_count,
            "consent": consent_row,
            "is_test_account": is_qa_test_user(user_row, user_id=uid),
        }
    except Exception as e:
        logger.error(f"Error in admin_get_user_deletion_summary for {target_id}: {e}")
        return None


async def admin_get_user_generated_quizzes(
    creator_id: int, limit: int = 5, offset: int = 0
) -> Tuple[List[Dict[str, Any]], int]:
    """جلب الكويزات التي أنشأها هذا الطالب فعلياً فقط (creator_id) لاستعراضها قبل الحذف."""
    try:
        uid = int(creator_id)
        count_res = await supabase.table("quizzes").select("id", count="exact").eq("creator_id", uid).execute()
        total = int(count_res.count or 0)
        if total == 0:
            return [], 0

        res = await (
            supabase.table("quizzes")
            .select("id, source_title, created_at, likes, dislikes, difficulty, question_type_label, quiz_data")
            .eq("creator_id", uid)
            .order("created_at", desc=True)
            .range(offset, offset + limit - 1)
            .execute()
        )
        rows = res.data or []
        for r in rows:
            qdata = r.get("quiz_data")
            r["questions_len"] = len(qdata) if isinstance(qdata, list) else 0
            r.pop("quiz_data", None)
        return rows, total
    except Exception as e:
        logger.error(f"Error in admin_get_user_generated_quizzes for {creator_id}: {e}")
        return [], 0


async def admin_delete_user_action(target_id: int, mode: str) -> Optional[Dict[str, Any]]:
    """
    تنفيذ أحد خيارات الحذف الأربعة للمستخدم:
      - 'full_wipe'    : حذف المستخدم بالكامل مع كامل أثره وكويزاته وموافقته على البوت.
      - 'keep_quizzes' : حذف المستخدم بالكامل مع كامل أثره وموافقته، مع الإبقاء على الكويزات المولدة عن طريقه (creator_id = NULL).
      - 'consent_only' : حذف حالة وسجل موافقته على البوت فقط (مقيّد قانونياً بحساب الاختبار @Kp7771a / 8959792818 فقط).
      - 'quizzes_only' : حذف الكويزات المولدة عن طريقه فقط.
    """
    if mode not in ("full_wipe", "keep_quizzes", "consent_only", "quizzes_only"):
        return None
    uid = int(target_id)
    try:
        try:
            rpc_res = await supabase.rpc("admin_delete_user_action", {
                "p_user_id": uid,
                "p_mode": mode,
            }).execute()
            data = rpc_res.data
            if isinstance(data, list) and data:
                data = data[0]
            if isinstance(data, dict):
                if data.get("status") == "forbidden_non_test_account":
                    return data
                if data.get("status") == "ok":
                    deleted_ids = data.get("deleted_quiz_ids") or []
                    if deleted_ids:
                        try:
                            from services.runner_cleanup import delete_runner_prefix
                            for qid in deleted_ids:
                                await delete_runner_prefix(str(qid))
                        except Exception:
                            logger.warning("Runner prefix cleanup deferred to retention loop", exc_info=True)
                    return data
        except Exception as rpc_exc:
            if str(getattr(rpc_exc, "code", "")) not in {"42883", "PGRST202"}:
                raise
            # مسار احتياطي مباشر في حال عدم توفر الـ RPC ببيئة اختبار معزولة
            deleted_quiz_ids: List[str] = []
            kept_quizzes_count = 0

            if mode == "consent_only":
                u_res = await supabase.table("users").select("user_id, username").eq("user_id", uid).limit(1).execute()
                u_row = (u_res.data or [{}])[0]
                if not is_qa_test_user(u_row, user_id=uid):
                    return {
                        "status": "forbidden_non_test_account",
                        "mode": mode,
                        "consent_deleted": False,
                        "deleted_quizzes_count": 0,
                        "kept_quizzes_count": 0,
                        "deleted_quiz_ids": [],
                    }
                await supabase.table("user_consent_events").delete().eq("user_id", uid).execute()
                await supabase.table("user_consents").delete().eq("user_id", uid).execute()
                return {
                    "status": "ok",
                    "mode": mode,
                    "consent_deleted": True,
                    "deleted_quizzes_count": 0,
                    "kept_quizzes_count": 0,
                    "deleted_quiz_ids": [],
                }

            if mode in ("quizzes_only", "full_wipe"):
                q_res = await supabase.table("quizzes").select("id, file_hash").eq("creator_id", uid).execute()
                q_rows = q_res.data or []
                deleted_quiz_ids = [str(r["id"]) for r in q_rows if r.get("id")]
                file_hashes = list({r["file_hash"] for r in q_rows if r.get("file_hash")})
                if deleted_quiz_ids:
                    await supabase.table("quizzes").delete().eq("creator_id", uid).execute()
                    if file_hashes:
                        await _cleanup_classification_for_hashes(file_hashes)
                    try:
                        from services.runner_cleanup import delete_runner_prefix
                        for qid in deleted_quiz_ids:
                            await delete_runner_prefix(str(qid))
                    except Exception:
                        pass
                if mode == "quizzes_only":
                    return {
                        "status": "ok",
                        "mode": mode,
                        "consent_deleted": False,
                        "deleted_quizzes_count": len(deleted_quiz_ids),
                        "kept_quizzes_count": 0,
                        "deleted_quiz_ids": deleted_quiz_ids,
                    }

            if mode == "keep_quizzes":
                q_res = await supabase.table("quizzes").select("id", count="exact").eq("creator_id", uid).execute()
                kept_quizzes_count = int(q_res.count or 0)
                if kept_quizzes_count > 0:
                    await supabase.table("quizzes").update({"creator_id": None}).eq("creator_id", uid).execute()

            await supabase.table("user_consent_events").delete().eq("user_id", uid).execute()
            await supabase.table("user_consents").delete().eq("user_id", uid).execute()
            for tbl in (
                "usage_events", "ai_generation_log", "classification_votes",
                "daily_renewal_deliveries", "gift_code_redemptions",
                "admin_gift_recipients", "payment_orders", "group_quiz_answers",
                "group_quiz_participants", "quiz_attempts", "quiz_votes", "logs",
            ):
                try:
                    await supabase.table(tbl).delete().eq("user_id", uid).execute()
                except Exception:
                    pass
            await supabase.table("users").delete().eq("user_id", uid).execute()
            return {
                "status": "ok",
                "mode": mode,
                "consent_deleted": True,
                "deleted_quizzes_count": len(deleted_quiz_ids),
                "kept_quizzes_count": kept_quizzes_count,
                "deleted_quiz_ids": deleted_quiz_ids,
            }
        return None
    except Exception as e:
        logger.error(f"Error in admin_delete_user_action({target_id}, {mode}): {e}")
        return None
