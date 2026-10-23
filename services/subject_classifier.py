# services/subject_classifier.py
"""Unified subject classification using the configured detection slot.

Native PDF models receive full files. Vision-only fallback processes at most
three rasterized pages at once and combines validated classifications. Failed
classification results are not cached. Single-image endpoints deliberately sample
the first image/page and never estimate whole-document question counts from it.
"""

import asyncio
from typing import Any, List, Literal, Optional

from pydantic import BaseModel, Field

from constants import (
    MATH_DETECTION_TIMEOUT,
    MAX_QUESTIONS_TO_GENERATE,
    MIN_QUESTIONS_TO_GENERATE,
    SUBJECT_ENGLISH,
    SUBJECT_FRENCH,
    SUBJECT_MATH,
    SUBJECT_OTHER,
    SUBJECT_QUIZ_SOLVED,
    SUBJECT_QUIZ_UNSOLVED,
    SYSTEM_PROMPT_CLASSIFY_SUBJECT,
)
from logger import get_logger, log_warning
from services.detection_common import IMAGE_EXTENSIONS, build_text_sample
from config import redis_client
from helpers.supabase_helper import get_classification_lock

logger = get_logger(__name__)

# 🆕 (تحسين أداء/تكلفة): كاش تصنيف بمفتاح file_hash - لو نفس الملف بالضبط (نفس الهاش)
# يُرفع من طالب آخر (أو نفس الطالب مرة ثانية)، مادته العلمية ثابتة منطقياً بغض النظر
# عن هوية الرافع، فلا داعٍ لإعادة استدعاء Gemini بالكامل من الصفر (زمن استجابة + تكلفة
# API). Redis (نفس البنية التحتية المستخدمة أصلاً لحالات الـ Poll) بدل جدول Supabase
# جديد - تخزين مؤقت بحت، لا حاجة لديمومة دائمة أو استعلامات SQL عليه. TTL طويل (7 أيام)
# لأنه غير مرتبط بزمن انتهاء صلاحية ملفات Gemini Files API (تلك تنتهي خلال ~48 ساعة أصلاً
# ولا علاقة لها بنتيجة التصنيف المخزَّنة هنا، وهي فقط subject/suggested_types/إلخ نصية).
CLASSIFY_CACHE_TTL_SECONDS = 7 * 24 * 3600
# v2 could associate a retained text input with a newly uploaded file's hash.
# Ignore those temporary results without deleting votes, locks or saved quizzes.
_CLASSIFY_CACHE_PREFIX = "subject_classify:v5:"

# 🆕 SUBJECT_FRENCH أُضيف بنفس معاملة SUBJECT_ENGLISH بالضبط (راجع constants.py).
_VALID_SUBJECTS = {
    SUBJECT_MATH, SUBJECT_ENGLISH, SUBJECT_FRENCH, SUBJECT_QUIZ_SOLVED, SUBJECT_QUIZ_UNSOLVED, SUBJECT_OTHER,
}

# 🆕 نفس حد helpers.gemini_helper.INLINE_DATA_SIZE_THRESHOLD (15MB): إرسال الملف كاملاً
# Inline ضمن الطلب لو كان بهذا الحجم أو أقل (أسرع، بدون Round-trip رفع منفصل)، وإلا
# نلجأ لـ Files API (نفس المسار المستخدم بالتوليد الفعلي للملفات الكبيرة/الألبومات).
DETECTION_INLINE_SIZE_THRESHOLD = 15 * 1024 * 1024


