"""
supabase_helper.storage_audio
================================
🆕 Audio Web Upload (Telegram Mini App): روابط رفع/تحميل/حذف الملفات
الصوتية المؤقتة بـ Supabase Storage (bucket: audio-temp).
"""

import datetime
import uuid
from typing import Optional, Dict

from ._client import supabase, logger, log_error, log_warning

# ==============================================================================
# 🆕 Audio Web Upload (Telegram Mini App) - دوال التخزين المؤقت بـ Supabase Storage
# يستخدم نفس عميل supabase المُهيّأ أصلاً أعلى الملف (نفس نمط QUIZ_IMAGES_BUCKET)
# ==============================================================================

from constants import AUDIO_UPLOAD_BUCKET


async def create_audio_upload_target(user_id: int, file_extension: str = "") -> Optional[Dict[str, str]]:
    """
    يولّد مسار فريد ورابط رفع موقّع (signed upload URL) لملف صوتي مؤقت بـ bucket
    خاص (audio-temp). المسار مُصاغ بـ {user_id}/{uuid}{ext} حتى يسهل تتبعه أو حذفه
    يدوياً لو احتجنا (مثال: تنظيف طارئ لكل ملفات مستخدم معيّن).

    Returns:
        {"path": ..., "signed_url": ..., "token": ...} عند النجاح، أو None عند الفشل
        (مثال: الـ bucket غير موجود بعد بمشروع Supabase - يجب إنشاؤه يدوياً من لوحة
        التحكم أو عبر migration، راجع ملاحظة "خطوات الإعداد" أسفل الملف).
    """
    try:
        ext = file_extension if file_extension.startswith(".") else f".{file_extension}" if file_extension else ""
        object_path = f"{user_id}/{uuid.uuid4().hex}{ext}"
        result = await supabase.storage.from_(AUDIO_UPLOAD_BUCKET).create_signed_upload_url(object_path)
        # شكل الاستجابة حسب supabase-py 2.31.0 (تحقّق فعلي من الكود المصدري):
        # {"signed_url": ..., "signedUrl": ..., "token": ..., "path": ...}
        return {
            "path": result.get("path", object_path),
            "signed_url": result.get("signed_url") or result.get("signedUrl"),
            "token": result.get("token"),
        }
    except Exception as e:
        log_error(logger, f"Could not create signed audio upload URL: {e}")
        return None


async def get_audio_temp_object_size(object_path: str) -> Optional[int]:
    """
    🆕 يستعلم عن الحجم الفعلي (بالبايت) لملف مرفوع مسبقاً بـ bucket المؤقت، عبر
    metadata التي يرجعها Supabase Storage عند list() - دون تحميل أي محتوى فعلي.

    يُستخدم كخط دفاع أول (قبل التحميل) للتحقق من أن الحجم الفعلي للملف المرفوع
    عبر TUS مطابق فعلياً للحد الأقصى المسموح - لأن فحص `file_size` المُصرَّح به
    بمرحلة /api/audio-upload/init هو فحص من طرف العميل فقط (غير موثوق لوحده)،
    والرفع الفعلي بعدها يذهب مباشرة من متصفح المستخدم لـ Supabase دون المرور
    عبر سيرفرنا إطلاقاً.

    Returns:
        الحجم بالبايت عند النجاح، أو None لو تعذّر إيجاد الملف أو قراءة الـ metadata
        (يجب معاملة None كفشل تحقق - أي رفض الملف بدل السماح له بشكل متساهل).
    """
    try:
        folder, _, file_name = object_path.rpartition("/")
        entries = await supabase.storage.from_(AUDIO_UPLOAD_BUCKET).list(folder)
        for entry in entries:
            if entry.get("name") != file_name:
                continue
            metadata = entry.get("metadata") or {}
            size = metadata.get("size") or metadata.get("contentLength")
            return int(size) if size is not None else None
        return None
    except Exception as e:
        log_error(logger, f"Could not read size metadata for temp audio '{object_path}': {e}")
        return None


