"""
Gemini AI integration for text extraction and quiz question generation.
Handles API key rotation and intelligent blocking for quota management.
"""

import os
import json
import datetime
from typing import Optional, List, Dict, Any, Tuple

from dotenv import load_dotenv
from google import genai
from google.genai import types
from pydantic import BaseModel, Field

from constants import (
    GEMINI_MODEL,
    MAX_TEXT_LENGTH_FOR_AI,
    AI_REQUEST_TIMEOUT,
    KEY_BLOCK_QUOTA_EXHAUSTED,
    KEY_BLOCK_TEMPORARY_ERROR,
    SYSTEM_PROMPT_EXTRACT_TEXT,
    SYSTEM_PROMPT_GENERATE_QUESTIONS,
    QUOTA_ERROR_KEYWORDS,
    ERROR_API_KEYS_NOT_CONFIGURED,
    ERROR_ALL_KEYS_EXHAUSTED,
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


class QuizQuestion(BaseModel):
    """Model for a quiz question with metadata."""

    question: str = Field(description="نص السؤال الاختياري المستخرج من النص المرفق حصراً.")
    options: List[str] = Field(description="أربعة خيارات فريدة ومتوازنة للسؤال.")
    correct_option_id: int = Field(description="مؤشر الخيار الصحيح من 0 إلى 3.")
    hint: str = Field(description="تلميح ذكي ومساعد يقرب الفكرة للطالب دون إعطائه الحل المباشر.")
    explanation: str = Field(default="", description="شرح مختصر للإجابة الصحيحة.")
    was_corrupted_text_fixed: bool = Field(
        default=False,
        description="true فقط إذا تم إصلاح تشوهات النص الناتجة عن OCR سياقياً.",
    )


def has_gemini_api_keys() -> bool:
    return bool(API_KEYS)


def _validate_api_keys() -> bool:
    if not API_KEYS:
        log_error(logger, "No GEMINI_API_KEYS configured in environment")
        return False
    return True


def _rotate_api_key() -> None:
    global current_key_idx
    if API_KEYS:
        current_key_idx = (current_key_idx + 1) % len(API_KEYS)


def _is_quota_error(error_msg: str) -> bool:
    error_lower = error_msg.lower()
    return any(keyword in error_lower for keyword in QUOTA_ERROR_KEYWORDS)


def _block_current_key(hours: int) -> None:
    unblock_time = datetime.datetime.now() + datetime.timedelta(hours=hours)
    blocked_keys[current_key_idx] = unblock_time
    log_warning(logger, f"Key {current_key_idx} blocked until {unblock_time.strftime('%Y-%m-%d %H:%M:%S')}")


def _is_key_blocked() -> bool:
    if current_key_idx not in blocked_keys:
        return False

    if datetime.datetime.now() >= blocked_keys[current_key_idx]:
        del blocked_keys[current_key_idx]
        return False

    return True


def _detect_image_mime_type(image_bytes: bytes) -> str:
    if image_bytes.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if image_bytes.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if image_bytes.startswith(b"GIF87a") or image_bytes.startswith(b"GIF89a"):
        return "image/gif"
    if image_bytes.startswith(b"RIFF") and len(image_bytes) >= 12 and image_bytes[8:12] == b"WEBP":
        return "image/webp"
    return "image/png"


def extract_text_from_image(image_bytes: bytes) -> str:
    """Extract text from image using Gemini Vision API."""
    global current_key_idx

    if not _validate_api_keys():
        return ERROR_API_KEYS_NOT_CONFIGURED

    max_attempts = len(API_KEYS)

    for _ in range(max_attempts):
        if _is_key_blocked():
            log_info(logger, f"Skipping blocked key {current_key_idx}")
            _rotate_api_key()
            continue

        try:
            key = API_KEYS[current_key_idx]
            client = genai.Client(api_key=key)
            mime_type = _detect_image_mime_type(image_bytes)

            response = client.models.generate_content(
                model=GEMINI_MODEL,
                contents=[
                    types.Part.from_bytes(data=image_bytes, mime_type=mime_type),
                    SYSTEM_PROMPT_EXTRACT_TEXT,
                ],
            )

            log_info(logger, f"Successfully extracted text using key {current_key_idx}")
            return response.text or ""

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
    """Generate quiz questions from text using Gemini API."""
    global current_key_idx

    if not _validate_api_keys():
        return None

    text = text[:MAX_TEXT_LENGTH_FOR_AI]
    prompt = SYSTEM_PROMPT_GENERATE_QUESTIONS.format(count=count)
    max_attempts = len(API_KEYS)

    for _ in range(max_attempts):
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
            if isinstance(result, dict) and "questions" in result:
                result = result["questions"]

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


def generate_quiz_from_file(
    file_path: str,
    count: int,
    mime_type: Optional[str] = None,
) -> Optional[Tuple[List[Dict[str, Any]], int]]:
    """
    Compatibility wrapper kept for older call sites.
    The current bot flow extracts text elsewhere and calls get_questions_from_text.
    """
    _ = file_path
    _ = mime_type
    return None