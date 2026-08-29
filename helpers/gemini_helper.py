"""
==============================================================================
MODULE: AI Quiz Generation Helper (Gemini & Groq Integration)
==============================================================================
الوصف:
موديول تنفيذي مسؤول عن تحويل المستندات (PDF/Images) والنصوص إلى أسئلة تفاعلية (Quizzes).

الميزات المعمارية والقرارات الهندسية الرئيسية:
1. Model Waterfall + Per-(Key, Model) Round-Robin: بدل الاعتماد على "نموذج أساسي +
   نموذج احتياطي واحد" وحظر كامل للمفتاح، أصبح لدينا سلسلة أولوية كاملة من النماذج
   (MODELS_CASCADE) وتتبّع دقيق لكل زوج (مفتاح، نموذج) على حدة عبر blocked_model_keys -
   بحيث حظر مفتاح على نموذج معيّن لا يمنعه إطلاقاً من العمل على نموذج آخر بنفس اللحظة.
2. Inline Data vs. Files API: إرسال الملفات الصغيرة بأسلوب inline للتقليل من تأخير الشبكة (Latency).
3. Resilience & Overload Handling: التمييز بين أخطاء الحصة (Quota Exhaustion → حظر 24 ساعة)
   وأخطاء الازدحام المؤقت (503/500/Overload → تبريد دقيقة واحدة فقط) على مستوى (مفتاح، نموذج).
4. Super PDF Parallel Processing: تقسيم ملفات PDF الكبيرة ومعالجتها بشكل متوازي بطلب مستقل لكل ثلث.
5. Robust Async Task Lifecycle: إدارة مهمة تحريك رسالة الانتظار بشكل آمن يمنع تسريب الاستثناءات (Log Pollution).
6. Smart SHA-256 Caching: التخزين المؤقت للاستجابات لتفادي الاستدعاءات التكرارية للذكاء الاصطناعي.
7. Generic Cascade Execution Helpers: `generate_structured_with_cascade` و
   `generate_text_with_cascade` يوفّران واجهة عامة قابلة لإعادة الاستخدام (كويزات، تفريغ
   صوتي، تلخيص...) تُشغّل تلقائياً كامل سلسلة (النماذج × المفاتيح) دون تكرار المنطق.
==============================================================================
"""

import asyncio
import datetime
import hashlib
import json
import mimetypes
import os
import random
import re
import uuid
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, List, Optional, Sequence, Tuple

import fitz
from aiogram.exceptions import TelegramBadRequest
from dotenv import load_dotenv
from google import genai
from google.genai import types
from groq import AsyncGroq
from pydantic import BaseModel, Field

from constants import (
    AI_REQUEST_TIMEOUT,
    DIFFICULTY_MEDIUM,
    DIFFICULTY_PROMPT_INSTRUCTIONS,
    MAX_LIMIT_PAGES,
    SUPER_IMAGE_BATCH_THRESHOLD,
    OPTION_COUNT,
    QUESTION_TYPE_INSTRUCTION_GENERAL,
    QUESTION_TYPE_PROMPT_INSTRUCTIONS,
    QUOTA_ERROR_KEYWORDS,
    SYSTEM_PROMPT_GENERATE_QUESTIONS,
    SYSTEM_PROMPT_GENERATE_MATH_QUESTIONS,
    SYSTEM_PROMPT_GENERATE_ENGLISH_PLAIN_QUESTIONS,
    SYSTEM_PROMPT_GENERATE_ENGLISH_TRANSLATED_QUESTIONS,
    MSG_PREVIOUS_QUESTIONS_INSTRUCTION,
)
from logger import get_logger, log_error, log_info, log_warning
from supabase_helper import get_cached_quiz
from utils import calculate_file_hash, safe_file_cleanup

# ==============================================================================
# CONFIGURATION & GLOBAL STATE
# ==============================================================================
load_dotenv()
logger = get_logger(__name__)

# AI-NOTE: يتم تحميل مفاتيح Gemini كقائمة وتتبع المفاتيح المعطلة مؤقتاً في ذاكرة السيرفر
API_KEYS = [key.strip() for key in os.getenv("GEMINI_API_KEYS", "").split(",") if key.strip()]
# 🛠️ FIX: يطابق أي backslash غير متبوع بحرف escape شرعي بمعيار JSON (" \ / b f n r t u)
# - يُستخدم لترميم استجابات Groq الخام قبل json.loads() (راجع _generate_text_quiz).
_JSON_BACKSLASH_REPAIR_RE = re.compile(r'\\(?!["\\/bfnrtu])')

GROQ_API_KEY = os.getenv("GROQ_API_KEY", "")

# AI-NOTE (memory-leak fix): سابقاً كان يتم إنشاء genai.Client()/AsyncGroq() جديد بكل
# استدعاء توليد (أحياناً عدة مرات بنفس الطلب عبر مسارات fallback/Super PDF)، وهاد العميل
# بيحمل جوّاه connection pool خاص فيه (httpx) ما بينسكر أبداً - وهاد كان سبب تسريب الذاكرة
# التدريجي اللي أدى لـ Memory Exceeded على Render. الحل: عميل واحد ثابت (Singleton) لكل
# مفتاح Gemini ولـ Groq، يتم إنشاؤه مرة وحدة عند تحميل الموديول ويُعاد استخدامه دائماً -
# نفس نمط `bot`/`redis_client` بملف config.py.
_GEMINI_CLIENTS: List[genai.Client] = [genai.Client(api_key=key) for key in API_KEYS]
_GROQ_CLIENT: Optional[AsyncGroq] = AsyncGroq(api_key=GROQ_API_KEY) if GROQ_API_KEY else None