async def download_audio_temp_to_file(
    object_path: str, destination_path: str, max_size_bytes: Optional[int] = None
) -> bool:
    """
    يحمّل الملف الصوتي المؤقت من Supabase Storage إلى القرص المحلي (downloads/)
    تمهيداً لمعالجته بنفس مسار handle_audio_message الحالي (mutagen، ثم Gemini).

    🆕 max_size_bytes: خط دفاع ثانٍ (بعد get_audio_temp_object_size) - لو حُدِّد،
    يُرفض ويُحذف أي محتوى تم تحميله فعلياً يتجاوز هذا الحد، حتى لو تجاوز فحص
    الـ metadata لأي سبب (تعارض بين الحجم المُبلَّغ والحجم الفعلي مثلاً).
    """
    try:
        file_bytes = await supabase.storage.from_(AUDIO_UPLOAD_BUCKET).download(object_path)
        if max_size_bytes is not None and len(file_bytes) > max_size_bytes:
            log_error(
                logger,
                f"Downloaded audio '{object_path}' exceeds max allowed size "
                f"({len(file_bytes)} > {max_size_bytes} bytes) - rejecting.",
            )
            return False
        with open(destination_path, "wb") as f:
            f.write(file_bytes)
        return True
    except Exception as e:
        log_error(logger, f"Could not download temp audio '{object_path}' from storage: {e}")
        return False


async def delete_audio_temp(object_path: str) -> None:
    """
    حذف الملف الصوتي المؤقت من Supabase Storage فوراً بعد انتهاء المعالجة (نجاحاً
    أو فشلاً). يُستدعى دائماً من finally بمسار المعالجة - راجع
    handlers/audio.py::process_web_uploaded_audio. لا يرمي استثناء عند الفشل
    (السجل فقط) حتى لا يكسر تدفق الرد على الطالب لو تعذّر الحذف لأي سبب - شبكة
    التنظيف الدورية (scheduled cleanup) هي الخط الثاني للدفاع بهذه الحالة.
    """
    try:
        await supabase.storage.from_(AUDIO_UPLOAD_BUCKET).remove([object_path])
    except Exception as e:
        log_warning(logger, f"Could not delete temp audio '{object_path}' from storage (will rely on scheduled cleanup): {e}")


async def cleanup_stale_audio_uploads(older_than_seconds: int = 3600) -> int:
    """
    🆕 شبكة أمان: تُستدعى دورياً (راجع scheduled_cleanup_loop بـ webhook_server.py)
    لحذف أي ملفات صوتية مؤقتة تبقّت بالـ bucket لأكثر من ساعة - غالباً بسبب معالجة
    انقطعت بشكل استثنائي قبل الوصول لـ finally (كراش السيرفر نفسه مثلاً، وليس
    استثناء عادي بايثون يُلتقط). يمسح على مستوى كل مجلدات المستخدمين (list بمستوى
    الجذر أولاً، ثم كل مجلد مستخدم على حدة، لأن Storage API لا يدعم فحص عمق تعسفي
    بنداء واحد).

    Returns:
        عدد الملفات المحذوفة (للـ logging فقط).
    """
    deleted_count = 0
    try:
        user_folders = await supabase.storage.from_(AUDIO_UPLOAD_BUCKET).list()
        now = datetime.datetime.now(datetime.timezone.utc)
        for folder in user_folders:
            folder_name = folder.get("name")
            if not folder_name:
                continue
            try:
                files = await supabase.storage.from_(AUDIO_UPLOAD_BUCKET).list(folder_name)
            except Exception:
                continue
            stale_paths = []
            for file_info in files:
                created_at_raw = file_info.get("created_at")
                if not created_at_raw:
                    continue
                try:
                    created_at = datetime.datetime.fromisoformat(created_at_raw.replace("Z", "+00:00"))
                except ValueError:
                    continue
                if (now - created_at).total_seconds() > older_than_seconds:
                    stale_paths.append(f"{folder_name}/{file_info.get('name')}")
            if stale_paths:
                await supabase.storage.from_(AUDIO_UPLOAD_BUCKET).remove(stale_paths)
                deleted_count += len(stale_paths)
    except Exception as e:
        log_error(logger, f"Stale audio uploads cleanup failed: {e}")
    return deleted_count


# ==============================================================================
# 📋 خطوات إعداد يدوية لازمة مرة وحدة بلوحة تحكم Supabase (Storage):
# 1. أنشئ bucket جديد بالاسم "audio-temp" واختر Private (مو Public).
# 2. لا حاجة لأي RLS policy إضافية للرفع/التحميل عبر signed URLs بحد ذاتها، لكن
#    تأكد إن الـ service_role key المستخدم بـ create_async_client هو نفسه المستخدم
#    حالياً (نفس النمط الموجود أعلى الملف) لأن create_signed_upload_url ودوال
#    list/remove هذه تتطلب صلاحيات service_role وليس anon key.
# ==============================================================================


