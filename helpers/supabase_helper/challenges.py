"""
supabase_helper.challenges
==========================
Repository لبنك اختبارات التحديات (جداول المحتوى فقط - db/migrations/0008).
التصميم: docs/challenges/DESIGN.md. كل الدوال تعمل بـ service_role عبر عميل Supabase المشترك.
لا تُمرَّر الإجابات الصحيحة للواجهة إلا عبر get_test_questions_admin (معاينة الأدمن حصراً).
"""

import datetime
import random
from typing import Any, Dict, List, Optional, Sequence

from ._client import supabase, logger, log_info, log_error, _is_valid_uuid
from services.challenge_content import ParsedTest, to_import_payload

CHALLENGE_FLAG = "challenge_system_enabled"
_TABLES = {"branch": "challenge_branches", "subject": "challenge_subjects", "unit": "challenge_units"}
_PARENT_COL = {"branch": None, "subject": "branch_id", "unit": "subject_id"}
_TEST_COLS = ("id, unit_id, title, status, source_type, source_reference, question_count, "
              "created_by_admin, created_at, updated_at, disabled_at, archived_at")


class ChallengeRepoError(Exception):
    """خطأ مفهوم للأدمن (مثلاً اسم مكرر) - الرسالة جاهزة للعرض."""


def _now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def _is_unique_violation(e: Exception) -> bool:
    s = str(e)
    return "23505" in s or "duplicate key" in s.lower()


# ==================== الهرم: فرع / مادة / وحدة ====================

async def list_nodes(kind: str, parent_id: Optional[str] = None, status: Optional[str] = "active") -> List[Dict[str, Any]]:
    """العقد الفعّالة (غير المؤرشفة) مرتبة بـ sort_order ثم الاسم. kind: branch|subject|unit."""
    q = supabase.table(_TABLES[kind]).select("*")
    if status is not None:
        q = q.eq("status", status)
    if _PARENT_COL[kind]:
        q = q.eq(_PARENT_COL[kind], parent_id)
    res = await q.order("sort_order").order("name").execute()
    return res.data or []


async def get_node(kind: str, node_id: str) -> Optional[Dict[str, Any]]:
    if not _is_valid_uuid(node_id):
        return None
    res = await supabase.table(_TABLES[kind]).select("*").eq("id", node_id).limit(1).execute()
    return res.data[0] if res.data else None


async def create_node(kind: str, name: str, parent_id: Optional[str], admin_id: int) -> Dict[str, Any]:
    name = (name or "").strip()
    if not 1 <= len(name) <= 80:
        raise ChallengeRepoError("الاسم يجب أن يكون بين 1 و80 حرفاً.")
    siblings = await list_nodes(kind, parent_id)
    row: Dict[str, Any] = {"name": name, "sort_order": (max((s["sort_order"] for s in siblings), default=0) + 10)}
    if _PARENT_COL[kind]:
        row[_PARENT_COL[kind]] = parent_id
    try:
        res = await supabase.table(_TABLES[kind]).insert(row).execute()
    except Exception as e:
        if _is_unique_violation(e):
            raise ChallengeRepoError("يوجد عنصر بنفس الاسم في هذا المستوى.") from None
        log_error(logger, f"challenge_{kind}_create failed: {e}")
        raise
    log_info(logger, f"challenge_{kind}_created id={res.data[0]['id']} admin={admin_id}")
    return res.data[0]


async def update_node(kind: str, node_id: str, admin_id: int, name: Optional[str] = None,
                      sort_order: Optional[int] = None) -> bool:
    patch: Dict[str, Any] = {"updated_at": _now()}
    if name is not None:
        name = name.strip()
        if not 1 <= len(name) <= 80:
            raise ChallengeRepoError("الاسم يجب أن يكون بين 1 و80 حرفاً.")
        patch["name"] = name
    if sort_order is not None:
        patch["sort_order"] = int(sort_order)
    try:
        res = await supabase.table(_TABLES[kind]).update(patch).eq("id", node_id).execute()
    except Exception as e:
        if _is_unique_violation(e):
            raise ChallengeRepoError("يوجد عنصر بنفس الاسم في هذا المستوى.") from None
        raise
    log_info(logger, f"challenge_{kind}_updated id={node_id} admin={admin_id}")
    return bool(res.data)


async def archive_node(kind: str, node_id: str, admin_id: int) -> bool:
    """أرشفة (Soft delete). لا cascade: لعبياً يختفي كل ما تحته لأن العرض يشترط أسلافاً فعّالين،
    والاختبارات والأسئلة تبقى كما هي لأي مرجع تاريخي."""
    res = await supabase.table(_TABLES[kind]).update(
        {"status": "archived", "archived_at": _now(), "updated_at": _now()}
    ).eq("id", node_id).eq("status", "active").execute()
    if res.data:
        log_info(logger, f"challenge_{kind}_archived id={node_id} admin={admin_id}")
    return bool(res.data)