# ==============================================================================
# 🆕 MODEL WATERFALL CASCADE + PER-(KEY, MODEL) ROUND-ROBIN STATE
# ==============================================================================
# AI-NOTE: سلسلة أولوية النماذج. 🩹 gemini-3.7-flash (الأحدث) كان أول النموذج بالسلسلة
# سابقاً، لكن لوغز 2026-08-26 أظهرت إنو كل حالات 503 UNAVAILABLE (ازدحام Google نفسه،
# مش خطأ بكودنا) صارت حصراً عليه - غالباً لأنه أحدث نموذج وعليه ضغط طلبات أكبر بكتير من
# البقية حالياً. تم تنزيله لموقع ثانٍ (يبقى مجرّباً كخيار احتياطي إضافي بنفس مستوى حد
# التوكنز) وترقية gemini-3.6-flash ليكون الأساسي - يطابق فعلياً GEMINI_PRIMARY_MODEL
# المُعرَّف بـ constants.py (كان معرَّفاً هناك من زمان بس ما كان مستخدَماً فعلياً هون).
# الحلقة الخارجية بمنطق التنفيذ تستنفد كل المفاتيح على النموذج الحالي قبل النزول للنموذج
# التالي بالسلسلة.
MODELS_CASCADE: List[str] = [
    "gemini-3.6-flash",
    "gemini-3.7-flash",
    "gemini-3.5-flash",
    "gemini-3.5-flash-lite",
]

# AI-NOTE: تتبّع دقيق لكل زوج (فهرس المفتاح، اسم النموذج) على حدة - بدل حظر المفتاح
# بالكامل عبر كل النماذج، هيك حظر مفتاح على نموذج مُعيّن (بسبب حصته انتهت مثلاً) لا يمنعه
# إطلاقاً من الاستمرار بالعمل على نموذج آخر من السلسلة بنفس اللحظة.
blocked_model_keys: Dict[Tuple[int, str], datetime.datetime] = {}

# AI-NOTE: مؤشر عام (Round-Robin) يحدد أي مفتاح نبدأ منه بكل طلب توليد جديد، بحيث تتوزع
# الطلبات المتتالية على كل المفاتيح المتاحة بالتساوي تقريباً بدل التحيّز الدائم لأول مفتاح.
current_key_pointer: int = 0

# مدة حظر (مفتاح، نموذج) عند نفاد الحصة (Quota Exhaustion: 429 / resource_exhausted / quota)
MODEL_KEY_BLOCK_QUOTA_HOURS = 24
# مدة التبريد القصيرة عند ازدحام/تعطّل مؤقت بالسيرفر (503 / 500 / overloaded / unavailable)
# أو أي خطأ آخر غير متوقع - لتفادي "حلقة ساخنة" (hot-loop) على نفس الزوج (مفتاح، نموذج) العاطل
# بينما بقية المفاتيح/النماذج بالسلسلة متاحة وجاهزة للتجربة فوراً.
MODEL_KEY_BLOCK_OVERLOAD_MINUTES = 1

# AI-NOTE: كلمات مفتاحية لتحديد أخطاء الضغط والازدحام في سيرفرات Gemini
OVERLOAD_ERROR_KEYWORDS = ["overloaded", "unavailable", "503", "internal error", "500"]
OVERLOAD_RETRY_ATTEMPTS = 2
OVERLOAD_RETRY_BASE_DELAY = 3

# AI-NOTE: الحد الأقصى لإرسال البيانات مباشرة ضمن الطلب (Inline) دون اللجوء لـ Files API.
# رفع الملف عبر Files API يضيف Round-trip شبكة وتأخير معالجة، لذا يُفضل تحاشيه في الملفات الصغيرة.
INLINE_DATA_SIZE_THRESHOLD = 15 * 1024 * 1024  # 15MB

LOADING_PHRASES = (
    "🔍 يقوم الذكاء الاصطناعي الآن بفحص ملفاتك المرفوعة...",
    "🧠 جاري تحليل النصوص واستخراج المفاهيم الأكاديمية...",
    "⚡ يتم الآن توليد الأسئلة التفاعلية وصيغ الكويز...",
    "✨ شارَفنا على الانتهاء... نقوم بتنسيق لوحة الاختبار...",
    "⏳ لحظات قليلة جداً ويصبح اختبارك التفاعلي جاهزاً للبدء...",
)

# AI-NOTE: قاموس مخصص لتجاوز مشاكل التعرف على MIME Types في بيئات Docker المجرّدة
CUSTOM_MIME_TYPES = {
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".doc": "application/msword",
    ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    ".ppt": "application/vnd.ms-powerpoint",
    ".txt": "text/plain",
    ".pdf": "application/pdf",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
}

def get_safe_mime_type(file_path: str) -> str:
    """استرجاع نوع MIME للملف بشكل آمن ومضمون لاستخدامه في طلبات الـ API."""
    ext = os.path.splitext(file_path)[1].lower()
    if ext in CUSTOM_MIME_TYPES:
        return CUSTOM_MIME_TYPES[ext]
    guess, _ = mimetypes.guess_type(file_path)
    return guess or "application/octet-stream"

# ==============================================================================
# PYDANTIC SCHEMAS (STRUCTURED OUTPUT)
# ==============================================================================
# AI-NOTE: نضمن إجبار Gemini و Groq على التقيُّد بهذه البنية الدقيقة لتفادي أخطاء Parsing
class QuizTable(BaseModel):
    """🆕 جدول بيانات هيكلي (زي جداول التوزيعات التكرارية بالإحصاء) - يُملأ فقط
    عند نمط الكويز المصوّر الرياضي (is_math_mode) ولمسائل تعتمد فعلياً على جدول،
    بدل محاولة حشر الجدول كنص/LaTeX داخل question (غير مدعوم برسم الصورة).
    راجع services/image_quiz_renderer.py لكيفية رسمه فعلياً ضمن صورة السؤال."""
    headers: List[str] = Field(default_factory=list, description="Table column headers")
    rows: List[List[str]] = Field(default_factory=list, description="Table data rows")


class QuizQuestion(BaseModel):
    question: str = Field(description="Question text")
    options: List[str] = Field(description="Four answer options")
    correct_option_id: int = Field(description="Correct option index")
    hint: str = Field(description="Hint")
    explanation: str = Field(default="", description="Explanation")
    # 🆕 اختياري - يُملأ فقط لو المسألة تعتمد فعلياً على جدول بيانات (راجع QuizTable أعلاه)
    table: Optional[QuizTable] = Field(default=None, description="Optional structured data table")


class QuizResponse(BaseModel):
    questions: List[QuizQuestion]

