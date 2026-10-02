# services/file_service.py
import asyncio
import hashlib
import os
import uuid
from typing import Any, Dict, List, Tuple

from config import bot
from utils import calculate_file_hash, extract_text_from_file, safe_file_cleanup
from validators import validate_file_size
from services.upload_validation import validate_image

DOWNLOADS_DIR = "downloads"

def compute_combined_hash(paths: List[str]) -> str:
    """حساب الهاش الموحد لمجموعة ملفات/صور ألبوم"""
    digest = hashlib.sha256()
    for path in paths:
        digest.update(calculate_file_hash(path).encode("ascii"))
    return digest.hexdigest()

async def download_photos_service(message_user_id: int, photos: List[Dict[str, Any]]) -> Tuple[List[str], str]:
    """تنزيل الصور محلياً مع التحقق من الحجم"""
    paths: List[str] = []
    try:
        for index, photo in enumerate(photos, start=1):
            valid, error = validate_file_size(photo.get("file_size") or 0, "photo")
            if not valid:
                for previous in paths:
                    safe_file_cleanup(previous)
                return [], f"❌ الصورة رقم {index}: {error}"
            path = os.path.join(DOWNLOADS_DIR, f"{message_user_id}_{uuid.uuid4().hex}.jpg")
            paths.append(path)
            await bot.download(photo["file_id"], destination=path)
            await asyncio.to_thread(validate_image, path)
        return paths, ""
    except BaseException:
        for path in paths:
            safe_file_cleanup(path)
        raise

async def extract_office_text_if_needed(file_path: str) -> Tuple[str, bool]:
    """استخراج النص محلياً لمستندات أوفيس والملفات النصية"""
    ext = os.path.splitext(file_path)[1].lower()
    if ext in [".docx", ".doc", ".pptx", ".ppt", ".txt"]:
        extracted_text = await asyncio.to_thread(extract_text_from_file, file_path)
        if extracted_text and len(extracted_text.strip()) >= 30:
            return extracted_text, True
        return "", False
    return "", True # ليس ملف أوفيس