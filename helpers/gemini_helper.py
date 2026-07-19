"""AI generation, SHA-256 cache lookup, PDF chunking, and loading feedback."""

import asyncio
import datetime
import hashlib
import json
import os
import random
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

import fitz
from aiogram.exceptions import TelegramBadRequest
from dotenv import load_dotenv
from google import genai
from google.genai import types
from groq import AsyncGroq
from pydantic import BaseModel, Field

from constants import (
    AI_REQUEST_TIMEOUT,
    GEMINI_FALLBACK_MODEL,
    GEMINI_PRIMARY_MODEL,
    KEY_BLOCK_QUOTA_EXHAUSTED,
    KEY_BLOCK_TEMPORARY_ERROR,
    MAX_LIMIT_PAGES,
    QUOTA_ERROR_KEYWORDS,
    SYSTEM_PROMPT_GENERATE_QUESTIONS,
)
from logger import get_logger, log_error, log_info, log_warning
from supabase_helper import get_cached_quiz, save_quiz_to_cache
from utils import calculate_file_hash, safe_file_cleanup

load_dotenv()
logger = get_logger(__name__)
API_KEYS = [key.strip() for key in os.getenv("GEMINI_API_KEYS", "").split(",") if key.strip()]
GROQ_API_KEY = os.getenv("GROQ_API_KEY", "")
blocked_keys: Dict[int, datetime.datetime] = {}

LOADING_PHRASES = (
    "🔍 يقوم الذكاء الاصطناعي الآن بفحص ملفاتك المرفوعة...",
    "🧠 جاري تحليل النصوص واستخراج المفاهيم الأكاديمية...",
    "⚡ يتم الآن توليد الأسئلة التفاعلية وصيغ الكويز...",
    "✨ شارَفنا على الانتهاء... نقوم بتنسيق لوحة الاختبار...",
    "⏳ لحظات قليلة جداً ويصبح اختبارك التفاعلي جاهزاً للبدء...",
)


class QuizQuestion(BaseModel):
    question: str = Field(description="Question text")
    options: List[str] = Field(description="Four answer options")
    correct_option_id: int = Field(description="Correct option index")
    hint: str = Field(description="Hint")
    explanation: str = Field(default="", description="Explanation")


class QuizResponse(BaseModel):
    questions: List[QuizQuestion]


def _available_key_indices() -> List[int]:
    now = datetime.datetime.now()
    indices: List[int] = []
    for index in range(len(API_KEYS)):
        if now >= blocked_keys.get(index, datetime.datetime.min):
            blocked_keys.pop(index, None)
            indices.append(index)
    return indices


def _combined_file_hash(paths: Sequence[str]) -> str:
    """Derive one SHA-256 cache key from ordered byte-payload digests only."""
    digest = hashlib.sha256()
    for path in paths:
        digest.update(calculate_file_hash(path).encode("ascii"))
    return digest.hexdigest()


def get_pdf_page_count_sync(file_path: str) -> int:
    with fitz.open(file_path) as document:
        return len(document)


def split_pdf_into_three_sync(file_path: str) -> List[str]:
    """Split a PDF into three physical, nearly equal PDF files."""
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
    try:
        await asyncio.to_thread(client.files.delete, name=file_name)
    except Exception as exc:
        log_warning(logger, f"Could not delete Gemini upload {file_name}: {exc}")


async def _loading_animation(message: Any, stop_event: asyncio.Event) -> None:
    phrase_index = 0
    while not stop_event.is_set():
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=3)
            break
        except asyncio.TimeoutError:
            try:
                await message.edit_text(LOADING_PHRASES[phrase_index])
            except TelegramBadRequest:
                # Telegram raises this when a phrase is already displayed.
                pass
            except Exception as exc:
                log_warning(logger, f"Loading-status update failed: {exc}")
            phrase_index = (phrase_index + 1) % len(LOADING_PHRASES)


def _mark_key_failure(key_index: int, error: Exception) -> None:
    message = str(error).lower()
    if any(keyword in message for keyword in QUOTA_ERROR_KEYWORDS):
        blocked_keys[key_index] = datetime.datetime.now() + datetime.timedelta(hours=KEY_BLOCK_QUOTA_EXHAUSTED)
    else:
        blocked_keys[key_index] = datetime.datetime.now() + datetime.timedelta(minutes=KEY_BLOCK_TEMPORARY_ERROR)


