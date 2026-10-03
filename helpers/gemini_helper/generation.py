"""
helpers/gemini_helper/generation.py
توليد الكويز من الملفات: المسار العادي وSuper PDF / Super Images.
"""

from typing import Any, Awaitable, Callable, Dict, List, Optional, Sequence, Tuple
import asyncio

from google import genai
from google.genai import types

from task_utils import spawn
from helpers.settings_helper import get_ai_request_timeout
from logger import get_logger, log_error
from mem_utils import release_memory_to_os
from utils import safe_file_cleanup
from helpers.gemini_helper.clients import API_KEYS
from helpers.gemini_helper.state import (
    _last_model_used_var,
    _last_token_usage_var,
    _record_token_usage,
)
from helpers.gemini_helper.parsing import _parse_structured_gemini_response
from helpers.gemini_helper.cascade import _execute_cascade
from helpers.gemini_helper.media import (
    _build_contents_for_paths,
    _safe_delete_gemini_file,
    split_images_into_three_sync,
    split_pdf_into_three_sync, split_pdf_without_orphans,
)
from helpers.gemini_helper.schemas import QuizResponse

logger = get_logger(__name__)


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

    async def _attempt(client: genai.Client, key_index: int, model: str, cascade_rank: int, cascade_total: int) -> Tuple[List[Dict[str, Any]], int]:
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
                timeout=await get_ai_request_timeout(),
            )
            parsed = _parse_structured_gemini_response(response, QuizResponse, model)
            if not hasattr(parsed, "questions"):
                raise ValueError("Gemini returned no structured questions")
            questions = [question.model_dump() for question in parsed.questions]
            token_count = _record_token_usage(response)
            return questions, int(token_count)
        finally:
            # AI-NOTE: تنظيف وتفريغ أي ملفات رُفعت مؤقتاً لـ Files API في الخلفية
            for uploaded_file in uploaded:
                spawn(_safe_delete_gemini_file(client, uploaded_file.name))

    return await _execute_cascade(_attempt)


def _build_super_chunk_attempt_fn(
    paths: Sequence[str], prompt: str
) -> Callable[[genai.Client, int, str, int, int], Awaitable[Tuple[List[Dict[str, Any]], int, int, int, int, str, int, int, int]]]:
    """🆕 (طلب صريح من المستخدم: كاسكيد نظامي كامل بدل تثبيت مفتاح واحد + محاولة احتياطية
    وحيدة) يبني attempt_fn متوافقة تماماً مع توقيع _execute_cascade العام (نفس المُنفِّذ
    المستخدَم بمسار التوليد العادي بالضبط) لجزء واحد ثابت (مسار/مسارات + برومبت) ضمن
    Super PDF/Images.

    الفرق الجوهري عن التصميم السابق: client/key_index/model هلق كلها تُمرَّر من
    _execute_cascade نفسه حسب مكانه الحالي بالسلسلة (رتبة الموديل × دورة المفاتيح)، بدل ما
    تكون مُثبَّتة سلفاً لكل جزء. عملياً: يجرَّب أقوى موديل (MODELS_CASCADE[0]) على كل مفتاح
    متاح بالتتالي؛ لو فشل على كل المفاتيح، ينتقل الكاسكيد كامل للموديل التالي بالسلسلة على كل
    المفاتيح من جديد، وهكذا - تماماً نفس منطق التوليد العادي، مطبَّق هون على مستوى كل جزء
    (Chunk) على حدة، بالتوازي مع بقية الأجزاء (لكل جزء نسخته الخاصة من هذا الكاسكيد، تعمل
    بنفس اللحظة عبر asyncio.gather بالمستدعي - راجع _generate_super_pdf).

    🆕 نُعيد أيضاً model/key_index الفائزين صراحة ضمن القيمة المُرجَعة (وليس فقط عبر
    ContextVar الذي يضبطه _execute_cascade داخلياً) لأن كل جزء يُنفَّذ بمهمة (Task) منفصلة
    عبر asyncio.gather بالمستدعي، وContextVar لا يتسرّب تلقائياً من مهمة فرعية لمهمة أصلية -
    بدونها ما كان بالإمكان معرفة أي موديل/مفتاح فاز فعلياً بكل جزء من نطاق الدالة المستدعية.

    ملاحظة تصميم: تسجيل فشل كل زوج (مفتاح، موديل) هون من مسؤولية _execute_cascade نفسه
    (يستدعي _mark_model_key_failure تلقائياً بكل استثناء) - لا نكرّر ذلك هون تفادياً لأي
    ازدواجية بالحظر.

    🆕 نرجّع أيضاً cascade_rank/cascade_total الفائزين (ممرَّرين هلق من _execute_cascade
    نفسه لكل attempt - راجع الملاحظة الموازية فوق _execute_cascade) لنفس سبب model/key_index:
    كل جزء Task منفصل، فلازم يرجّع رقم ترتيبه صراحة بقيمة العودة بدل الاعتماد على ContextVar.
    يُستخدم لاحقاً من _generate_super_pdf/_generate_super_images لبناء تفصيل دقيق لكل جزء
    (أي موديل فاز، وبأي ترتيب ضمن الكاسكيد وقتها) - يُعرض بلوحة الأدمن ("📊 سجل توليد
    الكويزات") بدل وصف عام واحد لكل السوبر."""
    async def _attempt(client: genai.Client, key_index: int, model: str, cascade_rank: int, cascade_total: int):
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
                timeout=await get_ai_request_timeout(),
            )
            parsed = _parse_structured_gemini_response(response, QuizResponse, model)
            if not hasattr(parsed, "questions"):
                raise ValueError("Gemini returned no structured questions")
            questions = [question.model_dump() for question in parsed.questions]
            usage = getattr(response, "usage_metadata", None)
            input_tokens = int(getattr(usage, "prompt_token_count", 0) or 0)
            output_tokens = int(getattr(usage, "candidates_token_count", 0) or 0)
            thoughts_tokens = int(getattr(usage, "thoughts_token_count", 0) or 0)
            token_count = int(getattr(usage, "total_token_count", 0) or 0)
            return (
                questions, int(token_count), input_tokens, output_tokens, thoughts_tokens,
                model, key_index, cascade_rank, cascade_total,
            )
        finally:
            for uploaded_file in uploaded:
                spawn(_safe_delete_gemini_file(client, uploaded_file.name))
    return _attempt


