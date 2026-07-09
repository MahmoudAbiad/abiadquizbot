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
from utils import calculate_file_hash
from supabase_helper import get_cached_quiz, save_quiz_to_cache

load_dotenv()
logger = get_logger(__name__)

# إعداد المفاتيح
api_keys_raw = os.getenv("GEMINI_API_KEYS", "")
API_KEYS: List[str] = [k.strip() for k in api_keys_raw.split(",") if k.strip()]
if not API_KEYS and os.getenv("GEMINI_API_KEY"):
    API_KEYS = [os.getenv("GEMINI_API_KEY")]

blocked_keys: Dict[int, datetime.datetime] = {}

# ==================== Pydantic Schemas ====================
class QuizQuestion(BaseModel):
    question: str = Field(description="نص السؤال.")
    options: List[str] = Field(description="أربعة خيارات.")
    correct_option_id: int = Field(description="مؤشر الخيار الصحيح (0-3).")
    hint: str = Field(description="تلميح ذكي.")
    explanation: str = Field(default="", description="شرح الإجابة.")

class QuizResponse(BaseModel):
    questions: List[QuizQuestion]

# ==================== Helper Functions ====================
def _get_available_key_indices() -> List[int]:
    now = datetime.datetime.now()
    available = []
    for idx in range(len(API_KEYS)):
        if idx not in blocked_keys or now >= blocked_keys[idx]:
            if idx in blocked_keys: del blocked_keys[idx]
            available.append(idx)
    return available

# ==================== Main Generation Function ====================
async def generate_quiz_smart(file_path: str, count: int, skip_cache: bool = False) -> Optional[List[Dict[str, Any]]]:
    if not API_KEYS: 
        log_error(logger, "لم يتم العثور على مفاتيح API.")
        return None

    # تشغيل الدالات المتزامنة في Threads منفصلة لحماية الـ Event Loop من التجمد
    file_hash = await asyncio.to_thread(calculate_file_hash, file_path)
    
    # 🆕 تعديل: التحقق من الكاش يتم فقط إذا لم يطلب المستخدم التوليد الفعلي الجديد
    if not skip_cache:
        cached_data = await asyncio.to_thread(get_cached_quiz, file_hash)
        if cached_data:
            log_info(logger, f"Cache Hit! Returning cached questions for hash: {file_hash}")
            return cached_data["questions_data"]

    contents = []
    prompt = SYSTEM_PROMPT_GENERATE_QUESTIONS.replace("{count}", str(count))
    
    try:
        if file_path.lower().endswith('.pdf'):
            doc = fitz.open(file_path)
            total_text = "".join([page.get_text() for page in doc])
            doc.close()
            
            if len(total_text.strip()) > 200:
                contents = [prompt, total_text[:MAX_TEXT_LENGTH_FOR_AI]]
            else:
                with open(file_path, "rb") as f:
                    contents = [types.Part.from_bytes(data=f.read(), mime_type="application/pdf"), prompt]
        else:
            with open(file_path, "rb") as f:
                contents = [types.Part.from_bytes(data=f.read(), mime_type="image/jpeg"), prompt]
    except Exception as e:
        log_error(logger, f"خطأ في قراءة الملف: {e}")
        return None

    max_attempts = len(API_KEYS)
    for attempt in range(max_attempts):
        available = _get_available_key_indices()
        if not available: 
            blocked_keys.clear()
            available = list(range(len(API_KEYS)))
        
        idx = random.choice(available)
        await asyncio.sleep(random.uniform(0.5, 1.5))
        
        try:
            client = genai.Client(api_key=API_KEYS[idx])
            
            # إضافة مهلة زمنية للطلب لمنع التعليق اللانهائي في حال وجود مشاكل شبكية
            response = await asyncio.wait_for(
                client.aio.models.generate_content(
                    model=GEMINI_MODEL,
                    contents=contents,
                    config=types.GenerateContentConfig(
                        response_mime_type="application/json", 
                        response_schema=QuizResponse,
                        temperature=0.7
                    ),
                ),
                timeout=45.0
            )
            
            if response.parsed and hasattr(response.parsed, 'questions'):
                # 1. تحويل كائنات Pydantic إلى قواميس (Dicts) لتقبلها Supabase بأمان
                questions = [q.model_dump() for q in response.parsed.questions]
                
                # 2. استخراج التوكينات الفعلية المستهلكة
                total_tokens = 0
                if hasattr(response, 'usage_metadata') and response.usage_metadata:
                    total_tokens = response.usage_metadata.total_token_count
                    
                # 3. حفظ الكويز في الكاش 
                await asyncio.to_thread(save_quiz_to_cache, file_hash, questions, total_tokens)
                
                return questions
            
            return []
            
        except Exception as e:
            error_msg = str(e).lower()
            log_warning(logger, f"فشل المفتاح {idx} في المحاولة {attempt + 1}: {e}")
            
            if any(k in error_msg for k in QUOTA_ERROR_KEYWORDS):
                blocked_keys[idx] = datetime.datetime.now() + datetime.timedelta(hours=KEY_BLOCK_QUOTA_EXHAUSTED)
            else:
                blocked_keys[idx] = datetime.datetime.now() + datetime.timedelta(minutes=KEY_BLOCK_TEMPORARY_ERROR)
            
    return None