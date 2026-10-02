"""
helpers/gemini_helper/generic.py
واجهات عامة قابلة لإعادة الاستخدام فوق الـ cascade (توليد مُهيكل / نصي حر).
"""

from typing import Any, List, Optional, Tuple
import asyncio
import time

from google import genai
from google.genai import types

from constants import AI_REQUEST_TIMEOUT
from helpers.gemini_helper.state import (
    _record_token_usage, _last_model_used_var, _last_token_usage_var, _last_generation_metadata_var,
)
from helpers.gemini_helper.parsing import _parse_structured_gemini_response
from helpers.gemini_helper.cascade import _execute_cascade


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

    async def _attempt(client: genai.Client, key_index: int, model: str, cascade_rank: int, cascade_total: int) -> Tuple[Any, int]:
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
        parsed = _parse_structured_gemini_response(response, response_schema, model)
        token_count = _record_token_usage(response)
        return parsed, int(token_count)

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
    _last_model_used_var.set(None)
    _last_token_usage_var.set(None)
    _last_generation_metadata_var.set(None)
    started_at = time.monotonic()
    final_contents: List[Any] = ([prompt_instruction] if prompt_instruction else []) + list(contents)

    async def _attempt(client: genai.Client, key_index: int, model: str, cascade_rank: int, cascade_total: int) -> Tuple[str, int, bool]:
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
        token_count = _record_token_usage(response)

        finish_reason = None
        candidates = getattr(response, "candidates", None) or []
        if candidates:
            finish_reason = getattr(candidates[0], "finish_reason", None)
        # الـ enum من الـ SDK بيرجّع .name = "MAX_TOKENS"، لكن بنتحقق كمان من str() الخام
        # كطبقة أمان إضافية بحال تغيّر شكل القيمة بنسخة SDK مختلفة.
        finish_reason_text = getattr(finish_reason, "name", None) or str(finish_reason or "")
        truncated = "MAX_TOKENS" in finish_reason_text

        return text, int(token_count), truncated

    result = await _execute_cascade(_attempt)
    if result:
        metadata = dict(_last_model_used_var.get() or {})
        metadata.update(_last_token_usage_var.get() or {})
        metadata["duration_seconds"] = time.monotonic() - started_at
        metadata["mode"] = "text"
        _last_generation_metadata_var.set(metadata)
    return result
