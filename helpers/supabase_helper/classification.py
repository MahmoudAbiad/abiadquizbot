"""
supabase_helper.classification
=================================
🆕 التحقق المجتمعي من تصنيف المادة: تصويت الطلاب على تصنيف الملف، تثبيت
التصنيف، وتنظيف الكويزات/التصنيفات اليتيمة السيئة.
"""

import datetime
from typing import Optional, Dict, List, Any

from ._client import supabase, logger, log_error, log_info
from settings_helper import get_setting, SETTING_MIN_VALUES

# ==================== 🆕 التحقق المجتمعي من تصنيف المادة ====================
# راجع migration_classification_votes.sql للمخطط الكامل + services/subject_classifier.py
# لمنطق قراءة التثبيت أولاً قبل أي كاش Redis أو استدعاء AI جديد.

async def get_classification_lock(file_hash: str) -> Optional[Dict[str, Any]]:
    """يرجع صف التثبيت الدائم لهذا الملف إن وُجد (تصنيف تحقق منه 3 طلاب مختلفين على
    الأقل)، وإلا None. فشل الاتصال بـ Supabase لا يوقف تدفق التصنيف العادي - يُعامل
    كـ"غير مثبّت بعد" فقط، ويكمل classify_subject مساره الطبيعي (فشل آمن)."""
    if not file_hash:
        return None
    try:
        res = await supabase.table("classification_locks").select(
            "classification_data"
        ).eq("file_hash", file_hash).limit(1).execute()
        return res.data[0] if res.data else None
    except Exception as e:
        log_error(logger, f"Error fetching classification lock for {file_hash}: {e}")
        return None


async def has_user_voted_on_classification(file_hash: str, user_id: int) -> bool:
    """يتحقق هل صوّت هذا المستخدم بالذات (نعم/لا) على تصنيف هذا الملف مسبقاً - يُستخدم
    *قبل* إرسال رسالة التصويت لتجنّب عرضها مجدداً على طالب أجاب عليها فعلاً (سواء "نعم"
    أو "لا") في مرة سابقة (مثلاً لو رفع نفس الملف أكثر من مرة قبل وصول التصنيف للتثبيت
    النهائي). فشل الاتصال بـ Supabase يُعامل كـ"لم يصوّت بعد" (فشل آمن - أسوأ حالة ممكنة
    هي عرض السؤال مجدداً استثنائياً، لا كسر بالتدفق)."""
    if not file_hash or not user_id:
        return False
    try:
        res = await supabase.table("classification_votes").select(
            "id"
        ).eq("file_hash", file_hash).eq("user_id", user_id).limit(1).execute()
        return bool(res.data)
    except Exception as e:
        log_error(logger, f"Error checking existing classification vote for {file_hash}/{user_id}: {e}")
        return False



async def submit_classification_vote(
    file_hash: str, user_id: int, vote: str, subject: str, classification_data: Dict[str, Any],
) -> Dict[str, Any]:
    """يسجّل صوت طالب واحد ("yes"/"no") على تصنيف مادة ملف معيّن عبر الـ RPC الذرّية
    (تمنع تكرار نفس المستخدم على نفس الملف وتحسب/تثبّت لحظياً بضربة واحدة، بلا Race
    Condition لو صوّت عدة طلاب بنفس اللحظة تقريباً). ترجع dict بمفاتيح duplicate/locked_now/
    yes_count؛ عند أي خطأ اتصال ترجع duplicate=False, locked_now=False (فشل آمن - التصويت
    لم يُسجَّل لكن التدفق العام للبوت يكمل بدون كسر)."""
    try:
        # 🩹 تُقرأ الآن من app_settings (عبر get_setting) بدل ثابت منفصل بـ constants.py
        # كان بلا أي ربط فعلي بـ p_threshold الافتراضية بدالة SQL - راجع
        # migration_classification_vote_threshold_setting.sql. تُمرَّر صراحة هنا فتصبح
        # app_settings مصدر الحقيقة الوحيد الفعلي، قابلاً للتعديل من لوحة الأدمن مباشرة.
        threshold = await get_setting("classification_vote_threshold")
        min_threshold = SETTING_MIN_VALUES.get("classification_vote_threshold", 1)
        res = await supabase.rpc("vote_on_classification", {
            "p_file_hash": file_hash,
            "p_user_id": user_id,
            "p_vote": vote,
            "p_subject": subject,
            "p_classification_data": classification_data,
            "p_threshold": int(max(threshold, min_threshold)),
        }).execute()
        return res.data or {"duplicate": False, "locked_now": False, "yes_count": 0}
    except Exception as e:
        log_error(logger, f"Error executing classification atomic vote function: {e}")
        return {"duplicate": False, "locked_now": False, "yes_count": 0}



