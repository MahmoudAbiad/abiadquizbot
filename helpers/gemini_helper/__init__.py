"""
==============================================================================
MODULE: AI Quiz Generation Helper (Gemini & Groq Integration)
==============================================================================
الوصف:
موديول تنفيذي مسؤول عن تحويل المستندات (PDF/Images) والنصوص إلى أسئلة تفاعلية (Quizzes).

الميزات المعمارية والقرارات الهندسية الرئيسية:
1. Model Waterfall + Per-(Key, Model) Round-Robin: بدل الاعتماد على "نموذج أساسي +
   نموذج احتياطي واحد" وحظر كامل للمفتاح، أصبح لدينا سلسلة أولوية كاملة من النماذج
   (MODELS_CASCADE) وتتبّع دقيق لكل زوج (مفتاح، نموذج) على حدة عبر blocked_model_keys -
   بحيث حظر مفتاح على نموذج معيّن لا يمنعه إطلاقاً من العمل على نموذج آخر بنفس اللحظة.
2. Inline Data vs. Files API: إرسال الملفات الصغيرة بأسلوب inline للتقليل من تأخير الشبكة (Latency).
3. Resilience & Overload Handling: التمييز بين أخطاء الحصة (Quota Exhaustion → حظر 24 ساعة)
   وأخطاء الازدحام المؤقت (503/500/Overload → تبريد دقيقة واحدة فقط) على مستوى (مفتاح، نموذج).
4. Super PDF Parallel Processing: تقسيم ملفات PDF الكبيرة ومعالجتها بشكل متوازي بطلب مستقل لكل ثلث.
5. Robust Async Task Lifecycle: إدارة مهمة تحريك رسالة الانتظار بشكل آمن يمنع تسريب الاستثناءات (Log Pollution).
6. Smart SHA-256 Caching: التخزين المؤقت للاستجابات لتفادي الاستدعاءات التكرارية للذكاء الاصطناعي.
7. Generic Cascade Execution Helpers: `generate_structured_with_cascade` و
   `generate_text_with_cascade` يوفّران واجهة عامة قابلة لإعادة الاستخدام (كويزات، تفريغ
   صوتي، تلخيص...) تُشغّل تلقائياً كامل سلسلة (النماذج × المفاتيح) دون تكرار المنطق.
==============================================================================

------------------------------------------------------------------------------
🆕 تقسيم الموديول (helpers/gemini_helper/ حزمة بدل ملف واحد ~1400 سطر):
  clients.py         مفاتيح/عملاء Gemini وGroq وVertex
  state.py           ContextVars لآخر توليد + _record_token_usage + get_last_generation_metadata
  parsing.py         ترميم JSON/LaTeX وتحويل الاستجابات المُهيكلة
  cascade.py         الحظر/Round-Robin و_execute_cascade
  media.py           MIME / Inline مقابل Files API / تقسيم PDF والصور
  loading.py         رسالة الانتظار المتحركة
  schemas.py         مخططات Pydantic
  generic.py         generate_structured_with_cascade / generate_text_with_cascade
  generation.py      المسار العادي + Super PDF/Images
  text_generation.py Groq السريع + احتياطي Gemini للنص
  prompts.py         تعليمات الصعوبة ونوع الأسئلة
  quiz_generator.py  generate_quiz_smart (نقطة الدخول العامة)
هذا الملف يعيد تصدير كل الأسماء السابقة، فلا يتغير أي `from helpers.gemini_helper import ...` موجود.
⚠️ استثناء: current_key_pointer (عدّاد Round-Robin يُعاد ربطه) لا يُعاد تصديره عمداً؛ نسخة منه هنا
كانت ستصبح قديمة. مكانه الوحيد helpers/gemini_helper/cascade.py.
------------------------------------------------------------------------------
"""

from helpers.gemini_helper.clients import (  # noqa: F401
    API_KEYS,
    GOOGLE_VERTEX_LOCATION,
    GOOGLE_VERTEX_PROJECT,
    GROQ_API_KEY,
    VERTEX_KEY_INDEX,
    _GEMINI_CLIENTS,
    _GOOGLE_VERTEX_SA_KEY_JSON,
    _GROQ_CLIENT,
    _GROQ_TPM_SAFE_LIMIT,
    _VERTEX_CLIENT,
)
from helpers.gemini_helper.state import (  # noqa: F401
    _groq_skip_reason_var,
    _last_generation_metadata_var,
    _last_model_used_var,
    _last_token_usage_var,
    _record_token_usage,
    get_last_generation_metadata,
)
from helpers.gemini_helper.parsing import (  # noqa: F401
    _JSON_AMBIGUOUS_ESCAPE_LETTERS,
    _JSON_LEGAL_ALWAYS_ESCAPES,
    _parse_structured_gemini_response,
    _repair_json_backslashes,
)
from helpers.gemini_helper.cascade import (  # noqa: F401
    MODEL_KEY_BLOCK_OVERLOAD_MINUTES,
    MODEL_KEY_BLOCK_QUOTA_HOURS,
    OVERLOAD_ERROR_KEYWORDS,
    _execute_cascade,
    _get_models_cascade,
    _is_model_key_blocked,
    _is_overload_error,
    _mark_model_key_failure,
    _round_robin_key_order,
    blocked_model_keys,
)
from helpers.gemini_helper.media import (  # noqa: F401
    CUSTOM_MIME_TYPES,
    INLINE_DATA_SIZE_THRESHOLD,
    _build_contents_for_paths,
    _combined_file_hash,
    _read_file_bytes_sync,
    _safe_delete_gemini_file,
    get_pdf_page_count_sync,
    get_safe_mime_type,
    split_images_into_three_sync,
    split_pdf_into_three_sync,
)
from helpers.gemini_helper.loading import (  # noqa: F401
    LOADING_PHRASES,
    _loading_animation,
)
from helpers.gemini_helper.schemas import (  # noqa: F401
    QuizMatrix,
    QuizQuestion,
    QuizResponse,
    QuizTable,
)
from helpers.gemini_helper.generic import (  # noqa: F401
    generate_structured_with_cascade,
    generate_text_with_cascade,
)
from helpers.gemini_helper.generation import (  # noqa: F401
    _build_super_chunk_attempt_fn,
    _generate_regular,
    _generate_super_images,
    _generate_super_pdf,
)
from helpers.gemini_helper.text_generation import (  # noqa: F401
    _generate_text_quiz,
    _generate_text_quiz_with_gemini,
)
from helpers.gemini_helper.prompts import (  # noqa: F401
    _resolve_difficulty_instruction,
    _resolve_question_type_instruction,
)
from helpers.gemini_helper.quiz_generator import (  # noqa: F401
    generate_quiz_smart,
)
