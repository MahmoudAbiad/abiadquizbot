# helpers/r2_helper.py
"""
تخزين مؤقت (Audio/File web upload) عبر Cloudflare R2 بدل Supabase Storage - بديل
drop-in لنفس الدوال الموجودة سابقاً بـ supabase_helper.py (نفس الأسماء، نفس شكل
القيم المرجعة قدر الإمكان) لتفادي تعديل أي كود استدعاء غير ضروري.

السبب: خطة Supabase المجانية بتفرض سقف صارم 50MB على أي ملف مرفوع للـ Storage
(غير قابل للتعديل بدون ترقية)، بينما R2 بيدعم رفع لحد كذا جيجا مجاناً (10GB تخزين
+ egress صفر) - وبما إن هذه الملفات مؤقتة بطبيعتها (تُرفع، تُعالَج، تُحذف فوراً)،
R2 مناسب تماماً لهذا النمط.

الفرق التقني الجوهري عن نسخة Supabase:
- Supabase استخدم TUS (رفع قابل للاستئناف) عبر create_signed_upload_url + توكن.
- R2 (متوافق مع S3 API) بيستخدم presigned PUT URL عادي: رابط واحد صالح لمدة
  محدودة، والمتصفح بيعمل عليه PUT مباشرة بمحتوى الملف كاملاً بطلب واحد (بدون
  تجزئة/استئناف). هذا كافٍ تماماً لحدودنا الحالية (250MB صوت / 100MB ملفات /
  15MB لكل صورة) - أي حجم من هذول أقل بكثير من حد R2 للرفع بطلب واحد (5GB).
  تم تعديل صفحات webapp/*.html لاستخدام XHR PUT مباشر بدل tus-js-client تبعاً لذلك.

المتغيرات البيئية المطلوبة (أضِفها لملف .env ولمنصة النشر):
    R2_ACCOUNT_ID=...           # من رابط لوحة تحكم R2 (Account ID يظهر بصفحة R2 الرئيسية)
    R2_ACCESS_KEY_ID=...        # من R2 > Manage API Tokens > Create API Token
    R2_SECRET_ACCESS_KEY=...    # نفس الخطوة فوق
    R2_AUDIO_BUCKET=audio-upload-bucket   # اسم الباكيت اللي أنشأته فعلياً (اختياري - له قيمة افتراضية مطابقة)
    R2_FILE_BUCKET=file-upload-bucket     # نفس الشي لباكيت الملفات/الصور
"""

import asyncio
import datetime
import os
import uuid
from typing import Dict, List, Optional

import boto3
from botocore.client import Config
from dotenv import load_dotenv, find_dotenv

from logger import get_logger, log_error, log_warning

dotenv_path = find_dotenv()
load_dotenv(dotenv_path)

logger = get_logger(__name__)

R2_ACCOUNT_ID = os.getenv("R2_ACCOUNT_ID", "")
R2_ACCESS_KEY_ID = os.getenv("R2_ACCESS_KEY_ID", "")
R2_SECRET_ACCESS_KEY = os.getenv("R2_SECRET_ACCESS_KEY", "")
R2_AUDIO_BUCKET = os.getenv("R2_AUDIO_BUCKET", "audio-upload-bucket")
R2_FILE_BUCKET = os.getenv("R2_FILE_BUCKET", "file-upload-bucket")

R2_ENDPOINT_URL = f"https://{R2_ACCOUNT_ID}.r2.cloudflarestorage.com" if R2_ACCOUNT_ID else ""

# مدة صلاحية رابط الرفع الموقّع (بالثواني) - ساعة كاملة كافية جداً لأي رفع حتى
# لو كان اتصال الطالب بطيء، وبما إنه رابط رفع (PUT) وليس تحميل (GET) فلا خطورة
# أمنية إضافية من طول الصلاحية.
PRESIGNED_UPLOAD_TTL_SECONDS = 3600

_r2_client = None


def _get_r2_client():
    """عميل boto3 واحد يُعاد استخدامه (lazy init) - نفس نمط عميل Supabase أعلى
    supabase_helper.py. يرمي استثناء واضح لو المتغيرات البيئية ناقصة بدل فشل
    غامض لاحقاً عند أول استدعاء فعلي."""
    global _r2_client
    if _r2_client is None:
        if not (R2_ACCOUNT_ID and R2_ACCESS_KEY_ID and R2_SECRET_ACCESS_KEY):
            raise RuntimeError(
                "متغيرات R2 البيئية ناقصة (R2_ACCOUNT_ID / R2_ACCESS_KEY_ID / "
                "R2_SECRET_ACCESS_KEY) - راجع تعليقات أعلى r2_helper.py."
            )
        _r2_client = boto3.client(
            "s3",
            endpoint_url=R2_ENDPOINT_URL,
            aws_access_key_id=R2_ACCESS_KEY_ID,
            aws_secret_access_key=R2_SECRET_ACCESS_KEY,
            config=Config(signature_version="s3v4"),
            region_name="auto",
        )
    return _r2_client


