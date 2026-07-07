"""
Gemini AI integration - Debugging Mode enabled to catch JSON/Model errors.
"""

import os
import json
import time
import datetime
from typing import Optional, List, Dict, Any, Tuple
from dotenv import load_dotenv
from google import genai
from google.genai import types
from pydantic import BaseModel, Field
from constants import (
    GEMINI_MODEL, KEY_BLOCK_QUOTA_EXHAUSTED, KEY_BLOCK_TEMPORARY_ERROR,
    SYSTEM_PROMPT_GENERATE_QUESTIONS, QUOTA_ERROR_KEYWORDS
)
from logger import get_logger, log_error, log_info

load_dotenv()
logger = get_logger(__name__)

# [تكوين المفاتيح كما هو]
api_keys_raw = os.getenv("GEMINI_API_KEYS", "")
API_KEYS: List[str] = [k.strip() for k in api_keys_raw.split(",") if k.strip()]
if not API_KEYS and os.getenv("GEMINI_API_KEY"):
    API_KEYS = [os.getenv("GEMINI_API_KEY")]

current_key_idx: int = 0
blocked_keys: Dict[int, datetime.datetime] = {}

class QuizQuestion(BaseModel):
    question: str = Field(...)
    options: List[str] = Field(...)
    correct_option_id: int = Field(...)
    hint: str = Field(...)
    explanation: str = Field(...)

def generate_quiz_from_file(file_path: str, count: int, mime_type: str = None) -> Optional[Tuple[List[Dict[str, Any]], int]]:
    global current_key_idx
    if not API_KEYS: return None
    
    prompt = SYSTEM_PROMPT_GENERATE_QUESTIONS.format(count=count)
    
    for _ in range(len(API_KEYS)):
        try:
            key = API_KEYS[current_key_idx]
            client = genai.Client(api_key=key)
            
            uploaded_file = client.files.upload(file=file_path)
            
            # انتظار الجاهزية لملفات PDF/الصور الثقيلة
            state_attempts = 0
            while uploaded_file.state.name == "PROCESSING" and state_attempts < 10:
                time.sleep(1)
                uploaded_file = client.files.get(name=uploaded_file.name)
                state_attempts += 1
            
            response = client.models.generate_content(
                model=GEMINI_MODEL,
                contents=[uploaded_file, prompt],
                config=types.GenerateContentConfig(
                    response_mime_type="application/json",
                    response_schema=list[QuizQuestion],
                ),
            )
            
            # 🔥 [DEBUG MODE]: طباعة النص الخام قبل المحاولة لتحويله لـ JSON
            log_info(logger, f"DEBUG: Raw response from Gemini: {response.text}")
            
            total_tokens = response.usage_metadata.total_token_count if response.usage_metadata else 0
            client.files.delete(name=uploaded_file.name)
            
            result = json.loads(response.text)
            return result, total_tokens
            
        except json.JSONDecodeError as e:
            log_error(logger, f"❌ خطأ في تنسيق JSON المستلم من جيمني: {e}")
        except Exception as e:
            log_error(logger, f"❌ خطأ عام أثناء التوليد: {str(e)}")
            
        _rotate_api_key()
            
    return None

def _rotate_api_key():
    global current_key_idx
    current_key_idx = (current_key_idx + 1) % len(API_KEYS)