import os
import json
import datetime
from dotenv import load_dotenv
from google import genai
from google.genai import types
from pydantic import BaseModel, Field

# تحميل المتغيرات
load_dotenv()

# إعداد مفاتيح API
api_keys_raw = os.getenv("GEMINI_API_KEYS", "")
API_KEYS = [k.strip() for k in api_keys_raw.split(",") if k.strip()]

if not API_KEYS and os.getenv("GEMINI_API_KEY"):
    API_KEYS = [os.getenv("GEMINI_API_KEY")]

current_key_idx = 0
blocked_keys = {}

class QuizQuestion(BaseModel):
    question: str = Field(description="نص السؤال الاختياري المستخرج من النص المرفق.")
    options: list[str] = Field(description="أربعة خيارات فريدة ومتوازنة للسؤال.")
    correct_option_id: int = Field(description="مؤشر الخيار الصحيح من 0 إلى 3.")
    hint: str = Field(description="تلميح ذكي للطالب.")
    was_corrupted_text_fixed: bool = Field(description="تكون true إذا تم تصحيح أخطاء النص.")

def extract_content(file_bytes, mime_type="image/jpeg"):
    """
    استخراج النص من أي ملف (صورة أو PDF) عبر Gemini مباشرة.
    mime_type: 'image/jpeg' للصور أو 'application/pdf' للملفات.
    """
    global current_key_idx
    
    if not API_KEYS:
        return "❌ خطأ: لم يتم ضبط مفاتيح GEMINI_API_KEYS"

    now = datetime.datetime.now()

    for _ in range(len(API_KEYS)):
        # تخطي المفاتيح المحظورة
        if current_key_idx in blocked_keys and now < blocked_keys[current_key_idx]:
            current_key_idx = (current_key_idx + 1) % len(API_KEYS)
            continue
            
        key = API_KEYS[current_key_idx]
        try:
            client = genai.Client(api_key=key)
            response = client.models.generate_content(
                model='gemini-2.0-flash',
                contents=[
                    types.Part.from_bytes(data=file_bytes, mime_type=mime_type),
                    "استخرج النص من هذا الملف بدقة عالية. أخرج النص فقط."
                ]
            )
            return response.text
            
        except Exception as e:
            error_msg = str(e).lower()
            if "429" in error_msg or "resource_exhausted" in error_msg or "quota" in error_msg:
                blocked_keys[current_key_idx] = datetime.datetime.now() + datetime.timedelta(hours=24)
            else:
                blocked_keys[current_key_idx] = datetime.datetime.now() + datetime.timedelta(minutes=2)
            
            current_key_idx = (current_key_idx + 1) % len(API_KEYS)
            
    return "❌ فشلت جميع المحاولات."

def get_questions_from_text(text, count):
    """توليد الأسئلة بصيغة JSON مع تدوير المفاتيح"""
    global current_key_idx
    
    if not API_KEYS:
        return []

    prompt = f"أنت خبير تعليمي، استخرج {count} أسئلة اختيار من متعدد من هذا النص: {text}"
    
    now = datetime.datetime.now()
    
    for _ in range(len(API_KEYS)):
        if current_key_idx in blocked_keys and now < blocked_keys[current_key_idx]:
            current_key_idx = (current_key_idx + 1) % len(API_KEYS)
            continue
            
        key = API_KEYS[current_key_idx]
        try:
            client = genai.Client(api_key=key)
            response = client.models.generate_content(
                model='gemini-2.0-flash',
                contents=prompt,
                config=types.GenerateContentConfig(
                    response_mime_type="application/json",
                    response_schema=list[QuizQuestion],
                ),
            )
            return json.loads(response.text)
            
        except Exception as e:
            error_msg = str(e).lower()
            if "429" in error_msg or "resource_exhausted" in error_msg or "quota" in error_msg:
                blocked_keys[current_key_idx] = datetime.datetime.now() + datetime.timedelta(hours=24)
            else:
                blocked_keys[current_key_idx] = datetime.datetime.now() + datetime.timedelta(minutes=2)
            
            current_key_idx = (current_key_idx + 1) % len(API_KEYS)
            
    return []
