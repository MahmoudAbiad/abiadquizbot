import os
import datetime
import asyncio
from dotenv import load_dotenv
from google import genai
from google.genai import types
from pydantic import BaseModel, Field

load_dotenv()

# إعداد مفاتيح API
api_keys_raw = os.getenv("GEMINI_API_KEYS", "")
API_KEYS = [k.strip() for k in api_keys_raw.split(",") if k.strip()]

if not API_KEYS and os.getenv("GEMINI_API_KEY"):
    API_KEYS = [os.getenv("GEMINI_API_KEY")]

# تحضير عملاء منفصلين مسبقاً لتوفير استهلاك الموارد وعمل تدوير سريع
CLIENTS = [genai.Client(api_key=k) for k in API_KEYS]

current_key_idx = 0
blocked_keys = {}
_lock = asyncio.Lock()

class QuizQuestion(BaseModel):
    question: str = Field(description="نص السؤال الاختياري المستخرج من النص المرفق.")
    options: list[str] = Field(description="أربعة خيارات فريدة ومتوازنة للسؤال.")
    correct_option_id: int = Field(description="مؤشر الخيار الصحيح من 0 إلى 3.")
    hint: str = Field(description="تلميح ذكي للطالب.")
    was_corrupted_text_fixed: bool = Field(description="تكون true إذا تم تصحيح أخطاء النص.")

class QuizResponse(BaseModel):
    questions: list[QuizQuestion]


async def extract_content(file_bytes, mime_type="image/jpeg"):
    """استخراج النص بأسلوب آمن برمجياً ومتزامن ومحمي من الـ Race Condition"""
    global current_key_idx
    if not API_KEYS:
        return "❌ خطأ: لم يتم ضبط مفاتيح GEMINI_API_KEYS"

    for _ in range(len(API_KEYS)):
        async with _lock:
            now = datetime.datetime.now() # تحديث الوقت داخلياً في كل لفة لضمان الدقة
            
            # تخطي المفاتيح المحظورة
            if current_key_idx in blocked_keys and now < blocked_keys[current_key_idx]:
                current_key_idx = (current_key_idx + 1) % len(API_KEYS)
                continue
                
            # تثبيت الـ index والعميل المستهدف لهذا الطلب تحديداً لحمايته من الـ Race Condition
            target_idx = current_key_idx
            client = CLIENTS[target_idx]
            
        try:
            response = await asyncio.wait_for(
                client.aio.models.generate_content(
                    model='gemini-2.0-flash',
                    contents=[
                        types.Part.from_bytes(data=file_bytes, mime_type=mime_type),
                        "استخرج النص من هذا الملف بدقة عالية. أخرج النص فقط."
                    ]
                ),
                timeout=45.0
            )
            return response.text
            
        except asyncio.TimeoutError:
            async with _lock:
                blocked_keys[target_idx] = datetime.datetime.now() + datetime.timedelta(minutes=2)
                # لا نحدث المؤشر العام إلا إذا كان ما زال يشير إلى نفس المفتاح الذي فحصناه
                if current_key_idx == target_idx:
                    current_key_idx = (current_key_idx + 1) % len(API_KEYS)
                
        except Exception as e:
            error_msg = str(e).lower()
            # نفحص إن كان الخطأ فعلياً من الـ API أو الحصة وليس خطأ داخلياً
            if any(x in error_msg for x in ["429", "exhausted", "quota", "limit", "api"]):
                async with _lock:
                    if "429" in error_msg or "quota" in error_msg:
                        blocked_keys[target_idx] = datetime.datetime.now() + datetime.timedelta(hours=24)
                    else:
                        blocked_keys[target_idx] = datetime.datetime.now() + datetime.timedelta(minutes=2)
                    
                    if current_key_idx == target_idx:
                        current_key_idx = (current_key_idx + 1) % len(API_KEYS)
            else:
                # خطأ برمجي أو استجابة غير متوقعة، لا تحظر المفتاح بل أخرج أو ارفع الخطأ
                return f"⚠️ خطأ أثناء المعالجة: {str(e)}"
            
    return "❌ فشلت جميع المحاولات بسبب حظر أو نفاد كوتا المفاتيح."


