"""
helpers/gemini_helper/quiz_generator.py
نقطة الدخول العامة generate_quiz_smart (اختيار الموجّه والمسار والتتبع).
"""

from typing import Any, Dict, List, Optional
import asyncio
import time

from constants import (
    MAX_LIMIT_PAGES,
    MAX_STANDARD_PAGES,
    MAX_STANDARD_QUESTIONS,
    DIFFICULTY_PROGRESSIVE,
    SUPER_IMAGE_BATCH_THRESHOLD,
    OPTION_COUNT,
    SYSTEM_PROMPT_GENERATE_QUESTIONS,
    SYSTEM_PROMPT_EXTRACT_READY_QUIZ,
    SYSTEM_PROMPT_EXTRACT_READY_MATH_FORMAT,
    SYSTEM_PROMPT_GENERATE_MATH_QUESTIONS,
    SYSTEM_PROMPT_GENERATE_SIMILAR_MATH_QUESTIONS, QUESTION_TYPE_SIMILAR_MATH,
    SYSTEM_PROMPT_GENERATE_ENGLISH_PLAIN_QUESTIONS,
    SYSTEM_PROMPT_GENERATE_ENGLISH_TRANSLATED_QUESTIONS,
    SYSTEM_PROMPT_GENERATE_FRENCH_PLAIN_QUESTIONS,
    SYSTEM_PROMPT_GENERATE_FRENCH_TRANSLATED_QUESTIONS,
    SUBJECT_FRENCH,
    MSG_PREVIOUS_QUESTIONS_INSTRUCTION,
)
from helpers.gemini_helper.state import (
    _groq_skip_reason_var,
    _last_generation_metadata_var,
    _last_model_used_var,
    _last_token_usage_var,
)
from helpers.gemini_helper.media import get_pdf_page_count_sync
from helpers.gemini_helper.loading import _loading_animation
from helpers.gemini_helper.generation import (
    _generate_regular,
    _generate_super_images,
    _generate_pdf_with_context,
)
from helpers.gemini_helper.text_generation import (
    _generate_text_quiz,
    _generate_text_quiz_with_gemini,
)
from helpers.gemini_helper.prompts import (
    _resolve_difficulty_instruction,
    _resolve_question_type_instruction,
)