class SubjectClassification(BaseModel):
    # 🩹 BUGFIX: كانت description تقول "one of: math, english, other" فقط - نسيت
    # تحديثها لما أُضيف SUBJECT_FRENCH، رغم إن SYSTEM_PROMPT_CLASSIFY_SUBJECT وكل
    # الطبقات الأخرى كانت محدّثة بالفعل. بما إن هالـ description بتتحول تلقائياً
    # لجزء من الـ JSON Schema المُرسل فعلياً لـ Gemini (response_schema=...)، كانت
    # بتناقض الـ system prompt وتمنع النموذج عملياً من إرجاع "french" أبداً.
    # استخدام Literal بدل str+description بيضمن القيمة من مستوى الـ schema نفسه
    # (تعليمة صريحة وملزمة للنموذج) بدل الاعتماد فقط على نص وصفي حر قد يُنسى تحديثه
    # بالمستقبل لو أُضيفت مادة جديدة.
    # 🆕 quiz_solved/quiz_unsolved أُضيفا بنفس منطق SUBJECT_FRENCH أعلاه (راجع البُقشة
    # التوضيحية أعلى BUGFIX 🩹 بهذا الملف): Literal صريح يضمن القيمة من مستوى الـ
    # schema نفسه المُرسل فعلياً لـ Gemini، لا يكفي تحديث SYSTEM_PROMPT_CLASSIFY_SUBJECT وحده.
    subject: Literal["math", "english", "french", "quiz_solved", "quiz_unsolved", "other"] = Field(
        description="one of: math, english, french, quiz_solved, quiz_unsolved, other"
    )
    suggested_types: List[str] = Field(default_factory=list, max_length=4)
    # 🆕 تقدير آلي لعدد الأسئلة الفعلي الموجود بالمستند - يُملأ فقط عندما subject يساوي
    # quiz_solved أو quiz_unsolved (اختبار جاهز الصياغة أصلاً)، ويُستخدم لاحقاً لعرض شاشة
    # تأكيد عدد/تكلفة جاهزة بدل تخيير الطالب بأزرار عدد عشوائية (5/10/15/20) لا علاقة لها
    # بالعدد الحقيقي بمستند اختبار جاهز أصلاً. راجع handlers/files.py.show_question_count_screen.
    question_count: Optional[int] = Field(
        default=None,
        description=(
            "approximate count of actual questions found in the document; only set when "
            "subject is quiz_solved or quiz_unsolved, otherwise null"
        ),
    )
    # 🆕 المادة العلمية الفعلية للمحتوى، بمعزل عن كونه اختباراً جاهزاً أم لا - تُملأ فقط
    # عندما subject يساوي quiz_solved أو quiz_unsolved (عندها subject نفسه يصف "شكل"
    # المحتوى فقط لا مادته العلمية - راجع SYSTEM_PROMPT_CLASSIFY_SUBJECT). تُستخدم لاحقاً
    # (services/quiz_service.py) لتفعيل نمط الكويز المصوّر LaTeX أو موجّه اللغة الصحيح حتى
    # للاختبارات الجاهزة، بدل معاملتها جميعاً كمادة "عامة" دائماً بغض النظر عن مضمونها
    # الفعلي. null لأي subject آخر (math/english/french/other تحمل مادتها أصلاً بقيمة subject نفسها).
    content_subject: Optional[Literal["math", "english", "french", "other"]] = Field(
        default=None,
        description=(
            "the actual academic subject of the content (math/english/french/other), filled "
            "only when subject is quiz_solved or quiz_unsolved, otherwise null"
        ),
    )


class SubjectClassificationResponse(SubjectClassification):
    """Require the academic subject on the wire; tolerate legacy cached snapshots separately."""
    content_subject: Literal["math", "english", "french", "other"] = Field(
        description="REQUIRED academic subject, independent of exam format. English language lessons with reading, vocabulary or comprehension are english even when the passage is about science."
    )


async def _get_cached_classification(file_hash: Optional[str]) -> Optional[SubjectClassification]:
    """يرجع نتيجة تصنيف محفوظة سابقاً لنفس file_hash إن وُجدت، وإلا None بصمت (أي خطأ
    اتصال بـ Redis لا يوقف تدفق التصنيف العادي - فشل آمن دائماً)."""
    if not file_hash:
        return None
    try:
        cached_raw = await redis_client.get(f"{_CLASSIFY_CACHE_PREFIX}{file_hash}")
        if cached_raw:
            return SubjectClassification.model_validate_json(cached_raw)
    except Exception as exc:
        log_warning(logger, f"[subject_classifier] Cache lookup failed for {file_hash}: {exc}")
    return None


async def _store_cached_classification(file_hash: Optional[str], result: SubjectClassification) -> None:
    if not file_hash:
        return
    try:
        await redis_client.set(
            f"{_CLASSIFY_CACHE_PREFIX}{file_hash}", result.model_dump_json(), ex=CLASSIFY_CACHE_TTL_SECONDS,
        )
    except Exception as exc:
        log_warning(logger, f"[subject_classifier] Cache store failed for {file_hash}: {exc}")


def _fallback() -> SubjectClassification:
    """التصنيف الافتراضي الآمن عند أي خطأ أو انقطاع - يعامل المحتوى كمادة عامة بلا تخصيص."""
    return SubjectClassification(subject=SUBJECT_OTHER, suggested_types=[])


