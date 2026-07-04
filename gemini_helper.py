import os
import json
import datetime
from dotenv import load_dotenv
from google import genai
from google.genai import types
from pydantic import BaseModel, Field

# تحميل المتغيرات
load_dotenv()

# إعداد مفاتيح API من ملف .env
# تأكد أن المتغير في .env هو: GEMINI_API_KEYS=key1,key2,key3
api_keys_raw = os.getenv("GEMINI_API_KEYS", "")
API_KEYS = [k.strip() for k in api_keys_raw.split(",") if k.strip()]

# خيار احتياطي في حال وجود مفتاح واحد فقط بالاسم القديم
if not API_KEYS and os.getenv("GEMINI_API_KEY"):
    API_KEYS = [os.getenv("GEMINI_API_KEY")]

current_key_idx = 0
blocked_keys = {}

class QuizQuestion(BaseModel):
    question: str = Field(description="نص السؤال الاختياري المستخرج من النص المرفق حصراً.")
    options: list[str] = Field(description="أربعة خيارات فريدة ومتوازنة للسؤال.")
    correct_option_id: int = Field(description="مؤشر الخيار الصحيح من 0 إلى 3.")
    hint: str = Field(description="تلميح ذكي ومساعد يقرب الفكرة للطالب دون إعطائه الحل المباشر.")
    was_corrupted_text_fixed: bool = Field(description="تكون true فقط إذا تم إصلاح تشوهات النص الناتجة عن الـ OCR سياقياً.")

def extract_text_from_image(image_bytes):
    """استخراج النص من الصورة مع تدوير المفاتيح عند الحظر"""
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
                model='gemini-2.0-flash', # الموديل الصحيح
                contents=[
                    types.Part.from_bytes(data=image_bytes, mime_type='image/png'),
                    "استخرج النص الموجود في هذه الصورة بدقة. أخرج النص فقط دون أي مقدمات."
                ]
            )
            print(f"DEBUG: تم استخراج نص بنجاح. الطول: {len(response.text)}")
            return response.text
            
        except Exception as e:
            error_msg = str(e).lower()
            if "429" in error_msg or "resource_exhausted" in error_msg or "quota" in error_msg:
                blocked_keys[current_key_idx] = datetime.datetime.now() + datetime.timedelta(hours=24)
                print(f"🛑 حظر مفتاح {current_key_idx} (استنفاد الحصة).")
            else:
                blocked_keys[current_key_idx] = datetime.datetime.now() + datetime.timedelta(minutes=2)
                print(f"⚠️ خطأ مؤقت في مفتاح {current_key_idx}: {e}")
            
            current_key_idx = (current_key_idx + 1) % len(API_KEYS)
            
    return ""

def get_questions_from_text(text, count):
    """توليد الأسئلة بصيغة JSON مع تدوير المفاتيح"""
    global current_key_idx
    
    if not API_KEYS:
        return []

    prompt = f"""
    أنت خبير تعليمي، استخرج {count} أسئلة اختيار من متعدد بناءً على النص المرفق.
    إذا وجدت كلمات مشوهة، قم بتصحيحها واجعل was_corrupted_text_fixed تساوي true.
    """
    
    now = datetime.datetime.now()
    
    for _ in range(len(API_KEYS)):
        if current_key_idx in blocked_keys and now < blocked_keys[current_key_idx]:
            current_key_idx = (current_key_idx + 1) % len(API_KEYS)
            continue
            
        key = API_KEYS[current_key_idx]
        try:
            client = genai.Client(api_key=key)
            response = client.models.generate_content(
                model='gemini-2.0-flash', # الموديل الصحيح
                contents=[prompt, text[:10000]],
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
                print(f"🛑 حظر مفتاح {current_key_idx} أثناء توليد الأسئلة.")
            else:
                blocked_keys[current_key_idx] = datetime.datetime.now() + datetime.timedelta(minutes=2)
                print(f"⚠️ خطأ مؤقت في مفتاح {current_key_idx} أثناء التوليد: {e}")
            
            current_key_idx = (current_key_idx + 1) % len(API_KEYS)
            
    return []
