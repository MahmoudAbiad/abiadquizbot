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
    from services.upload_validation import validate_document
    return validate_document(file_path)


def split_pdf_into_three_sync(file_path: str) -> List[str]:
    """Bound chunk disk usage and remove partial output even when splitting fails."""
    import shutil
    from services.upload_validation import MAX_PDF_PARTS_BYTES, MIN_FREE_DISK_BYTES
    get_pdf_page_count_sync(file_path)
    base = Path(file_path)
    # Source already occupies disk. Reserve space for parts + a safety margin.
    if shutil.disk_usage(base.parent).free < MAX_PDF_PARTS_BYTES + MIN_FREE_DISK_BYTES:
        raise ValueError("لا توجد مساحة مؤقتة كافية لمعالجة ملف PDF الكبير، حاول لاحقاً.")
    chunk_paths: List[str] = []
    with fitz.open(file_path) as source:
        page_count = len(source)
        if page_count < 3:
            return [file_path]
        try:
            for index in range(3):
                chunk_path = str(base.with_name(f"{base.stem}_{uuid.uuid4().hex}_part{index + 1}.pdf"))
                chunk_paths.append(chunk_path)  # Track before saving, including partial writes.
                with fitz.open() as chunk:
                    chunk.insert_pdf(source, from_page=(page_count * index) // 3,
                                     to_page=(page_count * (index + 1)) // 3 - 1)
                    remaining = MAX_PDF_PARTS_BYTES - sum(os.path.getsize(p) for p in chunk_paths[:-1])
                    # MuPDF writes through a bounded file-like object, avoiding a giant RAM copy.
                    with open(chunk_path, "wb") as output:
                        class BoundedOutput:
                            def write(self, data):
                                if output.tell() + len(data) > remaining:
                                    raise ValueError("PDF temporary parts exceed the disk budget")
                                return output.write(data)
                            def seek(self, *args):
                                return output.seek(*args)
                            def tell(self):
                                return output.tell()
                            def flush(self):
                                return output.flush()
                        chunk.save(BoundedOutput(), garbage=3, deflate=True)
            return chunk_paths
        except BaseException:
            for path in chunk_paths:
                try:
                    os.remove(path)
                except FileNotFoundError:
                    pass
            raise


async def split_pdf_without_orphans(file_path):
    task = asyncio.create_task(asyncio.to_thread(split_pdf_into_three_sync, file_path))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        try:
            paths = await task
            for path in paths:
                if path != file_path:
                    os.remove(path)
        finally:
            raise



def split_pdf_with_context_sync(file_path: str, part_count: int) -> List[dict]:
    """Split a PDF into owned page ranges with one adjacent context page.

    The returned local (1-based) page indices distinguish original pages
    owned by the chunk from pages included for context only, for both
    extraction and new-question generation.
    """
    import shutil
    from services.upload_validation import MAX_PDF_PARTS_BYTES, MIN_FREE_DISK_BYTES

    if part_count not in (2, 3):
        raise ValueError("PDF context splitting requires 2 or 3 parts")
    get_pdf_page_count_sync(file_path)
    base = Path(file_path)
    if shutil.disk_usage(base.parent).free < MAX_PDF_PARTS_BYTES + MIN_FREE_DISK_BYTES:
        raise ValueError("لا توجد مساحة مؤقتة كافية لمعالجة ملف PDF الكبير، حاول لاحقاً.")

    chunks: List[dict] = []
    with fitz.open(file_path) as source:
        page_count = len(source)
        if page_count < part_count:
            return []
        try:
            for index in range(part_count):
                owned_start = (page_count * index) // part_count
                owned_end = (page_count * (index + 1)) // part_count
                # Half-open original PDF offsets. One page on either side is
                # context only, never owned by two extraction requests.
                context_start = max(0, owned_start - 1)
                context_end = min(page_count, owned_end + 1)
                leading_context = owned_start - context_start
                trailing_context = context_end - owned_end
                chunk_path = str(base.with_name(
                    f"{base.stem}_{uuid.uuid4().hex}_context{index + 1}.pdf"
                ))
                chunk_info = {
                    "path": chunk_path,
                    "owned_first": leading_context + 1,
                    "owned_last": leading_context + (owned_end - owned_start),
                    "leading_context": leading_context,
                    "trailing_context": trailing_context,
                    "page_count": context_end - context_start,
                    "original_start": owned_start + 1,
                    "original_end": owned_end,
                }
                chunks.append(chunk_info)  # Track before saving partial bytes.
                with fitz.open() as chunk:
                    chunk.insert_pdf(source, from_page=context_start, to_page=context_end - 1)
                    already_written = sum(os.path.getsize(c["path"]) for c in chunks[:-1])
                    remaining = MAX_PDF_PARTS_BYTES - already_written
                    with open(chunk_path, "wb") as output:
                        class BoundedOutput:
                            def write(self, data):
                                if output.tell() + len(data) > remaining:
                                    raise ValueError("PDF temporary parts exceed the disk budget")
                                return output.write(data)
                            def seek(self, *args):
                                return output.seek(*args)
                            def tell(self):
                                return output.tell()
                            def flush(self):
                                return output.flush()
                        chunk.save(BoundedOutput(), garbage=3, deflate=True)
            return chunks
        except BaseException:
            for item in chunks:
                try:
                    os.remove(item["path"])
                except FileNotFoundError:
                    pass
            raise


async def split_pdf_with_context_without_orphans(file_path: str, part_count: int) -> List[dict]:
    """Wait for the thread on cancellation and delete every temporary chunk."""
    task = asyncio.create_task(
        asyncio.to_thread(split_pdf_with_context_sync, file_path, part_count)
    )
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        try:
            chunks = await task
            for item in chunks:
                try:
                    os.remove(item["path"])
                except FileNotFoundError:
                    pass
        finally:
            raise


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