def _normalize(result: Optional[SubjectClassification]) -> SubjectClassification:
    """يتحقق من صحة قيمة subject الراجعة من النموذج ويعيد للقيمة الآمنة لو كانت غير متوقعة."""
    if result is None:
        return _fallback()
    subject = (result.subject or "").strip().lower()
    if subject not in _VALID_SUBJECTS:
        log_warning(logger, f"[subject_classifier] Unexpected subject value '{subject}', defaulting to 'other'")
        subject = SUBJECT_OTHER
    # Academic subject remains authoritative for ordinary teaching material.
    academic_subject = result.content_subject
    if (subject not in (SUBJECT_QUIZ_SOLVED, SUBJECT_QUIZ_UNSOLVED)
            and academic_subject in (SUBJECT_MATH, SUBJECT_ENGLISH, SUBJECT_FRENCH)):
        subject = academic_subject
    suggested = [s.strip() for s in (result.suggested_types or []) if s and s.strip()][:4]

    # 🆕 question_count منطقي فقط لاختبار جاهز (محلول/غير محلول) - لأي مادة أخرى يبقى null
    # حتى لو أرجع النموذج قيمة عن طريق الخطأ. القيمة المُرجَعة تُقيَّد ضمن نفس مجال العدد
    # المسموح للتوليد (VALID_QUESTIONS_RANGE) لتفادي رقم شاذ (صفر/سالب/ضخم جداً) يصل لاحقاً
    # لحساب التكلفة أو موجّه التوليد بدون أي تحقق آخر بمنتصف الطريق.
    question_count: Optional[int] = None
    if subject in (SUBJECT_QUIZ_SOLVED, SUBJECT_QUIZ_UNSOLVED):
        try:
            raw_count = int(result.question_count) if result.question_count else None
        except (TypeError, ValueError):
            raw_count = None
        if raw_count is not None:
            question_count = max(MIN_QUESTIONS_TO_GENERATE, min(raw_count, MAX_QUESTIONS_TO_GENERATE))

    # 🆕 content_subject منطقي فقط لاختبار جاهز (محلول/غير محلول) - لأي subject آخر يبقى
    # null دائماً (مادته العلمية محمولة أصلاً بقيمة subject نفسها). قيمة غير متوقعة راجعة
    # من النموذج تُعامَل كـ "other" أماناً (نفس منطق subject أعلاه) بدل تعطيل الفحص بالكامل.
    content_subject: Optional[str] = None
    if subject in (SUBJECT_QUIZ_SOLVED, SUBJECT_QUIZ_UNSOLVED):
        raw_content_subject = (result.content_subject or "").strip().lower() if result.content_subject else ""
        if raw_content_subject in (SUBJECT_MATH, SUBJECT_ENGLISH, SUBJECT_FRENCH, SUBJECT_OTHER):
            content_subject = raw_content_subject
        else:
            content_subject = SUBJECT_OTHER

    return SubjectClassification(
        subject=subject, suggested_types=suggested, question_count=question_count, content_subject=content_subject,
    )


class ClassificationUnavailable(RuntimeError):
    pass


def _merge_classification_results(results):
    # Blank cover pages must not outvote real content. Preserve quiz format and
    # its underlying academic subject; counts cover every page batch.
    quizzes = [r for r in results if r.subject in (SUBJECT_QUIZ_SOLVED, SUBJECT_QUIZ_UNSOLVED)]
    useful = quizzes or [r for r in results if r.subject != SUBJECT_OTHER] or results
    from collections import Counter
    subject = Counter(r.subject for r in useful).most_common(1)[0][0]
    chosen = next(r for r in useful if r.subject == subject).model_copy(deep=True)
    if quizzes:
        counts = [r.question_count for r in quizzes if r.question_count is not None]
        chosen.question_count = sum(counts) if counts else None
        academics = [r.content_subject for r in quizzes if r.content_subject not in (None, SUBJECT_OTHER)]
        if academics:
            chosen.content_subject = Counter(academics).most_common(1)[0][0]
    result = _normalize(chosen)
    return result


async def _dispatch_classification(*, contents=None, paths=None):
    from services.ai.contracts import AIRequest, Attachment
    from services.ai.runtime import execute
    parts = (tuple(Attachment.from_path(path) for path in paths)
             if paths else tuple(contents or []))
    result = await execute(AIRequest(parts, SubjectClassificationResponse, allow_image_sampling=True,
                                     system_instruction=SYSTEM_PROMPT_CLASSIFY_SUBJECT),
                           slot="detection", mode="detection",
                           timeout=MATH_DETECTION_TIMEOUT)
    if result:
        if getattr(result, "sampled", False):
            result.parsed.question_count = None
        return _normalize(result.parsed)
    # Vision-only models can read PDF pages as images. Keep three pages in
    # memory at a time, matching the previous bounded Groq classifier.
    if paths and any(path.lower().endswith(".pdf") for path in paths):
        from services.ai.media import page_batches
        results = []
        async for batch in page_batches(paths):
            prompt = (SYSTEM_PROMPT_CLASSIFY_SUBJECT +
                      "\nThese pages belong to one document. Count only questions STARTING on these pages.")
            result = await execute(AIRequest(tuple(batch), SubjectClassificationResponse, allow_image_sampling=True,
                                             system_instruction=prompt),
                slot="detection", mode="detection", timeout=MATH_DETECTION_TIMEOUT)
            if result:
                if getattr(result, "sampled", False):
                    result.parsed.question_count = None
                    return _normalize(result.parsed)
                results.append(result.parsed)
            else:
                raise ClassificationUnavailable("Page classification failed")
        if results:
            return _merge_classification_results(results)
    raise ClassificationUnavailable("No configured classifier succeeded")


