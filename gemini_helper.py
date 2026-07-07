"""
Gemini AI integration for direct file processing and quiz generation.
Handles API key rotation and quota management.
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

# ==================== Configuration ====================
api_keys_raw = os.getenv("GEMINI_API_KEYS", "")
API_KEYS: List[str] = [k.strip() for k in api_keys_raw.split(",") if k.strip()]

if not API_KEYS and os.getenv("GEMINI_API_KEY"):
    API_KEYS = [os.getenv("GEMINI_API_KEY")]

current_key_idx: int = 0
blocked_keys: Dict[int, datetime.datetime] = {}

def has_gemini_api_keys() -> bool:
    """التحقق من وجود مفاتيح ربط مبرمجة"""
    return bool(API_KEYS)

# ==================== Data Models ====================
class QuizQuestion(BaseModel):
    question: str = Field(description="نص السؤال.")
    options: List[str] = Field(description="أربعة خيارات.")
    correct_option_id: int = Field(description="رقم الخيار الصحيح.")
    hint: str = Field(description="تلميح.")
    explanation: str = Field(description="شرح.")

# ==================== Helper Functions ====================
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

# ==================== Main Function ====================
def generate_quiz_from_file(file_path: str, count: int, mime_type: str = None) -> Optional[Tuple[List[Dict[str, Any]], int]]:
    global current_key_idx
    if not API_KEYS: 
        log_error(logger, "❌ لم يتم العثور على أي مفاتيح API لـ Gemini.")
        return None
    
    prompt = SYSTEM_PROMPT_GENERATE_QUESTIONS.format(count=count)
    
    for _ in range(len(API_KEYS)):
        if _is_key_blocked():
            _rotate_api_key()
            continue
        
        uploaded_file = None
        try:
            key = API_KEYS[current_key_idx]
            # تهيئة العميل ديناميكياً بالمفتاح الحالي النشط
            client = genai.Client(api_key=key)
            
            log_info(logger, f"🔄 محاولة رفع ومعالجة الملف بالمفتاح رقم [{current_key_idx}]...")
            
            # 🔥 تمرير الـ mime_type لحل مشكلة الفشل السريع للملفات المؤقتة
            if mime_type:
                uploaded_file = client.files.upload(file=file_path, mime_type=mime_type)
            else:
                uploaded_file = client.files.upload(file=file_path)
            
            # انتظار جاهزية الملف على خوادم جوجل بشكل آمن ومرن
            state_attempts = 0
            while state_attempts < 15:
                state_str = getattr(uploaded_file.state, "name", str(uploaded_file.state)).upper()
                if "PROCESSING" not in state_str:
                    break
                time.sleep(1)
                uploaded_file = client.files.get(name=uploaded_file.name)
                state_attempts += 1
            
            # التحقق من نجاح المعالجة
            final_state = getattr(uploaded_file.state, "name", str(uploaded_file.state)).upper()
            if "FAILED" in final_state:
                raise Exception("فشلت خوادم Google في معالجة محتوى الملف المرفوع.")

            # إرسال الطلب لتوليد الأسئلة بصيغة JSON وطبقاً للهيكل المحدد
            response = client.models.generate_content(
                model=GEMINI_MODEL,
                contents=[uploaded_file, prompt],
                config=types.GenerateContentConfig(
                    response_mime_type="application/json",
                    response_schema=list[QuizQuestion],
                ),
            )
            
            total_tokens = response.usage_metadata.total_token_count if response.usage_metadata else 0
            
            # حذف الملف فوراً بعد الانتهاء لتنظيف المساحة السحابية لجوجل
            try:
                client.files.delete(name=uploaded_file.name)
            except:
                pass
            
            result = json.loads(response.text)
            return result, total_tokens
            
        except Exception as e:
            log_error(logger, f"❌ خطأ في مفتاح {current_key_idx}: {str(e)}")
            if uploaded_file:
                try: 
                    client.files.delete(name=uploaded_file.name)
                except: 
                    pass
            
            # إدارة حظر وتبديل المفاتيح بناءً على نوع الخطأ
            if _is_quota_error(str(e)):
                log_error(logger, f"⚠️ المفتاح {current_key_idx} استهلك الحصة بالكامل (Quota Exceeded).")
                _block_current_key(KEY_BLOCK_QUOTA_EXHAUSTED)
            else:
                _block_current_key(KEY_BLOCK_TEMPORARY_ERROR)
                
            _rotate_api_key()
            
    return None