# ==============================================================================
# MAIN PUBLIC API ENTRYPOINT
# ==============================================================================
async def generate_quiz_smart(
    file_paths: Optional[List[str]] = None,
    pure_text: Optional[str] = None,
    count: int = 0,
    skip_cache: bool = False,
    file_hash: Optional[str] = None,
    status_message: Optional[Any] = None,
    previous_questions: Optional[List[Dict[str, Any]]] = None,
    is_math_mode: bool = False,
    english_mode: Optional[str] = None,
    content_language: Optional[str] = None,
    difficulty: Optional[str] = None,
    question_type: Optional[str] = None,
    custom_question_type_text: Optional[str] = None,
    quiz_extraction_instruction: Optional[str] = None,
) -> Optional[List[Dict[str, Any]]]:
    """
    الدالة الرئيسية المستدعاة من قبل البوت لتوليد الاختبار الذكي.
    تتولى إدارة الـ Cache، توجيه الطلبات للمسار المناسب، وتنظيف مهام الرسائل التفاعلية.

    is_math_mode: عندما تكون True (بعد التصنيف الموحّد عبر services.subject_classifier)،
    يُستبدل موجّه التوليد بنسخة LaTeX المخصصة لـ"نمط الكويز المصوّر" بدل الموجّه العادي.

    english_mode: عندما يكون "translated" أو "plain" (بعد اكتشاف محتوى إنجليزي/فرنسي واختيار
    الطالب عبر handlers/files.py)، يُستبدل موجّه التوليد بالنسخة المناسبة. يُتجاهل كلياً إذا
    كان is_math_mode=True (الأولوية دائماً لنمط الكويز المصوّر الرياضي).

    content_language: قيمة SUBJECT_ENGLISH أو SUBJECT_FRENCH (أو None لأي مادة أخرى) - تُحدَّد
    مع english_mode أعلاه أي موجّهات التوليد الأربعة (إنجليزي مترجم/عادي أو فرنسي مترجم/عادي)
    يُستخدم فعلياً.

    🆕 difficulty / question_type / custom_question_type_text: تُحقن كنصوص تعليمات إضافية
    ({difficulty_instruction} و{question_type_instruction}) بكل الموجّهات الأربعة على حد
    سواء، بغض النظر عن المسار (رياضي/إنجليزي/عادي) - راجع _resolve_difficulty_instruction
    و_resolve_question_type_instruction أعلاه لمنطق التحويل.

    🆕 quiz_extraction_instruction: نص جاهز (يُبنى بـ services/quiz_service.py عبر
    resolve_quiz_extraction_instruction) يُلحَق بنهاية الموجّه الأساسي المختار فقط عندما
    يكون المحتوى اختباراً جاهزاً (subject_type=quiz_solved/quiz_unsolved) - يحوّل مهمة
    النموذج من "توليد أسئلة جديدة" إلى "استخراج أسئلة موجودة أصلاً بالمستند" (مع اعتماد
    إجاباتها كما هي، أو حلّها بالذكاء الاصطناعي، حسب اختيار الطالب). None لأي مادة أخرى.

    🆕 يُنفَّذ الآن كل التوليد الفعلي (ملفات أو نص) عبر سلسلة أولوية النماذج الكاملة
    (MODELS_CASCADE) مع Round-Robin على المفاتيح، دون أي تغيير على توقيع أو سلوك هذه
    الدالة العامة نفسها.
    """
    stop_event = asyncio.Event()
    animation_task = asyncio.create_task(_loading_animation(status_message, stop_event)) if status_message else None

    # 🆕 توقيت التوليد الكامل (يشمل استخراج النص/بناء الطلب/كل الـ cascade) - يُقرأ لاحقاً
    # عبر get_last_generation_metadata() من services/quiz_service.py لتغذية لوحة الأدمن
    # الجديدة (وقت توليد كل كويز + الموديل المستخدم). نصفّر الـ ContextVar الخاص بالموديل
    # الفائز أولاً لضمان عدم قراءة نتيجة توليد سابق بنفس الـ Task لو حصل فشل جزئي هنا.
    _generation_start_time = time.monotonic()
    _last_model_used_var.set(None)
    _last_token_usage_var.set(None)
    _groq_skip_reason_var.set(None)

    def _finalize(result_questions: Optional[List[Dict[str, Any]]]) -> Optional[List[Dict[str, Any]]]:
        """يُستدعى مرة واحدة تماماً قبل كل return فعلي بهذه الدالة (نجاحاً أو فشلاً) -
        يحسب المدة الكاملة ويحفظ بيانات آخر توليد (موديل + مدة + توكنز) بـ ContextVar منفصل
        يبقى صالحاً للقراءة من المستدعي حتى بعد عودة هذه الدالة (نفس الـ Task)."""
        duration = round(time.monotonic() - _generation_start_time, 2)
        winner = _last_model_used_var.get()
        # 🆕 قد تكون None لو المسار الفائز لم يستدع _record_token_usage لأي سبب (مثل
        # مسار Groq النصي السريع، غير مغطى بعد - راجع _generate_text_quiz) - نستخدم
        # قاموساً فارغاً بالتوكنز حتى لا ينهار .get() اللاحق بـ quiz_service.py.
        tokens = _last_token_usage_var.get() or {
            "input_tokens": 0, "output_tokens": 0, "thoughts_tokens": 0, "total_tokens": 0,
        }
        if result_questions and winner:
            # 🆕 FIX: نرفق سبب تخطّي Groq الاستباقي (لو صار) بغض النظر مين الفائز النهائي -
            # None الافتراضي (المفتاح موجود دائماً، مش بس لو صار تخطي، حتى تسهل القراءة
            # بلوحة الأدمن عبر .get() بسيط بدون داعٍ لفحص وجود المفتاح أصلاً).
            _last_generation_metadata_var.set({
                **winner, "duration_seconds": duration, **tokens,
                "groq_skip_reason": _groq_skip_reason_var.get(),
            })
        else:
            _last_generation_metadata_var.set(None)
        return result_questions

    try:
        # 🆕 اختيار الموجّه المناسب حسب الأولوية: رياضي (LaTeX) > فرنسي (مترجم/عادي) >
        # إنجليزي (مترجم/عادي) > قياسي. content_language يميّز بين الموجّهين الفرنسيين
        # والإنجليزيين لأن english_mode ("translated"/"plain") وحده لا يكفي للتفريق بينهما.
        if quiz_extraction_instruction and not (is_math_mode and question_type == QUESTION_TYPE_SIMILAR_MATH):
            source_prompt = SYSTEM_PROMPT_EXTRACT_READY_QUIZ
            if is_math_mode:
                source_prompt += SYSTEM_PROMPT_EXTRACT_READY_MATH_FORMAT
            elif content_language in (SUBJECT_FRENCH,):
                source_prompt += "\nانقل الأسئلة بلغتها الفرنسية الأصلية دون فرض ترجمة.\n"
            elif english_mode == "plain":
                source_prompt += "\nانقل الأسئلة بلغتها الأصلية دون فرض ترجمة.\n"
        elif is_math_mode:
            source_prompt = (SYSTEM_PROMPT_GENERATE_SIMILAR_MATH_QUESTIONS
                             if question_type == QUESTION_TYPE_SIMILAR_MATH else SYSTEM_PROMPT_GENERATE_MATH_QUESTIONS)
        elif content_language == SUBJECT_FRENCH and english_mode == "translated":
            source_prompt = SYSTEM_PROMPT_GENERATE_FRENCH_TRANSLATED_QUESTIONS
        elif content_language == SUBJECT_FRENCH and english_mode == "plain":
            source_prompt = SYSTEM_PROMPT_GENERATE_FRENCH_PLAIN_QUESTIONS
        elif english_mode == "translated":
            source_prompt = SYSTEM_PROMPT_GENERATE_ENGLISH_TRANSLATED_QUESTIONS
        elif english_mode == "plain":
            source_prompt = SYSTEM_PROMPT_GENERATE_ENGLISH_PLAIN_QUESTIONS
        else:
            source_prompt = SYSTEM_PROMPT_GENERATE_QUESTIONS
        # IMPORTANT: استبدال {option_count} أولاً لضمان وصول العدد الصحيح للخيارات لكافة النماذج
        base_prompt_template = source_prompt.replace("{option_count}", str(OPTION_COUNT))

        is_extraction = bool(quiz_extraction_instruction) and not (
            is_math_mode and question_type == QUESTION_TYPE_SIMILAR_MATH
        )
        if is_extraction:
            # Never ask an extractor to invent different questions or to reorder the original exam.
            base_prompt_template += quiz_extraction_instruction
        else:
            base_prompt_template = base_prompt_template.replace(
                "{difficulty_instruction}", _resolve_difficulty_instruction(difficulty)
            ).replace(
                "{question_type_instruction}",
                _resolve_question_type_instruction(question_type, custom_question_type_text),
            )

        # Student-authored preferences and prior question texts belong to user data.
        context_parts = []
        if not is_extraction and custom_question_type_text and custom_question_type_text.strip():
            context_parts.append("[تفضيل الطالب لنوع الأسئلة]:\n" + custom_question_type_text.strip()[:1000])
        if previous_questions and not is_extraction:
            old_q_texts = "\n".join(
                f"- {str(question['question'])[:350]}"
                for question in previous_questions if question.get("question")
            )[:14000]
            if old_q_texts:
                context_parts.append("[أسئلة سابقة لا تكررها]:\n" + old_q_texts)
                if is_math_mode and question_type == QUESTION_TYPE_SIMILAR_MATH:
                    base_prompt_template += "\n[منع التكرار]: حافظ على الأنماط المطلوبة لكن استخدم مسائل بمعطيات جديدة مختلفة عن الأسئلة السابقة الموجودة في بيانات الطلب."
                else:
                    base_prompt_template += "\n[منع التكرار]: تجنب الأسئلة السابقة الواردة في بيانات الطلب."
        user_context = "\n\n".join(context_parts) or None

        prompt = base_prompt_template.replace("{count}", str(count))

        # 1. مسار النصوص الصريحة
        if pure_text:
            # 🛠️ FIX: نمط الكويز المصوّر الرياضي (is_math_mode) يُوجَّه مباشرة لمسار Gemini
            # (generate_structured_with_cascade + response_schema) ولا يمر إطلاقاً بمسار
            # Groq السريع (_generate_text_quiz)، لأن Groq أقل موثوقية عموماً بمهام LaTeX
            # الدقيقة. لكن ⚠️ مسار Gemini (response_schema=QuizResponse) **لا يضمن تهريب
            # JSON سليم فعلياً** رغم الافتراض القديم هنا - التوليد المُقيَّد بمخطط يضمن فقط
            # أن الناتج *صحيح نحوياً* كـ JSON (valid syntax)، لا أن كل \bar \text \frac
            # \theta \times \beta \neq (تبدأ بحرف escape شرعي بمعيار JSON: b/f/n/r/t) قد
            # ضُوعف الـ backslash فيها بشكل صحيح من طرف النموذج - فتُبتلع بصمت (\bar{d} →
            # backspace + "ar{d}") تماماً بنفس آلية عطل Groq الموصوفة، وبدون رمي أي استثناء
            # يسمح بإعادة المحاولة. كلا مسارَي Groq وGemini يمرّان الآن إلزامياً عبر
            # _repair_json_backslashes/_parse_structured_gemini_response قبل أي json.loads()
            # فعلي - راجعهما لتفاصيل الإصلاح الكامل (لم يعد الاعتماد كافياً على response.parsed
            # الجاهز من الـ SDK وحده).
            if is_math_mode:
                return _finalize(await _generate_text_quiz_with_gemini(pure_text, prompt, user_context=user_context))
            questions = await _generate_text_quiz(pure_text, prompt, english_mode=english_mode, user_context=user_context)
            if not questions:
                questions = await _generate_text_quiz_with_gemini(pure_text, prompt)
            return _finalize(questions)

        if not file_paths:
            return _finalize(None)

        # 🩹 FIX (تنظيف كود ميت): فحص الكاش القديم بالتركيبة القديمة (subject فقط، بلا
        # question_type/difficulty) أُزيل من هنا - المستدعي الوحيد الفعلي لهذه الدالة
        # (services/quiz_service.py) يمرر دائماً skip_cache=True منذ اعتماد منطق الكاش
        # الحقيقي بالتركيبة الكاملة subject×question_type×difficulty (عبر get_file_quizzes +
        # combo_quiz_count هناك)، فهذا الفرع لم يكن يُنفَّذ عملياً إطلاقاً. تركه كان يحمل
        # خطر تفعيله بالخطأ مستقبلاً (يرجع كويزاً بصعوبة/نوع خاطئين لأنه يتجاهل التركيبة
        # الكاملة). راجع get_cached_quiz بـ helpers/supabase_helper.py لو احتجت الكاش
        # القديم لاستخدام مستقبلي آخر منفصل عن هذا المسار.

        # 3. توجيه الملفات للمسار العادي، أو Super PDF للملفات الضخمة، أو 🆕 Super
        #    Images لألبومات الصور الكبيرة (أكبر من SUPER_IMAGE_BATCH_THRESHOLD -
        #    مصدرها حصراً رفع الويب حالياً؛ الألبوم العادي عبر تيليجرام محدود أصلاً
        #    بـ MAX_ALBUM_IMAGES من منصة تيليجرام نفسها، أقل من هذا السقف).
        is_single_pdf = (
            len(file_paths) == 1 and file_paths[0].lower().endswith(".pdf")
        )
        pdf_page_count = (
            await asyncio.to_thread(get_pdf_page_count_sync, file_paths[0])
            if is_single_pdf else 0
        )
        is_super_pdf = is_single_pdf and pdf_page_count > MAX_LIMIT_PAGES
        is_super_images = len(file_paths) > SUPER_IMAGE_BATCH_THRESHOLD
        # All extended/super PDF generation now uses context overlap.
        # For ready-made exams the prompt extracts only original questions
        # starting on owned pages; all other subjects generate questions from
        # the owned content, using boundary pages for context only.
        is_ready_exam = is_extraction
        if is_super_pdf:
            generated = await _generate_pdf_with_context(
                file_paths[0], count, base_prompt_template,
                part_count=3, ready_exam=is_ready_exam,
                progressive=(difficulty == DIFFICULTY_PROGRESSIVE),
                user_context=user_context,
            )
        elif is_single_pdf and (
            pdf_page_count > MAX_STANDARD_PAGES or count > MAX_STANDARD_QUESTIONS
        ):
            generated = await _generate_pdf_with_context(
                file_paths[0], count, base_prompt_template,
                part_count=2, ready_exam=is_ready_exam,
                progressive=(difficulty == DIFFICULTY_PROGRESSIVE),
                user_context=user_context,
            )
        elif is_super_images:
            generated = await _generate_super_images(
                file_paths, count, base_prompt_template,
                progressive=(difficulty == DIFFICULTY_PROGRESSIVE),
                user_context=user_context,
            )
        else:
            generated = await _generate_regular(file_paths, prompt, user_context=user_context)
        if not generated:
            return _finalize(None)

        questions, total_tokens = generated
        return _finalize(questions)

    finally:
        # IMPORTANT: إيقاف وإلغاء مهمة التحريك بشكل فوري ونظيف في كتلة finally
        stop_event.set()
        if animation_task:
            animation_task.cancel()
            try:
                await animation_task
            except asyncio.CancelledError:
                pass