async def _generate_super_pdf(file_path: str, count: int, prompt_template: str) -> Optional[Tuple[List[Dict[str, Any]], int]]:
    """معالجة متوازية لملفات الـ PDF الضخمة: تقسيم الملف لـ 3 أجزاء، وكل جزء يُنفَّذ بمهمة
    (Task) مستقلة عبر _execute_cascade (نفس المُنفِّذ العام المستخدَم بالتوليد العادي)،
    بالتوازي فيما بينها عبر asyncio.gather.

    🆕 (طلب صريح من المستخدم) كاسكيد نظامي كامل لكل جزء بدل تثبيت مفتاح واحد + محاولة
    احتياطية وحيدة: كل جزء يجرَّب أقوى موديل على كل مفتاح متاح بالتتالي، فإذا فشل على الكل
    ينتقل للموديل التالي بالسلسلة على كل المفاتيح من جديد، وهكذا حتى نفاد السلسلة كاملة (أو
    النجاح). يعني الأجزاء الثلاثة لم تعد "مقفولة" كل واحدة بمفتاح مختلف من البداية - ممكن
    جزءين ينتهي بهم المطاف يجربون نفس المفتاح لو الأول فشل عليه، وهذا مقبول ومقصود (كل مفتاح
    يتحمّل عدة طلبات متزامنة براحة)."""
    if len(API_KEYS) < 3:
        log_error(logger, "Super processing requires three distinct GEMINI_API_KEYS")
        return None
    chunk_paths = await split_pdf_without_orphans(file_path)
    try:
        # 🩹 FIX (memory-leak): fitz (PyMuPDF) يخصّص ذاكرة كبيرة أثناء فتح/تقسيم PDF
        # ضخم؛ رغم إغلاق المستندات بـ try/finally (وهو صحيح)، glibc لا يعيد هذه
        # الصفحات لنظام التشغيل تلقائيًا. نجبرها هنا فورًا بعد انتهاء التقسيم.
        await asyncio.to_thread(release_memory_to_os)
        if len(chunk_paths) != 3:
            return await _generate_regular([file_path], prompt_template.replace("{count}", str(count)))

        base, remainder = divmod(count, 3)
        question_counts = [base + (1 if index < remainder else 0) for index in range(3)]
        tasks = [
            _execute_cascade(
                _build_super_chunk_attempt_fn(
                    [chunk_path], prompt_template.replace("{count}", str(question_count))
                ),
                exclude_vertex=True,
            )
            for chunk_path, question_count in zip(chunk_paths, question_counts)
            if question_count > 0
        ]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        # 🆕 لو جزء واحد استنفد السلسلة كاملة (كل موديل × كل مفتاح فشل معه)، _execute_cascade
        # يرجّع له None بدل رفع استثناء - نعامل فشل أي جزء كفشل كامل للطلب (نفس سلوك
        # asyncio.gather السابق عند رفع استثناء، لكن بشكل صريح هلق).
        if any(result is None or isinstance(result, BaseException) for result in results):
            log_error(logger, "Super PDF: chunk exhausted the entire model/key cascade")
            return None
        questions = [question for result in results for question in result[0]]
        total_tokens = sum(result[1] for result in results)
        total_input = sum(result[2] for result in results)
        total_output = sum(result[3] for result in results)
        total_thoughts = sum(result[4] for result in results)
        winning_models = [result[5] for result in results]
        # 🆕 تفصيل كامل لكل جزء على حدة (موديل/مفتاح/رتبته ضمن الكاسكيد لحظة نجاحه) - يُقرأ
        # لاحقاً بلوحة الأدمن ("📊 سجل توليد الكويزات") لعرض كل جزء برقمه الحقيقي بدل وصف
        # عام واحد لكل عملية Super. راجع _build_super_chunk_attempt_fn لمصدر هالحقول.
        chunk_details = [
            {"model": result[5], "key_index": result[6], "cascade_rank": result[7], "cascade_total": result[8]}
            for result in results
        ]
        # 🆕 كل جزء نُفِّذ بمهمة (Task) منفصلة عبر asyncio.gather - ContextVar لا يتسرّب
        # تلقائياً من مهمة فرعية لمهمة أصلية، لذا نسجّل الموديل/المفتاح الفائزين والتوكنز
        # المجمّعة صراحة هنا من القيم المُعادة من كل جزء (راجع _build_super_chunk_attempt_fn).
        # حقل "model": موديل الجزء الأول لو كل الأجزاء استخدمت نفس الموديل (الحالة الشائعة)،
        # أو "mixed" صراحة لو اختلفت (كاسكيد فعلي صار فيه انتقال لموديل/مفتاح آخر لبعض
        # الأجزاء) - chunk_details يحفظ التفصيل الكامل (موديل + مفتاح + رتبة) لكل جزء بأي حال.
        _last_model_used_var.set({
            "provider": "gemini",
            "model": winning_models[0] if len(set(winning_models)) == 1 else "mixed",
            "key_index": None, "mode": "super_pdf",
            "chunk_details": chunk_details,
            # 🆕 هالمسار بيشمل كاسكيد كامل عبر عدة أزواج (مفتاح، موديل) محتملة لكل جزء - رقم
            # ترتيب دقيق واحد ضمن الكاسكيد الكامل مو منطقي هون (كل جزء ممكن يفوز برتبة
            # مختلفة)، فبنسيبه None صراحة - رتبة كل جزء الحقيقية موجودة جوا chunk_details.
            "cascade_rank": None, "cascade_total": None,
        })
        _last_token_usage_var.set({
            "input_tokens": total_input, "output_tokens": total_output,
            "thoughts_tokens": total_thoughts, "total_tokens": total_tokens,
        })
        return questions, total_tokens
    finally:
        for chunk_path in chunk_paths:
            if chunk_path != file_path:
                safe_file_cleanup(chunk_path)


