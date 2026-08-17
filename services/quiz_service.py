# services/quiz_service.py
import asyncio
import os
import random
from typing import Any, Dict, Tuple, Optional, List

from constants import (
    MAX_LIMIT_PAGES, MAX_LIMIT_QUESTIONS, MAX_STANDARD_PAGES, MAX_STANDARD_QUESTIONS,
    SUBJECT_MATH, SUBJECT_OTHER, DIFFICULTY_MEDIUM, DIFFICULTY_LABELS_AR,
    QUESTION_TYPE_GENERAL, QUESTION_TYPE_CUSTOM, QUESTION_TYPE_OPTIONS,
)
from gemini_helper import generate_quiz_smart
from logger import get_logger, log_error
from services.file_service import extract_office_text_if_needed
from supabase_helper import (
    get_file_quizzes,
    refund_user_points,
    save_file_quiz_multiple,
    log_usage_event,
)

logger = get_logger(__name__)

# AI-NOTE (memory/CPU guard): كل استدعاء لهذا التدفق قد يشمل استخراج نص من مستند أوفيس،
# تحويل صفحة PDF لصورة (PyMuPDF)، واستدعاءات Gemini الثقيلة. بدون سقف تزامن، عدة طلبات
# ملفات كبيرة بنفس اللحظة ممكن تستهلك الذاكرة/CPU بشكل تراكمي وتُبطئ كل الطلبات المتزامنة.
# Semaphore(2) يحصر معالجة الملفات الثقيلة بحد أقصى عمليتين متزامنتين فقط.
_HEAVY_PROCESSING_SEMAPHORE = asyncio.Semaphore(2)

def determine_execution_mode(items: int, questions: int, cached: bool = False) -> str:
    """تحديد وضع التنفيذ (عادي، متقدم، أو كاش)"""
    if cached: return "Cached"
    if items > MAX_LIMIT_PAGES or questions > MAX_LIMIT_QUESTIONS: return "Super-Processing"
    if items > MAX_STANDARD_PAGES or questions > MAX_STANDARD_QUESTIONS: return "Over-Limit"
    return "Standard"

MODE_LABELS_AR = {
    "Standard": "⚡ عادي",
    "Over-Limit": "📈 موسّع",
    "Super-Processing": "🚀 معالجة فائقة (ملف كبير)",
    "Cached": "🗃️ من الكاش",
}

def build_transparency_text(
    items: int, questions: int, mode: str, cost: float,
    difficulty: Optional[str] = None, question_type_label: Optional[str] = None,
) -> str:
    """رسالة الشفافية المالية لعرض تفاصيل الخصم.
    🆕 difficulty/question_type_label اختياريان (لعرض تفاصيل الاختيار عند تفعيل شاشة
    نوع/صعوبة الأسئلة لاحقاً) - لا يؤثران على الاستدعاءات الحالية التي لا تمررهما."""
    mode_label = MODE_LABELS_AR.get(mode, mode)
    lines = [
        "📋 <b>تفاصيل التنفيذ والشفافية المالية</b>\n",
        f"• العناصر/الصفحات: <code>{items}</code>",
        f"• الأسئلة المطلوبة: <code>{questions}</code>",
    ]
    if question_type_label:
        lines.append(f"• نوع الأسئلة: <code>{question_type_label}</code>")
    if difficulty:
        lines.append(f"• الصعوبة: <code>{DIFFICULTY_LABELS_AR.get(difficulty, difficulty)}</code>")
    lines.append(f"• وضع المعالجة: <code>{mode_label}</code>")
    lines.append(f"• تكلفة العملية: <b>{cost:.2f} نقطة</b>")
    return "\n".join(lines)

def _shuffle_question_options(question: Dict[str, Any]) -> Dict[str, Any]:
    """
    يخلط ترتيب الخيارات عشوائياً ويحدّث correct_option_id ليطابق الموضع الجديد.
    هذا ضروري لأن Gemini غالباً ما ينحاز لوضع الإجابة الصحيحة في نفس الترتيب
    (عادة الخيار الأول) بشكل متكرر، فبدون خلط تصبح كل الاختبارات المولّدة
    إجابتها الصحيحة رقم 1 دائماً.
    """
    options = question.get("options") or []
    try:
        correct_id = int(question.get("correct_option_id", 0))
    except (TypeError, ValueError):
        return question

    if not options or not (0 <= correct_id < len(options)):
        return question

    indices = list(range(len(options)))
    random.shuffle(indices)

    question["options"] = [options[i] for i in indices]
    question["correct_option_id"] = indices.index(correct_id)
    return question