# ==============================================================================
# 🆕 MODEL WATERFALL + ROUND-ROBIN KEY MANAGEMENT
# ==============================================================================
def _is_model_key_blocked(key_index: int, model: str) -> bool:
    """فحص لحظي (بدون أي تأخير - بمعنى zero-delay) على القاموس بالذاكرة لمعرفة إذا كان
    هذا الزوج (مفتاح، نموذج) بالذات محظوراً حالياً. يُنظّف تلقائياً أي إدخال انتهت صلاحيته."""
    blocked_until = blocked_model_keys.get((key_index, model))
    if blocked_until is None:
        return False
    if datetime.datetime.now() >= blocked_until:
        blocked_model_keys.pop((key_index, model), None)
        return False
    return True


def _mark_model_key_failure(key_index: int, model: str, error: Exception) -> None:
    """حظر الزوج (مفتاح، نموذج) الفاشل لفترة محددة حسب نوع الخطأ:
    - نفاد الحصة (429/resource_exhausted/quota) → حظر 24 ساعة على هذا الزوج تحديداً.
    - ازدحام/تعطّل مؤقت (503/500/overloaded/unavailable) → تبريد دقيقة واحدة فقط.
    - أي خطأ آخر غير متوقع → نفس تبريد الدقيقة الواحدة (تحوّطاً من حلقة ساخنة على زوج
      عاطل بشكل دائم، دون معاقبته بحظر طويل غير مبرر كما بحالة نفاد الحصة الصريحة)."""
    message = str(error).lower()
    now = datetime.datetime.now()
    if any(keyword in message for keyword in QUOTA_ERROR_KEYWORDS):
        blocked_model_keys[(key_index, model)] = now + datetime.timedelta(hours=MODEL_KEY_BLOCK_QUOTA_HOURS)
    else:
        blocked_model_keys[(key_index, model)] = now + datetime.timedelta(minutes=MODEL_KEY_BLOCK_OVERLOAD_MINUTES)


def _is_overload_error(error: Exception) -> bool:
    """التحقق مما إذا كان الخطأ ناتجاً عن ضغط/ازدحام مؤقت في سيرفرات AI."""
    message = str(error).lower()
    return any(keyword in message for keyword in OVERLOAD_ERROR_KEYWORDS)


def _round_robin_key_order() -> List[int]:
    """يبني ترتيب تجربة المفاتيح بدءاً من current_key_pointer الحالي (دورانياً عبر كل
    المفاتيح المتوفرة)، ثم يُقدّم المؤشر العام خطوة واحدة استعداداً لطلب التوليد التالي -
    هيك تتوزع الطلبات المتعاقبة على كل المفاتيح بالتساوي تقريباً (Round-Robin)."""
    global current_key_pointer
    total = len(API_KEYS)
    if total == 0:
        return []
    start = current_key_pointer % total
    order = [(start + offset) % total for offset in range(total)]
    current_key_pointer = (start + 1) % total
    return order


def _available_keys_for_model(model: str) -> List[int]:
    """قائمة فهارس المفاتيح غير المحظورة حالياً على نموذج مُعيّن بالذات (تُستخدم بمسار
    Super PDF المتوازي الذي يحتاج عدة مفاتيح متاحة بنفس اللحظة على نفس النموذج)."""
    return [index for index in range(len(API_KEYS)) if not _is_model_key_blocked(index, model)]


async def _execute_cascade(
    attempt_fn: Callable[[genai.Client, int, str], Awaitable[Any]],
) -> Optional[Any]:
    """المنفّذ العام لسلسلة الأولوية الكاملة:
    - الحلقة الخارجية: تمشي على MODELS_CASCADE من الأذكى للأضعف، وتستنفد كل المفاتيح على
      النموذج الحالي قبل النزول للنموذج التالي.
    - الحلقة الداخلية: تمشي على كل المفاتيح بدءاً من current_key_pointer (Round-Robin).
    - فحص لحظي بالذاكرة (بدون أي تأخير) ضد blocked_model_keys لتفادي أي زوج (مفتاح، نموذج)
      محظور حالياً، والانتقال فوراً للزوج التالي.
    - عند ازدحام مؤقت (503/...) يُعاد المحاولة على نفس الزوج بعدد محدود من المرات مع تأخير
      تصاعدي بسيط قبل اعتباره فاشلاً والانتقال للمفتاح التالي.
    """
    if not API_KEYS:
        log_error(logger, "GEMINI_API_KEYS is not configured")
        return None

    key_order = _round_robin_key_order()
    last_exc: Optional[Exception] = None

    for model in MODELS_CASCADE:
        for key_index in key_order:
            if _is_model_key_blocked(key_index, model):
                continue
            client = _GEMINI_CLIENTS[key_index]
            for attempt in range(OVERLOAD_RETRY_ATTEMPTS + 1):
                try:
                    return await attempt_fn(client, key_index, model)
                except Exception as exc:
                    last_exc = exc
                    if _is_overload_error(exc) and attempt < OVERLOAD_RETRY_ATTEMPTS:
                        delay = OVERLOAD_RETRY_BASE_DELAY * (attempt + 1)
                        log_warning(
                            logger,
                            f"Gemini key {key_index} (model={model}) overloaded, retrying in {delay}s "
                            f"(attempt {attempt + 1}/{OVERLOAD_RETRY_ATTEMPTS}): {exc}",
                        )
                        await asyncio.sleep(delay)
                        continue
                    _mark_model_key_failure(key_index, model, exc)
                    log_warning(logger, f"Gemini key {key_index} (model={model}) failed: {exc}")
                    break

    if last_exc:
        log_error(logger, f"Model waterfall cascade exhausted across all models/keys: {last_exc}")
    else:
        log_error(logger, "Model waterfall cascade exhausted: no available (key, model) pairs")
    return None


