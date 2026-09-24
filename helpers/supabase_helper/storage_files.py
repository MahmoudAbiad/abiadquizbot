"""
supabase_helper.storage_files
================================
🆕 File/Images Web Upload (Telegram Mini App): روابط رفع/تحميل/حذف الملفات
والصور المؤقتة بـ Supabase Storage (bucket: file-temp).
"""

import datetime
import uuid
from typing import Optional, Dict, List

from ._client import supabase, logger, log_error, log_warning

# ==============================================================================
# 🆕 File/Images Web Upload (Telegram Mini App) - نفس آلية Audio Web Upload فوق،
# مُعمَّمة لـ bucket ثاني (file-temp) تُستخدم له مستندات كبيرة (حتى 150 صفحة/100MB)
# وألبومات صور كبيرة (حتى 50 صورة). بدل تكرار كل الدوال من الصفر، الدوال هون بتاخد
# bucket كباراميتر وتُبنى فوقها أغلفة (wrappers) رقيقة بأسماء واضحة لكل استخدام -
# نفس منطق دوال audio_temp أعلاه تماماً، فقط أُعيد استخدامه بدل تكراره حرفياً.
# ==============================================================================

from constants import FILE_UPLOAD_BUCKET


async def _create_signed_upload_target(bucket: str, object_path: str) -> Optional[Dict[str, str]]:
    """يولّد رابط رفع موقّع (signed upload URL) لمسار مُعطى مسبقاً بـ bucket مُعطى."""
    try:
        result = await supabase.storage.from_(bucket).create_signed_upload_url(object_path)
        return {
            "path": result.get("path", object_path),
            "signed_url": result.get("signed_url") or result.get("signedUrl"),
            "token": result.get("token"),
        }
    except Exception as e:
        log_error(logger, f"Could not create signed upload URL for '{bucket}/{object_path}': {e}")
        return None


async def create_file_upload_target(user_id: int, file_extension: str = "") -> Optional[Dict[str, str]]:
    """نظير create_audio_upload_target لمستند مرفوع عبر صفحة الويب - مسار فريد
    {user_id}/{uuid}{ext} بـ bucket الملفات (FILE_UPLOAD_BUCKET)."""
    ext = file_extension if file_extension.startswith(".") else f".{file_extension}" if file_extension else ""
    object_path = f"{user_id}/{uuid.uuid4().hex}{ext}"
    return await _create_signed_upload_target(FILE_UPLOAD_BUCKET, object_path)


async def create_image_upload_targets(user_id: int, file_extensions: List[str]) -> Optional[List[Dict[str, str]]]:
    """
    🆕 يولّد دفعة روابط رفع موقّعة لعدة صور سوا (ألبوم كبير) تحت نفس مجلد الجلسة
    ({user_id}/{session_uuid}/{index}{ext}) - مجلد مشترك واحد لكل الصور يسهّل تتبعها/
    حذفها كمجموعة واحدة لاحقاً (delete_file_temp لكل مسار، أو التنظيف الدوري).
    يرجع None بالكامل لو فشل أي رابط توقيع واحد (فشل جزئي غير مقبول هون - إما كل
    الألبوم جاهز للرفع أو ولا شي، تفادياً لصور "يتيمة" بلا بقية الدفعة).
    """
    session_id = uuid.uuid4().hex
    targets: List[Dict[str, str]] = []
    for index, file_extension in enumerate(file_extensions):
        ext = file_extension if file_extension.startswith(".") else f".{file_extension}" if file_extension else ".jpg"
        object_path = f"{user_id}/{session_id}/{index}{ext}"
        target = await _create_signed_upload_target(FILE_UPLOAD_BUCKET, object_path)
        if not target:
            return None
        targets.append(target)
    return targets


async def get_file_temp_object_size(object_path: str) -> Optional[int]:
    """نظير get_audio_temp_object_size لـ bucket الملفات."""
    try:
        folder, _, file_name = object_path.rpartition("/")
        entries = await supabase.storage.from_(FILE_UPLOAD_BUCKET).list(folder)
        for entry in entries:
            if entry.get("name") != file_name:
                continue
            metadata = entry.get("metadata") or {}
            size = metadata.get("size") or metadata.get("contentLength")
            return int(size) if size is not None else None
        return None
    except Exception as e:
        log_error(logger, f"Could not read size metadata for temp file '{object_path}': {e}")
        return None


