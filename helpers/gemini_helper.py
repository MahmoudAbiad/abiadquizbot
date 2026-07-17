"""
AI integration for quiz generation.
Exclusive API Routing: Media/Files (All extensions & Albums) -> Gemini.
Pure Text inputs -> Groq.
"""

import os
import json
import datetime
import random
import asyncio
import fitz  # PyMuPDF
import base64
import uuid  # استيراد مكتبة توليد المعرفات الفريدة لحماية الملفات المؤقتة
from typing import Optional, List, Dict, Any
from google import genai
from google.genai import types
from groq import AsyncGroq
from pydantic import BaseModel, Field
from dotenv import load_dotenv

# استيراد الثوابت الجديدة والديناميكية بالكامل وإلغاء الثوابت القديمة الصلبة
from constants import (
    AI_REQUEST_TIMEOUT, GEMINI_PRIMARY_MODEL, GEMINI_FALLBACK_MODEL, MAX_PDF_PAGES,
    MAX_TEXT_LENGTH_FOR_AI, KEY_BLOCK_QUOTA_EXHAUSTED,
    KEY_BLOCK_TEMPORARY_ERROR, SYSTEM_PROMPT_GENERATE_QUESTIONS,
    QUOTA_ERROR_KEYWORDS
)
from logger import get_logger, log_error, log_warning, log_info
from utils import calculate_file_hash
from supabase_helper import get_cached_quiz, save_quiz_to_cache

load_dotenv()
logger = get_logger(__name__)

api_keys_raw = os.getenv("GEMINI_API_KEYS", "")
API_KEYS: List[str] = [k.strip() for k in api_keys_raw.split(",") if k.strip()]
blocked_keys: Dict[int, datetime.datetime] = {}
GROQ_API_KEY = os.getenv("GROQ_API_KEY", "")

class QuizQuestion(BaseModel):
    question: str = Field(description="نص السؤال.")
    options: List[str] = Field(description="أربعة خيارات.")
    correct_option_id: int = Field(description="مؤشر الخيار الصحيح (0-3).")
    hint: str = Field(description="تلميح ذكي.")
    explanation: str = Field(default="", description="شرح الإجابة.")

class QuizResponse(BaseModel):
    questions: List[QuizQuestion]

# دالة مساعدة متزامنة نضعها في ملف helpers/gemini_helper.py
def slice_pdf_if_needed_sync(file_path: str, max_pages: int, unique_id: str) -> tuple[str, bool]:
    """
    تقطيع ملف الـ PDF متزامناً داخل خيط منفصل لحماية السيرفر
    تعيد: (مسار الملف النهائي، هل تم قصه أم لا)
    """
    import fitz
    doc = fitz.open(file_path)
    if len(doc) > max_pages:
        new_doc = fitz.open()
        sliced_path = file_path.replace(".pdf", f"_{unique_id}_sliced.pdf")
        new_doc.insert_pdf(doc, from_page=0, to_page=max_pages - 1)
        new_doc.save(sliced_path)
        new_doc.close()
        doc.close()
        return sliced_path, True
    doc.close()
    return file_path, False

def _get_available_key_indices() -> List[int]:
    now = datetime.datetime.now()
    available = []
    for idx in range(len(API_KEYS)):
        if idx not in blocked_keys or now >= blocked_keys[idx]:
            if idx in blocked_keys: del blocked_keys[idx]
            available.append(idx)
    return available

async def _safe_delete_gemini_file(client: genai.Client, file_name: str):
    """دالة مساعدة آمنة لحذف الملفات من سحابة جوجل في الخلفية دون التسبب بانهيار المهام"""
    try:
        await asyncio.to_thread(client.files.delete, name=file_name)
    except Exception as e:
        log_warning(logger, f"فشل الحذف التلقائي للملف {file_name} من سحابة Gemini (تم تجاهله آلياً): {e}")

# ==================== Main Generation Function ====================