# ==============================================================================
# 🆕 GENERIC CASCADE EXECUTION HELPERS (REUSABLE ACROSS FEATURES)
# ==============================================================================
async def generate_structured_with_cascade(
    contents: List[Any],
    response_schema: type,
    prompt_instruction: Optional[str] = None,
) -> Optional[Tuple[Any, int]]:
    """توليد مُهيكل (JSON Schema) عبر كامل سلسلة الأولوية (نماذج × مفاتيح) - مناسب للكويزات
    أو أي إخراج يجب أن يتقيّد ببنية Pydantic محددة.

    contents: قائمة عناصر المحتوى (نص/Part.from_bytes/ملف مرفوع...) بدون البرومبت الأساسي.
    response_schema: كلاس Pydantic (مثل QuizResponse) يُفرض كـ response_schema بطلب Gemini.
    prompt_instruction: نص تعليمة اختياري يُضاف كأول عنصر بالمحتوى (البرومبت الرئيسي).

    يُرجع (parsed_object, token_count) عند النجاح، أو None إذا فشلت السلسلة بالكامل.
    """
    final_contents: List[Any] = ([prompt_instruction] if prompt_instruction else []) + list(contents)

    async def _attempt(client: genai.Client, key_index: int, model: str) -> Tuple[Any, int]:
        response = await asyncio.wait_for(
            client.aio.models.generate_content(
                model=model,
                contents=final_contents,
                config=types.GenerateContentConfig(
                    response_mime_type="application/json",
                    response_schema=response_schema,
                ),
            ),
            timeout=AI_REQUEST_TIMEOUT,
        )
        if response.parsed is None:
            raise ValueError(f"{model} returned no structured content")
        token_count = getattr(getattr(response, "usage_metadata", None), "total_token_count", 0) or 0
        return response.parsed, int(token_count)

    return await _execute_cascade(_attempt)


async def generate_text_with_cascade(
    contents: List[Any],
    prompt_instruction: Optional[str] = None,
) -> Optional[Tuple[str, int, bool]]:
    """توليد نصي حرّ (بدون إجبار JSON Schema) عبر كامل سلسلة الأولوية (نماذج × مفاتيح) -
    مناسب لمهام مثل تفريغ صوتي (Transcription) أو تلخيص نصوص حيث لا حاجة لبنية مُقيَّدة.

    contents: قائمة عناصر المحتوى (نص/صوت.../Part.from_bytes/ملف مرفوع...) بدون البرومبت.
    prompt_instruction: نص تعليمة اختياري يُضاف كأول عنصر بالمحتوى (البرومبت الرئيسي).

    يُرجع (النص الناتج, عدد التوكنز, هل انقطع النص بسبب استنفاد حد توكنز الإخراج) عند
    النجاح، أو None إذا فشلت السلسلة بالكامل.

    🆕 كشف الانقطاع (truncated=True عند finish_reason == MAX_TOKENS): النص الناتج بهذه
    الحالة غير فارغ وسليم شكلياً (يعدّي فحص `if not text` بالأسفل بلا مشاكل) لكنه ناقص
    فعلياً - توقف Gemini عن التوليد فجأة منتصف الجملة لأنه استنفد حد التوكنز الأقصى
    المسموح بالإخراج، وليس لأنه انتهى من المهمة فعلاً. على المستدعي (caller) معاملة
    truncated=True كـ"نجاح جزئي" لا نجاح كامل (تنبيه صريح + استرجاع نسبي للنقاط مثلاً)
    بدل تسليمها للمستخدم بصمت وكأنها نتيجة كاملة."""
    final_contents: List[Any] = ([prompt_instruction] if prompt_instruction else []) + list(contents)

    async def _attempt(client: genai.Client, key_index: int, model: str) -> Tuple[str, int, bool]:
        response = await asyncio.wait_for(
            client.aio.models.generate_content(
                model=model,
                contents=final_contents,
            ),
            timeout=AI_REQUEST_TIMEOUT,
        )
        text = getattr(response, "text", None)
        if not text or not text.strip():
            raise ValueError(f"{model} returned empty text response")
        token_count = getattr(getattr(response, "usage_metadata", None), "total_token_count", 0) or 0

        finish_reason = None
        candidates = getattr(response, "candidates", None) or []
        if candidates:
            finish_reason = getattr(candidates[0], "finish_reason", None)
        # الـ enum من الـ SDK بيرجّع .name = "MAX_TOKENS"، لكن بنتحقق كمان من str() الخام
        # كطبقة أمان إضافية بحال تغيّر شكل القيمة بنسخة SDK مختلفة.
        finish_reason_text = getattr(finish_reason, "name", None) or str(finish_reason or "")
        truncated = "MAX_TOKENS" in finish_reason_text

        return text, int(token_count), truncated

    return await _execute_cascade(_attempt)


# ==============================================================================
# HELPER FUNCTIONS (FILES / HASHING / PDF SPLITTING)
# ==============================================================================
def _combined_file_hash(paths: Sequence[str]) -> str:
    """توليد SHA-256 فريد لمجموعة من الملفات لاستخدامه كمفتاح للتخزين المؤقت (Cache Key)."""
    digest = hashlib.sha256()
    for path in paths:
        digest.update(calculate_file_hash(path).encode("ascii"))
    return digest.hexdigest()


def get_pdf_page_count_sync(file_path: str) -> int:
    """حساب عدد صفحات ملف الـ PDF ميكانيكياً."""
    with fitz.open(file_path) as document:
        return len(document)


def split_pdf_into_three_sync(file_path: str) -> List[str]:
    """تقسيم ملف PDF إلى 3 أجزاء متساوية للمعالجة المتوازية في نمط Super PDF."""
    source = fitz.open(file_path)
    try:
        page_count = len(source)
        if page_count < 3:
            return [file_path]
        base = Path(file_path)
        chunk_paths: List[str] = []
        for index in range(3):
            start = (page_count * index) // 3
            end = (page_count * (index + 1)) // 3 - 1
            chunk = fitz.open()
            try:
                chunk.insert_pdf(source, from_page=start, to_page=end)
                chunk_path = str(base.with_name(f"{base.stem}_{uuid.uuid4().hex}_part{index + 1}.pdf"))
                chunk.save(chunk_path)
                chunk_paths.append(chunk_path)
            finally:
                chunk.close()
        return chunk_paths
    finally:
        source.close()


async def _safe_delete_gemini_file(client: genai.Client, file_name: str) -> None:
    """حذف الملفات التابعة لـ Files API من خوادم جوجل بعد انتهاء التوليد لحفظ الخصوصية والنظافة."""
    try:
        await asyncio.to_thread(client.files.delete, name=file_name)
    except Exception as exc:
        log_warning(logger, f"Could not delete Gemini upload {file_name}: {exc}")


