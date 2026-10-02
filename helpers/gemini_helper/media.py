"""
helpers/gemini_helper/media.py
تجهيز الملفات للطلبات: MIME، Inline مقابل Files API، وتقسيم PDF/الصور.
"""

from pathlib import Path
from typing import Any, List, Sequence
import asyncio
import hashlib
import mimetypes
import os
import uuid

import fitz
from google import genai
from google.genai import types

from logger import get_logger, log_warning
from utils import calculate_file_hash

logger = get_logger(__name__)


# AI-NOTE: الحد الأقصى لإرسال البيانات مباشرة ضمن الطلب (Inline) دون اللجوء لـ Files API.
# رفع الملف عبر Files API يضيف Round-trip شبكة وتأخير معالجة، لذا يُفضل تحاشيه في الملفات الصغيرة.
INLINE_DATA_SIZE_THRESHOLD = 15 * 1024 * 1024  # 15MB

# AI-NOTE: قاموس مخصص لتجاوز مشاكل التعرف على MIME Types في بيئات Docker المجرّدة
CUSTOM_MIME_TYPES = {
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".doc": "application/msword",
    ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    ".ppt": "application/vnd.ms-powerpoint",
    ".txt": "text/plain",
    ".pdf": "application/pdf",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
}

def get_safe_mime_type(file_path: str) -> str:
    """استرجاع نوع MIME للملف بشكل آمن ومضمون لاستخدامه في طلبات الـ API."""
    ext = os.path.splitext(file_path)[1].lower()
    if ext in CUSTOM_MIME_TYPES:
        return CUSTOM_MIME_TYPES[ext]
    guess, _ = mimetypes.guess_type(file_path)
    return guess or "application/octet-stream"


# ==============================================================================
# HELPER FUNCTIONS (FILES / HASHING / PDF SPLITTING)
# ==============================================================================
def _combined_file_hash(paths: Sequence[str]) -> str:
    """توليد SHA-256 فريد لمجموعة من الملفات لاستخدامه كمفتاح للتخزين المؤقت (Cache Key)."""
    digest = hashlib.sha256()
    for path in paths:
        digest.update(calculate_file_hash(path).encode("ascii"))
    return digest.hexdigest()


def get_pdf_page_count_sync(file_path: str) -> int:
    """حساب عدد صفحات ملف الـ PDF ميكانيكياً."""
    with fitz.open(file_path) as document:
        return len(document)


def split_pdf_into_three_sync(file_path: str) -> List[str]:
    """تقسيم ملف PDF إلى 3 أجزاء متساوية للمعالجة المتوازية في نمط Super PDF."""
    source = fitz.open(file_path)
    try:
        page_count = len(source)
        if page_count < 3:
            return [file_path]
        base = Path(file_path)
        chunk_paths: List[str] = []
        for index in range(3):
            start = (page_count * index) // 3
            end = (page_count * (index + 1)) // 3 - 1
            chunk = fitz.open()
            try:
                chunk.insert_pdf(source, from_page=start, to_page=end)
                chunk_path = str(base.with_name(f"{base.stem}_{uuid.uuid4().hex}_part{index + 1}.pdf"))
                chunk.save(chunk_path)
                chunk_paths.append(chunk_path)
            finally:
                chunk.close()
        return chunk_paths
    finally:
        source.close()


async def _safe_delete_gemini_file(client: genai.Client, file_name: str) -> None:
    """حذف الملفات التابعة لـ Files API من خوادم جوجل بعد انتهاء التوليد لحفظ الخصوصية والنظافة."""
    try:
        await asyncio.to_thread(client.files.delete, name=file_name)
    except Exception as exc:
        log_warning(logger, f"Could not delete Gemini upload {file_name}: {exc}")


def _read_file_bytes_sync(path: str) -> bytes:
    with open(path, "rb") as f:
        return f.read()


async def _build_contents_for_paths(
    client: genai.Client, paths: Sequence[str], prompt: str, uploaded_out: List[Any]
) -> List[Any]:
    """يبني قائمة المحتوى (البرومبت + الملفات) لعميل مُعيّن: يفضّل الإرسال Inline للملفات
    الصغيرة (بدون أي اعتماد على عميل مُحدد لاحقاً)، أو يرفعها عبر Files API لهذا العميل
    بالذات (Files API مرتبطة بمفتاح/مشروع مُحدد فلا يمكن مشاركتها بين عملاء مختلفين) عند
    تجاوز حجمها لعتبة الإرسال المباشر - مع تجميع الملفات المرفوعة بـuploaded_out لتنظيفها لاحقاً."""
    contents: List[Any] = [prompt]

    total_size = 0
    for path in paths:
        try:
            total_size += os.path.getsize(path)
        except OSError:
            total_size = INLINE_DATA_SIZE_THRESHOLD + 1
            break

    mime_types = [get_safe_mime_type(path) for path in paths]
    use_inline = total_size <= INLINE_DATA_SIZE_THRESHOLD and all(mime_types)

    if use_inline:
        for path, mime_type in zip(paths, mime_types):
            file_bytes = await asyncio.to_thread(_read_file_bytes_sync, path)
            contents.append(types.Part.from_bytes(data=file_bytes, mime_type=mime_type))
    else:
        for path in paths:
            uploaded_file = await asyncio.to_thread(client.files.upload, file=path)
            uploaded_out.append(uploaded_file)
            contents.append(uploaded_file)

    return contents


def split_images_into_three_sync(file_paths: List[str]) -> List[List[str]]:
    """تقسيم قائمة صور ألبوم كبير لـ 3 دفعات متقاربة الحجم (توزيع دوري Round-Robin)
    للمعالجة المتوازية بنمط Super Images - نظير split_pdf_into_three_sync تماماً
    لكن على قائمة مسارات صور بدل صفحات PDF واحد."""
    if len(file_paths) < 3:
        return [file_paths]
    chunks: List[List[str]] = [[], [], []]
    for index, path in enumerate(file_paths):
        chunks[index % 3].append(path)
    return chunks
