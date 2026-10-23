"""Quiz prompts, chunk planning and merging; transport lives in services.ai."""
from typing import Any, Dict, List, Optional, Sequence, Tuple
import asyncio

from logger import get_logger, log_error
from mem_utils import release_memory_to_os
from utils import safe_file_cleanup
from helpers.gemini_helper.state import _last_model_used_var, _last_token_usage_var
from helpers.gemini_helper.media import (
    split_images_into_three_sync, split_pdf_without_orphans,
    split_pdf_with_context_without_orphans,
)
from helpers.gemini_helper.schemas import QuizResponse
from services.ai.contracts import AIRequest, Attachment
from services.ai.runtime import execute as _execute_request

logger = get_logger(__name__)


def _build_super_chunk_attempt_fn(paths, prompt, user_context=None):
    # Historical private name retained for import compatibility; now builds data.
    parts = ((user_context,) if user_context else ()) + tuple(
        Attachment.from_path(path) for path in paths
    )
    return AIRequest(parts, QuizResponse, system_instruction=prompt)


async def _execute_cascade(request, exclude_vertex=False, allow_empty=False):
    if allow_empty:
        from dataclasses import replace
        request = replace(request, allow_empty_questions=True)
    result = await _execute_request(request, exclude_vertex=exclude_vertex)
    if not result:
        return None
    meta = _last_model_used_var.get() or {}
    questions = [question.model_dump() for question in result.parsed.questions]
    # Extracting an existing exam uses [] as an explicit, successful end-of-source
    # signal. Generated quizzes must still reject an empty model response.
    if not questions and not allow_empty:
        raise ValueError("Provider returned no questions")
    return (questions, result.total_tokens, result.input_tokens, result.output_tokens,
            result.thoughts_tokens, meta.get("model"), meta.get("key_index"),
            meta.get("cascade_rank"), meta.get("cascade_total"), meta.get("provider"))


async def _generate_regular(paths, prompt, user_context=None):
    request = (_build_super_chunk_attempt_fn(paths, prompt, user_context)
               if user_context else _build_super_chunk_attempt_fn(paths, prompt))
    result = await _execute_cascade(request)
    return (result[0], result[1]) if result else None


def _ready_exam_chunk_prompt(prompt_template: str, question_limit: int, chunk: Dict[str, Any]) -> str:
    """Treat overlap as context, never as independent extraction ownership."""
    local_first = chunk["owned_first"]
    local_last = chunk["owned_last"]
    total_pages = chunk["page_count"]
    instruction = (
        "\n\n[تعليمات إلزامية لجزء من اختبار جاهز - حدود صفحات السياق]:\n"
        f"ملف PDF المرفق في هذا الطلب يتألف من {total_pages} صفحات. "
        "رقم الصفحة هنا هو ترتيب الصفحة داخل ملف PDF الجزئي، وليس الرقم المطبوع على الورقة. "
        f"استخرج حصراً الأسئلة التي يبدأ نصّها في الصفحات من {local_first} إلى {local_last} (شاملاً). "
        "حدد تبعية كل سؤال حسب الصفحة التي يبدأ فيها، لا حسب الصفحة التي ينتهي فيها. "
        "الصفحات خارج هذا النطاق صفحات سياق فقط: استخدمها لاستكمال قراءة السؤال "
        "أو الخيارات أو مفتاح الإجابة، لكن لا تُخرج سؤالاً يبدأ على صفحة سياقية، "
        "حتى لو استمر ذلك السؤال داخل الصفحات المخصصة لك. "
        "وبالمقابل، إذا بدأ السؤال ضمن الصفحات المخصصة لك وامتد إلى صفحة سياقية، "
        "استخرجه كاملاً بكل خياراته. "
        "لا تكرر أسئلة السياق، ولا تختلق أسئلة لتعويضها، ولا تعتبر صفحة السياق سؤالاً إضافياً. "
        "استخرج جميع الأسئلة التي تبدأ في نطاقك حتى السقف المحدد في التعليمات الأساسية."
    )
    return prompt_template.replace("{count}", str(question_limit)) + instruction