async def _loading_animation(message: Any, stop_event: asyncio.Event) -> None:
    """
    مهمة خلفية لتحديث رسالة الانتظار بعبارات تشجيعية.
    IMPORTANT: تم التعامل مع asyncio.CancelledError صراحة لمنع أخطاء إلغاء المهمة في الـ Logs.
    """
    phrase_index = 0
    while not stop_event.is_set():
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=3)
            break
        except asyncio.TimeoutError:
            try:
                await message.edit_text(LOADING_PHRASES[phrase_index])
            except TelegramBadRequest:
                # تحدث هذه الاستثناءات إذا لم يتغير النص في تلغرام
                pass
            except Exception as exc:
                log_warning(logger, f"Loading-status update failed: {exc}")
            phrase_index = (phrase_index + 1) % len(LOADING_PHRASES)
        except asyncio.CancelledError:
            # الخروج النظيف عند إلغاء المهمة من دالة generate_quiz_smart
            break


def _read_file_bytes_sync(path: str) -> bytes:
    with open(path, "rb") as f:
        return f.read()


async def _build_contents_for_paths(
    client: genai.Client, paths: Sequence[str], prompt: str, uploaded_out: List[Any]
) -> List[Any]:
    """يبني قائمة المحتوى (البرومبت + الملفات) لعميل مُعيّن: يفضّل الإرسال Inline للملفات
    الصغيرة (بدون أي اعتماد على عميل مُحدد لاحقاً)، أو يرفعها عبر Files API لهذا العميل
    بالذات (Files API مرتبطة بمفتاح/مشروع مُحدد فلا يمكن مشاركتها بين عملاء مختلفين) عند
    تجاوز حجمها لعتبة الإرسال المباشر - مع تجميع الملفات المرفوعة بـuploaded_out لتنظيفها لاحقاً."""
    contents: List[Any] = [prompt]

    total_size = 0
    for path in paths:
        try:
            total_size += os.path.getsize(path)
        except OSError:
            total_size = INLINE_DATA_SIZE_THRESHOLD + 1
            break

    mime_types = [get_safe_mime_type(path) for path in paths]
    use_inline = total_size <= INLINE_DATA_SIZE_THRESHOLD and all(mime_types)

    if use_inline:
        for path, mime_type in zip(paths, mime_types):
            file_bytes = await asyncio.to_thread(_read_file_bytes_sync, path)
            contents.append(types.Part.from_bytes(data=file_bytes, mime_type=mime_type))
    else:
        for path in paths:
            uploaded_file = await asyncio.to_thread(client.files.upload, file=path)
            uploaded_out.append(uploaded_file)
            contents.append(uploaded_file)

    return contents

# ==============================================================================
# CORE GENERATION LOGIC (GEMINI & GROQ)
# ==============================================================================
async def _generate_regular(paths: Sequence[str], prompt: str) -> Optional[Tuple[List[Dict[str, Any]], int]]:
    """المسار العادي لتوليد الكويز من ملفات: يُشغّل كامل سلسلة الأولوية (نماذج × مفاتيح)
    عبر _execute_cascade، بحيث تُبنى محتويات الطلب (Inline أو Files API) لكل محاولة بعميلها
    الخاص (ضروري لأن رفعات Files API غير قابلة للمشاركة بين مفاتيح/عملاء مختلفين)."""
    if not API_KEYS:
        log_error(logger, "GEMINI_API_KEYS is not configured")
        return None

    async def _attempt(client: genai.Client, key_index: int, model: str) -> Tuple[List[Dict[str, Any]], int]:
        uploaded: List[Any] = []
        try:
            contents = await _build_contents_for_paths(client, paths, prompt, uploaded)
            response = await asyncio.wait_for(
                client.aio.models.generate_content(
                    model=model,
                    contents=contents,
                    config=types.GenerateContentConfig(
                        response_mime_type="application/json",
                        response_schema=QuizResponse,
                    ),
                ),
                timeout=AI_REQUEST_TIMEOUT,
            )
            if not response.parsed or not hasattr(response.parsed, "questions"):
                raise ValueError("Gemini returned no structured questions")
            questions = [question.model_dump() for question in response.parsed.questions]
            token_count = getattr(getattr(response, "usage_metadata", None), "total_token_count", 0) or 0
            return questions, int(token_count)
        finally:
            # AI-NOTE: تنظيف وتفريغ أي ملفات رُفعت مؤقتاً لـ Files API في الخلفية
            for uploaded_file in uploaded:
                asyncio.create_task(_safe_delete_gemini_file(client, uploaded_file.name))

    return await _execute_cascade(_attempt)


async def _generate_single_attempt(
    paths: Sequence[str], prompt: str, key_index: int, model: str
) -> Tuple[List[Dict[str, Any]], int]:
    """محاولة توليد وحيدة بمفتاح ونموذج محدَّدين سلفاً (بدون المرور بسلسلة الأولوية الكاملة) -
    تُستخدم حصراً بمسار Super PDF المتوازي حيث كل جزء (Chunk) مُخصَّص لمفتاح مختلف بنفس اللحظة."""
    client = _GEMINI_CLIENTS[key_index]
    uploaded: List[Any] = []
    try:
        contents = await _build_contents_for_paths(client, paths, prompt, uploaded)
        response = await asyncio.wait_for(
            client.aio.models.generate_content(
                model=model,
                contents=contents,
                config=types.GenerateContentConfig(
                    response_mime_type="application/json",
                    response_schema=QuizResponse,
                ),
            ),
            timeout=AI_REQUEST_TIMEOUT,
        )
        if not response.parsed or not hasattr(response.parsed, "questions"):
            raise ValueError("Gemini returned no structured questions")
        questions = [question.model_dump() for question in response.parsed.questions]
        token_count = getattr(getattr(response, "usage_metadata", None), "total_token_count", 0) or 0
        return questions, int(token_count)
    except Exception as exc:
        _mark_model_key_failure(key_index, model, exc)
        raise
    finally:
        for uploaded_file in uploaded:
            asyncio.create_task(_safe_delete_gemini_file(client, uploaded_file.name))