async def _classify(contents):
    return await _dispatch_classification(contents=contents)


async def _classify_media(file_paths):
    return await _dispatch_classification(paths=file_paths)


async def _classify_text(pure_text: str) -> SubjectClassification:
    if not pure_text or not pure_text.strip():
        return _fallback()
    sample = build_text_sample(pure_text)
    return await _classify([f"[عينة النص]:\n{sample}"])


async def get_pending_classification_snapshot(file_hash: Optional[str]) -> Optional[SubjectClassification]:
    """🆕 يرجع اللقطة الكاملة (subject + suggested_types + question_count + content_subject)
    المخزَّنة حالياً بكاش Redis غير المؤكَّد لهذا الملف، إن وُجدت ولا زالت ضمن TTL - وإلا
    None. تُستخدم من handlers/files.py وقت تسجيل صوت تحقّق (راجع migration_classification_
    votes.sql) لالتقاط لقطة كاملة تُحفَظ مع التصويت، بدل الاكتفاء بقيمة subject فقط، بحيث
    لو فاز هذا التصنيف بالتحقق المجتمعي لاحقاً، يُثبَّت بكامل حقوله لا subject فقط."""
    return await _get_cached_classification(file_hash)


async def classify_subject(
    file_paths: Optional[List[str]], pure_text: Optional[str], file_hash: Optional[str] = None,
) -> SubjectClassification:
    """
    نقطة الدخول الموحّدة: تستدعى مرة واحدة فقط مبكراً من handlers/files.py (بنفس توقيت
    فحص الإنجليزي القديم، قبل عرض شاشة اختيار نوع/عدد الأسئلة). النتيجة تُخزَّن بحالة
    الـ FSM وتُعاد قراءتها لاحقاً بـ services/quiz_service.py وقت التوليد الفعلي بدل
    إعادة تنفيذ أي فحص إضافي (math_detector/english_detector القديمين لم يعودا يُستدعيان
    على المسار الساخن). أي خطأ غير متوقع يُعتبر تلقائياً subject="other" (فشل آمن).

    🆕 file_hash اختياري (مُمرَّر من handlers/files.py، محسوب مسبقاً لأغراض الكاش أصلاً)
    - لو وُجدت نتيجة تصنيف محفوظة لنفس الهاش، تُعاد مباشرة بلا أي استدعاء Gemini (نفس
    الملف بالضبط، مادته العلمية ثابتة بغض النظر عن هوية الرافع أو توقيت الرفع).

    🆕 التحقق المجتمعي (services/supabase_helper.get_classification_lock): تصنيف ثبَّته
    3 طلاب مختلفين على الأقل (راجع migration_classification_votes.sql) له أولوية مطلقة -
    يُتحقق منه أولاً حتى قبل كاش Redis أدناه، ولا يُعاد فحصه أبداً بعدها (لا TTL، دائم).
    كاش Redis (_get_cached_classification) يبقى كما هو لتسريع الاستجابة للملفات التي لم
    تصل بعد لعتبة التحقق المجتمعي، دون أي تعديل على سلوكه أو مدة صلاحيته.
    """
    locked = await get_classification_lock(file_hash) if file_hash else None
    if locked is not None:
        try:
            result = SubjectClassification.model_validate(locked["classification_data"])
            logger.info("SUBJECT_CLASSIFICATION source=community_lock subject=%s content_subject=%s", result.subject, result.content_subject)
            return result
        except Exception as exc:
            log_warning(logger, f"[subject_classifier] Failed to parse locked classification for {file_hash}: {exc}")
            # لا نمنع التدفق العادي لو كانت بيانات التثبيت تالفة لأي سبب - نكمل كالمعتاد (فشل آمن)

    cached = await _get_cached_classification(file_hash)
    if cached is not None:
        logger.info("SUBJECT_CLASSIFICATION source=redis subject=%s content_subject=%s", cached.subject, cached.content_subject)
        return cached
    try:
        # Explicit media takes precedence even for pre-existing dirty FSM data.
        # Office extraction supplies only text, with no media paths, upstream.
        if file_paths:
            result = await _classify_media(file_paths)
            source = "media"
        elif pure_text:
            result = await _classify_text(pure_text)
            source = "text"
        else:
            return _fallback()
    except Exception as exc:
        log_warning(logger, f"[subject_classifier] Unexpected error during classification, defaulting to 'other': {exc}")
        return _fallback()
    await _store_cached_classification(file_hash, result)
    logger.info("SUBJECT_CLASSIFICATION source=%s subject=%s content_subject=%s", source, result.subject, result.content_subject)
    return result

