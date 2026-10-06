# services/challenge_content.py
"""منطق نقي (بلا Telegram ولا قاعدة بيانات) لمحتوى بنك التحديات:
التحقق من JSON، التطبيع، بصمة المحتوى (idempotency)، تحويل كويز أبيض موجود إلى Snapshot،
وتقليم الإجابات للواجهة. راجع docs/challenges/DESIGN.md."""

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

MAX_FILE_BYTES = 512 * 1024
MAX_QUESTIONS = 200
MIN_OPTIONS, MAX_OPTIONS = 2, 10  # حد Telegram Poll
MAX_TITLE_CHARS = 120
MAX_QUESTION_CHARS = 2000
MAX_OPTION_CHARS = 500
MAX_EXPLANATION_CHARS = 2000
MAX_REPORTED_ERRORS = 5

SOURCE_JSON = "json_upload"
SOURCE_EXISTING = "existing_abiad_quiz"


class ChallengeContentError(ValueError):
    """خطأ محتوى مفهوم للأدمن (رسالته جاهزة للعرض)."""


@dataclass
class ParsedTest:
    title: str
    questions: List[Dict[str, Any]]
    source_type: str
    source_reference: Optional[str] = None
    metadata: Dict[str, Any] = field(default_factory=dict)

    @property
    def question_count(self) -> int:
        return len(self.questions)

    @property
    def content_hash(self) -> str:
        return content_hash(self.questions)


def _is_int(v: Any) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)


def _clean_text(v: Any) -> str:
    return v.strip() if isinstance(v, str) else ""


def normalize_questions(items: Any) -> List[Dict[str, Any]]:
    """يتحقق من قائمة أسئلة بصيغة أبيض ويرجع نسخة منظّفة. يجمع حتى 5 أخطاء بدل الفشل عند الأول."""
    if not isinstance(items, list) or not items:
        raise ChallengeContentError("قائمة الأسئلة (questions) مفقودة أو فارغة.")
    if len(items) > MAX_QUESTIONS:
        raise ChallengeContentError(f"عدد الأسئلة ({len(items)}) يتجاوز الحد الأقصى {MAX_QUESTIONS}.")

    errors: List[str] = []
    out: List[Dict[str, Any]] = []
    for i, q in enumerate(items, start=1):
        err = lambda msg, n=i: errors.append(f"السؤال {n}: {msg}")
        if not isinstance(q, dict):
            err("ليس كائناً صالحاً.")
            continue
        text = _clean_text(q.get("question"))
        if not text:
            err("نص السؤال (question) مفقود.")
        elif len(text) > MAX_QUESTION_CHARS:
            err(f"نص السؤال أطول من {MAX_QUESTION_CHARS} حرفاً.")
        opts_raw = q.get("options")
        opts: List[str] = []
        if not isinstance(opts_raw, list) or not (MIN_OPTIONS <= len(opts_raw) <= MAX_OPTIONS):
            err(f"الخيارات (options) يجب أن تكون قائمة من {MIN_OPTIONS} إلى {MAX_OPTIONS} خيارات.")
        else:
            opts = [_clean_text(o) if isinstance(o, str) else (str(o).strip() if _is_int(o) else "") for o in opts_raw]
            if any(not o for o in opts):
                err("يوجد خيار فارغ أو غير نصي.")
            elif any(len(o) > MAX_OPTION_CHARS for o in opts):
                err(f"يوجد خيار أطول من {MAX_OPTION_CHARS} حرفاً.")
            elif len(set(o.casefold() for o in opts)) != len(opts):
                err("يوجد خياران متطابقان.")
        cid = q.get("correct_option_id")
        if "correct_option_id" not in q:
            err("الإجابة الصحيحة (correct_option_id) مفقودة.")
        elif not _is_int(cid):
            err("correct_option_id يجب أن يكون رقماً صحيحاً (يبدأ من 0).")
        elif opts and not (0 <= cid < len(opts)):
            err(f"correct_option_id={cid} خارج نطاق الخيارات (0..{len(opts) - 1}).")
        exp = q.get("explanation")
        exp_clean = _clean_text(exp) if exp is not None else ""
        if exp is not None and not isinstance(exp, str):
            err("الشرح (explanation) يجب أن يكون نصاً.")
        elif len(exp_clean) > MAX_EXPLANATION_CHARS:
            err(f"الشرح أطول من {MAX_EXPLANATION_CHARS} حرفاً.")
        if not any(e.startswith(f"السؤال {i}:") for e in errors):
            item: Dict[str, Any] = {"question": text, "options": opts, "correct_option_id": cid}
            if exp_clean:
                item["explanation"] = exp_clean
            image_url = q.get("image_url")
            if isinstance(image_url, str) and image_url.startswith("https://"):
                item["metadata"] = {"image_url": image_url}
            out.append(item)
    if errors:
        shown = errors[:MAX_REPORTED_ERRORS]
        more = len(errors) - len(shown)
        msg = "\n".join(shown) + (f"\n… و{more} أخطاء أخرى." if more > 0 else "")
        raise ChallengeContentError(msg)
    return out


