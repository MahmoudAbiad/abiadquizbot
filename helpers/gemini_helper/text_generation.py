"""Fast text slot followed by the main cascade, both provider-independent."""
from helpers.gemini_helper.generic import generate_structured_with_cascade
from helpers.gemini_helper.schemas import QuizResponse
from services.ai.contracts import AIRequest
from services.ai.runtime import execute


async def _generate_text_quiz(pure_text, prompt, english_mode=None):
    # The schema is language-neutral; the quiz prompt selects the output language.
    result = await execute(AIRequest((prompt, pure_text), QuizResponse), slot="groq_fast", mode="fast_text")
    return [question.model_dump() for question in result.parsed.questions] if result else None


async def _generate_text_quiz_with_gemini(pure_text, prompt):
    # Public compatibility name. All configured providers now participate.
    result = await generate_structured_with_cascade([pure_text], QuizResponse, prompt)
    return [question.model_dump() for question in result[0].questions] if result else None
