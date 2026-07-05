"""
Input validation and error handling utilities.
Provides validation functions for user inputs and exception handling.
"""

from typing import Tuple
from constants import VALID_QUESTIONS_RANGE, MAX_DOC_SIZE, MAX_PHOTO_SIZE, MAX_PDF_PAGES
from logger import get_logger

logger = get_logger(__name__)

class ValidationError(Exception):
    """Custom exception for validation errors"""
    pass

def validate_question_count(count: int) -> Tuple[bool, str]:
    """
    Validate the number of questions requested.
    
    Args:
        count: Number of questions requested
        
    Returns:
        Tuple[bool, str]: (is_valid, error_message)
    """
    if not isinstance(count, int):
        return False, "Number must be an integer"
    
    min_q, max_q = VALID_QUESTIONS_RANGE
    if count < min_q:
        return False, f"Minimum {min_q} question required"
    
    if count > max_q:
        return False, f"Maximum {max_q} questions allowed"
    
    return True, ""

def validate_file_size(file_size: int, file_type: str = "document") -> Tuple[bool, str]:
    """
    Validate file size against limits.
    
    Args:
        file_size: Size of file in bytes
        file_type: Type of file ("document" or "photo")
        
    Returns:
        Tuple[bool, str]: (is_valid, error_message)
    """
    if file_type == "document":
        max_size = MAX_DOC_SIZE
        max_size_mb = max_size / (1024 * 1024)
        if file_size > max_size:
            return False, f"❌ هذا الملف كبير جداً! الحد الأقصى المسموح به هو {int(max_size_mb)} ميجابايت."
    
    elif file_type == "photo":
        max_size = MAX_PHOTO_SIZE
        max_size_mb = max_size / (1024 * 1024)
        if file_size > max_size:
            return False, f"❌ حجم الصورة كبير جداً! الحد الأقصى هو {int(max_size_mb)} ميجابايت."
    
    return True, ""

def validate_pdf_pages(page_count: int) -> Tuple[bool, str]:
    """
    Validate PDF page count against limit.
    
    Args:
        page_count: Number of pages in PDF
        
    Returns:
        Tuple[bool, str]: (is_valid, error_message)
    """
    if page_count > MAX_PDF_PAGES:
        return False, f"❌ الملف يحتوي على ({page_count}) صفحة! الحد الأقصى المسموح به هو {MAX_PDF_PAGES} صفحة."
    
    return True, ""

def validate_user_id(user_id: int) -> Tuple[bool, str]:
    """
    Validate user ID format.
    
    Args:
        user_id: User ID to validate
        
    Returns:
        Tuple[bool, str]: (is_valid, error_message)
    """
    if not isinstance(user_id, int) or user_id <= 0:
        return False, "Invalid user ID"
    
    return True, ""

def validate_points_amount(amount: int) -> Tuple[bool, str]:
    """
    Validate points amount.
    
    Args:
        amount: Points amount to validate
        
    Returns:
        Tuple[bool, str]: (is_valid, error_message)
    """
    if not isinstance(amount, int) or amount <= 0:
        return False, "Points amount must be a positive integer"
    
    if amount > 1000000:  # Reasonable upper limit
        return False, "Points amount exceeds maximum allowed"
    
    return True, ""

def validate_text_not_empty(text: str) -> Tuple[bool, str]:
    """
    Validate that text is not empty.
    
    Args:
        text: Text to validate
        
    Returns:
        Tuple[bool, str]: (is_valid, error_message)
    """
    if not text or not text.strip():
        return False, "Text cannot be empty"
    
    return True, ""

def safe_parse_command_args(args: str, expected_count: int = 2) -> Tuple[bool, list]:
    """
    Safely parse command arguments.
    
    Args:
        args: Raw command arguments string
        expected_count: Expected number of arguments
        
    Returns:
        Tuple[bool, list]: (success, parsed_args or error_message)
    """
    if not args:
        return False, ["No arguments provided"]
    
    try:
        parsed = args.strip().split()
        if len(parsed) < expected_count:
            return False, [f"Expected at least {expected_count} arguments, got {len(parsed)}"]
        
        return True, parsed
    except Exception as e:
        logger.error(f"Error parsing command args: {e}")
        return False, [str(e)]
