"""Incremental, validated questions; never expose incomplete JSON objects."""
import asyncio
import contextlib
import json
import re
from contextvars import ContextVar
from types import SimpleNamespace

from helpers.gemini_helper.parsing import _repair_json_backslashes
from helpers.gemini_helper.schemas import QuizQuestion, QuizResponse

question_sink = ContextVar("quiz_question_sink", default=None)


class StreamingInterrupted(Exception):
    """A visible prefix cannot safely be replaced by another model attempt."""


class QuestionDecoder:
    def __init__(self):
        self.text = ""
        self.position = None
        self.start = None
        self.depth = 0
        self.quoted = False
        self.escaped = False
        self.questions = []

    def feed(self, text):
        self.text += text
        if len(self.text) > 4_000_000:
            raise ValueError("Quiz response exceeds streaming buffer limit")
        if self.position is None:
            match = re.match(r'\s*\{\s*"questions"\s*:\s*\[', self.text)
            if not match:
                return []
            self.position = match.end()
        ready = []
        while self.position < len(self.text):
            char = self.text[self.position]
            if self.start is None:
                if char == "{":
                    self.start = self.position
                    self.depth = 1
                elif char == "]":
                    break
                elif char not in " \r\n\t,":
                    raise ValueError("Unexpected token in questions array")
            elif self.quoted:
                if self.escaped:
                    self.escaped = False
                elif char == "\\":
                    self.escaped = True
                elif char == '"':
                    self.quoted = False
            elif char == '"':
                self.quoted = True
            elif char in "{[":
                self.depth += 1
            elif char in "}]":
                self.depth -= 1
                if self.depth == 0:
                    raw = self.text[self.start:self.position + 1]
                    question = QuizQuestion.model_validate(json.loads(_repair_json_backslashes(raw))).model_dump()
                    if (not question["question"].strip() or len(question["options"]) < 2
                            or any(not option.strip() for option in question["options"])
                            or not 0 <= question["correct_option_id"] < len(question["options"])):
                        raise ValueError("Invalid streamed question")
                    self.questions.append(question)
                    ready.append(question)
                    self.start = None
            self.position += 1
        return ready

    def finish(self):
        parsed = QuizResponse.model_validate(json.loads(_repair_json_backslashes(self.text)))
        questions = [q.model_dump() for q in parsed.questions]
        if not questions or questions != self.questions:
            raise ValueError("Incomplete or inconsistent streamed quiz")
        return questions


async def _publish_outside_provider_timeout(sink, question, timeout_scope):
    """UI rendering/Redis/Telegram time must not consume the provider budget."""
    loop = asyncio.get_running_loop()
    remaining = timeout_scope.when() - loop.time()
    if remaining <= 0:
        raise TimeoutError("Provider time budget exhausted")
    timeout_scope.reschedule(None)
    try:
        await sink(question)
    finally:
        timeout_scope.reschedule(loop.time() + remaining)


async def generate_quiz_response(client, *, model, contents, config, timeout):
    sink = question_sink.get()
    if sink is None:
        return await asyncio.wait_for(client.aio.models.generate_content(
            model=model, contents=contents, config=config), timeout=timeout)
    decoder = QuestionDecoder()
    published = False
    usage = None
    stream = None
    try:
        async with asyncio.timeout(timeout) as timeout_scope:
            stream = await client.aio.models.generate_content_stream(
                model=model, contents=contents, config=config)
            async for chunk in stream:
                if getattr(chunk, "usage_metadata", None) is not None:
                    usage = chunk.usage_metadata
                for question in decoder.feed(getattr(chunk, "text", None) or ""):
                    published = True
                    await _publish_outside_provider_timeout(sink, question, timeout_scope)
            decoder.finish()
        return SimpleNamespace(text=decoder.text, parsed=None, usage_metadata=usage)
    except Exception as exc:
        if published:
            raise StreamingInterrupted(f"Stream failed after {len(decoder.questions)} questions: {type(exc).__name__}") from exc
        raise
    finally:
        if stream is not None and hasattr(stream, "aclose"):
            with contextlib.suppress(Exception):
                await stream.aclose()


class OrderedChunks:
    """Parallel generation, ordered delivery, matching PDF boundary deduplication."""
    def __init__(self, sink, count, quotas=None, deduplicate=True):
        self.sink, self.limit, self.quotas = sink, count, quotas
        self.deduplicate = deduplicate
        self.buffers = {}
        self.done = set()
        self.current = 0
        self.positions = {}
        self.previous = set()
        self.seen = set()
        self.sent = 0
        self.lock = asyncio.Lock()

    async def run(self, index, awaitable):
        async def emit(question):
            async with self.lock:
                self.buffers.setdefault(index, []).append(question)
                await self.drain()
        token = question_sink.set(emit)
        try:
            result = await awaitable
            if result is None:
                return None
            async with self.lock:
                self.done.add(index)
                await self.drain()
            return result
        finally:
            question_sink.reset(token)

    async def drain(self):
        while self.sent < self.limit:
            buffer = self.buffers.get(self.current, [])
            pos = self.positions.get(self.current, 0)
            quota = self.quotas[self.current] if self.quotas is not None and self.current < len(self.quotas) else None
            while pos < len(buffer):
                question = buffer[pos]
                pos += 1
                self.positions[self.current] = pos
                if quota is not None and pos > quota:
                    continue
                key = " ".join(str(question.get("question", "")).split()).casefold()
                if self.deduplicate and key and key in self.previous:
                    continue
                await self.sink(question)
                self.sent += 1
                if key:
                    self.seen.add(key)
                if self.sent >= self.limit:
                    return
            if self.current not in self.done:
                return
            self.previous, self.seen = self.seen, set()
            self.current += 1


async def generate_groq_response(client, **kwargs):
    sink = question_sink.get()
    if sink is None:
        return await asyncio.wait_for(client.chat.completions.create(**kwargs), timeout=45)
    decoder = QuestionDecoder()
    stream = None
    published = False
    try:
        async with asyncio.timeout(45) as timeout_scope:
            stream = await client.chat.completions.create(**kwargs, stream=True)
            async for chunk in stream:
                text = chunk.choices[0].delta.content if chunk.choices else None
                for question in decoder.feed(text or ""):
                    published = True
                    await _publish_outside_provider_timeout(sink, question, timeout_scope)
            decoder.finish()
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=decoder.text))])
    except Exception as exc:
        if published:
            raise StreamingInterrupted("Groq stream failed after emitting questions") from exc
        raise
    finally:
        if stream is not None:
            with contextlib.suppress(Exception):
                await stream.close()

