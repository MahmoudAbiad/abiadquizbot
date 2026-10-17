"""
helpers/gemini_helper/text_generation.py
توليد الكويز من نص صريح: Groq السريع + احتياطي Gemini.
"""

from typing import Any, Dict, List, Optional
import asyncio
import json

from constants import OPTION_COUNT
from logger import get_logger, log_error, log_info, log_warning
from helpers.ai_models_helper import get_groq_fast_model
from helpers.gemini_helper.clients import API_KEYS, GROQ_API_KEY, _GROQ_CLIENT, _GROQ_TPM_SAFE_LIMIT
from helpers.gemini_helper.state import _groq_skip_reason_var, _last_model_used_var
from helpers.gemini_helper.parsing import _repair_json_backslashes
from helpers.gemini_helper.schemas import QuizResponse
from helpers.gemini_helper.generic import generate_structured_with_cascade

logger = get_logger(__name__)


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

        # 🩹 FIX: حاجز استباقي لحد الـ TPM (Tokens Per Minute) الفعلي لـ Groq (8000 توكن على
        # الخطة الحالية - راجع رؤوس الاستجابة x-ratelimit-limit-tokens، وخطأ 413 الفعلي اللي
        # صار بالـ logs: "Limit 8000, Requested 9165"). بدل ما ننتظر Groq يرفض الطلب بـ 413
        # (نفس النتيجة النهائية فعلياً بفضل except أدناه، لكن بعد استهلاك round-trip كامل)،
        # نقدّر عدد التوكنز محلياً *قبل* أي نداء شبكة، ولو تجاوز هامش الأمان (7000 من أصل 8000)
        # نتخطى Groq فوراً بإرجاع None - نفس آلية الفولباك بـ generate_quiz_smart (سطر ~1340)
        # بتاخد المسار لـ Gemini على طول بدون أي تأخير إضافي. التقدير محافظ (حرفين/توكن تقريباً
        # - النص العربي يميل لتوكنز أكثر من الإنجليزي بنفس عدد الأحرف مع أغلب الـ BPE tokenizers).
        _estimated_tokens = len(formatted_content) / 2.0
        if _estimated_tokens > _GROQ_TPM_SAFE_LIMIT:
            log_warning(
                logger,
                f"Skipping Groq (estimated ~{int(_estimated_tokens)} tokens > safe limit "
                f"{_GROQ_TPM_SAFE_LIMIT}/8000 TPM) — routing directly to Gemini cascade",
            )
            # 🆕 FIX: نسجّل سبب التخطي هون - يُقرأ لاحقاً من _finalize بـ generate_quiz_smart
            # ويُرفَق بـ metadata التوليد النهائي (حتى لو الفائز صار Gemini/Vertex)، عشان
            # يُميَّز بلوحة الأدمن عن أي فشل Groq حقيقي (نداء فعلي انرفض/تأخّر).
            _groq_skip_reason_var.set("text_too_long")
            return None

        # 🆕 اسم الموديل صار ديناميكياً من لوحة التحكم (slot="groq_fast") بدل ثابت بالكود.
        groq_model = (await get_groq_fast_model())["model_name"]
        response = await asyncio.wait_for(
            client.chat.completions.create(
                model=groq_model,
                messages=[{"role": "user", "content": formatted_content}],
                response_format={"type": "json_object"},
                temperature=0.7,
            ),
            timeout=45,
        )
        raw_content = response.choices[0].message.content
        # 🛠️ FIX (Defense in Depth): نموذجات Groq أحياناً تكتب backslash خام داخل نص JSON
        # (أمر LaTeX متسرّب زي \frac \text \bar) بدل تهريبه بشكل صحيح (\\ بدل \) - راجع
        # _repair_json_backslashes أعلى الملف للتفاصيل الكاملة لماذا regex وحيد لم يكن كافياً.
        raw_content = _repair_json_backslashes(raw_content)
        parsed = QuizResponse(**json.loads(raw_content))
        log_info(logger, f"✅ Cascade success: provider=groq model={groq_model}")
        _last_model_used_var.set({
            "provider": "groq", "model": groq_model, "key_index": None,
            # 🆕 مسار Groq السريع مستقل تماماً عن سلسلة الكاسكيد الرئيسية (slot منفصل
            # groq_fast) - لا يوجد "ترتيب" ذو معنى هون، None صراحة بدل رقم وهمي.
            "cascade_rank": None, "cascade_total": None,
        })
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
