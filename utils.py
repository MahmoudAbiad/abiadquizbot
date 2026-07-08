"""
Utility functions for file processing and hashing.
"""

import os
import hashlib
from logger import get_logger, log_error, log_info

logger = get_logger(__name__)

def calculate_file_hash(file_path: str) -> str:
    """
    توليد بصمة فريدة للملف باستخدام MD5 للتحقق مما إذا كان قد تم رفعه مسبقاً.
    """
    hasher = hashlib.md5()
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