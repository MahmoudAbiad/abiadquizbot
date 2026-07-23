"""
Utility functions for file processing and hashing.
"""

import os
import hashlib
from logger import get_logger, log_error, log_info

logger = get_logger(__name__)

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

def extract_text_from_file(file_path: str) -> Optional[str]:
    """استخراج النص الصافي من ملفات Word, PowerPoint, TXT"""
    ext = os.path.splitext(file_path)[1].lower()
    try:
        if ext == ".txt":
            with open(file_path, "r", encoding="utf-8", errors="ignore") as f:
                return f.read()

        elif ext in [".docx", ".doc"]:
            import docx
            doc = docx.Document(file_path)
            full_text = [p.text.strip() for p in doc.paragraphs if p.text.strip()]
            # قراءة النصوص داخل الجداول أيضاً
            for table in doc.tables:
                for row in table.rows:
                    for cell in row.cells:
                        if cell.text.strip():
                            full_text.append(cell.text.strip())
            return "\n".join(full_text)

        elif ext in [".pptx", ".ppt"]:
            from pptx import Presentation
            prs = Presentation(file_path)
            text_runs = []
            for slide in prs.slides:
                for shape in slide.shapes:
                    if hasattr(shape, "text") and shape.text.strip():
                        text_runs.append(shape.text.strip())
            return "\n".join(text_runs)

    except Exception as e:
        logger.error(f"Error extracting text from {file_path}: {e}")
        return None

    return None