# ==================== الاختبارات ====================

async def list_tests(unit_id: str, include_archived: bool = False) -> List[Dict[str, Any]]:
    q = supabase.table("challenge_tests").select(_TEST_COLS).eq("unit_id", unit_id)
    if not include_archived:
        q = q.neq("status", "archived")
    res = await q.order("created_at", desc=True).execute()
    return res.data or []


async def get_test(test_id: str) -> Optional[Dict[str, Any]]:
    if not _is_valid_uuid(test_id):
        return None
    res = await supabase.table("challenge_tests").select(_TEST_COLS).eq("id", test_id).limit(1).execute()
    return res.data[0] if res.data else None


_ALLOWED_TRANSITIONS = {
    "active": {"disabled", "archived"},
    "disabled": {"active", "archived"},
    "draft": {"active", "archived"},
}


async def set_test_status(test_id: str, new_status: str, admin_id: int) -> bool:
    test = await get_test(test_id)
    if not test or new_status not in _ALLOWED_TRANSITIONS.get(test["status"], set()):
        return False
    patch: Dict[str, Any] = {"status": new_status, "updated_at": _now()}
    if new_status == "disabled":
        patch["disabled_at"] = _now()
    elif new_status == "archived":
        patch["archived_at"] = _now()
    else:
        patch["disabled_at"] = None
    # شرط الحالة السابقة يمنع تعارض ضغطتين متزامنتين
    res = await supabase.table("challenge_tests").update(patch).eq("id", test_id).eq("status", test["status"]).execute()
    if res.data:
        event = {"disabled": "challenge_test_disabled", "archived": "challenge_test_archived"}.get(
            new_status, "challenge_test_enabled")
        log_info(logger, f"{event} test_id={test_id} admin={admin_id}")
    return bool(res.data)


async def import_test(unit_id: str, parsed: ParsedTest, admin_id: int) -> Dict[str, Any]:
    """استيراد ذري عبر RPC (اختبار + أسئلته بمعاملة واحدة). يرجع {created, test_id}.
    created=False يعني أن نفس المحتوى موجود أصلاً بالوحدة (idempotent - لا تكرار)."""
    try:
        res = await supabase.rpc("challenge_import_test", {
            "p_unit_id": unit_id, "p_title": parsed.title, "p_source_type": parsed.source_type,
            "p_source_reference": parsed.source_reference, "p_content_hash": parsed.content_hash,
            "p_admin_id": admin_id, "p_questions": to_import_payload(parsed.questions),
            "p_metadata": parsed.metadata,
        }).execute()
    except Exception as e:
        if "unit_not_active" in str(e):
            raise ChallengeRepoError("الوحدة (أو مادتها/فرعها) غير فعّالة.") from None
        log_error(logger, f"challenge_import_test failed unit={unit_id}: {e}")
        raise
    out = res.data if isinstance(res.data, dict) else (res.data or [{}])[0]
    if out.get("created"):
        log_info(logger, f"challenge_test_imported test_id={out.get('test_id')} unit_id={unit_id} "
                         f"source={parsed.source_type} questions={parsed.question_count} admin={admin_id}")
    return out


async def get_test_questions_admin(test_id: str, limit: int = 200) -> List[Dict[str, Any]]:
    """معاينة الأدمن: تتضمن الإجابة الصحيحة والشرح. لا تُستخدم أبداً لواجهة اللاعبين."""
    res = await supabase.table("challenge_questions").select(
        "question_order, question_text, options, correct_option_id, explanation, metadata"
    ).eq("test_id", test_id).order("question_order").limit(limit).execute()
    return res.data or []


async def get_test_questions_public(test_id: str) -> List[Dict[str, Any]]:
    """أسئلة للواجهة بلا الإجابة الصحيحة ولا الشرح (الأعمدة لا تُجلب أصلاً من قاعدة البيانات)."""
    res = await supabase.table("challenge_questions").select(
        "id, question_order, question_text, options, metadata"
    ).eq("test_id", test_id).order("question_order").execute()
    return res.data or []