async def _generate_with_key(paths: Sequence[str], prompt: str, key_index: int) -> tuple[List[Dict[str, Any]], int]:
    client = genai.Client(api_key=API_KEYS[key_index])
    uploaded = []
    try:
        contents: List[Any] = [prompt]
        for path in paths:
            uploaded_file = await asyncio.to_thread(client.files.upload, file=path)
            uploaded.append(uploaded_file)
            contents.append(uploaded_file)

        response = await asyncio.wait_for(
            client.aio.models.generate_content(
                model=GEMINI_PRIMARY_MODEL,
                contents=contents,
                config=types.GenerateContentConfig(
                    response_mime_type="application/json",
                    response_schema=QuizResponse,
                    temperature=0.7,
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
        _mark_key_failure(key_index, exc)
        raise
    finally:
        for uploaded_file in uploaded:
            asyncio.create_task(_safe_delete_gemini_file(client, uploaded_file.name))


async def _generate_regular(paths: Sequence[str], prompt: str) -> Optional[tuple[List[Dict[str, Any]], int]]:
    if not API_KEYS:
        log_error(logger, "GEMINI_API_KEYS is not configured")
        return None
    candidates = _available_key_indices() or list(range(len(API_KEYS)))
    for key_index in random.sample(candidates, len(candidates)):
        try:
            return await _generate_with_key(paths, prompt, key_index)
        except Exception as exc:
            log_warning(logger, f"Gemini key {key_index} failed: {exc}")
    return None


async def _generate_super_pdf(file_path: str, count: int, prompt_template: str) -> Optional[tuple[List[Dict[str, Any]], int]]:
    if len(API_KEYS) < 3:
        log_error(logger, "Super processing requires three distinct GEMINI_API_KEYS")
        return None
    chunk_paths = await asyncio.to_thread(split_pdf_into_three_sync, file_path)
    if len(chunk_paths) != 3:
        return await _generate_regular([file_path], prompt_template.replace("{count}", str(count)))

    key_indices = (_available_key_indices() or list(range(len(API_KEYS))))[:3]
    if len(key_indices) < 3:
        return None
    base, remainder = divmod(count, 3)
    question_counts = [base + (1 if index < remainder else 0) for index in range(3)]
    try:
        tasks = [
            _generate_with_key([chunk_path], prompt_template.replace("{count}", str(question_count)), key_index)
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


async def _generate_text_quiz(pure_text: str, prompt: str) -> Optional[List[Dict[str, Any]]]:
    if not GROQ_API_KEY:
        log_warning(logger, "GROQ_API_KEY is not configured; skipping straight to Gemini for text generation")
        return None
    try:
        client = AsyncGroq(api_key=GROQ_API_KEY)
        response = await asyncio.wait_for(
            client.chat.completions.create(
                model="openai/gpt-oss-120b",
                messages=[{"role": "user", "content": f"{prompt}\n\n{pure_text}"}],
                response_format={"type": "json_object"},
                temperature=0.7,
            ),
            timeout=45,
        )
        parsed = QuizResponse(**json.loads(response.choices[0].message.content))
        return [question.model_dump() for question in parsed.questions]
    except Exception as exc:
        # لا نُفشل الطلب هنا؛ فقط نسجل السبب الحقيقي ونترك المتصل يجرّب البديل (Gemini).
        log_error(logger, f"Groq text generation failed, will fall back to Gemini: {exc}")
        return None


async def _generate_text_quiz_with_gemini(pure_text: str, prompt: str) -> Optional[List[Dict[str, Any]]]:
    """Fallback path: generate a quiz from plain text using Gemini directly (no file upload needed).

    This reuses the same reliable provider already used for files/images, so pure-text
    generation keeps working even if Groq's model lineup changes or GROQ_API_KEY is missing.
    """
    if not API_KEYS:
        log_error(logger, "GEMINI_API_KEYS is not configured; cannot fall back for text generation")
        return None
    candidates = _available_key_indices() or list(range(len(API_KEYS)))
    for key_index in random.sample(candidates, len(candidates)):
        client = genai.Client(api_key=API_KEYS[key_index])
        try:
            response = await asyncio.wait_for(
                client.aio.models.generate_content(
                    model=GEMINI_PRIMARY_MODEL,
                    contents=[f"{prompt}\n\n{pure_text}"],
                    config=types.GenerateContentConfig(
                        response_mime_type="application/json",
                        response_schema=QuizResponse,
                        temperature=0.7,
                    ),
                ),
                timeout=AI_REQUEST_TIMEOUT,
            )
            if not response.parsed or not hasattr(response.parsed, "questions"):
                raise ValueError("Gemini returned no structured questions")
            return [question.model_dump() for question in response.parsed.questions]
        except Exception as exc:
            _mark_key_failure(key_index, exc)
            log_warning(logger, f"Gemini text-fallback key {key_index} failed: {exc}")
    return None


async def generate_quiz_smart(
    file_paths: Optional[List[str]] = None,
    pure_text: Optional[str] = None,
    count: int = 0,
    skip_cache: bool = False,
    file_hash: Optional[str] = None,
    status_message: Optional[Any] = None,
) -> Optional[List[Dict[str, Any]]]:
    """Generate a quiz, using SHA-256 cache lookup before any external API call."""
    stop_event = asyncio.Event()
    animation_task = asyncio.create_task(_loading_animation(status_message, stop_event)) if status_message else None
    try:
        prompt = SYSTEM_PROMPT_GENERATE_QUESTIONS.replace("{count}", str(count))
        if pure_text:
            questions = await _generate_text_quiz(pure_text, prompt)
            if not questions:
                questions = await _generate_text_quiz_with_gemini(pure_text, prompt)
            return questions
        if not file_paths:
            return None

        cache_key = file_hash or await asyncio.to_thread(_combined_file_hash, file_paths)
        if not skip_cache:
            cached = await get_cached_quiz(cache_key)
            if cached and cached.get("questions_data"):
                log_info(logger, f"Cache hit for {cache_key}; external generation bypassed")
                return cached["questions_data"]

        is_super_pdf = (
            len(file_paths) == 1
            and file_paths[0].lower().endswith(".pdf")
            and await asyncio.to_thread(get_pdf_page_count_sync, file_paths[0]) > MAX_LIMIT_PAGES
        )
        generated = (
            await _generate_super_pdf(file_paths[0], count, SYSTEM_PROMPT_GENERATE_QUESTIONS)
            if is_super_pdf
            else await _generate_regular(file_paths, prompt)
        )
        if not generated:
            return None
        questions, total_tokens = generated
        await save_quiz_to_cache(cache_key, questions, total_tokens)
        return questions
    finally:
        stop_event.set()
        if animation_task:
            await animation_task