async def _generate_super_images(
    file_paths: List[str], count: int, prompt_template: str
) -> Optional[Tuple[List[Dict[str, Any]], int]]:
    """🆕 معالجة متوازية لألبومات الصور الكبيرة (أكبر من SUPER_IMAGE_BATCH_THRESHOLD -
    مصدرها حصراً رفع الويب حالياً، راجع handlers/files.py::process_web_uploaded_images)،
    بنفس منطق _generate_super_pdf أعلاه حرفياً (كاسكيد نظامي كامل عبر _execute_cascade لكل
    دفعة صور، بالتوازي مع بقية الدفعات) - فقط الدفعات هون قوائم صور بدل أجزاء PDF.

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

    base, remainder = divmod(count, len(chunks))
    question_counts = [base + (1 if index < remainder else 0) for index in range(len(chunks))]
    tasks = [
        _execute_cascade(
            _build_super_chunk_attempt_fn(chunk, prompt_template.replace("{count}", str(question_count))),
            exclude_vertex=True,
        )
        for chunk, question_count in zip(chunks, question_counts)
        if question_count > 0
    ]
    if not tasks:
        return None
    results = await asyncio.gather(*tasks)
    if any(result is None for result in results):
        log_error(logger, "Super Images: chunk exhausted the entire model/key cascade")
        return None
    questions = [question for result in results for question in result[0]]
    total_tokens = sum(result[1] for result in results)
    total_input = sum(result[2] for result in results)
    total_output = sum(result[3] for result in results)
    total_thoughts = sum(result[4] for result in results)
    winning_models = [result[5] for result in results]
    # 🆕 راجع نفس الملاحظة بـ _generate_super_pdf أعلاه حول ContextVar/asyncio.gather وchunk_details.
    chunk_details = [
        {"model": result[5], "key_index": result[6], "cascade_rank": result[7], "cascade_total": result[8]}
        for result in results
    ]
    _last_model_used_var.set({
        "provider": "gemini",
        "model": winning_models[0] if len(set(winning_models)) == 1 else "mixed",
        "key_index": None, "mode": "super_images",
        "chunk_details": chunk_details,
        # 🆕 راجع نفس الملاحظة بـ _generate_super_pdf أعلاه حول عدم وجود رقم ترتيب دقيق هون -
        # رتبة كل جزء الحقيقية موجودة جوا chunk_details.
        "cascade_rank": None, "cascade_total": None,
    })
    _last_token_usage_var.set({
        "input_tokens": total_input, "output_tokens": total_output,
        "thoughts_tokens": total_thoughts, "total_tokens": total_tokens,
    })
    return questions, total_tokens
