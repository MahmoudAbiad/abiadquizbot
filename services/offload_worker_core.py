"""
services/offload_worker_core.py
==============================================================================
منطق السيرفر الثاني (render worker) بدون أي اعتماد على FastAPI: يتحقق من الـ payload
ثم ينفّذ نفس دوال المشروع الأصلية حرفياً (render_question_image / build_quiz_* /
build_document_*) - لا نسخة ثانية من منطق الرسم أو التصدير، فالمخرجات متطابقة مع
المعالجة المحلية.

التحقق هنا دفاعي عمداً: السيرفر الثاني ظاهر على الإنترنت (محمي بسر مشترك)، فلا نثق
بحجم/شكل أي payload.
==============================================================================
"""

from typing import Any, Dict, Tuple

MAX_QUESTIONS = 300
MAX_TITLE_CHARS = 500
MAX_TEXT_CHARS = 600_000
MAX_INDEX = 10_000

MEDIA_TYPES = {
    "png": "image/png",
    "pdf": "application/pdf",
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
}


class BadPayload(ValueError):
    """payload غير صالح (يُرجَع كـ 400)."""


def _require_dict(value: Any, name: str) -> Dict[str, Any]:
    if not isinstance(value, dict):
        raise BadPayload(f"{name} must be an object")
    return value


def _require_int(value: Any, name: str, lo: int, hi: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not (lo <= value <= hi):
        raise BadPayload(f"{name} must be an integer in [{lo}, {hi}]")
    return value


def _require_str(value: Any, name: str, max_len: int, allow_empty: bool = True) -> str:
    if not isinstance(value, str) or len(value) > max_len or (not allow_empty and not value.strip()):
        raise BadPayload(f"{name} must be a string (max {max_len} chars)")
    return value


def _require_fmt(payload: Dict[str, Any]) -> str:
    fmt = payload.get("fmt")
    if fmt not in ("docx", "pdf"):
        raise BadPayload("fmt must be 'docx' or 'pdf'")
    return fmt


def _questions(payload: Dict[str, Any]) -> list:
    questions = payload.get("questions")
    if not isinstance(questions, list) or len(questions) > MAX_QUESTIONS or any(not isinstance(q, dict) for q in questions):
        raise BadPayload(f"questions must be a list of objects (max {MAX_QUESTIONS})")
    return questions


def render_image(payload: Dict[str, Any]) -> Tuple[bytes, str]:
    """يرسم صورة PNG لسؤال واحد (نفس render_question_image الأصلية)."""
    question = _require_dict(payload.get("question"), "question")
    idx = _require_int(payload.get("idx"), "idx", 0, MAX_INDEX)
    total = _require_int(payload.get("total"), "total", 1, MAX_INDEX)
    is_ar = payload.get("is_ar")
    if not isinstance(is_ar, bool):
        raise BadPayload("is_ar must be a boolean")

    from services.image_quiz_renderer import render_question_image
    return render_question_image(question, idx, total, is_ar), MEDIA_TYPES["png"]


def build_quiz(payload: Dict[str, Any]) -> Tuple[bytes, str]:
    """ملف Word/PDF لكويز كامل. ExportError تمر كما هي لتُحوَّل إلى 422 بطبقة HTTP."""
    from services.export_service import build_quiz_docx, build_quiz_pdf, normalize_style

    fmt = _require_fmt(payload)
    title = _require_str(payload.get("title", ""), "title", MAX_TITLE_CHARS)
    questions = _questions(payload)
    style = normalize_style(str(payload.get("style") or ""))

    include_answers = payload.get("include_answers", True)
    if not isinstance(include_answers, bool):
        raise BadPayload("include_answers must be a boolean")
    builder = build_quiz_docx if fmt == "docx" else build_quiz_pdf
    return builder(title, questions, style, include_answers), MEDIA_TYPES[fmt]


def build_document(payload: Dict[str, Any]) -> Tuple[bytes, str]:
    """ملف Word/PDF لنص حر (تفريغ محاضرة/ملخص)."""
    from services.export_service import build_document_docx, build_document_pdf, normalize_style

    fmt = _require_fmt(payload)
    title = _require_str(payload.get("title", ""), "title", MAX_TITLE_CHARS)
    text = _require_str(payload.get("text", ""), "text", MAX_TEXT_CHARS)
    style = normalize_style(str(payload.get("style") or ""))

    builder = build_document_docx if fmt == "docx" else build_document_pdf
    return builder(title, text, style), MEDIA_TYPES[fmt]