def _generated_pdf_chunk_prompt(
    prompt_template: str, question_target: int, chunk: Dict[str, Any],
    difficulty_segment: Optional[str] = None,
) -> str:
    """Only owned pages supply question topics; adjacent pages are context."""
    first = chunk["owned_first"]
    last = chunk["owned_last"]
    total = chunk["page_count"]
    instruction = (
        "\n\n[تعليمات إلزامية لتوليد أسئلة من جزء PDF - حدود صفحات السياق]:\n"
        f"الملف الجزئي المرفق يتكوّن من {total} صفحات، وترقيمها هو ترتيب الصفحات "
        "داخل هذا الملف الجزئي وليس الترقيم المطبوع. "
        f"ولّد الأسئلة المطلوبة اعتماداً على المحتوى الذي يبدأ في الصفحات من {first} إلى {last} فقط (شاملاً). "
        "الصفحات السابقة واللاحقة خارج هذا المجال صفحات سياق للقراءة والفهم "
        "واستكمال تعريف أو معادلة أو مثال يمتد عبر الحد الفاصل، وليست مصادر "
        "مستقلة لأفكار أسئلة جديدة. "
        "إذا بدأت فكرة أو مسألة في صفحة سياق ثم امتدت إلى صفحة مخصصة لك، "
        "لا تولّد سؤالاً يستند إليها بصفتها فكرة مستقلة. "
        "إذا بدأت فكرة في صفحة مخصصة لك وامتدت لصفحة سياق، "
        "استخدم تكملتها لفهمها وصياغة سؤال دقيق. "
        "لا تكرر أسئلة مقتبسة حصرياً من صفحات السياق، "
        "ولا تختلق معلومات لتعويض نقص المحتوى. "
        f"العدد المستهدف من الأسئلة لهذا الجزء هو {question_target} سؤالاً، "
        "من الصفحات المخصصة لهذا الجزء وحده. التزم بنوع الأسئلة والصعوبة "
        "وشروط المخرجات في البرومبت الأساسي."
    )
    return (
        prompt_template.replace("{count}", str(question_target))
        + instruction
        + (
            "\n\n[تدرّج الصعوبة على مستوى الاختبار الكامل]: " + difficulty_segment
            if difficulty_segment else ""
        )
    )