async def _generate_super_pdf(file_path: str, count: int, prompt_template: str) -> Optional[Tuple[List[Dict[str, Any]], int]]:
    """معالجة متوازية لملفات الـ PDF الضخمة بتوزيع المهام على 3 مفاتيح API مختلفة بطلب واحد
    لكل جزء، باستخدام أقوى نموذج بسلسلة الأولوية (MODELS_CASCADE[0]) لكل الأجزاء الثلاثة."""
    if len(API_KEYS) < 3:
        log_error(logger, "Super processing requires three distinct GEMINI_API_KEYS")
        return None
    chunk_paths = await asyncio.to_thread(split_pdf_into_three_sync, file_path)
    if len(chunk_paths) != 3:
        return await _generate_regular([file_path], prompt_template.replace("{count}", str(count)))

    top_model = MODELS_CASCADE[0]
    key_indices = (_available_keys_for_model(top_model) or list(range(len(API_KEYS))))[:3]
    if len(key_indices) < 3:
        return None
    base, remainder = divmod(count, 3)
    question_counts = [base + (1 if index < remainder else 0) for index in range(3)]
    try:
        tasks = [
            _generate_single_attempt(
                [chunk_path], prompt_template.replace("{count}", str(question_count)), key_index, top_model
            )
            for chunk_path, question_count, key_index in zip(chunk_paths, question_counts, key_indices)
            if question_count > 0
        ]
        results = await asyncio.gather(*tasks)
        questions = [question for result, _ in results for question in result]
        total_tokens = sum(tokens for _, tokens in results)
        return questions, total_tokens
    finally:
        for chunk_path in chunk_paths:
            safe_file_cleanup(chunk_path)


def split_images_into_three_sync(file_paths: List[str]) -> List[List[str]]:
    """تقسيم قائمة صور ألبوم كبير لـ 3 دفعات متقاربة الحجم (توزيع دوري Round-Robin)
    للمعالجة المتوازية بنمط Super Images - نظير split_pdf_into_three_sync تماماً
    لكن على قائمة مسارات صور بدل صفحات PDF واحد."""
    if len(file_paths) < 3:
        return [file_paths]
    chunks: List[List[str]] = [[], [], []]
    for index, path in enumerate(file_paths):
        chunks[index % 3].append(path)
    return chunks


async def _generate_super_images(
    file_paths: List[str], count: int, prompt_template: str
) -> Optional[Tuple[List[Dict[str, Any]], int]]:
    """🆕 معالجة متوازية لألبومات الصور الكبيرة (أكبر من SUPER_IMAGE_BATCH_THRESHOLD -
    مصدرها حصراً رفع الويب حالياً، راجع handlers/files.py::process_web_uploaded_images)
    بتوزيع المهام على 3 مفاتيح API مختلفة بطلب واحد لكل دفعة صور، بنفس منطق
    _generate_super_pdf أعلاه حرفياً - فقط الدفعات هون قوائم صور بدل أجزاء PDF.

    ملاحظة مهمة: التقسيم لـ 3 دفعات هون سببه توزيع عدد الأسئلة الكبير على طلبات
    متعددة (تفادي انقطاع finish_reason=MAX_TOKENS بالمخرجات) وتسريع الاستجابة عبر
    التوازي - وليس بسبب أي سقف على عدد الصور بالطلب الواحد لدى Gemini (النماذج
    المستخدمة هون تدعم حتى 3,600 ملف صورة بالطلب الواحد، أعلى بكثير من أي ألبوم
    واقعي هون).
    """
    if len(API_KEYS) < 3:
        log_error(logger, "Super image processing requires three distinct GEMINI_API_KEYS")
        return None
    chunks = [chunk for chunk in await asyncio.to_thread(split_images_into_three_sync, file_paths) if chunk]
    if len(chunks) < 2:
        return await _generate_regular(file_paths, prompt_template.replace("{count}", str(count)))

    top_model = MODELS_CASCADE[0]
    key_indices = (_available_keys_for_model(top_model) or list(range(len(API_KEYS))))[:len(chunks)]
    if len(key_indices) < len(chunks):
        return None
    base, remainder = divmod(count, len(chunks))
    question_counts = [base + (1 if index < remainder else 0) for index in range(len(chunks))]
    tasks = [
        _generate_single_attempt(
            chunk, prompt_template.replace("{count}", str(question_count)), key_index, top_model
        )
        for chunk, question_count, key_index in zip(chunks, question_counts, key_indices)
        if question_count > 0
    ]
    if not tasks:
        return None
    results = await asyncio.gather(*tasks)
    questions = [question for result, _ in results for question in result]
    total_tokens = sum(tokens for _, tokens in results)
    return questions, total_tokens


