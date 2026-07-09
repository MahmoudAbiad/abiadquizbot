"""
AI integration for quiz generation.
Hybrid API Flow: Routes PDFs to Gemini and Photos to Groq.
Optimized with Gemini File API and safe 5-image cap for Groq Fallback.
"""

import os
import json
import datetime
import random
import asyncio
import fitz  # PyMuPDF
import base64
from typing import Optional, List, Dict, Any
from google import genai
from google.genai import types
from groq import AsyncGroq
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

# إعداد مفاتيح جيميني
api_keys_raw = os.getenv("GEMINI_API_KEYS", "")
API_KEYS: List[str] = [k.strip() for k in api_keys_raw.split(",") if k.strip()]
if not API_KEYS and os.getenv("GEMINI_API_KEY"):
    API_KEYS = [os.getenv("GEMINI_API_KEY")]

blocked_keys: Dict[int, datetime.datetime] = {}
GROQ_API_KEY = os.getenv("GROQ_API_KEY", "")

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
    file_hash = await asyncio.to_thread(calculate_file_hash, file_path)
    
    if not skip_cache:
        cached_data = await asyncio.to_thread(get_cached_quiz, file_hash)
        if cached_data:
            log_info(logger, f"Cache Hit! Returning cached questions for hash: {file_hash}")
            return cached_data["questions_data"]

    prompt = SYSTEM_PROMPT_GENERATE_QUESTIONS.replace("{count}", str(count))
    

    # ====================================================
    # 📸 أولاً: مسار معالجة الصور عبر Groq 
    # ====================================================
    if not file_path.lower().endswith('.pdf'):
        if not GROQ_API_KEY:
            log_error(logger, "خطأ: لم يتم العثور على مفتاح GROQ_API_KEY.")
            return None
        
        log_info(logger, f"توجيه الطلب إلى Groq لمعالجة الصورة: {file_path}")
        try:
            with open(file_path, "rb") as img_file:
                base64_image = base64.b64encode(img_file.read()).decode('utf-8')
            
            groq_client = AsyncGroq(api_key=GROQ_API_KEY)
            response = await asyncio.wait_for(
                groq_client.chat.completions.create(
                    model="meta-llama/llama-4-scout-17b-16e-instruct",
                    messages=[
                        {
                            "role": "user",
                            "content": [
                                {"type": "text", "text":prompt },
                                {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{base64_image}"}}
                            ]
                        }
                    ],
                    response_format={"type": "json_object"},
                    temperature=0.7
                ),
                timeout=45.0
            )
            
            parsed_json = json.loads(response.choices[0].message.content)
            validated_response = QuizResponse(**parsed_json)
            questions = [q.model_dump() for q in validated_response.questions]
            
            total_tokens = response.usage.total_tokens if hasattr(response, 'usage') and response.usage else 0
            await asyncio.to_thread(save_quiz_to_cache, file_hash, questions, total_tokens)
            return questions
            
        except Exception as e:
            log_error(logger, f"خطأ أثناء توليد الكويز من الصورة عبر Groq: {e}")
            return None

    # ====================================================
    # 📄 ثانياً: مسار ملفات الـ PDF (جيميني أولاً كخيار أساسي)
    # ====================================================
    log_info(logger, f"توجيه الطلب إلى Gemini لمعالجة مستند PDF: {file_path}")
    contents = []
    total_text = ""
    is_scanned = False
    
    try:
        doc = fitz.open(file_path)
        total_text = "".join([page.get_text() for page in doc])
        doc.close()
        
        if len(total_text.strip()) < 100:
            is_scanned = True
            log_warning(logger, "الملف يبدو ممسوحاً ضوئياً (Scanned PDF).")
        
        if not is_scanned and len(total_text.strip()) > 200:
            contents = [prompt, total_text[:MAX_TEXT_LENGTH_FOR_AI]]
    except Exception as e:
        log_error(logger, f"خطأ في قراءة الملف الفنية: {e}")
        return None

    # محاولة إرسال الملف إلى جيميني
    if API_KEYS:
        max_attempts = min(len(API_KEYS), 2)
        for attempt in range(max_attempts):
            available = _get_available_key_indices()
            if not available: 
                blocked_keys.clear()
                available = list(range(len(API_KEYS)))
            
            idx = random.choice(available)
            await asyncio.sleep(random.uniform(0.3, 0.8))
            
            gemini_file = None
            try:
                client = genai.Client(api_key=API_KEYS[idx])
                
                if is_scanned or not contents:
                    log_info(logger, f"رفع الملف عبر Gemini File API المستقر والمخصص للملفات...")
                    gemini_file = await asyncio.to_thread(client.files.upload, file=file_path)
                    contents = [gemini_file, prompt]
                
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
                    timeout=20.0
                )
                
                if gemini_file:
                    asyncio.create_task(asyncio.to_thread(client.files.delete, name=gemini_file.name))
                
                if response.parsed and hasattr(response.parsed, 'questions'):
                    questions = [q.model_dump() for q in response.parsed.questions]
                    total_tokens = response.usage_metadata.total_token_count if hasattr(response, 'usage_metadata') and response.usage_metadata else 0
                    await asyncio.to_thread(save_quiz_to_cache, file_hash, questions, total_tokens)
                    return questions
                
            except Exception as e:
                if gemini_file:
                    try: asyncio.create_task(asyncio.to_thread(client.files.delete, name=gemini_file.name))
                    except: pass
                    
                error_msg = str(e).lower()
                log_warning(logger, f"فشل مفتاح جيميني {idx} في المحاولة {attempt + 1}: {e}")
                
                if any(k in error_msg for k in QUOTA_ERROR_KEYWORDS):
                    blocked_keys[idx] = datetime.datetime.now() + datetime.timedelta(hours=KEY_BLOCK_QUOTA_EXHAUSTED)
                else:
                    blocked_keys[idx] = datetime.datetime.now() + datetime.timedelta(minutes=KEY_BLOCK_TEMPORARY_ERROR)

    # ====================================================
    # 🚀 الخيار البديل الذكي (Fallback) إلى Groq في حال فشل جيميني
    # ====================================================
    if GROQ_API_KEY:
        log_warning(logger, "⚠️ تفعل نظام الإنقاذ الفوري: جاري تحويل الطلب إلى Groq لإنهاء الانتظار...")
        try:
            groq_client = AsyncGroq(api_key=GROQ_API_KEY)
            groq_messages_content = [{"type": "text", "text": groq_instructions}]
            
            if is_scanned or len(total_text.strip()) < 100:
                doc = fitz.open(file_path)
                
                # 💡 تعديل جوهري: تحديد الحد الأقصى لـ Groq بـ 5 صور فقط منعاً للخطأ 400
                max_pages = min(len(doc), 5)
                log_info(logger, f"جاري تحويل أول {max_pages} صفحات من الـ PDF الممسوح ضوئياً لـ Groq Vision...")
                
                matrix = fitz.Matrix(0.5, 0.5)
                for i in range(max_pages):
                    page = doc[i]
                    pix = page.get_pixmap(matrix=matrix)
                    img_bytes = pix.tobytes("jpg")  # تم تنظيف دالة تحويل البايتات لتكون متوافقة بالكامل
                    
                    base64_page = base64.b64encode(img_bytes).decode('utf-8')
                    groq_messages_content.append({
                        "type": "image_url",
                        "image_url": {"url": f"data:image/jpeg;base64,{base64_page}"}
                    })
                doc.close()
            else:
                groq_messages_content.append({"type": "text", "text": f"\n\nالمحتوى النصي للمستند:\n{total_text[:MAX_TEXT_LENGTH_FOR_AI]}"})
            
            response = await asyncio.wait_for(
                groq_client.chat.completions.create(
                    model="meta-llama/llama-4-scout-17b-16e-instruct",
                    messages=[{"role": "user", "content": groq_messages_content}],
                    response_format={"type": "json_object"},
                    temperature=0.7
                ),
                timeout=90.0
            )
            
            parsed_json = json.loads(response.choices[0].message.content)
            validated_response = QuizResponse(**parsed_json)
            questions = [q.model_dump() for q in validated_response.questions]
            
            total_tokens = response.usage.total_tokens if hasattr(response, 'usage') and response.usage else 0
            await asyncio.to_thread(save_quiz_to_cache, file_hash, questions, total_tokens)
            
            log_info(logger, "✅ تم إنقاذ الملف الممسوح ضوئياً وتوليد الأسئلة عبر Groq بنجاح باهر وبدون تأخير!")
            return questions
            
        except Exception as groq_err:
            log_error(logger, f"❌ حتى الخيار البديل (Groq) فشل في معالجة الملف: {groq_err}")

    return None