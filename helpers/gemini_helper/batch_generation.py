"""Shared resumable generation batches for expanded/super text, images and PDF."""

import asyncio
import hashlib
import json
import math
from typing import Any, Dict, List, Optional

from logger import get_logger
from mem_utils import release_memory_to_os
from utils import safe_file_cleanup
from helpers.gemini_helper.generation import (
    _execute_cascade, _generated_pdf_chunk_prompt, _generated_pdf_chunk_targets,
    _ready_exam_chunk_prompt,
)
from helpers.gemini_helper.media import split_pdf_with_context_without_orphans, split_images_into_three_sync
from helpers.gemini_helper.schemas import QuizResponse
from helpers.gemini_helper.state import _last_model_used_var, _last_token_usage_var
from services.ai.contracts import AIRequest, Attachment
from services.ai.file_session import GeminiFileSession

logger = get_logger(__name__)
MAX_BATCH_QUESTIONS = 20
CHECKPOINT_TTL_SECONDS = 7200


def _question_key(question: Dict[str, Any]) -> str:
    return " ".join(str(question.get("question") or "").split()).casefold()


def _valid_question(question: Dict[str, Any]) -> bool:
    options = question.get("options")
    answer = question.get("correct_option_id")
    return bool(_question_key(question)) and isinstance(options, list) and len(options) == 4 and (
        type(answer) is int and 0 <= answer < 4
    )


def _valid_extracted_question(question: Dict[str, Any]) -> bool:
    """Keep 4+ original options; the established post-processing handles five choices."""
    options = question.get("options")
    answer = question.get("correct_option_id")
    return (bool(_question_key(question)) and isinstance(options, list) and
            len(options) >= 4 and all(isinstance(o, str) and o.strip() for o in options) and
            type(answer) is int and 0 <= answer < len(options))


def _batch_prompt(base_prompt: str, chunk: Dict[str, Any], target: int,
                  previous: List[Dict[str, Any]], progressive: bool, completed: int, total: int) -> str:
    progress = completed / max(total, 1)
    level = (
        "ابدأ بمستوى سهل ثم اصعد تدريجياً نحو المتوسط." if progress < 0.34 else
        "ولّد أسئلة متوسطة الصعوبة مع تصاعد تدريجي." if progress < 0.67 else
        "ولّد أسئلة أصعب وأكثر تقدماً من الدفعات السابقة."
    ) if progressive else None
    prompt = _generated_pdf_chunk_prompt(base_prompt, target, chunk, level)
    if previous:
        # Bounded context: prevent unbounded prompts for 120-question requests.
        samples = ["- " + str(q["question"])[:200] for q in previous[-65:]]
        prompt += ("\n\n[أسئلة وُلّدت سابقاً لنفس الاختبار، ممنوع تكرارها أو إعادة صياغتها]:\n" +
                   "\n".join(samples))[:14000]
    return prompt