async def _generate_text_quiz(pure_text: str, prompt: str, english_mode: Optional[str] = None) -> Optional[List[Dict[str, Any]]]:
    """المسار السريع لتوليد الكويز من النص الصريح فقط عبر Groq API.

    english_mode: عندما لا تكون None، تُستبدل القيم العربية التوضيحية داخل مثال بنية الـ JSON
    بقيم إنجليزية محايدة - لأن ترك مثال عربي هنا كان يدفع النموذج للانحياز نحو الإخراج
    العربي حتى لو طلب الموجّه الرئيسي (prompt) إخراجاً إنجليزياً صراحة.
    """
    if not GROQ_API_KEY or not _GROQ_CLIENT:
        log_warning(logger, "GROQ_API_KEY is not configured; skipping straight to Gemini for text generation")
        return None
    try:
        client = _GROQ_CLIENT  # عميل ثابت مُعاد استخدامه (بدل إنشاء عميل جديد بكل نداء)

        if english_mode:
            example_question = "Question text (Question text (الترجمة) if translated)"
            example_options = '["Option 1", "Option 2", "Option 3", "Option 4"]'
            example_hint = "Hint text"
            example_explanation = "Explanation text"
        else:
            example_question = "نص السؤال"
            example_options = '["خيار 1", "خيار 2", "خيار 3", "خيار 4"]'
            example_hint = "تلميح للمساعدة"
            example_explanation = "شرح الإجابة الصحيحة"

        json_schema_instruction = f"""
IMPORTANT: Respond in valid JSON structure matching this exact format:
{{
  "questions": [
    {{
      "question": "{example_question}",
      "options": {example_options},
      "correct_option_id": 0,
      "hint": "{example_hint}",
      "explanation": "{example_explanation}"
    }}
  ]
}}
Note: "correct_option_id" MUST be an integer representing the 0-based index of the correct option in "options" list.
"""
        formatted_prompt = prompt.replace("{option_count}", str(OPTION_COUNT))
        formatted_content = f"{formatted_prompt}\n\n{json_schema_instruction}\n\n[المحتوى التعليمي]:\n{pure_text}"

        response = await asyncio.wait_for(
            client.chat.completions.create(
                model="openai/gpt-oss-120b",
                messages=[{"role": "user", "content": formatted_content}],
                response_format={"type": "json_object"},
                temperature=0.7,
            ),
            timeout=45,
        )
        raw_content = response.choices[0].message.content
        # 🛠️ FIX (Defense in Depth): نموذجات Groq أحياناً تكتب backslash خام داخل نص JSON
        # (مثلاً مسار ويندوز أو أمر LaTeX متسرّب) بدل تهريبه بشكل صحيح (\\ بدل \) - وهذا
        # يجعل json.loads يُفسِّر \f \t \n \r \b كحروف تحكّم صامتة (تلف بصمت) أو يرمي
        # استثناء "Invalid \escape" لأي حرف آخر. نُرمِّم أي backslash غير متبوع بحرف
        # escape شرعي في JSON قبل التحليل، بدل تركه يتلف البيانات أو يُسقط الاستجابة كلها.
        raw_content = _JSON_BACKSLASH_REPAIR_RE.sub(r"\\\\", raw_content)
        parsed = QuizResponse(**json.loads(raw_content))
        return [question.model_dump() for question in parsed.questions]
    except Exception as exc:
        log_error(logger, f"Groq text generation failed, will fall back to Gemini: {exc}")
        return None


async def _generate_text_quiz_with_gemini(pure_text: str, prompt: str) -> Optional[List[Dict[str, Any]]]:
    """المسار الاحتياطي لتوليد الكويز من النص باستخدام Gemini عند تعثر Groq - يُنفَّذ الآن
    عبر الدالة العامة generate_structured_with_cascade التي تُشغّل كامل سلسلة الأولوية
    (نماذج × مفاتيح) بدل التقيّد بنموذج أساسي واحد + نموذج احتياطي واحد فقط."""
    if not API_KEYS:
        log_error(logger, "GEMINI_API_KEYS is not configured; cannot fall back for text generation")
        return None

    result = await generate_structured_with_cascade(
        contents=[pure_text],
        response_schema=QuizResponse,
        prompt_instruction=prompt,
    )
    if not result:
        return None
    parsed, _token_count = result
    if not hasattr(parsed, "questions"):
        return None
    return [question.model_dump() for question in parsed.questions]


def _resolve_difficulty_instruction(difficulty: Optional[str]) -> str:
    """يحوّل قيمة الصعوبة المخزّنة إلى نص التعليمة المحقونة بالبرومبت، مع افتراض
    'متوسط' الآمن لأي قيمة غير معروفة أو غير محددة."""
    return DIFFICULTY_PROMPT_INSTRUCTIONS.get(difficulty or DIFFICULTY_MEDIUM, DIFFICULTY_PROMPT_INSTRUCTIONS[DIFFICULTY_MEDIUM])


def _resolve_question_type_instruction(question_type: Optional[str], custom_question_type_text: Optional[str]) -> str:
    """يحوّل نوع الأسئلة المختار إلى نص التعليمة المحقونة بالبرومبت:
    - نوع مخصص (custom): يُحقن نص الطالب الحر كما هو (بعد تنظيف بسيط).
    - نوع ثابت من القوائم الجاهزة أو من اقتراحات AI: يُستخدم النص الجاهز المطابق إن وجد.
    - غير محدد أو 'general': تعليمة محايدة (تغطية متوازنة لكل الأنواع)."""
    if custom_question_type_text and custom_question_type_text.strip():
        return f"ركّز حصراً على نوع الأسئلة التالي كما حدده الطالب بالضبط: {custom_question_type_text.strip()}"
    if question_type and question_type in QUESTION_TYPE_PROMPT_INSTRUCTIONS:
        return QUESTION_TYPE_PROMPT_INSTRUCTIONS[question_type]
    return QUESTION_TYPE_INSTRUCTION_GENERAL