async def generate_quiz_smart(
    file_paths: Optional[List[str]] = None, 
    pure_text: Optional[str] = None, 
    count: int = 0, 
    skip_cache: bool = False
) -> Optional[List[Dict[str, Any]]]:
    
    prompt = SYSTEM_PROMPT_GENERATE_QUESTIONS.replace("{count}", str(count))
    
    groq_instructions = (
        f"{prompt}\n\n"
        "⚠️ تنبيه صارم: يجب أن تكون المخرجات عبارة عن كائن JSON صالح تماماً، يحتوي على حقل رئيسي باسم 'questions' وهو عبارة عن مصفوفة، وكل عنصر داخل المصفوفة يحتوي حصراً على الحقول الحالية باللغة العربية.\n"
        "- question\n- options (4 خيارات)\n- correct_option_id (0-3)\n- hint\n- explanation"
    )

    # ----------------------------------------------------
    # 🧠 أولاً: مسار النصوص النقية -> يتم توجيهه حصرياً لـ Groq
    # ----------------------------------------------------
    if pure_text:
        if not GROQ_API_KEY:
            log_error(logger, "خطأ: لم يتم ضبط GROQ_API_KEY لمعالجة النصوص.")
            return None
        
        log_info(logger, "توجيه طلب النص النقي حصرياً إلى سيرفر Groq...")
        try:
            groq_client = AsyncGroq(api_key=GROQ_API_KEY)
            response = await asyncio.wait_for(
                groq_client.chat.completions.create(
                    model="meta-llama/llama-4-scout-17b-16e-instruct",
                    messages=[{"role": "user", "content": f"{groq_instructions}\n\nالنص المطلوب التوليد منه:\n{pure_text}"}],
                    response_format={"type": "json_object"},
                    temperature=0.7
                ),
                timeout=45.0
            )
            parsed_json = json.loads(response.choices[0].message.content)
            validated_response = QuizResponse(**parsed_json)
            return [q.model_dump() for q in validated_response.questions]
        except Exception as e:
            log_error(logger, f"خطأ أثناء معالجة النص عبر Groq: {e}")
            return None

    # ----------------------------------------------------
    # 📄 ثانياً: مسار جميع أنواع الملفات والوسائط والالبومات -> جيميني حصرياً
    # ----------------------------------------------------
    if not file_paths: return None
    
    log_info(logger, f"توجيه الملفات والوسائط المرفوعة ({len(file_paths)} ملف) حصرياً إلى Gemini...")
    
    # حساب هاش مجمع لكل الملفات لضمان دقة الكاش في حال الألبومات والصور المتعددة
    try:
        hashes = [await asyncio.to_thread(calculate_file_hash, path) for path in file_paths]
        file_hash = "-".join(hashes) if len(hashes) > 1 else hashes[0]
    except Exception as hash_err:
        log_error(logger, f"خطأ أثناء حساب الهاش للملفات: {hash_err}")
        file_hash = await asyncio.to_thread(calculate_file_hash, file_paths[0])

    # التحقق من الكاش في Supabase قبل استهلاك الـ API والرفع
    if not skip_cache:
        cached_quiz = await asyncio.to_thread(get_cached_quiz, file_hash)
        if cached_quiz:
            log_info(logger, "⚡ تم العثور على الكويز في الكاش (Supabase)! إرجاع النتيجة فوراً بدون استهلاك API.")
            return cached_quiz

    local_sliced_paths = []

    try:
        # ======= 🚀 بداية الموضع المعدل بدقة =======
        # إذا كان ملف واحد PDF نقوم بتمريره مباشرة للخيط الخلفي ليفحص ويقتطع دون حظر السيرفر
        if len(file_paths) == 1 and file_paths[0].lower().endswith('.pdf'):
            try:
                unique_id = uuid.uuid4().hex
                # الاستدعاء المباشر للخيط الخلفي، الدالة تتكفل بالفحص والقص بأمان وثبات
                final_path, is_sliced = await asyncio.to_thread(
                    slice_pdf_if_needed_sync, file_paths[0], MAX_PDF_PAGES, unique_id
                )
        
                if is_sliced:
                    local_sliced_paths.append(final_path)
                    target_paths = [final_path]
                else:
                    target_paths = file_paths
            except Exception as pdf_err:
                log_error(logger, f"خطأ في معالجة خيط الـ PDF الخلفي: {pdf_err}")
                target_paths = file_paths
        else:
            target_paths = file_paths
        # ======= 🚀 نهاية الموضع المعدل بدقة =======

        # تجهيز ورفع كل الملفات لـ جيميني عبر نظام التدوير الذكي للمفاتيح والتبديل الديناميكي للموديل
        if API_KEYS:
            # جعل المحاولات الأقصى تتسع لـ 3 محاولات لإعطاء فرصة كافية للموديل الاحتياطي ليعمل
            max_attempts = min(len(API_KEYS), 3)
            # تهيئة الموديل الحالي ليبدأ بالموديل الأساسي ديناميكياً
            current_model = GEMINI_PRIMARY_MODEL
            
            for attempt in range(max_attempts):
                available = _get_available_key_indices()
                if not available:
                    blocked_keys.clear()
                    available = list(range(len(API_KEYS)))
                
                idx = random.choice(available)
                await asyncio.sleep(random.uniform(0.2, 0.5))
                
                attempt_uploaded_files = []
                attempt_contents = [prompt]
                
                try:
                    client = genai.Client(api_key=API_KEYS[idx])
                    log_info(logger, f"محاولة توليد الأسئلة باستخدام الموديل ({current_model}) عبر مفتاح جيميناي رقم {idx} (محاولة {attempt + 1})...")
                    
                    # رفع كافة الملفات والوسائط دفعة واحدة تحت صلاحية المفتاح الحالي حصرياً
                    for path in target_paths:
                        g_file = await asyncio.to_thread(client.files.upload, file=path)
                        attempt_uploaded_files.append(g_file)
                        attempt_contents.append(g_file)
                    
                    response = await asyncio.wait_for(
                        client.aio.models.generate_content(
                            model=current_model, # تمرير الموديل الحالي المتغير
                            contents=attempt_contents,
                            config=types.GenerateContentConfig(
                                response_mime_type="application/json", 
                                response_schema=QuizResponse,
                                temperature=0.7
                            ),
                        ),
                        timeout=AI_REQUEST_TIMEOUT
                    )
                    
                    # تنظيف سحابي آمن فوري للملفات المرفوعة بنجاح
                    for gf in attempt_uploaded_files:
                        asyncio.create_task(_safe_delete_gemini_file(client, gf.name))
                    
                    # تنظيف محلي للملفات المقتطعة إن وُجدت
                    for sp in local_sliced_paths:
                        if os.path.exists(sp): os.remove(sp)
                    
                    if response.parsed and hasattr(response.parsed, 'questions'):
                        questions = [q.model_dump() for q in response.parsed.questions]
                        total_tokens = response.usage_metadata.total_token_count if hasattr(response, 'usage_metadata') and response.usage_metadata else 0
                        
                        # حفظ النتيجة في الكاش للاستخدام المستقبلي
                        await asyncio.to_thread(save_quiz_to_cache, file_hash, questions, total_tokens)
                        return questions
                    
                except Exception as e:
                    # تنظيف فوري لملفات المحاولة الفاشلة الحالية لمنع تسريبها أو خلط الصلاحيات مع المفتاح التالي
                    for gf in attempt_uploaded_files:
                        asyncio.create_task(_safe_delete_gemini_file(client, gf.name))
                    
                    error_msg = str(e).lower()
                    log_warning(logger, f"فشل مفتاح جيميني {idx} أثناء استخدام الموديل {current_model}: {e}")
                    
                    # 🎯 كشف خطأ الضغط والسيرفر 503 للتحويل التلقائي للموديل الاحتياطي
                    if "503" in error_msg or "service_unavailable" in error_msg:
                        log_warning(logger, f"⚠️ تم رصد خطأ ضغط (503). تحويل الموديل فوراً إلى الاحتياطي: {GEMINI_FALLBACK_MODEL}")
                        current_model = GEMINI_FALLBACK_MODEL  # تحويل الموديل الحالي للاحتياطي الخفيف للمحاولة التالية
                        blocked_keys[idx] = datetime.datetime.now() + datetime.timedelta(minutes=1)  # تهدئة المفتاح الحالي لدقيقة
                        
                    elif any(k in error_msg for k in QUOTA_ERROR_KEYWORDS):
                        blocked_keys[idx] = datetime.datetime.now() + datetime.timedelta(hours=KEY_BLOCK_QUOTA_EXHAUSTED)
                    else:
                        blocked_keys[idx] = datetime.datetime.now() + datetime.timedelta(minutes=KEY_BLOCK_TEMPORARY_ERROR)
                        
    except Exception as outer_err:
        log_error(logger, f"خطأ هيكلي في مسار معالجة جيميني للملفات: {outer_err}")
    finally:
        for sp in local_sliced_paths:
            if os.path.exists(sp): os.remove(sp)
            
    return None