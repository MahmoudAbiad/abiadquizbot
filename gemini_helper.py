"""
Gemini AI integration for text extraction and quiz question generation.
Handles randomized API key rotation, async jitter delay, and smart hybrid file processing.
"""

import os
import json
import datetime
import random
import asyncio
from typing import Optional, List, Dict, Any, Tuple

from dotenv import load_dotenv
import fitz  # مكتبة PyMuPDF لفحص وقراءة الملفات محلياً
from google import genai
from google.genai import types
from pydantic import BaseModel, Field

from constants import (
    GEMINI_MODEL,
    MAX_TEXT_LENGTH_FOR_AI,
    AI_REQUEST_TIMEOUT,
    KEY_BLOCK_QUOTA_EXHAUSTED,
    KEY_BLOCK_TEMPORARY_ERROR,
    SYSTEM_PROMPT_EXTRACT_TEXT,
    SYSTEM_PROMPT_GENERATE_QUESTIONS,
    QUOTA_ERROR_KEYWORDS,
    ERROR_API_KEYS_NOT_CONFIGURED,
    ERROR_ALL_KEYS_EXHAUSTED,
)
from logger import get_logger, log_error, log_warning, log_info

load_dotenv()
logger = get_logger(__name__)

# ==================== Configuration ====================
api_keys_raw = os.getenv("GEMINI_API_KEYS", "")
API_KEYS: List[str] = [k.strip() for k in api_keys_raw.split(",") if k.strip()]

# Fallback for single API key format
if not API_KEYS and os.getenv("GEMINI_API_KEY"):
    API_KEYS = [os.getenv("GEMINI_API_KEY")]

blocked_keys: Dict[int, datetime.datetime] = {}


class QuizQuestion(BaseModel):
    """Model for a quiz question with metadata."""

    question: str = Field(description="نص السؤال الاختياري المستخرج من النص المرفق حصراً.")
    options: List[str] = Field(description="أربعة خيارات فريدة ومتوازنة للسؤال.")
    correct_option_id: int = Field(description="مؤشر الخيار الصحيح من 0 إلى 3.")
    hint: str = Field(description="تلميح ذكي ومساعد يقرب الفكرة للطالب دون إعطائه الحل المباشر.")
    explanation: str = Field(default="", description="شرح مختصر للإجابة الصحيحة.")
    was_corrupted_text_fixed: bool = Field(
        default=False,
        description="true فقط إذا تم إصلاح تشوهات النص الناتجة عن OCR سياقياً.",
    )


def has_gemini_api_keys() -> bool:
    return bool(API_KEYS)


def _validate_api_keys() -> bool:
    if not API_KEYS:
        log_error(logger, "No GEMINI_API_KEYS configured in environment")
        return False
    return True


def _get_available_key_indices() -> List[int]:
    """تصفية وفحص المفاتيح المتاحة حالياً واستبعاد المحظورة مؤقتاً."""
    now = datetime.datetime.now()
    available_indices = []

    for idx in range(len(API_KEYS)):
        if idx in blocked_keys:
            if now >= blocked_keys[idx]:
                log_info(logger, f"🔄 انتهى الحظر المؤقت للمفتاح رقم {idx} وتمت إعادته للخدمة.")
                del blocked_keys[idx]
                available_indices.append(idx)
        else:
            available_indices.append(idx)
            
    return available_indices


def _is_quota_error(error_msg: str) -> bool:
    error_lower = error_msg.lower()
    return any(keyword in error_lower for keyword in QUOTA_ERROR_KEYWORDS)


def _block_key_by_index(idx: int, hours: int) -> None:
    unblock_time = datetime.datetime.now() + datetime.timedelta(hours=hours)
    blocked_keys[idx] = unblock_time
    log_warning(logger, f"Key {idx} blocked until {unblock_time.strftime('%Y-%m-%d %H:%M:%S')}")


def _detect_image_mime_type(image_bytes: bytes) -> str:
    if image_bytes.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if image_bytes.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if image_bytes.startswith(b"GIF87a") or image_bytes.startswith(b"GIF89a"):
        return "image/gif"
    if image_bytes.startswith(b"RIFF") and len(image_bytes) >= 12 and image_bytes[8:12] == b"WEBP":
        return "image/webp"
    return "image/png"