async def find_quiz_for_import(ref: str) -> Optional[Dict[str, Any]]:
    """يبحث عن كويز أبيض بمعرّفه (UUID) أو share_code. صلاحية الأدمن للوصول لكويزات
    المستخدمين = نفس سياسة الأدمن الحالية (service_role)؛ يُرجع المحتوى دون أي حاجة لبيانات المالك."""
    ref = (ref or "").strip()
    if not ref:
        return None
    cols = "id, source_title, quiz_data, is_math_quiz, subject_type, difficulty"
    if _is_valid_uuid(ref):
        res = await supabase.table("quizzes").select(cols).eq("id", ref).limit(1).execute()
    else:
        res = await supabase.table("quizzes").select(cols).eq("share_code", ref).limit(1).execute()
    return res.data[0] if res.data else None


# ==================== واجهات المراحل التالية (اللعب) ====================

async def _playable_rows(**eq) -> List[Dict[str, Any]]:
    q = supabase.table("challenge_playable_units").select("*")
    for k, v in eq.items():
        q = q.eq(k, v)
    res = await q.execute()
    return res.data or []


def _distinct(rows: Sequence[Dict[str, Any]], id_key: str, name_key: str, order_key: str) -> List[Dict[str, Any]]:
    seen: Dict[str, Dict[str, Any]] = {}
    for r in rows:
        seen.setdefault(r[id_key], {"id": r[id_key], "name": r[name_key], "sort_order": r[order_key]})
    return sorted(seen.values(), key=lambda x: (x["sort_order"], x["name"]))


async def get_playable_branches() -> List[Dict[str, Any]]:
    """الفروع التي فيها وحدة بها اختبار active واحد على الأقل."""
    return _distinct(await _playable_rows(), "branch_id", "branch_name", "branch_order")


async def get_playable_subjects(branch_id: str) -> List[Dict[str, Any]]:
    return _distinct(await _playable_rows(branch_id=branch_id), "subject_id", "subject_name", "subject_order")


async def get_playable_units(subject_id: str) -> List[Dict[str, Any]]:
    rows = await _playable_rows(subject_id=subject_id)
    return sorted(({"id": r["unit_id"], "name": r["unit_name"], "sort_order": r["unit_order"],
                    "active_test_count": r["active_test_count"]} for r in rows),
                  key=lambda x: (x["sort_order"], x["name"]))


async def pick_active_test(unit_id: str, exclude_test_ids: Sequence[str] = ()) -> Optional[Dict[str, Any]]:
    """اختيار اختبار active لوحدة Server-Side بلا ORDER BY RANDOM: نجلب أعمدة خفيفة لاختبارات
    الوحدة الفعّالة (عددها صغير ومحدود بالوحدة) ونختار عشوائياً بالذاكرة، مع تفضيل ما ليس
    ضمن exclude_test_ids (آخر اختبارات لعبها اللاعب). إن لم يبقَ غيرها نعيد أحدها بدل الفراغ.
    يرجع None إن لم يوجد اختبار فعّال (الواجهة تعرض: لا توجد اختبارات متاحة)."""
    res = await supabase.table("challenge_tests").select("id, title, question_count") \
        .eq("unit_id", unit_id).eq("status", "active").gt("question_count", 0).execute()
    candidates = res.data or []
    if not candidates:
        return None
    excluded = set(exclude_test_ids)
    fresh = [t for t in candidates if t["id"] not in excluded]
    return random.choice(fresh or candidates)


async def restore_branch(branch_id: str, admin_id: int) -> bool:
    try:
        res = await supabase.table("challenge_branches").update(
            {"status": "active", "archived_at": None, "updated_at": _now()}
        ).eq("id", branch_id).eq("status", "archived").execute()
    except Exception as exc:
        if _is_unique_violation(exc):
            raise ChallengeRepoError("يوجد فرع فعّال بنفس الاسم. غيّر اسم الفرع المؤرشف أولاً ثم أعد الاستعادة.") from None
        raise
    log_info(logger, f"challenge_branch_restored id={branch_id} admin={admin_id}")
    return bool(res.data)


async def delete_archived_branch(branch_id: str, admin_id: int) -> bool:
    try:
        res = await supabase.rpc("challenge_delete_archived_branch", {"p_branch": branch_id}).execute()
    except Exception as exc:
        if "23503" in str(exc) or "foreign key" in str(exc).lower():
            raise ChallengeRepoError("لا يمكن الحذف النهائي: الفرع أو محتواه مرتبط بطلاب أو أراضٍ أو مباريات. لم يُحذف أي شيء؛ يمكنك إبقاؤه مؤرشفاً.") from None
        if "branch_not_archived" in str(exc):
            raise ChallengeRepoError("يجب أرشفة الفرع قبل حذفه نهائياً.") from None
        raise
    log_info(logger, f"challenge_branch_deleted id={branch_id} admin={admin_id}")
    return bool(res.data)
