"""Resume-safe, bounded PDF question batches for expanded/super generation only."""

import hashlib
import json
import math
from typing import Any, Dict, List, Optional

from logger import get_logger
from utils import safe_file_cleanup
from helpers.gemini_helper.generation import (
    _execute_cascade, _generated_pdf_chunk_prompt, _generated_pdf_chunk_targets,
)
from helpers.gemini_helper.media import split_pdf_with_context_without_orphans
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
    def __init__(self, user_id, file_hash, prompt, context, count, part_count, progressive):
        self.key = None
        if user_id is not None and file_hash:
            identity = json.dumps(
                [str(user_id), str(file_hash), prompt, context, count, part_count, progressive,
                 MAX_BATCH_QUESTIONS, "pdf-batches-v1"],
                ensure_ascii=False,
            )
            self.key = "quiz:pdf-batch:v1:" + hashlib.sha256(identity.encode()).hexdigest()

    async def load(self, num_parts):
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
                        isinstance(q, dict) and _valid_question(q) for q in part
                    ) for part in parts)):
                return None
            return data
        except Exception:
            logger.warning("PDF_BATCH_CHECKPOINT_READ_FAILED")
            return None

    async def save(self, parts, usage, details):
        if not self.key:
            return
        try:
            from config import redis_client
            await redis_client.setex(
                self.key, CHECKPOINT_TTL_SECONDS,
                json.dumps({"parts": parts, "usage": usage, "details": details},
                           ensure_ascii=False, separators=(",", ":")),
            )
        except Exception:
            logger.warning("PDF_BATCH_CHECKPOINT_WRITE_FAILED")

    async def clear(self):
        if not self.key:
            return
        try:
            from config import redis_client
            await redis_client.delete(self.key)
        except Exception:
            logger.warning("PDF_BATCH_CHECKPOINT_CLEAR_FAILED")


async def generate_pdf_in_batches(
    file_path: str, page_count: int, count: int, prompt_template: str,
    part_count: int, progressive: bool = False, user_context: Optional[str] = None,
    user_id=None, file_hash=None,
):
    """Generate one bounded batch at a time, rerouting only failures via cascade.

    Short PDFs retain *all* source pages on each batch. Large PDFs keep the
    established context-overlap ownership scheme, then batch within each part.
    A validated checkpoint may resume a failed attempt without regenerating
    earlier successful batches. The ordinary (<=15 pages, <=30 questions)
    generation path never calls this function.
    """
    if part_count == 1:
        chunks = [{"path": file_path, "owned_first": 1, "owned_last": page_count,
                   "page_count": page_count}]
    else:
        chunks = await split_pdf_with_context_without_orphans(file_path, part_count)
    try:
        if len(chunks) != part_count or not chunks:
            return None
        targets = _generated_pdf_chunk_targets(count, chunks)
        checkpoint = _Checkpoint(user_id, file_hash, prompt_template, user_context,
                                 count, part_count, progressive)
        saved = await checkpoint.load(len(chunks))
        parts = saved["parts"] if saved else [[] for _ in chunks]
        usage = saved.get("usage", {}) if saved else {}
        usage = {k: int(usage.get(k, 0) or 0) for k in
                 ("input_tokens", "output_tokens", "thoughts_tokens", "total_tokens")}
        details = saved.get("details", []) if saved else []
        if not isinstance(details, list) or any(
            not isinstance(item, dict) for item in details
        ) or any(len(part) > target for part, target in zip(parts, targets)):
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
                        logger.warning("PDF_BATCH_FAILED part=%d reason=insufficient_unique_questions",
                                       part_index + 1)
                        return None
                    attempts += 1
                    missing = target - len(parts[part_index])
                    batch_size = min(MAX_BATCH_QUESTIONS, missing)
                    previous = [q for part in parts for q in part]
                    instruction = _batch_prompt(prompt_template, chunk, batch_size,
                                                previous, progressive, len(previous), count)
                    contents = ((user_context,) if user_context else ()) + (
                        Attachment.from_path(chunk["path"]),
                    )
                    request = AIRequest(contents, QuizResponse,
                                        system_instruction=instruction, file_session=file_session)
                    result = await _execute_cascade(request, exclude_vertex=(part_count == 3))
                    if result is None:
                        logger.warning("PDF_BATCH_FAILED part=%d reason=model_cascade_exhausted",
                                       part_index + 1)
                        return None
                    for name, value in zip(
                        ("total_tokens", "input_tokens", "output_tokens", "thoughts_tokens"),
                        (result[1], result[2], result[3], result[4]),
                    ):
                        usage[name] += value
                    details.append({"provider": result[9], "model": result[5],
                                    "key_index": result[6], "cascade_rank": result[7],
                                    "cascade_total": result[8]})
                    added = 0
                    for question in result[0]:
                        qkey = _question_key(question)
                        if not _valid_question(question) or qkey in existing:
                            continue
                        parts[part_index].append(question)
                        existing.add(qkey)
                        added += 1
                        if len(parts[part_index]) >= target:
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
                "key_index": None, "mode": "batched_super_pdf" if part_count == 3 else "batched_over_limit_pdf",
                "chunk_details": details, "cascade_rank": None, "cascade_total": None,
            })
            _last_token_usage_var.set(usage)
            return questions, usage["total_tokens"]
        finally:
            await file_session.aclose()
    finally:
        if part_count != 1:
            for chunk in chunks:
                if chunk["path"] != file_path:
                    safe_file_cleanup(chunk["path"])
