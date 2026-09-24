"""
supabase_helper.sharing
=========================
Shared Quiz Operations (Deep Linking): توليد/حفظ/قراءة روابط مشاركة الكويز.
"""

import uuid
from typing import Optional, Dict, List, Any

from ._client import supabase, logger, log_error, log_info, _is_valid_uuid

# ==================== Shared Quiz Operations (Deep Linking) ====================
def create_shared_quiz_id() -> str:
    return uuid.uuid4().hex[:12]

async def save_shared_quiz(share_id: str, owner_id: int, title: str, quiz_data: List[Dict[str, Any]], quiz_id: Optional[str] = None) -> Optional[str]:
    """تفعيل ميزة المشاركة بدمج كود الرابط مباشرة بالخلية المركزية لمنع تكرار الـ JSONB وهدر المساحة.

    🩹 FIX (رابط المشاركة يظهر "منتهي الصلاحية" رغم أنه لم ينتهِ فعلياً):
    كل كويز مركزي له عمود واحد فقط (share_code)، وكانت الدالة تكتب share_id الجديد
    فوقه مباشرة بلا أي تحقق في كل مرة تُستدعى. بما أن _start_loaded_quiz() تصفّر
    share_id بالـ state إلى None عند كل بدء/إعادة تشغيل للكويز (quiz_runner.py)،
    كانت أي إعادة مشاركة لاحقة لنفس الكويز (حتى بجلسة جديدة) تُولّد كوداً عشوائياً
    جديداً وتكتبه فوق القديم مباشرة - فتُبطل فوراً كل رابط قديم كان موزَّعاً مسبقاً
    لنفس الكويز، رغم أنه لسا صالحاً منطقياً.
    الحل: قبل أي كتابة، نتحقق أولاً إن كان للكويز أصلاً share_code محفوظ بقاعدة
    البيانات، ولو موجود نعيد استخدامه بدل الكتابة فوقه (الرابط يضل ثابتاً طالما
    الكويز نفسه لم يُحذف). لهذا صار الرجوع الآن بالكود الفعلي المستخدم (str) بدل
    bool - قد يكون الكود القديم المُعاد استخدامه، وليس بالضرورة share_id الممرَّر -
    والمتصل (caller) لازم يستخدم القيمة المُرجعة تحديداً ببناء رابط المشاركة وتخزينها
    بالـ state، وإلا الرابط المعروض للمستخدم بيصير غلط (راجع sharing.py).
    يرجع None عند فشل العملية بدل False.
    """
    try:
        target_id = None

        # 🆕 الأولوية دائماً لمعرف الكويز الحقيقي (UUID) القادم من جلسة المستخدم الحالية،
        # لأن المطابقة بالعنوان وحده غير موثوقة عند تكرار العناوين (مثلاً "كويز من ملف")
        # وقد تحقن رمز المشاركة بصف كويز مختلف تماماً عن اللي المستخدم يقصده فعلاً.
        if _is_valid_uuid(quiz_id):
            target_id = quiz_id
        else:
            # مسار احتياطي فقط لو ما توفر quiz_id صالح (مثلاً كويز نصي قديم بدون سجل مركزي بعد)
            check_res = await supabase.table("quizzes").select("id").eq("creator_id", owner_id).eq("source_title", title).order("created_at", desc=True).limit(1).execute()
            if check_res.data:
                target_id = check_res.data[0]["id"]

        if target_id:
            # 🩹 تحقق من كود مشاركة موجود مسبقاً على نفس الصف قبل أي كتابة فوقه
            existing_res = await supabase.table("quizzes").select("share_code").eq("id", target_id).execute()
            existing_code = existing_res.data[0].get("share_code") if existing_res.data else None
            if existing_code:
                log_info(logger, f"Reusing existing share code {existing_code} for quiz {target_id} instead of overwriting with {share_id}")
                return existing_code

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
        return share_id
    except Exception as e:
        log_error(logger, f"Error linking shared quiz code: {e}")
        return None

async def get_shared_quiz(share_id: str) -> Optional[Dict[str, Any]]:
    try:
        res = await supabase.table("quizzes").select("*").eq("share_code", share_id).execute()
        if res.data:
            return res.data[0]
        return None
    except Exception as e:
        log_error(logger, f"Error loading shared quiz from code: {e}")
        return None