def _generated_pdf_chunk_targets(count: int, chunks: Sequence[Dict[str, Any]]) -> List[int]:
    """Apportion requested questions to chunks by owned pages, not context pages.

    Largest remainder keeps the exact total while avoiding giving an entire
    large request to the first chunk. For an extracted ready-made exam we do
    NOT use quotas: its question density may differ across pages.
    """
    owned = [chunk["owned_last"] - chunk["owned_first"] + 1 for chunk in chunks]
    total_pages = sum(owned)
    if total_pages <= 0 or count <= 0:
        return [0] * len(chunks)
    targets = [(count * pages) // total_pages for pages in owned]
    spare = count - sum(targets)
    indices = sorted(
        range(len(chunks)),
        key=lambda i: (-(count * owned[i] % total_pages), i),
    )
    for index in indices[:spare]:
        targets[index] += 1
    return targets


def _merge_pdf_chunk_questions(
    results: Sequence[Any],
    question_limit: int,
    chunk_targets: Optional[Sequence[int]] = None,
) -> List[Dict[str, Any]]:
    """Merge in PDF order and suppress identical cross-boundary questions."""
    questions: List[Dict[str, Any]] = []
    prior_chunk_questions: set = set()
    for index, result in enumerate(results):
        current_chunk_questions: set = set()
        source_questions = result[0]
        # Generated quizzes have per-chunk quotas; existing-exam extraction
        # deliberately does not, because source questions may be uneven.
        if chunk_targets is not None:
            source_questions = source_questions[:chunk_targets[index]]
        for question in source_questions:
            question_text = " ".join(str(question.get("question", "")).split()).casefold()
            if question_text and question_text in prior_chunk_questions:
                continue
            questions.append(question)
            if question_text:
                current_chunk_questions.add(question_text)
            if len(questions) >= question_limit:
                return questions
        prior_chunk_questions = current_chunk_questions
    return questions


async def _generate_pdf_with_context(
    file_path: str, count: int, prompt_template: str, part_count: int,
    ready_exam: bool = False, progressive: bool = False,
    user_context: Optional[str] = None,
) -> Optional[Tuple[List[Dict[str, Any]], int]]:
    """Context-overlap PDF generation for all subject types.

    Extended PDFs use two parallel calls; Super-PDFs use three. Extraction of
    a ready-made exam attributes original questions to their starting page.
    Generated tests assign questions to owned source pages, with adjacent
    context pages available only for completing a topic at the boundary.
    """
    chunks = await split_pdf_with_context_without_orphans(file_path, part_count)
    try:
        await asyncio.to_thread(release_memory_to_os)
        if len(chunks) != part_count:
            return await _generate_regular([file_path], prompt_template.replace("{count}", str(count)),
                                           user_context=user_context)

        # Keep per-chunk count a generous UPPER bound for extracting existing
        # questions; density varies. For generating questions, quotas are
        # proportional to owned pages and always sum to the requested count.
        chunk_targets = (
            [count] * len(chunks) if ready_exam
            else _generated_pdf_chunk_targets(count, chunks)
        )
        active = [
            (chunk, target)
            for chunk, target in zip(chunks, chunk_targets)
            if target > 0
        ]
        if not active:
            return None
        # Progressive difficulty is global across the merged quiz, not
        # re-started from easy at each independently prompted PDF chunk.
        progressive_segments = (
            (
                "هذه المرحلة الأولى من الاختبار: ابدأ بالسهل وتدرّج نحو المتوسط، "
                "ولا تصل إلى مستوى الأسئلة الأصعب في هذا الجزء.",
                "هذه المرحلة الأخيرة من الاختبار: ابدأ من المتوسط، ثم تدرّج "
                "حتى الأسئلة المتقدمة والأصعب في النهاية.",
            ) if part_count == 2 else (
                "المرحلة الأولى: ركّز على الأسئلة السهلة مع تصاعد طفيف فقط.",
                "المرحلة الوسطى: ركّز على الأسئلة المتوسطة مع زيادة تدريجية.",
                "المرحلة الأخيرة: ركّز على الأسئلة المتقدمة والأصعب تدريجياً.",
            )
        ) if progressive and not ready_exam else None
        def make_request(paths, instruction):
            return (_build_super_chunk_attempt_fn(paths, instruction, user_context)
                    if user_context else _build_super_chunk_attempt_fn(paths, instruction))

        attempts = [
            _execute_cascade(
                make_request(
                    [chunk["path"]],
                    (
                        _ready_exam_chunk_prompt(prompt_template, target, chunk)
                        if ready_exam else
                        _generated_pdf_chunk_prompt(
                            prompt_template, target, chunk,
                            progressive_segments[index] if progressive_segments else None,
                        )
                    ),
                ),
                # Retain the current three-part Super-PDF provider policy.
                exclude_vertex=(part_count == 3),
            )
            for index, (chunk, target) in enumerate(active)
        ]
        results = await asyncio.gather(*attempts, return_exceptions=True)
        if any(result is None or isinstance(result, BaseException) for result in results):
            log_error(logger, "PDF: context chunk exhausted its model/key cascade")
            return None

        questions = _merge_pdf_chunk_questions(
            results, count,
            None if ready_exam else [target for _, target in active],
        )
        if not questions:
            return None
        if not ready_exam and len(questions) < count:
            logger.warning(
                "PDF context generation returned %s of %s requested questions",
                len(questions), count,
            )
        winning_models = [result[5] for result in results]
        _last_model_used_var.set({
            "provider": results[0][9] if len({result[9] for result in results}) == 1 else "mixed",
            "model": winning_models[0] if len(set(winning_models)) == 1 else "mixed",
            "key_index": None,
            "mode": "super_pdf" if part_count == 3 else "over_limit_pdf",
            "chunk_details": [
                {
                    "provider": result[9], "model": result[5],
                    "key_index": result[6],
                    "cascade_rank": result[7],
                    "cascade_total": result[8],
                }
                for result in results
            ],
            "cascade_rank": None,
            "cascade_total": None,
        })
        _last_token_usage_var.set({
            "input_tokens": sum(result[2] for result in results),
            "output_tokens": sum(result[3] for result in results),
            "thoughts_tokens": sum(result[4] for result in results),
            "total_tokens": sum(result[1] for result in results),
        })
        return questions, sum(result[1] for result in results)
    finally:
        for chunk in chunks:
            safe_file_cleanup(chunk["path"])


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
            {"provider": result[9], "model": result[5], "key_index": result[6], "cascade_rank": result[7], "cascade_total": result[8]}
            for result in results
        ]
        # 🆕 كل جزء نُفِّذ بمهمة (Task) منفصلة عبر asyncio.gather - ContextVar لا يتسرّب
        # تلقائياً من مهمة فرعية لمهمة أصلية، لذا نسجّل الموديل/المفتاح الفائزين والتوكنز
        # المجمّعة صراحة هنا من القيم المُعادة من كل جزء (راجع _build_super_chunk_attempt_fn).
        # حقل "model": موديل الجزء الأول لو كل الأجزاء استخدمت نفس الموديل (الحالة الشائعة)،
        # أو "mixed" صراحة لو اختلفت (كاسكيد فعلي صار فيه انتقال لموديل/مفتاح آخر لبعض
        # الأجزاء) - chunk_details يحفظ التفصيل الكامل (موديل + مفتاح + رتبة) لكل جزء بأي حال.
        _last_model_used_var.set({
            "provider": results[0][9] if len({result[9] for result in results}) == 1 else "mixed",
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
    file_paths: List[str], count: int, prompt_template: str, progressive: bool = False,
    user_context: Optional[str] = None
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
    chunks = [chunk for chunk in await asyncio.to_thread(split_images_into_three_sync, file_paths) if chunk]
    if len(chunks) < 2:
        return await _generate_regular(file_paths, prompt_template.replace("{count}", str(count)),
                                       user_context=user_context)

    base, remainder = divmod(count, len(chunks))
    question_counts = [base + (1 if index < remainder else 0) for index in range(len(chunks))]
    active = [(chunk, amount) for chunk, amount in zip(chunks, question_counts) if amount > 0]
    segments = (
        ("هذه الدفعة الأولى: أسئلة سهلة تتصاعد قليلاً.",
         "هذه الدفعة الوسطى: أسئلة متوسطة متصاعدة.",
         "هذه الدفعة الأخيرة: أسئلة متقدمة تتصاعد نحو الأصعب.")
        if len(active) == 3 else
        ("الدفعة الأولى: أسئلة سهلة إلى متوسطة.",
         "الدفعة الأخيرة: أسئلة متوسطة إلى متقدمة.")
    ) if progressive else ()
    def make_request(paths, instruction):
        return (_build_super_chunk_attempt_fn(paths, instruction, user_context)
                if user_context else _build_super_chunk_attempt_fn(paths, instruction))

    tasks = [
        _execute_cascade(
            make_request(
                chunk, prompt_template.replace("{count}", str(question_count)) +
                ("\n[موقع هذه الدفعة ضمن التدرج العام]: " + segments[index]
                 if progressive else "")
            ),
            exclude_vertex=True,
        )
        for index, (chunk, question_count) in enumerate(active)
    ]
    if not tasks:
        return None
    results = await asyncio.gather(*tasks, return_exceptions=True)
    if any(result is None or isinstance(result, BaseException) for result in results):
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
        {"provider": result[9], "model": result[5], "key_index": result[6], "cascade_rank": result[7], "cascade_total": result[8]}
        for result in results
    ]
    _last_model_used_var.set({
        "provider": results[0][9] if len({result[9] for result in results}) == 1 else "mixed",
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