# ==============================================================================
# MAIN PUBLIC API ENTRYPOINT
# ==============================================================================
async def generate_quiz_smart(
    file_paths: Optional[List[str]] = None,
    pure_text: Optional[str] = None,
    count: int = 0,
    skip_cache: bool = False,
    file_hash: Optional[str] = None,
    status_message: Optional[Any] = None,
    previous_questions: Optional[List[Dict[str, Any]]] = None,
    is_math_mode: bool = False,
    english_mode: Optional[str] = None,
    difficulty: Optional[str] = None,
    question_type: Optional[str] = None,
    custom_question_type_text: Optional[str] = None,
) -> Optional[List[Dict[str, Any]]]:
    """
    الدالة الرئيسية المستدعاة من قبل البوت لتوليد الاختبار الذكي.
    تتولى إدارة الـ Cache، توجيه الطلبات للمسار المناسب، وتنظيف مهام الرسائل التفاعلية.

    is_math_mode: عندما تكون True (بعد التصنيف الموحّد عبر services.subject_classifier)،
    يُستبدل موجّه التوليد بنسخة LaTeX المخصصة لـ"نمط الكويز المصوّر" بدل الموجّه العادي.

    english_mode: عندما يكون "translated" أو "plain" (بعد اكتشاف محتوى إنجليزي واختيار
    الطالب عبر handlers/files.py)، يُستبدل موجّه التوليد بالنسخة الإنجليزية المناسبة. يُتجاهل
    كلياً إذا كان is_math_mode=True (الأولوية دائماً لنمط الكويز المصوّر الرياضي).

    🆕 difficulty / question_type / custom_question_type_text: تُحقن كنصوص تعليمات إضافية
    ({difficulty_instruction} و{question_type_instruction}) بكل الموجّهات الأربعة على حد
    سواء، بغض النظر عن المسار (رياضي/إنجليزي/عادي) - راجع _resolve_difficulty_instruction
    و_resolve_question_type_instruction أعلاه لمنطق التحويل.

    🆕 يُنفَّذ الآن كل التوليد الفعلي (ملفات أو نص) عبر سلسلة أولوية النماذج الكاملة
    (MODELS_CASCADE) مع Round-Robin على المفاتيح، دون أي تغيير على توقيع أو سلوك هذه
    الدالة العامة نفسها.
    """
    stop_event = asyncio.Event()
    animation_task = asyncio.create_task(_loading_animation(status_message, stop_event)) if status_message else None

    try:
        # 🆕 اختيار الموجّه المناسب حسب الأولوية: رياضي (LaTeX) > إنجليزي (مترجم/عادي) > قياسي
        if is_math_mode:
            source_prompt = SYSTEM_PROMPT_GENERATE_MATH_QUESTIONS
        elif english_mode == "translated":
            source_prompt = SYSTEM_PROMPT_GENERATE_ENGLISH_TRANSLATED_QUESTIONS
        elif english_mode == "plain":
            source_prompt = SYSTEM_PROMPT_GENERATE_ENGLISH_PLAIN_QUESTIONS
        else:
            source_prompt = SYSTEM_PROMPT_GENERATE_QUESTIONS
        # IMPORTANT: استبدال {option_count} أولاً لضمان وصول العدد الصحيح للخيارات لكافة النماذج
        base_prompt_template = source_prompt.replace("{option_count}", str(OPTION_COUNT))

        # 🆕 حقن تعليمات الصعوبة ونوع الأسئلة - موجودتان بالأربع موجّهات على حد سواء
        base_prompt_template = base_prompt_template.replace(
            "{difficulty_instruction}", _resolve_difficulty_instruction(difficulty)
        ).replace(
            "{question_type_instruction}",
            _resolve_question_type_instruction(question_type, custom_question_type_text),
        )

        # حقن الأسئلة السابقة لمنع التكرار
        if previous_questions:
            old_q_texts = "\n".join([f"- {q['question']}" for q in previous_questions if 'question' in q])
            base_prompt_template += MSG_PREVIOUS_QUESTIONS_INSTRUCTION.format(previous_questions=old_q_texts)

        prompt = base_prompt_template.replace("{count}", str(count))

        # 1. مسار النصوص الصريحة
        if pure_text:
            # 🛠️ FIX: نمط الكويز المصوّر الرياضي (is_math_mode) يُوجَّه مباشرة لمسار Gemini
            # (generate_structured_with_cascade + response_schema) ولا يمر إطلاقاً بمسار
            # Groq السريع (_generate_text_quiz). السبب: Groq يُستدعى بـ
            # response_format={"type": "json_object"} ثم يُحلَّل ناتجه بـ json.loads() يدوياً،
            # ونموذج gpt-oss-120b لا يُهرِّب الـ backslash بشكل صحيح داخل نصوص LaTeX
            # (يكتب \frac بدل \\frac). قواعد تهريب JSON الرسمية تُفسِّر \f و\t و\n و\r و\b
            # كحروف تحكّم صامتة (form-feed/tab/newline...) فتُبتلع بصمت - وهذا بالضبط
            # سبب ظهور "⍰rac"/"⍰ext" بدل \frac/\text داخل صور الكويزات الرياضية (رموز
            # LaTeX تبدأ بحرف escape شرعي بـ JSON زي \frac \text \theta \times \beta \neq
            # تتلف بصمت، بينما \sqrt \alpha \pi \sum ... تُسبب استثناء JSON كامل فيُعاد
            # التوليد تلقائياً عبر Gemini - ولهذا كانت المشكلة تظهر جزئياً فقط). مسار Gemini
            # (response_schema=QuizResponse) يستخدم توليداً مُقيَّداً يضمن تهريب JSON سليم
            # دائماً، بغض النظر عن محتوى الـ backslash داخل النص.
            if is_math_mode:
                return await _generate_text_quiz_with_gemini(pure_text, prompt)
            questions = await _generate_text_quiz(pure_text, prompt, english_mode=english_mode)
            if not questions:
                questions = await _generate_text_quiz_with_gemini(pure_text, prompt)
            return questions

        if not file_paths:
            return None

        # 2. فحص الـ Cache أولاً للحد من استهلاك API
        cache_key = file_hash or await asyncio.to_thread(_combined_file_hash, file_paths)
        if not skip_cache:
            cached = await get_cached_quiz(cache_key)
            if cached and cached.get("questions_data"):
                log_info(logger, f"Cache hit for {cache_key}; external generation bypassed")
                return cached["questions_data"]

        # 3. توجيه الملفات للمسار العادي، أو Super PDF للملفات الضخمة، أو 🆕 Super
        #    Images لألبومات الصور الكبيرة (أكبر من SUPER_IMAGE_BATCH_THRESHOLD -
        #    مصدرها حصراً رفع الويب حالياً؛ الألبوم العادي عبر تيليجرام محدود أصلاً
        #    بـ MAX_ALBUM_IMAGES من منصة تيليجرام نفسها، أقل من هذا السقف).
        is_super_pdf = (
            len(file_paths) == 1
            and file_paths[0].lower().endswith(".pdf")
            and await asyncio.to_thread(get_pdf_page_count_sync, file_paths[0]) > MAX_LIMIT_PAGES
        )
        is_super_images = len(file_paths) > SUPER_IMAGE_BATCH_THRESHOLD
        if is_super_pdf:
            generated = await _generate_super_pdf(file_paths[0], count, base_prompt_template)
        elif is_super_images:
            generated = await _generate_super_images(file_paths, count, base_prompt_template)
        else:
            generated = await _generate_regular(file_paths, prompt)
        if not generated:
            return None

        questions, total_tokens = generated
        return questions

    finally:
        # IMPORTANT: إيقاف وإلغاء مهمة التحريك بشكل فوري ونظيف في كتلة finally
        stop_event.set()
        if animation_task:
            animation_task.cancel()
            try:
                await animation_task
            except asyncio.CancelledError:
                pass
