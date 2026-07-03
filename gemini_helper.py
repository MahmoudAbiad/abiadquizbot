import os
import json
import datetime
from dotenv import load_dotenv
from google import genai
from google.genai import types
from pydantic import BaseModel, Field

load_dotenv()

# 1. جلب المفاتيح من ملف .env وتحويلها إلى قائمة (List)
api_keys_raw = os.getenv("GEMINI_API_KEYS", "")
API_KEYS = [k.strip() for k in api_keys_raw.split(",") if k.strip()]

# خيار أمان: إذا نسيت وتذكرت مفتاحاً واحداً بالصيغة القديمة، سيعتمد عليه ولا يتوقف الكود
if not API_KEYS and os.getenv("GEMINI_API_KEY"):
    API_KEYS = [os.getenv("GEMINI_API_KEY")]

# مؤشر لتتبع المفتاح الحالي
current_key_idx = 0

# 🌟 قاموس ذكي لتتبع المفاتيح المحظورة وموعد فك الحظر عنها تلقائياً
# الهيكل الصغير سيكون: { index_المفتاح: وقت_فك_الحظر }
blocked_keys = {}

class QuizQuestion(BaseModel):
    question: str = Field(description="نص السؤال الاختياري المستخرج من النص المرفق حصراً.")
    options: list[str] = Field(description="أربعة خيارات فريدة ومتوازنة للسؤال.")
    correct_option_id: int = Field(description="مؤشر الخيار الصحيح من 0 إلى 3.")
    hint: str = Field(description="تلميح ذكي ومساعد يقرب الفكرة للطالب دون إعطائه الحل المباشر.")
    was_corrupted_text_fixed: bool = Field(description="تكون true فقط إذا تم إصلاح تشوهات النص الناتجة عن الـ OCR سياقياً.")

def extract_text_from_image(image_bytes):
    """استخراج النص من الصورة مع ميزة الحظر المؤقت الذكي للمفاتيح وتخطيها دون تأخير"""
    global current_key_idx
    
    if not API_KEYS:
        return "❌ خطأ: لم يتم ضبط مفاتيح GEMINI_API_KEYS في ملف .env"

    now = datetime.datetime.now()

    # محاولة التنقل بين المفاتيح المتاحة بالتوالي
    for _ in range(len(API_KEYS)):
        
        # 🌟 فحص ما إذا كان المفتاح الحالي محظوراً حالياً وهل ما زلنا ضمن فترة الحظر
        if current_key_idx in blocked_keys and now < blocked_keys[current_key_idx]:
            print(f"ℹ️ تخطي المفتاح {current_key_idx} تلقائياً لأنه محظور (ينتهي الحظر في: {blocked_keys[current_key_idx].strftime('%Y-%m-%d %H:%M:%S')})")
            current_key_idx = (current_key_idx + 1) % len(API_KEYS)
            continue # تخطي إلى المفتاح التالي فوراَ دون إضاعة وقت في الاتصال
            
        key = API_KEYS[current_key_idx]
        try:
            client = genai.Client(api_key=key)
            
            response = client.models.generate_content(
                model='gemini-2.5-flash',
                contents=[
                    types.Part.from_bytes(data=image_bytes, mime_type='image/png'),
                    "استخرج النص الموجود في هذه الصورة بدقة. أخرج النص فقط دون أي مقدمات."
                ]
            )
            return response.text
            
        except Exception as e:
            error_msg = str(e).lower()
            
            # 🌟 فحص نوع الخطأ: هل هو نفاد حصة (429 أو Resource Exhausted)؟
            if "429" in error_msg or "resource_exhausted" in error_msg or "quota" in error_msg:
                # حظر المفتاح لمدة 24 ساعة (سيعود للعمل تلقائياً غداً في نفس هذا الوقت)
                blocked_keys[current_key_idx] = datetime.datetime.now() + datetime.timedelta(hours=24)
                print(f"🛑 تم حظر المفتاح رقم {current_key_idx} لمدة 24 ساعة بسبب استنفاد الحصة اليومية.")
            else:
                # خطأ مؤقت آخر (مثل بطء الشبكة)، نعطيه حظر مؤقت قصير جداً لمدة دقيقتين فقط
                blocked_keys[current_key_idx] = datetime.datetime.now() + datetime.timedelta(minutes=2)
                print(f"⚠️ خطأ مؤقت أو شبكي في المفتاح رقم {current_key_idx}، تم تعليقه لمدة دقيقتين. الخطأ: {e}")
                
            current_key_idx = (current_key_idx + 1) % len(API_KEYS)
            
    return "❌ فشلت جميع محاولات قراءة الصورة بسبب نفاد مخصصات كافة المفاتيح المتاحة حالياً."

def get_questions_from_text(text, count):
    """توليد الأسئلة بصيغة JSON مع تخطي ذكي للمفاتيح الميتة وإعادتها للخدمة في اليوم التالي"""
    global current_key_idx
    
    if not API_KEYS:
        return []

    prompt = f"""
    أنت خبير تعليمي، مهمتك استخراج {count} أسئلة اختيار من متعدد بناءً على النص المرفق حصراً ووفق القواعد الصارمة التالية:
    1. الاستناد الكامل: استخرج الأسئلة من العبارات والمعلومات الموجودة في النص المرفق فقط. لا تقم بإضافة معلومات خارجية.
    2. معالجة عيوب النصوص (OCR): افحص النص بدقة، وإذا وجدت كلمات مشوهة قم بتعديلها لتصبح مناسبة للسياق، واجعل قيمة `was_corrupted_text_fixed` تساوي `true`.
    3. صغ لكل سؤال تلميحاً ذكياً (hint) يساعد الطالب على الاستنتاج دون إعطائه الحل المباشر.
    """
    
    now = datetime.datetime.now()
    
    for _ in range(len(API_KEYS)):
        # 🌟 فحص حالة الحظر قبل الاستخدام لضمان السرعة القصوى للبوت
        if current_key_idx in blocked_keys and now < blocked_keys[current_key_idx]:
            current_key_idx = (current_key_idx + 1) % len(API_KEYS)
            continue
            
        key = API_KEYS[current_key_idx]
        try:
            client = genai.Client(api_key=key)
            
            response = client.models.generate_content(
                model='gemini-2.5-flash',
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
                print(f"🛑 تم حظر المفتاح رقم {current_key_idx} لمدة 24 ساعة أثناء توليد الأسئلة.")
            else:
                blocked_keys[current_key_idx] = datetime.datetime.now() + datetime.timedelta(minutes=2)
                print(f"⚠️ خطأ مؤقت في المفتاح رقم {current_key_idx} أثناء التوليد، عُلق لدقيقتين. الخطأ: {e}")
                
            current_key_idx = (current_key_idx + 1) % len(API_KEYS)
            
    return []