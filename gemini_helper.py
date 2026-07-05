"""
Gemini AI integration for text extraction and quiz question generation.
Handles API key rotation and intelligent blocking for quota management.
"""

import os
import json
import datetime
from typing import Optional, List, Dict, Any
from dotenv import load_dotenv
from google import genai
from google.genai import types
from pydantic import BaseModel, Field
from constants import (
    GEMINI_MODEL, MAX_TEXT_LENGTH_FOR_AI, AI_REQUEST_TIMEOUT,
    KEY_BLOCK_QUOTA_EXHAUSTED, KEY_BLOCK_TEMPORARY_ERROR,
    SYSTEM_PROMPT_EXTRACT_TEXT, SYSTEM_PROMPT_GENERATE_QUESTIONS,
    QUOTA_ERROR_KEYWORDS, ERROR_API_KEYS_NOT_CONFIGURED,
    ERROR_ALL_KEYS_EXHAUSTED
)
from logger import get_logger, log_error, log_warning, log_info

load_dotenv()
logger = get_logger(__name__)

# ==================== Configuration ====================
api_keys_raw = os.getenv("GEMINI_API_KEYS", "")
API_KEYS: List[str] = [k.strip() for k in api_keys_raw.split(",") if k.strip()]

# Fallback for single API key format
if not API_KEYS and os.getenv("GEMINI_API_KEY"):
    API_KEYS = [os.getenv("GEMINI_API_KEY")]

current_key_idx: int = 0
blocked_keys: Dict[int, datetime.datetime] = {}

# ==================== Data Models ====================
class QuizQuestion(BaseModel):
    """Model for a quiz question with metadata"""
    question: str = Field(description="نص السؤال الاختياري المستخرج من النص المرفق حصراً.")
    options: List[str] = Field(description="أربعة خيارات فريدة ومتوازنة للسؤال.")
    correct_option_id: int = Field(description="مؤشر الخيار الصحيح من 0 إلى 3.")
    hint: str = Field(description="تلميح ذكي ومساعد يقرب الفكرة للطالب دون إعطائه الحل المباشر.")
    was_corrupted_text_fixed: bool = Field(description="تكون true فقط إذا تم إصلاح تشوهات النص الناتجة عن الـ OCR سياقياً.")

# ==================== Helper Functions ====================
def _validate_api_keys() -> bool:
    """
    Validate that API keys are configured.
    
    Returns:
        bool: True if API keys are available
    """
    if not API_KEYS:
        log_error(logger, "No GEMINI_API_KEYS configured in .env")
        return False
    return True

def _is_quota_error(error_msg: str) -> bool:
    """
    Check if error is due to quota exhaustion.
    
    Args:
        error_msg: Error message from API
        
    Returns:
        bool: True if quota error
    """
    error_lower = error_msg.lower()
    return any(keyword in error_lower for keyword in QUOTA_ERROR_KEYWORDS)

def _rotate_api_key() -> None:
    """Rotate to next available API key"""
    global current_key_idx
    current_key_idx = (current_key_idx + 1) % len(API_KEYS)

def _block_current_key(hours: int) -> None:
    """
    Block current API key for specified duration.
    
    Args:
        hours: Number of hours to block
    """
    unblock_time = datetime.datetime.now() + datetime.timedelta(hours=hours)
    blocked_keys[current_key_idx] = unblock_time
    log_warning(logger, f"Key {current_key_idx} blocked until {unblock_time.strftime('%Y-%m-%d %H:%M:%S')}")

def _is_key_blocked() -> bool:
    """
    Check if current API key is blocked.
    
    Returns:
        bool: True if key is currently blocked
    """
    if current_key_idx not in blocked_keys:
        return False
    
    if datetime.datetime.now() >= blocked_keys[current_key_idx]:
        # Unblock expired key
        del blocked_keys[current_key_idx]
        return False
    
    return True

# ==================== Main Functions ====================
def extract_text_from_image(image_bytes: bytes) -> str:
    """
    Extract text from image using Gemini Vision API.
    Implements intelligent API key rotation and blocking.
    
    Args:
        image_bytes: Image data in bytes
        
    Returns:
        str: Extracted text or error message
    """
    global current_key_idx
    
    if not _validate_api_keys():
        return ERROR_API_KEYS_NOT_CONFIGURED
    
    max_attempts = len(API_KEYS)
    
    for attempt in range(max_attempts):
        # Skip blocked keys
        if _is_key_blocked():
            log_info(logger, f"Skipping blocked key {current_key_idx}")
            _rotate_api_key()
            continue
        
        try:
            key = API_KEYS[current_key_idx]
            client = genai.Client(api_key=key)
            
            response = client.models.generate_content(
                model=GEMINI_MODEL,
                contents=[
                    types.Part.from_bytes(data=image_bytes, mime_type='image/png'),
                    SYSTEM_PROMPT_EXTRACT_TEXT
                ]
            )
            
            log_info(logger, f"Successfully extracted text using key {current_key_idx}")
            return response.text
            
        except Exception as e:
            error_msg = str(e)
            
            if _is_quota_error(error_msg):
                _block_current_key(KEY_BLOCK_QUOTA_EXHAUSTED)
                log_warning(logger, f"Quota exhausted on key {current_key_idx}")
            else:
                _block_current_key(KEY_BLOCK_TEMPORARY_ERROR)
                log_warning(logger, f"Temporary error on key {current_key_idx}: {error_msg}")
            
            _rotate_api_key()
    
    log_error(logger, "All API keys exhausted")
    return ERROR_ALL_KEYS_EXHAUSTED

def get_questions_from_text(text: str, count: int) -> Optional[List[Dict[str, Any]]]:
    """
    Generate quiz questions from text using Gemini API.
    
    Args:
        text: Input text to generate questions from
        count: Number of questions to generate
        
    Returns:
        Optional[List[Dict]]: List of quiz questions or None on failure
    """
    global current_key_idx
    
    if not _validate_api_keys():
        return None
    
    # Limit text length
    text = text[:MAX_TEXT_LENGTH_FOR_AI]
    
    prompt = SYSTEM_PROMPT_GENERATE_QUESTIONS.format(count=count)
    
    max_attempts = len(API_KEYS)
    
    for attempt in range(max_attempts):
        # Skip blocked keys
        if _is_key_blocked():
            log_info(logger, f"Skipping blocked key {current_key_idx}")
            _rotate_api_key()
            continue
        
        try:
            key = API_KEYS[current_key_idx]
            client = genai.Client(api_key=key)
            
            response = client.models.generate_content(
                model=GEMINI_MODEL,
                contents=[prompt, text],
                config=types.GenerateContentConfig(
                    response_mime_type="application/json",
                    response_schema=list[QuizQuestion],
                ),
            )
            
            result = json.loads(response.text)
            log_info(logger, f"Generated {len(result)} questions using key {current_key_idx}")
            return result
            
        except Exception as e:
            error_msg = str(e)
            
            if _is_quota_error(error_msg):
                _block_current_key(KEY_BLOCK_QUOTA_EXHAUSTED)
                log_warning(logger, f"Quota exhausted during question generation on key {current_key_idx}")
            else:
                _block_current_key(KEY_BLOCK_TEMPORARY_ERROR)
                log_warning(logger, f"Error during question generation on key {current_key_idx}: {error_msg}")
            
            _rotate_api_key()
    
    log_error(logger, "Failed to generate questions - all API keys exhausted")
    return None