async def get_questions_from_text(text, count):
    """توليد الأسئلة مع حماية المفاتيح والاستفادة التامة من ميزة التفكيك التلقائي للمكتبة"""
    global current_key_idx
    if not API_KEYS:
        return []

    prompt = (
        f"أنت خبير تعليمي، استخرج {count} أسئلة اختيار من متعدد من هذا النص:\n\n{text}\n\n"
        "ملاحظة هامة: إذا كان النص غير مفهوم أو لا يحتوي على معلومات تعليمية، أرجع مصفوفة أسئلة فارغة تماماً."
    )
    
    for _ in range(len(API_KEYS)):
        async with _lock:
            now = datetime.datetime.now()
            if current_key_idx in blocked_keys and now < blocked_keys[current_key_idx]:
                current_key_idx = (current_key_idx + 1) % len(API_KEYS)
                continue
                
            target_idx = current_key_idx
            client = CLIENTS[target_idx]
            
        try:
            response = await asyncio.wait_for(
                client.aio.models.generate_content(
                    model='gemini-2.0-flash',
                    contents=prompt,
                    config=types.GenerateContentConfig(
                        response_mime_type="application/json",
                        response_schema=QuizResponse, 
                        temperature=0.7
                    ),
                ),
                timeout=45.0
            )
            
            # الاستفادة من التفكيك التلقائي للمكتبة الأصلية الناتجة عن الـ Schema
            # الـ parsed سيعيد لك كائن QuizResponse مباشرة كـ Pydantic object سليم ومفحوص تائيبياً
            if response.parsed and hasattr(response.parsed, 'questions'):
                return response.parsed.questions
            return []
            
        except asyncio.TimeoutError:
            async with _lock:
                blocked_keys[target_idx] = datetime.datetime.now() + datetime.timedelta(minutes=2)
                if current_key_idx == target_idx:
                    current_key_idx = (current_key_idx + 1) % len(API_KEYS)
                
        except Exception as e:
            error_msg = str(e).lower()
            if any(x in error_msg for x in ["429", "exhausted", "quota", "limit", "api"]):
                async with _lock:
                    if "429" in error_msg or "quota" in error_msg:
                        blocked_keys[target_idx] = datetime.datetime.now() + datetime.timedelta(hours=24)
                    else:
                        blocked_keys[target_idx] = datetime.datetime.now() + datetime.timedelta(minutes=2)
                    
                    if current_key_idx == target_idx:
                        current_key_idx = (current_key_idx + 1) % len(API_KEYS)
            else:
                # إذا واجه الموديل مشكلة في هيكلة البيانات، نخرج مباشرة دون معاقبة المفتاح البريء
                print(f"⚠️ خطأ في معالجة النموذج للبيانات وليس في المفتاح: {e}")
                return []
            
    return []
            key = API_KEYS[current_key_idx]
            
        try:
            client = genai.Client(api_key=key)
            # ✅ استخدام العميل غير المتزامن والموديل الحاوي
            response = await asyncio.wait_for(
                client.aio.models.generate_content(
                    model='gemini-2.0-flash',
                    contents=prompt,
                    config=types.GenerateContentConfig(
                        response_mime_type="application/json",
                        response_schema=QuizResponse,  # 👈 استخدام الموديل الحاوي هنا
                        temperature=0.7
                    ),
                ),
                timeout=45.0
            )
            
            # استخراج الأسئلة من الكائن الحاوي
            result = json.loads(response.text)
            return result.get("questions", [])
            
        except asyncio.TimeoutError:
            async with _lock:
                blocked_keys[current_key_idx] = datetime.datetime.now() + datetime.timedelta(minutes=2)
                current_key_idx = (current_key_idx + 1) % len(API_KEYS)
                
        except Exception as e:
            error_msg = str(e).lower()
            async with _lock:
                if "429" in error_msg or "resource_exhausted" in error_msg or "quota" in error_msg:
                    blocked_keys[current_key_idx] = datetime.datetime.now() + datetime.timedelta(hours=24)
                else:
                    blocked_keys[current_key_idx] = datetime.datetime.now() + datetime.timedelta(minutes=2)
                current_key_idx = (current_key_idx + 1) % len(API_KEYS)
            
    return []
