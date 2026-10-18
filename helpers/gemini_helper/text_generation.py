"""Fast text slot followed by the main cascade, both provider-independent."""
from helpers.gemini_helper.generic import generate_structured_with_cascade
from helpers.gemini_helper.schemas import QuizResponse
from services.ai.contracts import AIRequest
from services.ai.runtime import execute


async def _generate_text_quiz(pure_text, prompt, english_mode=None, user_context=None):
    # The schema is language-neutral; the quiz prompt selects the output language.
    contents = ((user_context,) if user_context else ()) + (pure_text,)
    result = await execute(AIRequest(contents, QuizResponse, system_instruction=prompt),
                           slot="groq_fast", mode="fast_text")
    return [question.model_dump() for question in result.parsed.questions] if result else None


async def _generate_text_quiz_with_gemini(pure_text, prompt, user_context=None):
    # Public compatibility name. All configured providers now participate.
    contents = ([user_context] if user_context else []) + [pure_text]
    result = await generate_structured_with_cascade(contents, QuizResponse, prompt)
    return [question.model_dump() for question in result[0].questions] if result else None
