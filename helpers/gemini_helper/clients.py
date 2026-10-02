"""
helpers/gemini_helper/clients.py
مزوّدو الذكاء الاصطناعي: مفاتيح Gemini، عملاء Gemini/Groq/Vertex (Singletons)، وإعدادات البيئة.
"""

from typing import List, Optional
import json
import os

from dotenv import load_dotenv
from google import genai
from groq import AsyncGroq

from logger import get_logger, log_error, log_info, log_warning

logger = get_logger(__name__)


# ==============================================================================
# CONFIGURATION & GLOBAL STATE
# ==============================================================================
load_dotenv()

# AI-NOTE: يتم تحميل مفاتيح Gemini كقائمة وتتبع المفاتيح المعطلة مؤقتاً في ذاكرة السيرفر
API_KEYS = [key.strip() for key in os.getenv("GEMINI_API_KEYS", "").split(",") if key.strip()]

GROQ_API_KEY = os.getenv("GROQ_API_KEY", "")

# 🩹 FIX: هامش أمان لتقدير التوكنز قبل نداء Groq (راجع الشرح الكامل داخل _generate_text_quiz
# أسفل منطق الحاجز الاستباقي). حد Groq الفعلي (TPM) هو 8000 - نترك هامش ~1000 توكن كاحتياط
# لعدم دقة التقدير (حرفين/توكن تقريبي، مش tokenizer حقيقي).
_GROQ_TPM_SAFE_LIMIT = 7000

# AI-NOTE (memory-leak fix): سابقاً كان يتم إنشاء genai.Client()/AsyncGroq() جديد بكل
# استدعاء توليد (أحياناً عدة مرات بنفس الطلب عبر مسارات fallback/Super PDF)، وهاد العميل
# بيحمل جوّاه connection pool خاص فيه (httpx) ما بينسكر أبداً - وهاد كان سبب تسريب الذاكرة
# التدريجي اللي أدى لـ Memory Exceeded على Render. الحل: عميل واحد ثابت (Singleton) لكل
# مفتاح Gemini ولـ Groq، يتم إنشاؤه مرة وحدة عند تحميل الموديول ويُعاد استخدامه دائماً -
# نفس نمط `bot`/`redis_client` بملف config.py.
_GEMINI_CLIENTS: List[genai.Client] = [genai.Client(api_key=key) for key in API_KEYS]
_GROQ_CLIENT: Optional[AsyncGroq] = AsyncGroq(api_key=GROQ_API_KEY) if GROQ_API_KEY else None

# ==============================================================================
# 🆕 VERTEX AI CLIENT (مزوّد إضافي جانبي بسلسلة الـ cascade - راجع docs/HISTORY_LOG.md)
# ==============================================================================
# AI-NOTE: عميل Vertex واحد (Singleton) اختياري بالكامل - لاستهلاك كريدت GCP المجاني
# (Free Trial / Google Developer Program) عبر نفس موديلات Gemini، جنباً إلى جنب مع مفاتيح
# AI Studio العادية (GEMINI_API_KEYS) بنفس سلسلة الـ cascade، دون أي استبدال لها. أولويته
# تتحدد فقط بمكانه بترتيب slot="cascade" (لوحة الأدمن: handlers/admin/ai_control.py) -
# تماماً مثل أي موديل Gemini آخر، لا حاجة لأي منطق أولوية إضافي هنا.
#
# المصادقة عبر Service Account (لا API Key) - محتوى ملف الـ JSON الكامل يُقرأ من متغيّر
# بيئة واحد (GOOGLE_VERTEX_SA_KEY_JSON) بدل كتابته كملف على القرص، لأن نظام ملفات Heroku
# إيفيميرال (يُمسح عند كل إعادة تشغيل/dyno جديد) - تمرير الاعتماد ككائن Credentials مباشرة
# لـ genai.Client أنسب بيئياً ولا يحتاج أي كتابة ملفات وقت الإقلاع.
#
# لو أي من المتغيرات الثلاثة ناقص أو فشل تحليل الـ JSON، يبقى _VERTEX_CLIENT = None بهدوء
# (بدون كراش على إقلاع البوت بأكمله) - أي صف provider="vertex" بالـ cascade يُتخطى بصمت
# وقتها (راجع _execute_cascade)، وباقي السلسلة (Gemini) تستمر بالعمل طبيعياً تماماً.
GOOGLE_VERTEX_PROJECT = os.getenv("GOOGLE_VERTEX_PROJECT", "").strip()
GOOGLE_VERTEX_LOCATION = os.getenv("GOOGLE_VERTEX_LOCATION", "us-central1").strip()
_GOOGLE_VERTEX_SA_KEY_JSON = os.getenv("GOOGLE_VERTEX_SA_KEY_JSON", "").strip()

# فهرس محجوز (خارج نطاق 0..len(API_KEYS)-1 دائماً) لتتبّع حظر (مفتاح، موديل) الخاص بـ
# Vertex ضمن نفس قاموس blocked_model_keys الحالي - بدون أي تعديل على بنية ذلك القاموس.
VERTEX_KEY_INDEX = -1

_VERTEX_CLIENT: Optional[genai.Client] = None
if GOOGLE_VERTEX_PROJECT and _GOOGLE_VERTEX_SA_KEY_JSON:
    try:
        from google.oauth2 import service_account as _google_service_account

        _vertex_credentials = _google_service_account.Credentials.from_service_account_info(
            json.loads(_GOOGLE_VERTEX_SA_KEY_JSON),
            scopes=["https://www.googleapis.com/auth/cloud-platform"],
        )
        _VERTEX_CLIENT = genai.Client(
            vertexai=True,
            project=GOOGLE_VERTEX_PROJECT,
            location=GOOGLE_VERTEX_LOCATION,
            credentials=_vertex_credentials,
        )
        log_info(logger, f"✅ Vertex AI client initialized (project={GOOGLE_VERTEX_PROJECT}, location={GOOGLE_VERTEX_LOCATION})")
    except Exception as exc:
        log_error(logger, f"Failed to initialize Vertex AI client (cascade will skip provider='vertex' rows): {exc}")
        _VERTEX_CLIENT = None
else:
    log_warning(
        logger,
        "Vertex AI env vars not fully configured (GOOGLE_VERTEX_PROJECT/GOOGLE_VERTEX_SA_KEY_JSON) - "
        "any provider='vertex' rows in the cascade slot will be skipped silently",
    )