async def extract_text_from_image(image_bytes: bytes) -> str:
    """Extract text from image using Gemini Vision API with random key selection and jitter."""
    if not _validate_api_keys():
        return ERROR_API_KEYS_NOT_CONFIGURED

    max_attempts = len(API_KEYS)

    for _ in range(max_attempts):
        available_indices = _get_available_key_indices()
        if not available_indices:
            log_warning(logger, "🚨 جميع المفاتيح محظورة! إعادة تصفير الحظر لتفادي توقف البوت.")
            blocked_keys.clear()
            available_indices = list(range(len(API_KEYS)))

        # 1. تدوير المفاتيح عشوائياً
        selected_idx = random.choice(available_indices)
        key = API_KEYS[selected_idx]

        # 2. إضافة تأخير عشوائي (Jitter) لتفادي الـ RPM Block
        delay = random.uniform(1.0, 2.5)
        log_info(logger, f"⏳ [OCR] إضافة تأخير عشوائي {delay:.2f} ثانية باستخدام المفتاح رقم {selected_idx}...")
        await asyncio.sleep(delay)

        try:
            client = genai.Client(api_key=key)
            mime_type = _detect_image_mime_type(image_bytes)

            # استخدام العميل غير المتزامن (client.aio) لمنع حظر السيرفر
            response = await client.aio.models.generate_content(
                model=GEMINI_MODEL,
                contents=[
                    types.Part.from_bytes(data=image_bytes, mime_type=mime_type),
                    SYSTEM_PROMPT_EXTRACT_TEXT,
                ],
            )

            log_info(logger, f"Successfully extracted text using key {selected_idx}")
            return response.text or ""

        except Exception as e:
            error_msg = str(e)
            if _is_quota_error(error_msg):
                _block_key_by_index(selected_idx, KEY_BLOCK_QUOTA_EXHAUSTED)
                log_warning(logger, f"Quota exhausted on key {selected_idx}")
            else:
                _block_key_by_index(selected_idx, KEY_BLOCK_TEMPORARY_ERROR)
                log_warning(logger, f"Temporary error on key {selected_idx}: {error_msg}")

    log_error(logger, "All API keys exhausted")
    return ERROR_ALL_KEYS_EXHAUSTED


async def get_questions_from_text(text: str, count: int) -> Optional[List[Dict[str, Any]]]:
    """Generate quiz questions from pure text using Gemini API with random key selection and jitter."""
    if not _validate_api_keys():
        return None

    text = text[:MAX_TEXT_LENGTH_FOR_AI]
    prompt = SYSTEM_PROMPT_GENERATE_QUESTIONS.format(count=count)
    max_attempts = len(API_KEYS)

    for _ in range(max_attempts):
        available_indices = _get_available_key_indices()
        if not available_indices:
            log_warning(logger, "🚨 جميع المفاتيح محظورة! إعادة تصفير الحظر لتفادي توقف البوت.")
            blocked_keys.clear()
            available_indices = list(range(len(API_KEYS)))

        # 1. تدوير المفاتيح عشوائياً
        selected_idx = random.choice(available_indices)
        key = API_KEYS[selected_idx]

        # 2. إضافة تأخير عشوائي (Jitter) لتفادي الـ RPM Block
        delay = random.uniform(1.0, 2.5)
        log_info(logger, f"⏳ [Text Gen] إضافة تأخير عشوائي {delay:.2f} ثانية باستخدام المفتاح رقم {selected_idx}...")
        await asyncio.sleep(delay)

        try:
            client = genai.Client(api_key=key)

            response = await client.aio.models.generate_content(
                model=GEMINI_MODEL,
                contents=[prompt, text],
                config=types.GenerateContentConfig(
                    response_mime_type="application/json",
                    response_schema=list[QuizQuestion],
                ),
            )

            result = json.loads(response.text)
            if isinstance(result, dict) and "questions" in result:
                result = result["questions"]

            log_info(logger, f"Generated {len(result)} questions using key {selected_idx}")
            return result

        except Exception as e:
            error_msg = str(e)
            if _is_quota_error(error_msg):
                _block_key_by_index(selected_idx, KEY_BLOCK_QUOTA_EXHAUSTED)
                log_warning(logger, f"Quota exhausted during question generation on key {selected_idx}")
            else:
                _block_key_by_index(selected_idx, KEY_BLOCK_TEMPORARY_ERROR)
                log_warning(logger, f"Error during question generation on key {selected_idx}: {error_msg}")

    log_error(logger, "Failed to generate questions - all API keys exhausted")
    return None


