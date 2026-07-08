"""
Gemini AI integration for quiz generation.
Optimized for Hybrid API Flow (Single request processing).
"""

import os
import json
import datetime
import random
import asyncio
import fitz  # PyMuPDF
from typing import Optional, List, Dict, Any
from google import genai
from google.genai import types
from pydantic import BaseModel, Field
from dotenv import load_dotenv

from constants import (
    GEMINI_MODEL, MAX_TEXT_LENGTH_FOR_AI, KEY_BLOCK_QUOTA_EXHAUSTED,
    KEY_BLOCK_TEMPORARY_ERROR, SYSTEM_PROMPT_GENERATE_QUESTIONS,
    QUOTA_ERROR_KEYWORDS
)
from logger import get_logger, log_error, log_warning, log_info

load_dotenv()
logger = get_logger(__name__)

# إعداد المفاتيح
api_keys_raw = os.getenv("GEMINI_API_KEYS", "")
API_KEYS: List[str] = [k.strip() for k in api_keys_raw.split(",") if k.strip()]
if not API_KEYS and os.getenv("GEMINI_API_KEY"):
    API_KEYS = [os.getenv("GEMINI_API_KEY")]

blocked_keys: Dict[int, datetime.datetime] = {}

class QuizQuestion(BaseModel):
    question: str = Field(description="نص السؤال.")
    options: List[str] = Field(description="أربعة خيارات.")
    correct_option_id: int = Field(description="مؤشر الخيار الصحيح (0-3).")
    hint: str = Field(description="تلميح ذكي.")
    explanation: str = Field(default="", description="شرح الإجابة.")

def _get_available_key_indices() -> List[int]:
    now = datetime.datetime.now()
    available = []
    for idx in range(len(API_KEYS)):
        if idx not in blocked_keys or now >= blocked_keys[idx]:
            if idx in blocked_keys: del blocked_keys[idx]
            available.append(idx)
    return available

async def generate_quiz_smart(file_path: str, count: int) -> Optional[List[Dict[str, Any]]]:
    """
    الدالة الرئيسية الموحدة: تعالج الملف (PDF أو صورة) وترسل الطلب لـ Gemini.
    """
    if not API_KEYS: 
        log_error(logger, "لم يتم العثور على مفاتيح API.")
        return None

    contents = []
    prompt = SYSTEM_PROMPT_GENERATE_QUESTIONS.replace("{count}", str(count))
    
    # 1. تجهيز المحتوى بناءً على نوع الملف
    try:
        if file_path.lower().endswith(('.pdf')):
            doc = fitz.open(file_path)
            total_text = "".join([page.get_text() for page in doc])
            doc.close()
            
            # إذا كان الـ PDF يحتوي على نص نصي
            if len(total_text.strip()) > 200:
                contents = [prompt, total_text[:MAX_TEXT_LENGTH_FOR_AI]]
            else:
                # PDF ممسوح ضوئياً (Scanned)
                with open(file_path, "rb") as f:
                    contents = [types.Part.from_bytes(data=f.read(), mime_type="application/pdf"), prompt]
        else:
            # معالجة الصور
            with open(file_path, "rb") as f:
                contents = [types.Part.from_bytes(data=f.read(), mime_type="image/jpeg"), prompt]
    except Exception as e:
        log_error(logger, f"خطأ في معالجة الملف: {e}")
        return None

    # 2. إرسال الطلب مع تدوير المفاتيح
    max_attempts = len(API_KEYS)
    for _ in range(max_attempts):
        available = _get_available_key_indices()
        if not available: 
            blocked_keys.clear()
            available = list(range(len(API_KEYS)))
        
        idx = random.choice(available)
        await asyncio.sleep(random.uniform(0.5, 1.5))
        
        try:
            client = genai.Client(api_key=API_KEYS[idx])
            response = await client.aio.models.generate_content(
                model=GEMINI_MODEL,
                contents=contents,
                config=types.GenerateContentConfig(
                    response_mime_type="application/json", 
                    response_schema=list[QuizQuestion]
                ),
            )
            result = json.loads(response.text)
            return result
        except Exception as e:
            error_msg = str(e).lower()
            block_time = KEY_BLOCK_QUOTA_EXHAUSTED if any(k in error_msg for k in QUOTA_ERROR_KEYWORDS) else KEY_BLOCK_TEMPORARY_ERROR
            blocked_keys[idx] = datetime.datetime.now() + datetime.timedelta(hours=block_time)
            log_warning(logger, f"فشل المفتاح {idx}: {e}")
            
    return None