async def _get_file_hashes_for_quiz_ids(quiz_ids: List[str]) -> List[str]:
    """🆕 يجلب قائمة file_hash الفريدة لمجموعة IDs كويزات - يُستدعى دائماً *قبل* الحذف
    الفعلي لتلك الكويزات (بعد الحذف الصفوف تختفي ولا يمكن معرفة الـ hash الخاص بها)."""
    if not quiz_ids:
        return []
    try:
        hash_rows = await supabase.table("quizzes").select("file_hash").in_("id", quiz_ids).execute()
        return list({r["file_hash"] for r in (hash_rows.data or []) if r.get("file_hash")})
    except Exception as e:
        log_error(logger, f"Error fetching file_hashes for quiz ids before deletion: {e}")
        return []


async def _cleanup_classification_for_hashes(file_hashes: List[str]) -> None:
    """🆕 لكل file_hash بالقائمة: يتحقق هل بقي أي كويز آخر يستخدمه، وإن لم يبقَ أي كويز
    (يعني آخر نسخة منه انحذفت للتو) يحذف صفوف classification_votes/classification_locks
    المرتبطة به. يُستدعى دائماً *بعد* تنفيذ حذف الكويزات فعلياً."""
    for file_hash in file_hashes:
        try:
            remaining = await supabase.table("quizzes") \
                .select("id", count="exact") \
                .eq("file_hash", file_hash) \
                .limit(1) \
                .execute()
            if not (remaining.count or 0):
                await supabase.table("classification_votes").delete().eq("file_hash", file_hash).execute()
                await supabase.table("classification_locks").delete().eq("file_hash", file_hash).execute()
        except Exception as e:
            log_error(logger, f"Error cleaning up classification data for file_hash {file_hash}: {e}")


async def _get_safe_to_delete_quiz_ids(threshold: str) -> List[str]:
    """يرجع فقط IDs الكويزات المؤهلة للحذف الفعلي: قديمة + سيئة التقييم،
    وبنفس الوقت ماإلها share_code (مو مشاركة)، مو محفوظة بمفضلة أي مستخدم،
    وما حدا رجع لعبها أبداً (ما إلها أي صف بجدول quiz_scores).
    هيك منتجنب خرق foreign key constraint (quiz_scores_quiz_id_fkey) ومنحافظ
    على أي كويز عندو قيمة فعلية (مشاركة/مفضلة/استخدام)."""
    try:
        candidates_res = await supabase.table("quizzes") \
            .select("id") \
            .lt("created_at", threshold) \
            .lt("score", 0) \
            .is_("share_code", "null") \
            .execute()
        candidate_ids = [q["id"] for q in (candidates_res.data or [])]
        if not candidate_ids:
            return []

        fav_res = await supabase.table("favorite_quizzes") \
            .select("quiz_id") \
            .in_("quiz_id", candidate_ids) \
            .execute()
        favorited_ids = {r["quiz_id"] for r in (fav_res.data or [])}

        scores_res = await supabase.table("quiz_scores") \
            .select("quiz_id") \
            .in_("quiz_id", candidate_ids) \
            .execute()
        used_ids = {r["quiz_id"] for r in (scores_res.data or [])}

        return [qid for qid in candidate_ids if qid not in favorited_ids and qid not in used_ids]
    except Exception as e:
        log_error(logger, f"Error resolving safe-to-delete quiz ids: {e}")
        return []


async def auto_cleanup_bad_quizzes():
    """تنظيف تلقائي شامل للكويزات المرفوضة من الطلاب (ديسلايكات عالية) والتي تجاوزت 48 ساعة،
    باستثناء أي كويز عندو share_code أو محفوظ بالمفضلة أو تم استخدامه ولو مرة."""
    try:
        threshold = (datetime.datetime.utcnow() - datetime.timedelta(days=2)).isoformat()
        deletable_ids = await _get_safe_to_delete_quiz_ids(threshold)
        if deletable_ids:
            # 🆕 نفس منطق تنظيف تصويتات/تثبيت التصنيف اليتيمة المطبَّق بـ admin_delete_quiz
            # (راجع تعليقها للتفاصيل) - لازم نجلب file_hash *قبل* الحذف الفعلي.
            file_hashes = await _get_file_hashes_for_quiz_ids(deletable_ids)
            await supabase.table("quizzes").delete().in_("id", deletable_ids).execute()
            if file_hashes:
                await _cleanup_classification_for_hashes(file_hashes)
        log_info(logger, f"Automated database garbage cleanup loop executed successfully. Deleted {len(deletable_ids)} quizzes.")
    except Exception as e:
        log_error(logger, f"Error running the background auto cleanup query: {e}")