async def download_file_temp_to_file(
    object_path: str, destination_path: str, max_size_bytes: Optional[int] = None
) -> bool:
    """نظير download_audio_temp_to_file لـ bucket الملفات."""
    try:
        file_bytes = await supabase.storage.from_(FILE_UPLOAD_BUCKET).download(object_path)
        if max_size_bytes is not None and len(file_bytes) > max_size_bytes:
            log_error(
                logger,
                f"Downloaded file '{object_path}' exceeds max allowed size "
                f"({len(file_bytes)} > {max_size_bytes} bytes) - rejecting.",
            )
            return False
        with open(destination_path, "wb") as f:
            f.write(file_bytes)
        return True
    except Exception as e:
        log_error(logger, f"Could not download temp file '{object_path}' from storage: {e}")
        return False


async def delete_file_temp(object_path: str) -> None:
    """نظير delete_audio_temp لـ bucket الملفات. لا يرمي استثناء عند الفشل (تنظيف
    السجل فقط) - نفس مبدأ الدالة الأصلية بالضبط."""
    try:
        await supabase.storage.from_(FILE_UPLOAD_BUCKET).remove([object_path])
    except Exception as e:
        log_warning(logger, f"Could not delete temp file '{object_path}' from storage (will rely on scheduled cleanup): {e}")


async def delete_file_temp_batch(object_paths: List[str]) -> None:
    """🆕 حذف دفعة مسارات سوا (ألبوم صور كامل) بنداء واحد بدل حلقة نداءات منفصلة."""
    if not object_paths:
        return
    try:
        await supabase.storage.from_(FILE_UPLOAD_BUCKET).remove(object_paths)
    except Exception as e:
        log_warning(logger, f"Could not delete temp file batch from storage (will rely on scheduled cleanup): {e}")


async def cleanup_stale_file_uploads(older_than_seconds: int = 3600) -> int:
    """نظير cleanup_stale_audio_uploads لـ bucket الملفات - بيمشي على مستوى مجلدات
    المستخدمين، وبداخل كل مجلد مستخدم بيفحص أيضاً مجلدات الجلسات الفرعية لألبومات
    الصور (user_id/session_uuid/*) بالإضافة للملفات المباشرة (user_id/*)."""
    deleted_count = 0
    try:
        user_folders = await supabase.storage.from_(FILE_UPLOAD_BUCKET).list()
        now = datetime.datetime.now(datetime.timezone.utc)
        for folder in user_folders:
            folder_name = folder.get("name")
            if not folder_name:
                continue
            try:
                entries = await supabase.storage.from_(FILE_UPLOAD_BUCKET).list(folder_name)
            except Exception:
                continue
            stale_paths = []
            for entry in entries:
                entry_name = entry.get("name")
                if not entry_name:
                    continue
                # عنصر بلا "id" بالـ metadata عادةً مجلد فرعي (جلسة ألبوم صور) وليس ملفاً
                if entry.get("id") is None and entry.get("metadata") is None:
                    try:
                        sub_entries = await supabase.storage.from_(FILE_UPLOAD_BUCKET).list(f"{folder_name}/{entry_name}")
                    except Exception:
                        continue
                    for sub_entry in sub_entries:
                        created_at_raw = sub_entry.get("created_at")
                        if not created_at_raw:
                            continue
                        try:
                            created_at = datetime.datetime.fromisoformat(created_at_raw.replace("Z", "+00:00"))
                        except ValueError:
                            continue
                        if (now - created_at).total_seconds() > older_than_seconds:
                            stale_paths.append(f"{folder_name}/{entry_name}/{sub_entry.get('name')}")
                    continue
                created_at_raw = entry.get("created_at")
                if not created_at_raw:
                    continue
                try:
                    created_at = datetime.datetime.fromisoformat(created_at_raw.replace("Z", "+00:00"))
                except ValueError:
                    continue
                if (now - created_at).total_seconds() > older_than_seconds:
                    stale_paths.append(f"{folder_name}/{entry_name}")
            if stale_paths:
                await supabase.storage.from_(FILE_UPLOAD_BUCKET).remove(stale_paths)
                deleted_count += len(stale_paths)
    except Exception as e:
        log_error(logger, f"Stale file uploads cleanup failed: {e}")
    return deleted_count


# ==============================================================================
# 📋 خطوة إعداد يدوية إضافية بلوحة تحكم Supabase (Storage):
# أنشئ bucket ثانٍ بالاسم "file-temp" (Private) - نفس خطوات "audio-temp" فوق بالضبط.
# ==============================================================================
