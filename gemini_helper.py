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

# [إعداد المفاتيح - لا تغيير]
api_keys_raw = os.getenv("GEMINI_API_KEYS", "")
API_KEYS = [k.strip() for k in api_keys_raw.split(",") if k.strip()]
if not API_KEYS and os.getenv("GEMINI_API_KEY"): API_KEYS = [os.getenv("GEMINI_API_KEY")]
current_key_idx = 0
blocked_keys = {}

def has_gemini_api_keys() -> bool: return bool(API_KEYS)

def generate_quiz_from_file(file_path: str, count: int) -> Tuple[Optional[List[Dict[str, Any]]], int]:
    global current_key_idx
    
    if not API_KEYS:
        logger.error("❌ لا توجد مفاتيح API مهيأة في القائمة.")
        return None, 0

    # محاولة استخدام المفاتيح المتاحة بالتناوب في حال فشل أحدها
    for _ in range(len(API_KEYS)):
        if current_key_idx in blocked_keys:
            # تحقق من انتهاء وقت الحظر هنا...
            pass
            
        current_key = API_KEYS[current_key_idx]
        uploaded_file = None
        
        try:
            # 🔥 الإصلاح الفوري: إنشاء العميل بالمفتاح الحالي النشط في الدورة
            client = genai.Client(api_key=current_key)
            
            logger.info(f"🔄 محاولة معالجة الملف باستخدام المفتاح رقم {current_key_idx}...")
            
            # رفع الملف إلى Gemini Files API
            uploaded_file = client.files.upload(file=file_path)
            
            # انتظار جاهزية الملف لمعالجته
            state_attempts = 0
            while uploaded_file.state.name == "PROCESSING" and state_attempts < 10:
                time.sleep(1)
                uploaded_file = client.files.get(name=uploaded_file.name)
                state_attempts += 1
            
            # إعداد الـ Prompt وتوليد المحتوى
            prompt = SYSTEM_PROMPT_GENERATE_QUESTIONS.format(count=count)
            
            response = client.models.generate_content(
                model=GEMINI_MODEL,
                contents=[uploaded_file, prompt],
                config=types.GenerateContentConfig(
                    response_mime_type="application/json",
                    response_schema=list[QuizQuestion],
                ),
            )
            
            total_tokens = response.usage_metadata.total_token_count if response.usage_metadata else 0
            
            # تنظيف الملف من خوادم جوجل بعد المعالجة بنجاح
            client.files.delete(name=uploaded_file.name)
            
            result = json.loads(response.text)
            return result, total_tokens
            
        except Exception as e:
            log_error(logger, f"❌ خطأ في مفتاح {current_key_idx}: {str(e)}")
            
            # محاولة تنظيف الملف في حال الفشل
            if uploaded_file:
                try:
                    client.files.delete(name=uploaded_file.name)
                except:
                    pass
            
            # الانتقال للمفتاح التالي في حال حدوث خطأ
            current_key_idx = (current_key_idx + 1) % len(API_KEYS)
            
    return None, 0