class _Checkpoint:
    def __init__(self, user_id, file_hash, prompt, context, count, part_count, progressive, source_kind="pdf"):
        self.key = None
        if user_id is not None and file_hash:
            identity = json.dumps(
                [str(user_id), str(file_hash), prompt, context, count, part_count, progressive,
                 MAX_BATCH_QUESTIONS, source_kind, "unified-batches-v2"],
                ensure_ascii=False,
            )
            self.key = "quiz:question-batches:v2:" + hashlib.sha256(identity.encode()).hexdigest()

    async def load(self, num_parts, extraction=False):
        if not self.key:
            return None
        try:
            from config import redis_client
            value = await redis_client.get(self.key)
            if not value:
                return None
            data = json.loads(value)
            parts = data.get("parts")
            if (not isinstance(parts, list) or len(parts) != num_parts or
                    not all(isinstance(part, list) and all(
                        isinstance(q, dict) and (_valid_extracted_question(q) if extraction else _valid_question(q)) for q in part
                    ) for part in parts)):
                return None
            if extraction:
                completed = data.get("completed_parts")
                if (not isinstance(completed, list) or len(completed) != num_parts or
                        not all(type(value) is bool for value in completed) or
                        not all(_valid_extracted_question(q) for part in parts for q in part)):
                    return None
            return data
        except Exception:
            logger.warning("QUIZ_BATCH_CHECKPOINT_READ_FAILED")
            return None

    async def save(self, parts, usage, details, completed_parts=None):
        if not self.key:
            return
        try:
            from config import redis_client
            await redis_client.setex(
                self.key, CHECKPOINT_TTL_SECONDS,
                json.dumps({"parts": parts, "usage": usage, "details": details,
                            **({"completed_parts": completed_parts} if completed_parts is not None else {})},
                           ensure_ascii=False, separators=(",", ":")),
            )
        except Exception:
            logger.warning("QUIZ_BATCH_CHECKPOINT_WRITE_FAILED")

    async def clear(self):
        if not self.key:
            return
        try:
            from config import redis_client
            await redis_client.delete(self.key)
        except Exception:
            logger.warning("QUIZ_BATCH_CHECKPOINT_CLEAR_FAILED")



def _plain_batch_prompt(base_prompt: str, target: int, previous: List[Dict[str, Any]],
                        progressive: bool, completed: int, total: int, source_kind: str) -> str:
    """For text/images, never replace the source with a truncated summary."""
    prompt = base_prompt.replace("{count}", str(target))
    prompt += ("\n\n[تعليمات الدفعة الحالية]: "
               f"أنشئ {target} سؤالاً جديداً فقط من المصادر المرفقة دون تكرار أسئلة الدفعات السابقة. "
               "لا تختلق معلومات ليست في المحتوى.")
    if progressive:
        ratio = completed / max(total, 1)
        stage = (
            "هذه بداية الاختبار: الأسئلة سهلة ومتدرجة نحو المتوسط." if ratio < 0.34 else
            "هذه وسط الاختبار: الأسئلة متوسطة ومتدرجة." if ratio < 0.67 else
            "هذه نهاية الاختبار: الأسئلة متقدمة ومتدرجة إلى الأصعب."
        )
        prompt += "\n" + stage
    if previous:
        samples = ["- " + str(q["question"])[:200] for q in previous[-65:]]
        prompt += "\n\n[أسئلة سبق إنشاؤها - لا تكررها أو تعيد صياغتها]:\n" + "\n".join(samples)[:14000]
    return prompt


