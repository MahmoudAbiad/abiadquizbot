"""Public quiz API retained for existing callers.

Quiz prompt planning and merging live in this package. Vendor-neutral contracts,
adapters, credential rotation and execution live in services.ai. Provider settings
are managed through helpers.ai_providers_helper and the private admin panel.
"""

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
from helpers.gemini_helper.cascade import _execute_cascade
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