async def generate_quiz_smart(file_path: str, count: int) -> Optional[List[Dict[str, Any]]]:
    """
    الآلية الهجينة الذكية (تطبيق خطوة واحدة بدلاً من 16 طلباً):
    تفحص الملف محلياً، فإذا كان نصياً ترسل النص، وإذا كان ممسوحاً ضوئياً ترسل الـ PDF كاملاً.
    تعتمد على التدوير العشوائي والتأخير الآمن المانع للحظر (Jitter).
    """
    if not _validate_api_keys():
        return None

    # 1. الفحص السريع للمحتوى النصي محلياً بواسطة PyMuPDF
    total_text = ""
    try:
        doc = fitz.open(file_path)
        for page in doc:
            total_text += page.get_text() or ""
        doc.close()
    except Exception as e:
        log_error(logger, f"خطأ أثناء قراءة ملف الـ PDF عبر PyMuPDF: {e}")

    prompt = SYSTEM_PROMPT_GENERATE_QUESTIONS.format(count=count)
    max_attempts = len(API_KEYS)

    for _ in range(max_attempts):
        available_indices = _get_available_key_indices()
        if not available_indices:
            log_warning(logger, "🚨 جميع المفاتيح محظورة! إعادة تصفير الحظر لتفادي توقف البوت تماماً.")
            blocked_keys.clear()
            available_indices = list(range(len(API_KEYS)))

        # أ. اختيار مفتاح عشوائي غير محظور
        selected_idx = random.choice(available_indices)
        key = API_KEYS[selected_idx]

        # ب. إضافة تأخير عشوائي ذكي (Jitter) لكسر تتابع الطلبات السريعة
        delay = random.uniform(1.0, 3.0)
        log_info(logger, f"⏳ [Smart Hybrid] إضافة تأخير عشوائي {delay:.2f} ثانية للمفتاح رقم {selected_idx}...")
        await asyncio.sleep(delay)

        try:
            client = genai.Client(api_key=key)

            # جـ. اتخاذ القرار بناءً على طبيعة المستند المستخرج
            if len(total_text.strip()) > 200:
                log_info(logger, f"🎯 الملف نصي رقمي (Digital). إرسال {len(total_text)} حرف في طلب واحد...")
                # قص النص إذا تجاوز الحد لسلامة السياق
                text_chunk = total_text[:MAX_TEXT_LENGTH_FOR_AI]
                contents = [prompt, text_chunk]
            else:
                log_info(logger, "📸 الملف ممسوح ضوئياً (Scanned). قراءة وإرسال بايتات ملف الـ PDF الأصلي كما هو لـ Gemini...")
                with open(file_path, "rb") as f:
                    pdf_bytes = f.read()

                pdf_part = types.Part.from_bytes(
                    data=pdf_bytes,
                    mime_type="application/pdf"
                )
                contents = [pdf_part, prompt]

            log_info(logger, f"🚀 إرسال طلب المعالجة والتوليد الموحد إلى Gemini عبر المفتاح {selected_idx}...")
            
            response = await client.aio.models.generate_content(
                model=GEMINI_MODEL,
                contents=contents,
                config=types.GenerateContentConfig(
                    response_mime_type="application/json",
                    response_schema=list[QuizQuestion],
                ),
            )

            result = json.loads(response.text)
            if isinstance(result, dict) and "questions" in result:
                result = result["questions"]

            log_info(logger, f"✅ تم توليد {len(result)} سؤال بنجاح بطلب واحد عبر المفتاح {selected_idx}!")
            return result

        except Exception as e:
            error_msg = str(e)
            if _is_quota_error(error_msg):
                _block_key_by_index(selected_idx, KEY_BLOCK_QUOTA_EXHAUSTED)
                log_warning(logger, f"Quota exhausted during hybrid generation on key {selected_idx}")
            else:
                _block_key_by_index(selected_idx, KEY_BLOCK_TEMPORARY_ERROR)
                log_warning(logger, f"Error during hybrid generation on key {selected_idx}: {error_msg}")

    log_error(logger, "فشلت العملية بالكامل - تم استنفاذ جميع مفاتيح الـ API المتوفرة.")
    return None


def generate_quiz_from_file(
    file_path: str,
    count: int,
    mime_type: Optional[str] = None,
) -> Optional[Tuple[List[Dict[str, Any]], int]]:
    """
    Compatibility wrapper kept for older call sites.
    The current bot flow extracts text elsewhere and calls get_questions_from_text.
    """
    _ = file_path
    _ = mime_type
    return None