async def _run_batch_chunks(chunks: List[Dict[str, Any]], count: int, prompt_template: str,
                            progressive: bool, user_context: Optional[str],
                            user_id, source_hash, source_kind: str, mode: str,
                            exclude_vertex: bool = False):
    """One orchestrator: exact total, cross-batch de-dup, durable partial work.

    Each chunk contains paths OR text plus its proportional weight.
    Only PDFs use ownership-aware prompts; all other modalities read native
    files/text directly without OCR or an extract-text prepass.
    """
    if not chunks or count < 1:
        return None
    shapes = [{"owned_first": 1, "owned_last": part["weight"]} for part in chunks]
    targets = _generated_pdf_chunk_targets(count, shapes)
    checkpoint = _Checkpoint(user_id, source_hash, prompt_template, user_context,
                             count, len(chunks), progressive, source_kind=source_kind)
    saved = await checkpoint.load(len(chunks))
    parts = saved["parts"] if saved else [[] for _ in chunks]
    usage = saved.get("usage", {}) if saved else {}
    usage = {k: int(usage.get(k, 0) or 0) for k in
             ("input_tokens", "output_tokens", "thoughts_tokens", "total_tokens")}
    details = saved.get("details", []) if saved else []
    if (not isinstance(details, list) or
            any(not isinstance(item, dict) for item in details) or
            any(len(part) > target for part, target in zip(parts, targets))):
        parts, details = [[] for _ in chunks], []
        usage = {k: 0 for k in usage}
    existing = {_question_key(q) for part in parts for q in part}
    file_session = GeminiFileSession()
    try:
        for part_index, (chunk, target) in enumerate(zip(chunks, targets)):
            no_progress = 0
            attempts = 0
            max_attempts = math.ceil(target / MAX_BATCH_QUESTIONS) * 4 + 2
            while len(parts[part_index]) < target:
                if attempts >= max_attempts or no_progress >= 2:
                    logger.warning("QUIZ_BATCH_FAILED modality=%s part=%d reason=insufficient_unique_questions",
                                   source_kind, part_index + 1)
                    return None
                attempts += 1
                missing = target - len(parts[part_index])
                batch_size = min(MAX_BATCH_QUESTIONS, missing)
                previous = [q for part in parts for q in part]
                if chunk.get("pdf_chunk"):
                    instruction = _batch_prompt(
                        prompt_template, chunk["pdf_chunk"], batch_size, previous,
                        progressive, len(previous), count,
                    )
                else:
                    instruction = _plain_batch_prompt(
                        prompt_template, batch_size, previous, progressive,
                        len(previous), count, source_kind,
                    )
                source = ((chunk["text"],) if "text" in chunk else tuple(
                    Attachment.from_path(path) for path in chunk["paths"]
                ))
                contents = ((user_context,) if user_context else ()) + source
                request = AIRequest(
                    contents, QuizResponse, system_instruction=instruction,
                    file_session=file_session,
                )
                try:
                    result = await _execute_cascade(request, exclude_vertex=exclude_vertex)
                except Exception:
                    logger.warning("QUIZ_BATCH_FAILED modality=%s part=%d reason=unexpected_cascade_error",
                                   source_kind, part_index + 1)
                    return None
                if result is None:
                    logger.warning("QUIZ_BATCH_FAILED modality=%s part=%d reason=cascade_exhausted",
                                   source_kind, part_index + 1)
                    return None
                for name, value in zip(
                    ("total_tokens", "input_tokens", "output_tokens", "thoughts_tokens"),
                    (result[1], result[2], result[3], result[4]),
                ):
                    usage[name] += value
                details.append({
                    "provider": result[9], "model": result[5], "key_index": result[6],
                    "cascade_rank": result[7], "cascade_total": result[8],
                    "part": part_index + 1,
                })
                added = 0
                for question in result[0]:
                    key = _question_key(question)
                    if not _valid_question(question) or key in existing:
                        continue
                    parts[part_index].append(question)
                    existing.add(key)
                    added += 1
                    if len(parts[part_index]) == target:
                        break
                no_progress = 0 if added else no_progress + 1
                await checkpoint.save(parts, usage, details)

        questions = [q for part in parts for q in part]
        if len(questions) != count:
            return None
        await checkpoint.clear()
        providers = {d.get("provider") for d in details}
        models = {d.get("model") for d in details}
        _last_model_used_var.set({
            "provider": next(iter(providers)) if len(providers) == 1 else "mixed",
            "model": next(iter(models)) if len(models) == 1 else "mixed",
            "key_index": None,
            "mode": mode,
            "chunk_details": details,
            "cascade_rank": None, "cascade_total": None,
        })
        _last_token_usage_var.set(usage)
        return questions, usage["total_tokens"]
    finally:
        await file_session.aclose()