def _build_object_path(user_id: int, file_extension: str = "") -> str:
    ext = file_extension if file_extension.startswith(".") else f".{file_extension}" if file_extension else ""
    return f"{user_id}/{uuid.uuid4().hex}{ext}"


def _presign_put_sync(bucket: str, object_path: str) -> Optional[str]:
    try:
        client = _get_r2_client()
        return client.generate_presigned_url(
            "put_object",
            Params={"Bucket": bucket, "Key": object_path},
            ExpiresIn=PRESIGNED_UPLOAD_TTL_SECONDS,
        )
    except Exception as e:
        log_error(logger, f"Could not create R2 presigned PUT URL for '{bucket}/{object_path}': {e}")
        return None


def _head_object_size_sync(bucket: str, object_path: str) -> Optional[int]:
    try:
        client = _get_r2_client()
        head = client.head_object(Bucket=bucket, Key=object_path)
        return int(head.get("ContentLength"))
    except Exception:
        return None


def _download_object_sync(bucket: str, object_path: str, destination_path: str, max_size_bytes: Optional[int]) -> bool:
    try:
        client = _get_r2_client()
        head = client.head_object(Bucket=bucket, Key=object_path)
        size = int(head.get("ContentLength", 0))
        if max_size_bytes is not None and size > max_size_bytes:
            log_error(
                logger,
                f"R2 object '{bucket}/{object_path}' exceeds max allowed size ({size} > {max_size_bytes} bytes) - rejecting.",
            )
            return False
        client.download_file(bucket, object_path, destination_path)
        return True
    except Exception as e:
        log_error(logger, f"Could not download R2 object '{bucket}/{object_path}': {e}")
        return False


def _delete_object_sync(bucket: str, object_path: str) -> None:
    try:
        client = _get_r2_client()
        client.delete_object(Bucket=bucket, Key=object_path)
    except Exception as e:
        log_warning(logger, f"Could not delete R2 object '{bucket}/{object_path}' (will rely on scheduled cleanup): {e}")


def _delete_objects_batch_sync(bucket: str, object_paths: List[str]) -> None:
    if not object_paths:
        return
    try:
        client = _get_r2_client()
        # حد S3/R2 لكل نداء DeleteObjects هو 1000 مفتاح - كافٍ جداً هون
        # (MAX_IMAGE_WEB_UPLOAD_COUNT = 50) لكن أُبقيَ التقسيم لأمان مستقبلي.
        for i in range(0, len(object_paths), 1000):
            chunk = object_paths[i:i + 1000]
            client.delete_objects(Bucket=bucket, Delete={"Objects": [{"Key": p} for p in chunk]})
    except Exception as e:
        log_warning(logger, f"Could not delete R2 object batch from '{bucket}' (will rely on scheduled cleanup): {e}")


def _cleanup_stale_sync(bucket: str, older_than_seconds: int) -> int:
    deleted_count = 0
    try:
        client = _get_r2_client()
        now = datetime.datetime.now(datetime.timezone.utc)
        stale_keys: List[str] = []
        paginator = client.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=bucket):
            for obj in page.get("Contents", []):
                last_modified = obj.get("LastModified")
                if last_modified and (now - last_modified).total_seconds() > older_than_seconds:
                    stale_keys.append(obj["Key"])
        if stale_keys:
            for i in range(0, len(stale_keys), 1000):
                chunk = stale_keys[i:i + 1000]
                client.delete_objects(Bucket=bucket, Delete={"Objects": [{"Key": k} for k in chunk]})
            deleted_count = len(stale_keys)
    except Exception as e:
        log_error(logger, f"Stale R2 uploads cleanup failed for bucket '{bucket}': {e}")
    return deleted_count


# ==============================================================================
# 🎙️ Audio bucket - نفس أسماء دوال supabase_helper.py الأصلية (drop-in)
# ==============================================================================

