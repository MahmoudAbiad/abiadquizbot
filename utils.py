"""
Utility functions for file processing and hashing.
"""

import base64
import html
import os
import hashlib
import unicodedata
import zipfile
from logger import get_logger, log_error, log_info

logger = get_logger(__name__)


def safe_title(text) -> str:
    """
    يهرّب (escape) أي نص حر مصدره المستخدم (عنوان كويز، اسم غروب/قناة...) قبل
    ما ينحط جوا رسالة بـ parse_mode="HTML"، لتفادي HTML injection (تاغات مكسورة
    أو روابط <a href> مزيفة تنعرض لمستخدم تاني). استخدمها بأي مكان بتحط فيه
    نص من الداتابيس أو من تيليجرام (chat.title مثلاً) جوا <b>...</b> أو مشابه.
    """
    return html.escape(str(text) if text is not None else "")

# 🆕 file_hash (SHA-256 hex, 64 حرف) طويل جداً ليُستخدم مباشرة ضمن callback_data
# (حد تيليجرام 64 بايت للحقل كاملاً). هذا التحويل ذهاب/إياب Base64 URL-Safe يضغطه
# لـ 43 حرفاً فقط (32 بايت خام) بدون أي فقدان أو حاجة لتخزين وسيط (Redis/DB) لحفظ
# العلاقة - راجع services/classification_votes.py لاستخدامه الفعلي بأزرار "هل التصنيف
# صحيح؟". يعمل فقط لهاشات SHA-256 الصحيحة (64 حرف hex)؛ أي شيء آخر يرمي استثناءً.
def hash_to_short_token(file_hash: str) -> str:
    raw = bytes.fromhex(file_hash)
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def short_token_to_hash(token: str) -> str:
    padding = "=" * (-len(token) % 4)
    raw = base64.urlsafe_b64decode(token + padding)
    return raw.hex()

def calculate_file_hash(file_path: str) -> str:
    """
    Return a SHA-256 digest of file bytes only. File names are deliberately ignored.
    """
    hasher = hashlib.sha256()
    try:
        with open(file_path, 'rb') as f:
            for chunk in iter(lambda: f.read(4096), b""):
                hasher.update(chunk)
        return hasher.hexdigest()
    except Exception as e:
        log_error(logger, f"Error calculating hash for {file_path}: {e}")
        return ""

def safe_file_cleanup(file_path: str) -> bool:
    """Safely delete a file with error handling."""
    try:
        if os.path.exists(file_path):
            os.remove(file_path)
            log_info(logger, f"File deleted: {file_path}")
            return True
        return False
    except Exception as e:
        log_error(logger, f"Error deleting file {file_path}: {e}", exception=e)
        return False

def ensure_directory_exists(directory_path: str) -> bool:
    """Ensure a directory exists, create if necessary."""
    try:
        if not os.path.exists(directory_path):
            os.makedirs(directory_path)
        return True
    except Exception as e:
        log_error(logger, f"Error creating directory {directory_path}: {e}", exception=e)
        return False

def format_file_size(size_bytes: int) -> str:
    """Format bytes to human-readable file size."""
    for unit in ['B', 'KB', 'MB', 'GB']:
        if size_bytes < 1024.0:
            return f"{size_bytes:.2f} {unit}"
        size_bytes /= 1024.0
    return f"{size_bytes:.2f} TB"

_MAX_OFFICE_ARCHIVE_ENTRIES = 10000
_MAX_OFFICE_UNCOMPRESSED_BYTES = 512 * 1024 * 1024
_MAX_OFFICE_SINGLE_ENTRY_BYTES = 256 * 1024 * 1024
_MAX_OFFICE_SUSPICIOUS_RATIO = 200.0
_MAX_OFFICE_RATIO_CHECK_BYTES = 50 * 1024 * 1024