def _extraction_prompt(base_prompt: str, target: int, chunk: Dict[str, Any],
                       last_question: Optional[str]) -> str:
    """Read next original questions in source order, never fill missing content."""
    if chunk.get("pdf_chunk"):
        prompt = _ready_exam_chunk_prompt(base_prompt, target, chunk["pdf_chunk"])
    else:
        prompt = base_prompt.replace("{count}", str(target))
    prompt += (
        "\n\n[دفعة استخراج من اختبار جاهز - تعليمات إلزامية]: "
        f"استخرج بحد أقصى {target} سؤالاً أصلياً موجوداً فعلياً في المصدر وبنفس ترتيب ظهورها، "
        "ولا تنشئ أسئلة جديدة أو تعوّض عدد الأسئلة الناقص. "
        "لا تتخطّ أي سؤال أصلي بين الدفعات. "
        "إذا انتهت الأسئلة المتاحة فأعد فقط ما تبقى، وإن لم يبقَ أي سؤال فأعد JSON صالحاً "
        "بالحقل questions كمصفوفة فارغة []. "
        "لا تجعل حد الدفعة مبرراً لاختلاق سؤال، والتزم بوضع الإجابة الأصلي أو حل AI في البرومبت الأساسي."
    )
    if last_question:
        prompt += (
            "\n\n[نقطة استكمال الاستخراج - لا تُعِد أسئلة سابقة]: "
            "ابدأ مباشرةً بالسؤال الأصلي الذي يظهر بعد السؤال التالي ضمن نفس نطاق المصدر، "
            "مهما كان رقمه المطبوع؛ إذا لم يوجد سؤال بعده فأعد questions=[]:\n"
            f"{last_question[:750]}"
        )
    else:
        prompt += "\nابدأ بأول سؤال أصلي يبدأ ضمن نطاق هذا الجزء، وليس في صفحة سياق مجاورة."
    return prompt


async def _run_extraction_chunks(chunks: List[Dict[str, Any]], count: int,
                                 prompt_template: str, user_context: Optional[str],
                                 user_id, source_hash, source_kind: str, mode: str,
                                 exclude_vertex: bool = False):
    """Shared bounded extraction: ordered continuation, never target-filling.

    A completed chunk may have fewer than its requested 20 questions. A
    provider error is different from a successful end-of-document ([]).
    """
    if not chunks or count <= 0:
        return None
    checkpoint = _Checkpoint(user_id, source_hash, prompt_template, user_context,
                             count, len(chunks), False, source_kind="extract:" + source_kind)
    saved = await checkpoint.load(len(chunks), extraction=True)
    parts = saved["parts"] if saved else [[] for _ in chunks]
    completed = saved["completed_parts"] if saved else [False] * len(chunks)
    usage = saved.get("usage", {}) if saved else {}
    usage = {k: int(usage.get(k, 0) or 0) for k in
             ("input_tokens", "output_tokens", "thoughts_tokens", "total_tokens")}
    details = saved.get("details", []) if saved else []
    if (not isinstance(details, list) or
            not all(isinstance(item, dict) for item in details) or
            sum(map(len, parts)) > count):
        parts, completed, details = [[] for _ in chunks], [False] * len(chunks), []
        usage = {k: 0 for k in usage}
    seen = {_question_key(q) for part in parts for q in part}
    session = GeminiFileSession()
    try:
        for index, chunk in enumerate(chunks):
            if completed[index]:
                continue
            # Never re-extract a finished prefix after resuming a failed request.
            repeated = 0
            attempts = 0
            max_attempts = math.ceil(count / MAX_BATCH_QUESTIONS) + 3
            while sum(map(len, parts)) < count:
                if attempts >= max_attempts or repeated >= 2:
                    logger.warning("QUIZ_EXTRACTION_FAILED source=%s part=%d reason=no_forward_progress",
                                   source_kind, index + 1)
                    return None
                attempts += 1
                remaining = count - sum(map(len, parts))
                target = min(remaining, MAX_BATCH_QUESTIONS)
                anchor = str(parts[index][-1]["question"]) if parts[index] else None
                instruction = _extraction_prompt(prompt_template, target, chunk, anchor)
                source = ((chunk["text"],) if "text" in chunk else tuple(
                    Attachment.from_path(path) for path in chunk["paths"]
                ))
                request = AIRequest(
                    ((user_context,) if user_context else ()) + source,
                    QuizResponse, system_instruction=instruction, file_session=session,
                )
                try:
                    result = await _execute_cascade(
                        request, exclude_vertex=exclude_vertex, allow_empty=True,
                    )
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.warning("QUIZ_EXTRACTION_FAILED source=%s part=%d reason=unexpected_cascade_error",
                                   source_kind, index + 1)
                    return None
                if result is None:
                    logger.warning("QUIZ_EXTRACTION_FAILED source=%s part=%d reason=cascade_exhausted",
                                   source_kind, index + 1)
                    return None
                for name, value in zip(
                    ("total_tokens", "input_tokens", "output_tokens", "thoughts_tokens"),
                    (result[1], result[2], result[3], result[4]),
                ):
                    usage[name] += value
                details.append({
                    "provider": result[9], "model": result[5], "key_index": result[6],
                    "cascade_rank": result[7], "cascade_total": result[8],
                    "part": index + 1,
                })
                extracted = result[0]
                # Malformed options are not evidence that the document ended.
                if any(not _valid_extracted_question(q) for q in extracted):
                    logger.warning("QUIZ_EXTRACTION_FAILED source=%s part=%d reason=invalid_question",
                                   source_kind, index + 1)
                    return None
                new_questions = []
                for question in extracted:
                    key = _question_key(question)
                    if key not in seen:
                        parts[index].append(question)
                        seen.add(key)
                        new_questions.append(question)
                        if sum(map(len, parts)) >= count:
                            break
                if new_questions:
                    repeated = 0
                elif len(extracted) >= target:
                    # Model ignored continuation anchor. Never falsely claim
                    # extraction is complete or discard prior work.
                    repeated += 1
                # A short *valid* response signals end of this owned source.
                # Only return a partial exam after all chunks have been visited.
                if len(extracted) < target:
                    completed[index] = True
                await checkpoint.save(parts, usage, details, completed_parts=completed)
                if completed[index] or sum(map(len, parts)) >= count:
                    break
            if sum(map(len, parts)) >= count:
                break
        questions = [q for part in parts for q in part][:count]
        if not questions:
            return None
        await checkpoint.clear()
        providers = {entry.get("provider") for entry in details}
        models = {entry.get("model") for entry in details}
        _last_model_used_var.set({
            "provider": next(iter(providers)) if len(providers) == 1 else "mixed",
            "model": next(iter(models)) if len(models) == 1 else "mixed",
            "key_index": None,
            "mode": mode,
            "chunk_details": details,
            "cascade_rank": None, "cascade_total": None,
        })
        _last_token_usage_var.set(usage)
        if len(questions) < count:
            logger.info("QUIZ_EXTRACTION_SOURCE_EXHAUSTED source=%s extracted=%d requested_upper_bound=%d",
                        source_kind, len(questions), count)
        return questions, usage["total_tokens"]
    finally:
        await session.aclose()