async def create_audio_upload_target(user_id: int, file_extension: str = "") -> Optional[Dict[str, str]]:
    object_path = _build_object_path(user_id, file_extension)
    upload_url = await asyncio.to_thread(_presign_put_sync, R2_AUDIO_BUCKET, object_path)
    if not upload_url:
        return None
    return {"path": object_path, "upload_url": upload_url}


async def get_audio_temp_object_size(object_path: str) -> Optional[int]:
    return await asyncio.to_thread(_head_object_size_sync, R2_AUDIO_BUCKET, object_path)


async def download_audio_temp_to_file(object_path: str, destination_path: str, max_size_bytes: Optional[int] = None) -> bool:
    return await asyncio.to_thread(_download_object_sync, R2_AUDIO_BUCKET, object_path, destination_path, max_size_bytes)


async def delete_audio_temp(object_path: str) -> None:
    await asyncio.to_thread(_delete_object_sync, R2_AUDIO_BUCKET, object_path)


async def cleanup_stale_audio_uploads(older_than_seconds: int = 3600) -> int:
    return await asyncio.to_thread(_cleanup_stale_sync, R2_AUDIO_BUCKET, older_than_seconds)


# ==============================================================================
# 📄 File/Images bucket - نفس أسماء دوال supabase_helper.py الأصلية (drop-in)
# ==============================================================================

async def create_file_upload_target(user_id: int, file_extension: str = "") -> Optional[Dict[str, str]]:
    object_path = _build_object_path(user_id, file_extension)
    upload_url = await asyncio.to_thread(_presign_put_sync, R2_FILE_BUCKET, object_path)
    if not upload_url:
        return None
    return {"path": object_path, "upload_url": upload_url}


async def create_image_upload_targets(user_id: int, file_extensions: List[str]) -> Optional[List[Dict[str, str]]]:
    """نظير النسخة الأصلية: كل صور الألبوم تحت نفس مجلد الجلسة
    ({user_id}/{session_uuid}/{index}{ext})."""
    session_id = uuid.uuid4().hex
    targets: List[Dict[str, str]] = []
    for index, file_extension in enumerate(file_extensions):
        ext = file_extension if file_extension.startswith(".") else f".{file_extension}" if file_extension else ".jpg"
        object_path = f"{user_id}/{session_id}/{index}{ext}"
        upload_url = await asyncio.to_thread(_presign_put_sync, R2_FILE_BUCKET, object_path)
        if not upload_url:
            return None
        targets.append({"path": object_path, "upload_url": upload_url})
    return targets


async def get_file_temp_object_size(object_path: str) -> Optional[int]:
    return await asyncio.to_thread(_head_object_size_sync, R2_FILE_BUCKET, object_path)


async def download_file_temp_to_file(object_path: str, destination_path: str, max_size_bytes: Optional[int] = None) -> bool:
    return await asyncio.to_thread(_download_object_sync, R2_FILE_BUCKET, object_path, destination_path, max_size_bytes)


async def delete_file_temp(object_path: str) -> None:
    await asyncio.to_thread(_delete_object_sync, R2_FILE_BUCKET, object_path)


async def delete_file_temp_batch(object_paths: List[str]) -> None:
    await asyncio.to_thread(_delete_objects_batch_sync, R2_FILE_BUCKET, object_paths)


async def cleanup_stale_file_uploads(older_than_seconds: int = 3600) -> int:
    return await asyncio.to_thread(_cleanup_stale_sync, R2_FILE_BUCKET, older_than_seconds)


# ==============================================================================
# 📋 خطوات إعداد مطلوبة (مرة وحدة):
# 1. أنشئ باكيتين على R2: الاسم الافتراضي المتوقع "audio-upload-bucket" و
#    "file-upload-bucket" (أو أي اسم تاني، بس حدّده وقتها بـ R2_AUDIO_BUCKET/
#    R2_FILE_BUCKET بمتغيرات البيئة ليطابق الاسم الفعلي).
# 2. R2 > Manage API Tokens > Create API Token: اختر صلاحية Object Read & Write
#    مربوطة بالباكيتين فوق تحديداً (لا داعي لصلاحية حساب كاملة).
# 3. خذ Access Key ID + Secret Access Key من التوكن، و Account ID من الصفحة
#    الرئيسية لـ R2 بلوحة التحكم، وحطهم بـ .env (راجع أعلى الملف لأسماء المتغيرات).
# 4. الباكيتين ممكن يضلوا Private تماماً (بدون Public Access) - كل الوصول هون
#    عبر presigned URLs موقّعة من السيرفر، تماماً متل signed URLs بتاعة Supabase.
# ==============================================================================