def _validate_office_archive(file_path: str) -> None:
    """رفض ملفات DOCX/PPTX ذات بنية ZIP مرضية قبل تمريرها لمكتبات التحليل."""
    try:
        with zipfile.ZipFile(file_path) as archive:
            infos = archive.infolist()
            if len(infos) > _MAX_OFFICE_ARCHIVE_ENTRIES:
                raise ValueError("Office archive contains too many entries")

            total_uncompressed = 0
            total_compressed = 0
            for info in infos:
                if info.file_size < 0 or info.compress_size < 0:
                    raise ValueError("Office archive contains invalid entry sizes")
                if info.file_size > _MAX_OFFICE_SINGLE_ENTRY_BYTES:
                    raise ValueError("Office archive contains an oversized entry")
                total_uncompressed += info.file_size
                total_compressed += info.compress_size
                if total_uncompressed > _MAX_OFFICE_UNCOMPRESSED_BYTES:
                    raise ValueError("Office archive expands beyond the safe limit")

            if total_uncompressed >= _MAX_OFFICE_RATIO_CHECK_BYTES:
                ratio = total_uncompressed / max(total_compressed, 1)
                if ratio > _MAX_OFFICE_SUSPICIOUS_RATIO:
                    raise ValueError("Office archive has a suspicious compression ratio")
    except zipfile.BadZipFile as exc:
        raise ValueError("Invalid Office archive") from exc


def _normalize_extracted_text(text: str) -> str:
    """🩹 FIX: بعض ملفات Word (عادةً محوَّلة أصلاً من PDF عبر أدوات تحويل ضعيفة) بتخزّن
    الحروف العربية كأشكال عرض توافقية (Unicode Presentation Forms، نطاق U+FE70-U+FEFF -
    مثلاً 'ﻟ' U+FEDF) بدل الحرف المنطقي القياسي ('ل' U+0644). النص بيبين سليم بصرياً (الخط
    بيرندر الشكل صح) لكن python-docx بيستخرجه "صحيحاً تقنياً" وفاسداً فعلياً لأي معالجة نصية
    لاحقة (بحث/مقارنة/فهم الموديل). NFKC هو الحل القياسي - بيحوّل هاي الأشكال التوافقية رجوع
    لمكافئها المنطقي القياسي تلقائياً، ما بيأثر على نص سليم أصلاً."""
    return unicodedata.normalize("NFKC", text) if text else text


def extract_text_from_file(file_path: str) -> str | None:
    """
    استخراج النص الصافي من ملفات Word (.docx), PowerPoint (.pptx), TXT.
    ملاحظة: الصيغ القديمة (.doc, .ppt) لا تُقرأ بواسطة python-docx/pptx مباشرة.
    """
    ext = os.path.splitext(file_path)[1].lower()
    try:
        # 1. ملفات النص المباشر
        if ext == ".txt":
            with open(file_path, "r", encoding="utf-8", errors="ignore") as f:
                return _normalize_extracted_text(f.read())

        # 2. مستندات Word الحديثة فقط
        elif ext == ".docx":
            _validate_office_archive(file_path)
            import docx
            doc = docx.Document(file_path)
            full_text = []

            # استخراج النصوص من الفقرات
            for p in doc.paragraphs:
                if p.text.strip():
                    full_text.append(p.text.strip())

            # استخراج النصوص من الجداول
            for table in doc.tables:
                for row in table.rows:
                    for cell in row.cells:
                        cell_text = cell.text.strip()
                        if cell_text:
                            full_text.append(cell_text)

            return _normalize_extracted_text("\n".join(full_text)) if full_text else None

        # 3. عروض PowerPoint الحديثة فقط
        elif ext == ".pptx":
            _validate_office_archive(file_path)
            from pptx import Presentation
            prs = Presentation(file_path)
            text_runs = []

            for slide in prs.slides:
                for shape in slide.shapes:
                    if shape.has_text_frame:
                        for paragraph in shape.text_frame.paragraphs:
                            text = paragraph.text.strip()
                            if text:
                                text_runs.append(text)

            return _normalize_extracted_text("\n".join(text_runs)) if text_runs else None

        # 4. الصيغ الثنائية القديمة (Log Warning بدلاً من العطل)
        elif ext in [".doc", ".ppt"]:
            logger.warning(f"File {file_path} is legacy binary format ({ext}) and cannot be parsed directly.")
            return None

    except Exception as e:
        logger.error(f"Error extracting text from {file_path}: {e}")
        return None

    return None