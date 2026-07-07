"""
Gemini AI integration for direct file processing and quiz generation.
Handles API key rotation, quota management, and Usage Metadata tracking.
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
    GEMINI_MODEL, KEY_BLOCK_QUOTA_EXHAUSTED, KEY_BLOCK_TEMPORARY_ERROR,
    SYSTEM_PROMPT_GENERATE_QUESTIONS, QUOTA_ERROR_KEYWORDS, ERROR_API_KEYS_NOT_CONFIGURED
)
from logger import get_logger, log_error, log_warning, log_info

load_dotenv()
logger = get_logger(__name__)

api_keys_raw = os.getenv("GEMINI_API_KEYS", "")
API_KEYS: List[str] = [k.strip() for k in api_keys_raw.split(",") if k.strip()]

if not API_KEYS and os.getenv("GEMINI_API_KEY"):
    API_KEYS = [os.getenv("GEMINI_API_KEY")]

current_key_idx: int = 0
blocked_keys: Dict[int, datetime.datetime] = {}

def has_gemini_api_keys() -> bool:
    return bool(API_KEYS)

class QuizQuestion(BaseModel):
    question: str = Field(description="نص السؤال الاختياري المستخرج من المستند المرفق حصراً.")
    options: List[str] = Field(description="أربعة خيارات فريدة ومتوازنة للسؤال.")
    correct_option_id: int = Field(description="مؤشر الخيار الصحيح من 0 إلى 3.")
    hint: str = Field(description="تلميح ذكي ومساعد يقرب الفكرة للطالب.")
    explanation: str = Field(description="شرح أكاديمي مقتضب يوضح لماذا هذه الإجابة هي الصحيحة.")

def _is_quota_error(error_msg: str) -> bool:
    return any(k in error_msg.lower() for k in QUOTA_ERROR_KEYWORDS)

def _rotate_api_key() -> None:
    global current_key_idx
    current_key_idx = (current_key_idx + 1) % len(API_KEYS)

def _block_current_key(hours: int) -> None:
    blocked_keys[current_key_idx] = datetime.datetime.now() + datetime.timedelta(hours=hours)

def _is_key_blocked() -> bool:
    if current_key_idx not in blocked_keys: return False
    if datetime.datetime.now() >= blocked_keys[current_key_idx]:
        del blocked_keys[current_key_idx]
        return False
    return True

def generate_quiz_from_file(file_path: str, count: int, mime_type: str = None) -> Optional[Tuple[List[Dict[str, Any]], int]]:
    """
    رفع الملف مباشرة لجيميني وقراءة عدد التوكينات الفعلي المستهلك عبر usage_metadata.
    Returns: Tuple[quiz_data, total_tokens]
    """
    global current_key_idx
    if not API_KEYS: return None
    
    prompt = SYSTEM_PROMPT_GENERATE_QUESTIONS.format(count=count)
    max_attempts = len(API_KEYS)
    
    for attempt in range(max_attempts):
        if _is_key_blocked():
            _rotate_api_key()
            continue
        
        uploaded_file = None
        try:
            key = API_KEYS[current_key_idx]
            client = genai.Client(api_key=key)
            
            uploaded_file = client.files.upload(file=file_path, mime_type=mime_type)
            
            response = client.models.generate_content(
                model=GEMINI_MODEL,
                contents=[uploaded_file, prompt],
                config=types.GenerateContentConfig(
                    response_mime_type="application/json",
                    response_schema=list[QuizQuestion],
                ),
            )
            
            total_tokens = 0
            if response.usage_metadata:
                total_tokens = response.usage_metadata.total_token_count
            
            client.files.delete(name=uploaded_file.name)
            
            result = json.loads(response.text)
            log_info(logger, f"Generated successfully. Tokens: {total_tokens}")
            return result, total_tokens
            
        except Exception as e:
            error_msg = str(e)
            if uploaded_file:
                try: client.files.delete(name=uploaded_file.name)
                except: pass
            
            if _is_quota_error(error_msg):
                _block_current_key(KEY_BLOCK_QUOTA_EXHAUSTED)
            else:
                _block_current_key(KEY_BLOCK_TEMPORARY_ERROR)
            
            _rotate_api_key()
            
    return None