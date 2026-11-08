"""
supabase_helper.export_profiles
================================
إعدادات التصدير الدائمة لكل مستخدم (جدول export_profiles، صف واحد لكل مستخدم).

نخزّن فقط إعدادات المعلم (services.export_options.PROFILE_FIELDS) لا إعدادات كويز بعينه.
كل الدوال آمنة الفشل: خطأ أو جدول غير موجود => None/False، والبوت يكمل بإعدادات الجلسة.
"""

from datetime import datetime, timezone
from typing import Any, Dict, Optional

from ._client import supabase, logger, log_error

_TABLE = "export_profiles"


async def get_export_profile(user_id: int) -> Optional[Dict[str, Any]]:
    """يُرجع dict الإعدادات المحفوظة، أو None إن لم توجد أو تعذّرت القراءة."""
    try:
        res = await supabase.table(_TABLE).select("profile").eq("user_id", user_id).limit(1).execute()
        rows = res.data or []
        profile = rows[0].get("profile") if rows else None
        return profile if isinstance(profile, dict) and profile else None
    except Exception as e:
        log_error(logger, f"Error loading export profile for {user_id}: {e}")
        return None


async def save_export_profile(user_id: int, profile: Dict[str, Any]) -> bool:
    """يحفظ (upsert) إعدادات المعلم. profile فارغ = حذف الصف (العودة للافتراضي). True عند النجاح."""
    try:
        if not profile:
            await supabase.table(_TABLE).delete().eq("user_id", user_id).execute()
            return True
        await supabase.table(_TABLE).upsert({
            "user_id": user_id,
            "profile": profile,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }, on_conflict="user_id").execute()
        return True
    except Exception as e:
        log_error(logger, f"Error saving export profile for {user_id}: {e}")
        return False