def _ordered_image_groups(paths: List[str]) -> List[List[str]]:
    """Contiguous ranges preserve original exam question/image sequence."""
    number = min(3, len(paths))
    return [paths[(len(paths) * i) // number:(len(paths) * (i + 1)) // number]
            for i in range(number)]


async def generate_pdf_in_batches(
    file_path: str, page_count: int, count: int, prompt_template: str,
    part_count: int, progressive: bool = False, user_context: Optional[str] = None,
    user_id=None, file_hash=None, extraction: bool = False,
):
    """Full short PDFs; overlapping page ownership for longer PDFs."""
    if part_count == 1:
        pdf_parts = [{"path": file_path, "owned_first": 1, "owned_last": page_count,
                      "page_count": page_count}]
    else:
        pdf_parts = await split_pdf_with_context_without_orphans(file_path, part_count)
        await asyncio.to_thread(release_memory_to_os)
    try:
        if len(pdf_parts) != part_count or not pdf_parts:
            return None
        chunks = [{
            "paths": [part["path"]],
            "pdf_chunk": part,
            "weight": part["owned_last"] - part["owned_first"] + 1,
        } for part in pdf_parts]
        if extraction:
            return await _run_extraction_chunks(
                chunks, count, prompt_template, user_context,
                user_id, file_hash, "pdf",
                "batched_exam_pdf",
                exclude_vertex=(part_count == 3),
            )
        return await _run_batch_chunks(
            chunks, count, prompt_template, progressive, user_context,
            user_id, file_hash, "pdf",
            "batched_super_pdf" if part_count == 3 else "batched_over_limit_pdf",
            exclude_vertex=(part_count == 3),
        )
    finally:
        if part_count != 1:
            for part in pdf_parts:
                if part["path"] != file_path:
                    safe_file_cleanup(part["path"])


async def generate_images_in_batches(
    paths: List[str], count: int, prompt_template: str, progressive: bool = False,
    user_context: Optional[str] = None, user_id=None, file_hash=None,
    extraction: bool = False,
):
    """One image/short albums remain intact; super albums use three image groups."""
    if extraction:
        groups = (_ordered_image_groups(paths) if len(paths) > 10 else [paths])
        chunks = [{"paths": group, "weight": len(group)} for group in groups]
        return await _run_extraction_chunks(
            chunks, count, prompt_template, user_context,
            user_id, file_hash, "images", "batched_exam_images",
            exclude_vertex=(len(paths) > 10),
        )
    groups = ([group for group in await asyncio.to_thread(split_images_into_three_sync, paths)
               if group] if len(paths) > 10 else [paths])
    chunks = [{"paths": group, "weight": len(group)} for group in groups]
    return await _run_batch_chunks(
        chunks, count, prompt_template, progressive, user_context,
        user_id, file_hash, "images",
        "batched_super_images" if len(paths) > 10 else "batched_over_limit_images",
        exclude_vertex=(len(paths) > 10),
    )


async def generate_text_in_batches(
    text: str, count: int, prompt_template: str, progressive: bool = False,
    user_context: Optional[str] = None, user_id=None, file_hash=None,
    extraction: bool = False,
):
    """A long text/Office/audio transcript is not sliced between questions."""
    identity = hashlib.sha256(text.encode("utf-8")).hexdigest()
    source_hash = hashlib.sha256(((file_hash or "") + ":" + identity).encode()).hexdigest()
    if extraction:
        return await _run_extraction_chunks(
            [{"text": text, "weight": 1}], count, prompt_template,
            user_context, user_id, source_hash, "text", "batched_exam_text",
        )
    return await _run_batch_chunks(
        [{"text": text, "weight": 1}], count, prompt_template,
        progressive, user_context, user_id, source_hash, "text",
        "batched_over_limit_text",
    )


def _extraction_batch_prompt(base_prompt: str, chunk: Dict[str, Any], limit: int,
                             previous: List[Dict[str, Any]]) -> str:
    """An extraction cursor must preserve original order and never invent questions."""
    if chunk.get("pdf_chunk"):
        prompt = _ready_exam_chunk_prompt(base_prompt, limit, chunk["pdf_chunk"])
    else:
        prompt = base_prompt.replace("{count}", str(limit))
    if chunk.get("image_ownership"):
        first, last, total = chunk["image_ownership"]
        prompt += (
            "\n\n[ملكية أسئلة الألبوم]: الصور مرفقة بترتيب المصدر. "
            f"استخرج فقط الأسئلة التي تبدأ في الصور من {first} إلى {last} "
            f"من أصل {total} صور مرفقة بهذا الطلب. "
            "الصور خارج هذا المجال هي صور سياق فقط؛ لا تكرر سؤالاً يبدأ فيها. "
            "السؤال الذي يمتد عبر الصور يتبع الصورة التي يبدأ بها."
        )
    prompt += (
        "\n\n[استكمال استخراج الاختبار الجاهز]: "
        f"أخرج حتى {limit} سؤالاً أصلياً تَالياً بالترتيب، وليس أسئلة مولدة. "
        "استخرج النص والخيارات الأصلية، والتزم باختيار المستخدم لنقل الإجابات "
        "أو حلها بالذكاء الاصطناعي. لا تعِد سؤالاً استخرجته في دفعة سابقة. "
        "لا تعوض نقص عدد الأسئلة بسؤال مؤلف. "
        "إذا انتهت جميع الأسئلة الأصلية ضمن الصفحات أو الصور المخصصة، "
        "أرجع JSON يحتوي questions: [] بصورة صريحة. "
        "إذا تبقى عدد قليل، أعد الموجود فقط دون اختلاق."
    )
    if previous:
        titles = ["- " + str(q["question"])[:220] for q in previous[-50:]]
        prompt += (
            "\n\n[الأسئلة المستخرجة سابقاً من هذا الجزء بترتيبها، "
            "ابدأ بعد آخر سؤال منها ولا تكررها]:\n" + "\n".join(titles)
        )[:14000]
    else:
        prompt += "\n[هذه أول دفعة في هذا الجزء، ابدأ من أول سؤال تابع له.]"
    return prompt


async def _extract_exam_chunks(chunks: List[Dict[str, Any]], count: int,
                               prompt_template: str, user_context: Optional[str],
                               user_id, source_hash, kind: str, mode: str,
                               exclude_vertex: bool = False):
    """No per-chunk quota: density of questions in an existing exam is uneven.

    An empty successful structured output signals end-of-source. Any cascade
    error remains a failure and preserves the checkpoint, never a fake EOF.
    """
    if not chunks or count < 1:
        return None
    checkpoint = _Checkpoint(user_id, source_hash, prompt_template, user_context,
                             count, len(chunks), False, source_kind=kind + ":exam")
    saved = await checkpoint.load(len(chunks), extraction=True)
    parts = saved["parts"] if saved else [[] for _ in chunks]
    completed = saved["completed_parts"] if saved else [False] * len(chunks)
    usage = saved.get("usage", {}) if saved else {}
    usage = {key: int(usage.get(key, 0) or 0) for key in
             ("input_tokens", "output_tokens", "thoughts_tokens", "total_tokens")}
    details = saved.get("details", []) if saved else []
    if (not isinstance(details, list) or
            any(not isinstance(v, dict) for v in details) or
            sum(map(len, parts)) > count):
        parts, completed, details = [[] for _ in chunks], [False] * len(chunks), []
        usage = {key: 0 for key in usage}
    seen = {_question_key(question) for part in parts for question in part}
    session = GeminiFileSession()
    try:
        for index, chunk in enumerate(chunks):
            repeats = 0
            tries = 0
            max_tries = math.ceil(count / MAX_BATCH_QUESTIONS) * 4 + 4
            while not completed[index] and sum(map(len, parts)) < count:
                if repeats >= 2 or tries >= max_tries:
                    logger.warning("EXAM_BATCH_FAILED source=%s part=%d reason=no_progress",
                                   kind, index + 1)
                    return None
                tries += 1
                limit = min(MAX_BATCH_QUESTIONS, count - sum(map(len, parts)))
                prompt = _extraction_batch_prompt(prompt_template, chunk, limit, parts[index])
                source = ((chunk["text"],) if "text" in chunk else tuple(
                    Attachment.from_path(path) for path in chunk["paths"]
                ))
                request = AIRequest(
                    ((user_context,) if user_context else ()) + source,
                    QuizResponse, system_instruction=prompt, file_session=session,
                )
                try:
                    result = await _execute_cascade(
                        request, exclude_vertex=exclude_vertex, allow_empty=True,
                    )
                except Exception:
                    logger.warning("EXAM_BATCH_FAILED source=%s part=%d reason=unexpected_error",
                                   kind, index + 1)
                    return None
                if result is None:
                    logger.warning("EXAM_BATCH_FAILED source=%s part=%d reason=cascade_exhausted",
                                   kind, index + 1)
                    return None
                for name, value in zip(
                    ("total_tokens", "input_tokens", "output_tokens", "thoughts_tokens"),
                    (result[1], result[2], result[3], result[4]),
                ):
                    usage[name] += value
                details.append({
                    "provider": result[9], "model": result[5],
                    "key_index": result[6], "cascade_rank": result[7],
                    "cascade_total": result[8], "part": index + 1,
                })
                if not result[0]:
                    completed[index] = True
                    await checkpoint.save(parts, usage, details, completed_parts=completed)
                    break
                added = 0
                for question in result[0]:
                    if not _valid_extracted_question(question):
                        logger.warning("EXAM_BATCH_FAILED source=%s part=%d reason=invalid_question",
                                       kind, index + 1)
                        return None
                    key = _question_key(question)
                    if key in seen:
                        continue
                    seen.add(key)
                    parts[index].append(question)
                    added += 1
                    if sum(map(len, parts)) >= count:
                        break
                repeats = repeats + 1 if added == 0 else 0
                await checkpoint.save(parts, usage, details, completed_parts=completed)

        questions = [question for part in parts for question in part]
        if not questions:
            logger.warning("EXAM_BATCH_FAILED source=%s reason=no_questions", kind)
            return None
        if len(questions) < count and not all(completed):
            return None
        await checkpoint.clear()
        providers = {info.get("provider") for info in details}
        models = {info.get("model") for info in details}
        _last_model_used_var.set({
            "provider": next(iter(providers)) if len(providers) == 1 else "mixed",
            "model": next(iter(models)) if len(models) == 1 else "mixed",
            "key_index": None, "mode": mode, "chunk_details": details,
            "cascade_rank": None, "cascade_total": None,
        })
        _last_token_usage_var.set(usage)
        return questions, usage["total_tokens"]
    finally:
        await session.aclose()


async def extract_pdf_in_batches(file_path: str, page_count: int, count: int,
                                 prompt_template: str, part_count: int,
                                 user_context: Optional[str] = None,
                                 user_id=None, file_hash=None):
    """PDF context-overlap ownership with original exam order."""
    if part_count == 1:
        pdf_parts = [{"path": file_path, "owned_first": 1, "owned_last": page_count,
                      "page_count": page_count}]
    else:
        pdf_parts = await split_pdf_with_context_without_orphans(file_path, part_count)
        await asyncio.to_thread(release_memory_to_os)
    try:
        if not pdf_parts or len(pdf_parts) != part_count:
            return None
        chunks = [{"paths": [part["path"]], "pdf_chunk": part} for part in pdf_parts]
        return await _extract_exam_chunks(
            chunks, count, prompt_template, user_context, user_id,
            file_hash, "pdf", "batched_exam_pdf", exclude_vertex=(part_count == 3),
        )
    finally:
        if part_count != 1:
            for part in pdf_parts:
                if part["path"] != file_path:
                    safe_file_cleanup(part["path"])


async def extract_images_in_batches(paths: List[str], count: int, prompt_template: str,
                                    user_context: Optional[str] = None,
                                    user_id=None, file_hash=None):
    """Split long albums contiguously with one overlap photo for context."""
    if len(paths) <= 10:
        chunks = [{"paths": paths}]
    else:
        chunks = []
        length = len(paths)
        for index in range(3):
            start = (length * index) // 3
            end = (length * (index + 1)) // 3
            first = max(0, start - 1)
            last = min(length, end + 1)
            chunks.append({
                "paths": paths[first:last],
                "image_ownership": (start - first + 1,
                                    end - first, last - first),
            })
    return await _extract_exam_chunks(
        chunks, count, prompt_template, user_context, user_id,
        file_hash, "images", "batched_exam_images",
        exclude_vertex=(len(paths) > 10),
    )


async def extract_text_in_batches(text: str, count: int, prompt_template: str,
                                  user_context: Optional[str] = None,
                                  user_id=None, file_hash=None):
    """Keep original text intact for each extraction request."""
    digest = hashlib.sha256((file_hash or "").encode() + b":" +
                            text.encode("utf-8")).hexdigest()
    return await _extract_exam_chunks(
        [{"text": text}], count, prompt_template, user_context, user_id,
        digest, "text", "batched_exam_text",
    )