def content_hash(questions: List[Dict[str, Any]]) -> str:
    """بصمة المحتوى التنافسي فقط (السؤال+الخيارات+الإجابة) - لا العنوان ولا الشرح."""
    canon = [[q["question"], q["options"], q["correct_option_id"]] for q in questions]
    blob = json.dumps(canon, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def clean_title(title: Any, fallback: str = "") -> str:
    t = _clean_text(title) or _clean_text(fallback)
    if not t:
        raise ChallengeContentError("عنوان الاختبار (title) مفقود.")
    return t[:MAX_TITLE_CHARS]


def parse_challenge_json(raw: bytes, default_title: str = "") -> ParsedTest:
    """الصيغتان المدعومتان: كائن {"title","questions":[...]} أو قائمة أسئلة مباشرة
    (نفس صيغة quizzes.quiz_data؛ العنوان حينها من اسم الملف)."""
    if len(raw) > MAX_FILE_BYTES:
        raise ChallengeContentError(f"حجم الملف يتجاوز {MAX_FILE_BYTES // 1024} كيلوبايت.")
    try:
        data = json.loads(raw.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError) as e:
        raise ChallengeContentError(f"الملف ليس JSON صالحاً (UTF-8): {e}") from None
    if isinstance(data, list):
        title, items = default_title, data
    elif isinstance(data, dict):
        title, items = data.get("title") or default_title, data.get("questions")
    else:
        raise ChallengeContentError("بنية JSON غير مدعومة: المطلوب كائن فيه questions أو قائمة أسئلة.")
    return ParsedTest(clean_title(title), normalize_questions(items), SOURCE_JSON)


def from_existing_quiz(quiz_row: Dict[str, Any]) -> ParsedTest:
    """Snapshot من صف quizzes: يُنسخ المحتوى فقط - لا creator_id ولا likes ولا share_code.
    المرجع الوحيد للمصدر: معرّف الكويز (للتتبع، لا FK)."""
    data = quiz_row.get("quiz_data")
    if isinstance(data, str):
        try:
            data = json.loads(data)
        except json.JSONDecodeError:
            raise ChallengeContentError("بيانات الكويز المصدر تالفة.") from None
    questions = normalize_questions(data)
    meta = {k: quiz_row[k] for k in ("is_math_quiz", "subject_type", "difficulty") if quiz_row.get(k) is not None}
    return ParsedTest(clean_title(quiz_row.get("source_title"), "كويز"), questions, SOURCE_EXISTING,
                      str(quiz_row.get("id")), meta)


def to_import_payload(questions: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """الشكل الذي تقبله دالة challenge_import_test (جاهز كما هو)."""
    return [dict(q) for q in questions]


def strip_answers(question_row: Dict[str, Any]) -> Dict[str, Any]:
    """نسخة آمنة للواجهة: بلا الإجابة الصحيحة ولا الشرح."""
    return {k: v for k, v in question_row.items() if k not in ("correct_option_id", "explanation")}


def paginate(items: List[Any], page: int, size: int):
    """(عناصر الصفحة, الصفحة المصحّحة, عدد الصفحات)."""
    pages = max(1, (len(items) + size - 1) // size)
    page = min(max(page, 0), pages - 1)
    return items[page * size:(page + 1) * size], page, pages
