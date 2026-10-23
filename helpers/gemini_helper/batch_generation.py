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
    _ready_exam_chunk_prompt, _merge_pdf_chunk_questions,
)
from helpers.gemini_helper.media import split_pdf_with_context_without_orphans, split_images_into_three_sync
from helpers.gemini_helper.schemas import QuizResponse
from helpers.gemini_helper.state import _last_model_used_var, _last_token_usage_var
from services.ai.contracts import AIRequest, Attachment
from services.ai.file_session import GeminiFileSession
from services.ai.runtime import choose_parallel_preference

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
    """Keep every original choice, including two- and three-choice exams."""
    options = question.get("options")
    answer = question.get("correct_option_id")
    return (bool(_question_key(question)) and isinstance(options, list) and
            len(options) >= 2 and all(isinstance(o, str) and o.strip() for o in options) and
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
                            exclude_vertex: bool = False,
                            inline_only: bool = False):
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
    file_session = None if inline_only else GeminiFileSession()
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
                    file_session=file_session, inline_only=inline_only,
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
        if file_session is not None:
            await file_session.aclose()




async def _run_expanded_pdf_parallel(chunks: List[Dict[str, Any]], count: int,
                                     prompt_template: str, progressive: bool,
                                     user_context: Optional[str], user_id, file_hash,
                                     extraction: bool):
    """Two independent inline PDF requests, with per-part cascade and checkpoint.

    The first attempt in each part uses the same eligible model and credential.
    Each task then owns its own fallback cascade. Pages adjacent to the
    partition are context-only, as specified by the existing PDF prompts.
    Unlike large/super PDF batches, this path never creates a Files API session.
    """
    if len(chunks) != 2 or count < 1:
        return None
    targets = _generated_pdf_chunk_targets(count, [c["pdf_chunk"] for c in chunks])
    source_kind = "expanded_inline_exam" if extraction else "expanded_inline_generated"
    checkpoint = _Checkpoint(
        user_id, file_hash, prompt_template, user_context, count, 2,
        progressive, source_kind=source_kind,
    )
    saved = await checkpoint.load(2, extraction=extraction)
    parts = saved["parts"] if saved else [[], []]
    completed = saved.get("completed_parts", [False, False]) if saved else [False, False]
    usage = saved.get("usage", {}) if saved else {}
    usage = {key: int(usage.get(key, 0) or 0) for key in
             ("total_tokens", "input_tokens", "output_tokens", "thoughts_tokens")}
    details = saved.get("details", []) if saved else []
    if (not isinstance(details, list) or not isinstance(completed, list) or
            len(completed) != 2 or any(type(v) is not bool for v in completed) or
            any(not isinstance(v, dict) for v in details) or
            sum(len(part) for part in parts) > count or
            (not extraction and any(len(part) > target for part, target in zip(parts, targets)))):
        parts, completed, details = [[], []], [False, False], []
        usage = {key: 0 for key in usage}
    if not extraction:
        completed = [len(part) >= target for part, target in zip(parts, targets)]

    lock = asyncio.Lock()
    seed_request = AIRequest(
        ((user_context,) if user_context else ()) +
        (Attachment.from_path(chunks[0]["paths"][0]),),
        QuizResponse, inline_only=True,
    )
    try:
        preference = await choose_parallel_preference(seed_request)
    except Exception:
        logger.warning("EXPANDED_PDF_PREFERENCE_FAILED using_regular_cascade")
        preference = None

    async def attempt(index: int, initial: bool) -> bool:
        if completed[index]:
            return True
        chunk = chunks[index]
        if extraction:
            # First request extracts all questions owned by this page range.
            # Subsequent requests are small only when something is missing.
            limit = count if initial else min(
                MAX_BATCH_QUESTIONS, max(1, count - sum(map(len, parts))))
            instruction = (
                _ready_exam_chunk_prompt(prompt_template, limit, chunk["pdf_chunk"])
                if initial else
                _extraction_batch_prompt(prompt_template, chunk, limit, parts[index])
            )
        else:
            missing = targets[index] - len(parts[index])
            if missing <= 0:
                completed[index] = True
                return True
            limit = missing if initial else min(MAX_BATCH_QUESTIONS, missing)
            segment = (
                "المرحلة الأولى: من السهل إلى المتوسط دون الوصول إلى الأصعب."
                if index == 0 else
                "المرحلة الأخيرة: من المتوسط إلى الأصعب."
            ) if progressive else None
            instruction = (
                _generated_pdf_chunk_prompt(prompt_template, limit, chunk["pdf_chunk"], segment)
                if initial else
                _batch_prompt(
                    prompt_template, chunk["pdf_chunk"], limit,
                    [q for part in parts for q in part],
                    progressive, sum(map(len, parts)), count,
                )
            )
        source = ((user_context,) if user_context else ()) + tuple(
            Attachment.from_path(path) for path in chunk["paths"]
        )
        request = AIRequest(
            source, QuizResponse, system_instruction=instruction,
            preferred_attempt=preference if initial else None,
            inline_only=True,
        )
        try:
            result = await _execute_cascade(request, allow_empty=extraction)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning("EXPANDED_PDF_PART_FAILED part=%d stage=%s reason=cascade_error",
                           index + 1, "initial" if initial else "continuation")
            return False
        if result is None:
            logger.warning("EXPANDED_PDF_PART_FAILED part=%d stage=%s reason=cascade_exhausted",
                           index + 1, "initial" if initial else "continuation")
            return False
        validator = _valid_extracted_question if extraction else _valid_question
        if any(not validator(q) for q in result[0]):
            logger.warning("EXPANDED_PDF_PART_FAILED part=%d reason=invalid_questions", index + 1)
            return False

        async with lock:
            existing = {_question_key(q) for q in parts[index]}
            added = 0
            for question in result[0]:
                key = _question_key(question)
                if key in existing:
                    continue
                if not extraction and len(parts[index]) >= targets[index]:
                    break
                if extraction and sum(map(len, parts)) >= count:
                    break
                parts[index].append(question)
                existing.add(key)
                added += 1
            for key, value in zip(
                ("total_tokens", "input_tokens", "output_tokens", "thoughts_tokens"),
                (result[1], result[2], result[3], result[4]),
            ):
                usage[key] += value
            details.append({
                "provider": result[9], "model": result[5],
                "key_index": result[6], "cascade_rank": result[7],
                "cascade_total": result[8], "part": index + 1,
            })
            if extraction:
                # A structured empty response is explicit EOF; lack of progress
                # otherwise is not evidence that the source has ended.
                completed[index] = not result[0]
            else:
                completed[index] = len(parts[index]) >= targets[index]
            await checkpoint.save(parts, usage, details, completed_parts=completed)
        logger.info("EXPANDED_PDF_PART_RESULT part=%d stage=%s added=%d total=%d eof=%s",
                    index + 1, "initial" if initial else "continuation",
                    added, len(parts[index]), completed[index])
        return added > 0 or completed[index]

    # Only missing parts run on retry. Successful parts are durable independently
    # of their neighbour, so a failure on part two never repeats part one.
    await asyncio.gather(*(attempt(i, True) for i in range(2) if not completed[i]))

    stalled = 0
    max_rounds = math.ceil(count / MAX_BATCH_QUESTIONS) * 2 + 2
    for _ in range(max_rounds):
        merged = _merge_pdf_chunk_questions([(part,) for part in parts], count)
        if (len(merged) >= count if extraction else all(completed)):
            break
        if extraction and all(completed):
            break
        pending = [i for i in range(2) if not completed[i]]
        if not pending:
            break
        previous = (sum(map(len, parts)), sum(completed))
        outcomes = await asyncio.gather(*(attempt(i, False) for i in pending))
        progress = (sum(map(len, parts)), sum(completed)) != previous
        stalled = 0 if progress else stalled + 1
        if stalled >= 2:
            logger.warning("EXPANDED_PDF_INCOMPLETE reason=no_progress attempts=%d", max_rounds)
            break

    questions = _merge_pdf_chunk_questions([(part,) for part in parts], count)
    if not questions or (not extraction and len(questions) != count):
        return None
    if extraction and len(questions) < count and not all(completed):
        # The estimate may be high, but only an explicit EOF on each owned
        # range proves that the extraction is complete.
        return None
    await checkpoint.clear()
    providers = {detail.get("provider") for detail in details}
    models = {detail.get("model") for detail in details}
    _last_model_used_var.set({
        "provider": next(iter(providers)) if len(providers) == 1 else "mixed",
        "model": next(iter(models)) if len(models) == 1 else "mixed",
        "key_index": None, "mode": source_kind, "chunk_details": details,
        "cascade_rank": None, "cascade_total": None,
    })
    _last_token_usage_var.set(usage)
    return questions, usage["total_tokens"]


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
        if part_count == 2:
            return await _run_expanded_pdf_parallel(
                chunks, count, prompt_template, progressive, user_context,
                user_id, file_hash, extraction,
            )
        if extraction:
            return await _extract_exam_chunks(
                chunks, count, prompt_template, user_context,
                user_id, file_hash, "pdf", "batched_exam_pdf",
                exclude_vertex=(part_count == 3), inline_only=(part_count == 1),
            )
        return await _run_batch_chunks(
            chunks, count, prompt_template, progressive, user_context,
            user_id, file_hash, "pdf",
            "batched_super_pdf" if part_count == 3 else "batched_over_limit_pdf",
            exclude_vertex=(part_count == 3), inline_only=(part_count == 1),
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
        return await extract_images_in_batches(
            paths, count, prompt_template, user_context,
            user_id, file_hash,
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
        return await _extract_exam_chunks(
            [{"text": text, "weight": 1}], count, prompt_template,
            user_context, user_id, source_hash, "text", "batched_exam_text",
        )
    return await _run_batch_chunks(
        [{"text": text, "weight": 1}], count, prompt_template,
        progressive, user_context, user_id, source_hash, "text",
        "batched_over_limit_text",
    )




def _extraction_batch_prompt(prompt_template: str, chunk: Dict[str, Any],
                             limit: int, previous: List[Dict[str, Any]]) -> str:
    """Continue original questions in document order, with no fill-to-target."""
    if chunk.get("pdf_chunk"):
        prompt = _ready_exam_chunk_prompt(prompt_template, limit, chunk["pdf_chunk"])
    else:
        prompt = prompt_template.replace("{count}", str(limit))
    if chunk.get("image_ownership"):
        first, last, total = chunk["image_ownership"]
        prompt += (
            "\n\n[ملكية صور الاختبار]: الصور في ترتيب المصدر. "
            f"لا تستخرج إلا الأسئلة التي تبدأ في الصور {first} إلى {last} من أصل {total} "
            "صورة مرقمة داخل هذه الدفعة. الصور خارج المجال سياق فقط؛ "
            "إذا امتد السؤال من صورة مملوكة إلى صورة سياق فاقرأه كاملاً."
        )
    prompt += (
        "\n\n[استخراج تتابعي إلزامي]: "
        f"استخرج حتى {limit} أسئلة موجودة أصلاً في هذا المصدر مرتبة بحسب ظهورها. "
        "لا تختلق سؤالاً لاستكمال العدد، ولا تغير أرقام الأسئلة أو المعطيات الأصلية. "
        "التزم بنمط استخدام الإجابات الأصلية أو حلها آلياً المحدد في تعليمات الاختبار. "
        "إذا لم يتبق أي سؤال أصلي بعد آخر سؤال مستخرج فأعد JSON صحيحاً يحتوي "
        "questions: [] وليس أسئلة مكررة. إذا تبقى أقل من الحد فأعد الموجود فقط."
    )
    if previous:
        last = str(previous[-1]["question"])[:600]
        prompt += (
            "\n\n[موضع الاستكمال]: آخر سؤال مُستخرج من هذا الجزء كان:\n"
            + last + "\nابدأ بالسؤال الذي يليه مباشرةً في المصدر، لا تعِد هذا السؤال."
        )
        # Supply more anchors for short/ambiguous question titles without
        # flooding the context with all previous questions.
        anchors = ["- " + str(q["question"])[:160] for q in previous[-15:]]
        prompt += "\n[أسئلة سبق استخراجها من الجزء، ممنوع تكرارها]:\n" + "\n".join(anchors)
    else:
        prompt += "\n[هذه أول دفعة من الجزء، ابدأ بأول سؤال أصلي تابع له.]"
    return prompt


async def _extract_exam_chunks(chunks: List[Dict[str, Any]], count: int,
                               prompt_template: str, user_context: Optional[str],
                               user_id, source_hash, source_kind: str, mode: str,
                               exclude_vertex: bool = False,
                               inline_only: bool = False):
    """Conservative, resumable extraction with original question ordering.

    Actual question counts vary by source chunk; do not divide the estimated
    count proportionally as done when *generating* new questions. A successful
    empty structured response is EOF. Provider failures are not EOF.
    """
    if count < 1 or not chunks:
        return None
    checkpoint = _Checkpoint(user_id, source_hash, prompt_template, user_context,
                             count, len(chunks), False, source_kind="exam:" + source_kind)
    saved = await checkpoint.load(len(chunks), extraction=True)
    parts = saved["parts"] if saved else [[] for _ in chunks]
    completed = saved["completed_parts"] if saved else [False] * len(chunks)
    usage = saved.get("usage", {}) if saved else {}
    usage = {k: int(usage.get(k, 0) or 0) for k in
             ("input_tokens", "output_tokens", "thoughts_tokens", "total_tokens")}
    details = saved.get("details", []) if saved else []
    if (not isinstance(details, list) or
            any(not isinstance(v, dict) for v in details) or
            sum(len(part) for part in parts) > count):
        parts, completed, details = [[] for _ in chunks], [False] * len(chunks), []
        usage = {k: 0 for k in usage}
    seen = {_question_key(q) for part in parts for q in part}
    session = None if inline_only else GeminiFileSession()
    try:
        for index, chunk in enumerate(chunks):
            repeats = 0
            attempts = 0
            max_attempts = math.ceil(count / MAX_BATCH_QUESTIONS) * 4 + 4
            while not completed[index] and sum(map(len, parts)) < count:
                if repeats >= 2 or attempts >= max_attempts:
                    logger.warning("EXAM_BATCH_FAILED modality=%s part=%d reason=no_progress",
                                   source_kind, index + 1)
                    return None
                attempts += 1
                remaining = count - sum(map(len, parts))
                limit = min(MAX_BATCH_QUESTIONS, remaining)
                instruction = _extraction_batch_prompt(
                    prompt_template, chunk, limit, parts[index])
                source = ((chunk["text"],) if "text" in chunk else tuple(
                    Attachment.from_path(path) for path in chunk["paths"]
                ))
                request = AIRequest(
                    ((user_context,) if user_context else ()) + source,
                    QuizResponse, system_instruction=instruction,
                    file_session=session, inline_only=inline_only,
                )
                try:
                    result = await _execute_cascade(
                        request, exclude_vertex=exclude_vertex, allow_empty=True,
                    )
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.warning("EXAM_BATCH_FAILED modality=%s part=%d reason=cascade_error",
                                   source_kind, index + 1)
                    return None
                if result is None:
                    logger.warning("EXAM_BATCH_FAILED modality=%s part=%d reason=cascade_exhausted",
                                   source_kind, index + 1)
                    return None
                for key, value in zip(
                    ("total_tokens", "input_tokens", "output_tokens", "thoughts_tokens"),
                    (result[1], result[2], result[3], result[4]),
                ):
                    usage[key] += value
                details.append({
                    "provider": result[9], "model": result[5],
                    "key_index": result[6], "cascade_rank": result[7],
                    "cascade_total": result[8], "part": index + 1,
                })
                if not result[0]:
                    completed[index] = True
                    await checkpoint.save(parts, usage, details, completed_parts=completed)
                    break
                # An invalid response is not proof that the source ended.
                if any(not _valid_extracted_question(q) for q in result[0]):
                    logger.warning("EXAM_BATCH_FAILED modality=%s part=%d reason=malformed_options",
                                   source_kind, index + 1)
                    return None
                added = 0
                for question in result[0]:
                    key = _question_key(question)
                    if key in seen:
                        continue
                    parts[index].append(question)
                    seen.add(key)
                    added += 1
                    if added >= limit or sum(map(len, parts)) >= count:
                        break
                repeats = repeats + 1 if added == 0 else 0
                await checkpoint.save(parts, usage, details, completed_parts=completed)

        extracted = [q for part in parts for q in part]
        if not extracted:
            return None
        # Fewer extracted questions than estimated is acceptable only when all
        # owned source segments explicitly reached EOF. Never pad the result.
        if len(extracted) < count and not all(completed):
            return None
        await checkpoint.clear()
        providers = {d.get("provider") for d in details}
        models = {d.get("model") for d in details}
        _last_model_used_var.set({
            "provider": next(iter(providers)) if len(providers) == 1 else "mixed",
            "model": next(iter(models)) if len(models) == 1 else "mixed",
            "key_index": None, "mode": mode, "chunk_details": details,
            "cascade_rank": None, "cascade_total": None,
        })
        _last_token_usage_var.set(usage)
        return extracted, usage["total_tokens"]
    finally:
        if session is not None:
            await session.aclose()


async def extract_images_in_batches(paths: List[str], count: int,
                                    prompt_template: str,
                                    user_context: Optional[str] = None,
                                    user_id=None, file_hash=None):
    """Contiguous image groups with overlap to retain spanning exam questions."""
    if len(paths) <= 10:
        chunks = [{"paths": paths}]
    else:
        chunks = []
        for i in range(3):
            start = (len(paths) * i) // 3
            stop = (len(paths) * (i + 1)) // 3
            first = max(0, start - 1)
            last = min(len(paths), stop + 1)
            chunks.append({
                "paths": paths[first:last],
                "image_ownership": (start - first + 1, stop - first, last - first),
            })
    return await _extract_exam_chunks(
        chunks, count, prompt_template, user_context,
        user_id, file_hash, "images", "batched_exam_images",
        exclude_vertex=len(paths) > 10,
    )