def shuffle_quiz_options(quiz_data: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """يطبّق خلط الخيارات على كل أسئلة الكويز المولّد."""
    return [_shuffle_question_options(q) for q in quiz_data]


async def refund_user_on_failure(user_id: int, data: Dict[str, Any]) -> None:
    """إعادة النقاط تلقائياً في حال فشل التوليد"""
    cost = float(data.get("debited_cost") or 0)
    if cost > 0:
        await refund_user_points(user_id, cost)


def build_question_type_label(
    subject_type: str, question_type: str, custom_text: Optional[str] = None,
    suggested_types: Optional[List[str]] = None,
) -> str:
    """
    🆕 يبني نص التسمية العربية القصيرة المخزّنة بعمود quizzes.question_type_label -
    تُعرض للطالب بجانب كل كويز مخزّن بالكاش (راجع migration_quiz_options.sql).
    يدعم أيضاً أنواع "other_<index>" (اقتراحات AI الديناميكية للمواد غير المصنّفة -
    راجع keyboards.get_quiz_type_keyboard) بقراءة النص الفعلي من suggested_types.
    """
    if question_type == QUESTION_TYPE_CUSTOM and custom_text:
        return custom_text.strip()[:80]
    if question_type.startswith("other_") and suggested_types:
        try:
            idx = int(question_type.split("_", 1)[1])
            if 0 <= idx < len(suggested_types):
                return suggested_types[idx]
        except (ValueError, IndexError):
            pass
    for value, label in QUESTION_TYPE_OPTIONS.get(subject_type, []):
        if value == question_type:
            return label
    return "🔀 متنوع (بدون تخصيص)"


def resolve_generation_question_type_text(
    question_type: str, custom_question_type_text: Optional[str], suggested_types: Optional[List[str]] = None,
) -> Optional[str]:
    """
    🆕 يحوّل اختيار "other_<index>" (اقتراح AI ديناميكي) إلى نص حر مكافئ لتفضيل
    مخصص، لأن helpers/gemini_helper.py لا يعرف قيم "other_i" (غير ثابتة مسبقاً
    بـ constants.QUESTION_TYPE_PROMPT_INSTRUCTIONS) - يُمرَّر الناتج كـ
    custom_question_type_text لـ generate_quiz_smart بدل تركه يسقط للتعليمة
    العامة المحايدة (فقدان اختيار الطالب الفعلي).
    """
    if custom_question_type_text:
        return custom_question_type_text
    if question_type.startswith("other_") and suggested_types:
        try:
            idx = int(question_type.split("_", 1)[1])
            if 0 <= idx < len(suggested_types):
                return suggested_types[idx]
        except (ValueError, IndexError):
            pass
    return None


def combo_quiz_count(
    cached_quizzes: List[Dict[str, Any]], subject_type: str, question_type: str, difficulty: str
) -> int:
    """
    🆕 يحسب عدد الكويزات المخزّنة لنفس تركيبة (نوع مادة × نوع أسئلة × صعوبة) تحديداً،
    بدل عدّ كل الكويزات المخزّنة للملف بغض النظر عن نوعها كما كان سابقاً. هذا يسمح
    بسقف مستقل لكل تركيبة (MAX_FILE_QUIZZES_LIMIT لكل تركيبة على حدة) بدل سقف
    مشترك واحد يُستهلك بسرعة من أي تركيبة. البيانات محمّلة أصلاً بالذاكرة من
    get_file_quizzes (نداء واحد لقاعدة البيانات)، فهذا الفلتر محلي بدون أي تكلفة إضافية.
    """
    return sum(
        1 for q in cached_quizzes
        if q.get("subject_type", SUBJECT_OTHER) == subject_type
        and q.get("question_type", QUESTION_TYPE_GENERAL) == question_type
        and q.get("difficulty", DIFFICULTY_MEDIUM) == difficulty
    )

async def execute_quiz_generation_workflow(
    user_id: int,
    data: Dict[str, Any],
    count: int,
    status_message: Any
) -> Tuple[Optional[List[Dict[str, Any]]], Optional[str], str]:
    """
    التدفق التنفيذي المركزي لتوليد الكويز:
    يستخرج النص، يجلب الكويزات السابقة لمنع التكرار، يستدعي الذكاء الاصطناعي، ويحفظ في الكاش.
    """
    async with _HEAVY_PROCESSING_SEMAPHORE:
        is_media = data.get("input_type") == "media"
        file_hash = data.get("file_hash")
        file_paths = data.get("file_paths", []) or []
        pure_text = data.get("pure_text")

        # 1. معالجة مستندات أوفيس
        # 🆕 إذا كان النص قد استُخرج مسبقاً في handlers/files.py (أثناء الفحص اللغوي المبكر
        # قبل شاشة عدد الأسئلة)، نعيد استخدامه مباشرة بدل استخراجه من جديد من نفس الملف.
        cached_office_text = data.get("cached_office_text")
        if is_media and file_paths:
            first_path = file_paths[0]
            ext = os.path.splitext(first_path)[1].lower()
            if ext in [".docx", ".doc", ".pptx", ".ppt", ".txt"]:
                if cached_office_text:
                    extracted_text, is_valid = cached_office_text, True
                else:
                    extracted_text, is_valid = await extract_office_text_if_needed(first_path)
                if is_valid and extracted_text:
                    pure_text = extracted_text
                    is_media = False
                else:
                    return None, None, "unreadable_office"

        # 2. جلب الأسئلة السابقة لمنع التكرار
        previous_questions = []
        existing_uuids = set()
        if file_hash:
            old_quizzes = await get_file_quizzes(file_hash)
            for qz in old_quizzes:
                existing_uuids.add(str(qz["id"]))
                if "quiz_data" in qz and isinstance(qz["quiz_data"], list):
                    previous_questions.extend(qz["quiz_data"])

        # 2.5 🆕 نتيجة التصنيف الموحّد (services/subject_classifier.py) - نُفِّذت مرة واحدة
        #     فقط مبكراً بـ handlers/files.py وخُزِّنت بحالة الـ FSM، فبدل إعادة فحص
        #     المحتوى من جديد هنا (كما كان math_detector يفعل سابقاً على مساره الخاص)،
        #     نقرأ النتيجة الجاهزة مباشرة. فشل آمن: تخلّف subject_type يُعامل كـ "other".
        subject_type = data.get("subject_type", SUBJECT_OTHER)
        is_math_mode = subject_type == SUBJECT_MATH

        # 2.6 نمط الترجمة (اختيار الطالب "مترجمة/بدون ترجمة" عند اكتشاف محتوى إنجليزي فور
        #     الاستقبال في handlers/files.py، محفوظ مسبقاً بحالة الـ FSM). يُتجاهل إذا كان
        #     المحتوى رياضياً (is_math_mode) لأن نمط الكويز المصوّر LaTeX له موجّه خاص به
        #     منفصل تماماً ولا يدعم الترجمة المزدوجة داخل نفس الحقل.
        english_mode = None if is_math_mode else data.get("english_mode")

        # 2.7 🆕 نوع الأسئلة والصعوبة المختاران من الطالب عبر شاشة الخيارات
        #     (handlers/quiz_options.py). قيمة question_type قد تكون: قيمة ثابتة من
        #     القائمة الجاهزة، "general" (افتراضي)، "custom" (نص حر)، أو "other_<index>"
        #     (اقتراح AI ديناميكي لمادة غير مصنّفة - يُحوَّل هنا لنص فعلي).
        question_type = data.get("question_type", QUESTION_TYPE_GENERAL)
        suggested_types = data.get("suggested_question_types", [])
        custom_question_type_text = resolve_generation_question_type_text(
            question_type, data.get("custom_question_type_text"), suggested_types
        )
        difficulty = data.get("difficulty", DIFFICULTY_MEDIUM)

        # 3. استدعى محرك AI
        quiz_data = await generate_quiz_smart(
            file_paths=file_paths if is_media else None,
            pure_text=pure_text if not is_media else None,
            count=count,
            skip_cache=True,
            file_hash=file_hash,
            status_message=status_message,
            previous_questions=previous_questions if previous_questions else None,
            is_math_mode=is_math_mode,
            english_mode=english_mode,
            difficulty=difficulty,
            question_type=question_type,
            custom_question_type_text=custom_question_type_text,
        )

        if not quiz_data:
            return None, None, "ai_failed"

        # 3.5 خلط ترتيب الخيارات لتفادي انحياز الذكاء الاصطناعي لوضع
        #     الإجابة الصحيحة دائماً في نفس الموضع (غالباً الخيار الأول)
        quiz_data = shuffle_quiz_options(quiz_data)

        # 3.6 تعليم كل سؤال بنمط الكويز المصوّر LaTeX ليتحول التنفيذ لاحقاً
        #     (services/quiz_engine.py) لمسار الصورة + Poll الحروف بدل النص العادي
        if is_math_mode:
            for question in quiz_data:
                question["is_math"] = True

        # 4. حفظ الكاش لملفات أوفيس والنصوص
        # 🆕 نخزّن الآن تركيبة (subject_type, question_type, difficulty) كاملة مع كل
        # كويز - راجع migration_quiz_options.sql - ليُعرض بتفاصيله الصحيحة لاحقاً
        # ويُحسب بشكل صحيح ضمن سقف تركيبته المستقلة (combo_quiz_count أعلاه).
        if file_hash and quiz_data:
            await save_file_quiz_multiple(
                file_hash=file_hash,
                creator_id=user_id,
                source_title=data.get("source_title", "كويز من مستند"),
                quiz_data=quiz_data,
                total_tokens=0,
                is_math_quiz=is_math_mode,
                subject_type=subject_type,
                question_type=question_type,
                question_type_label=build_question_type_label(subject_type, question_type, custom_question_type_text, suggested_types),
                difficulty=difficulty,
            )

        # 5. استخراج الـ UUID للكويز الجديد
        new_quiz_id = None
        if file_hash:
            await asyncio.sleep(0.5)
            updated_quizzes = await get_file_quizzes(file_hash)
            for uq in updated_quizzes:
                if str(uq["id"]) not in existing_uuids:
                    new_quiz_id = str(uq["id"])
                    break

        return quiz_data, new_quiz_id, ""