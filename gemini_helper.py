"""
AI integration for quiz generation.
Hybrid API Flow: Routes PDFs to Gemini and Photos to Groq.
Includes advanced Fallback for Scanned PDFs to Groq Vision (Supports up to 15 compressed pages).
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
    
    # صياغة تعليمات المخرجات لـ Groq
    groq_instructions = (
        f"{prompt}\n\n"
        "⚠️ تنبيه صارم: يجب أن تكون المخرجات عبارة عن كائن JSON صالح تماماً، يحتوي على حقل رئيسي باسم 'questions' وهو عبارة عن مصفوفة، وكل عنصر داخل المصفوفة يحتوي حصراً على الحقول التالية باللغة العربية:\n"
        "- question\n"
        "- options (مصفوفة من 4 خيارات)\n"
        "- correct_option_id (رقم من 0 إلى 3)\n"
        "- hint\n"
        "- explanation"
    )

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
                                {"type": "text", "text": groq_instructions},
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
        
        # إذا كان النص المستخرج شبه معدوم، فهذا يعني أن الملف ممسوح ضوئياً (Scanned)
        if len(total_text.strip()) < 100:
            is_scanned = True
            log_warning(logger, "الملف يبدو ممسوحاً ضوئياً (Scanned PDF).")
        
        if not is_scanned and len(total_text.strip()) > 200:
            contents = [prompt, total_text[:MAX_TEXT_LENGTH_FOR_AI]]
        else:
            # إرسال بايتات الملف كاملة لجيميني (جيميني يمتلك OCR داخلي للملفات)
            with open(file_path, "rb") as f:
                contents = [types.Part.from_bytes(data=f.read(), mime_type="application/pdf"), prompt]
    except Exception as e:
        log_error(logger, f"خطأ في قراءة الملف عبر Gemini: {e}")
        return None

    # محاولة إرسال الملف إلى جيميني
    if API_KEYS:
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
                    questions = [q.model_dump() for q in response.parsed.questions]
                    total_tokens = response.usage_metadata.total_token_count if hasattr(response, 'usage_metadata') and response.usage_metadata else 0
                    await asyncio.to_thread(save_quiz_to_cache, file_hash, questions, total_tokens)
                    return questions
                
            except Exception as e:
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
        log_warning(logger, "⚠️ تفعل نظام الإنقاذ: جاري تحويل الطلب إلى Groq...")
        try:
            groq_client = AsyncGroq(api_key=GROQ_API_KEY)
            groq_messages_content = [{"type": "text", "text": groq_instructions}]
            
            if is_scanned:
                # 💥 الخدعة الذكية: تحويل أول 15 صفحة وضغطها بشكل خارق لتناسب حجم طلب غروك الآمن
                log_info(logger, "جاري تحويل أول 15 صفحة من الـ PDF الممسوح ضوئياً وضغطها لـ Groq Vision...")
                doc = fitz.open(file_path)
                
                # تعيين الحد الأقصى الجديد إلى 15 صفحة
                max_pages = min(len(doc), 15)
                
                # إعداد مصفوفة خفض الدقة بنسبة 40% لتصغير مساحة بيكسلات الصورة
                matrix = fitz.Matrix(0.6, 0.6)
                
                for i in range(max_pages):
                    page = doc[i]
                    pix = page.get_pixmap(matrix=matrix)  # تحويل الصفحة لصورة بالدقة الجديدة
                    img_bytes = pix.tobytes("jpeg", quality=50)  # ضغط جودة الـ JPEG إلى 50% لتوفير المساحة الخارقة
                    
                    base64_page = base64.b64encode(img_bytes).decode('utf-8')
                    groq_messages_content.append({
                        "type": "image_url",
                        "image_url": {"url": f"data:image/jpeg;base64,{base64_page}"}
                    })
                doc.close()
            else:
                # إذا كان الملف نصي عادي وجيميني فقط مضغوط، نرسل النص كالعادة
                groq_messages_content.append({"type": "text", "text": f"\n\nالمحتوى النصي للمستند:\n{total_text[:MAX_TEXT_LENGTH_FOR_AI]}"})
            
            # تمديد المهلة الزمنية إلى 90 ثانية لتتسع لمعالجة وتحليل الـ 15 صورة بأريحية
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
            
            log_info(logger, "✅ تم إنقاذ الملف الممسوح ضوئياً وتوليد الأسئلة عبر Groq بنجاح باهر!")
            return questions
            
        except Exception as groq_err:
            log_error(logger, f"❌ حتى الخيار البديل (Groq) فشل في معالجة الملف: {groq_err}")

    return None