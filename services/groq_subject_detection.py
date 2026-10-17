"""Bounded Groq vision requests for content classification only.

PDF pages and albums are processed in batches of three, never all rasterized
at once. No quiz generation provider or database schema is changed.
"""
import asyncio
import base64
import io
import json
import os

import fitz
from groq import AsyncGroq
from PIL import Image, ImageOps

DEFAULT_VISION_MODEL = "qwen/qwen3.8-27b"
_key = os.getenv("GROQ_API_KEY", "").strip()
_client = AsyncGroq(api_key=_key, max_retries=0, timeout=15) if _key else None
_slots = asyncio.Semaphore(2)


def available():
    return _client is not None


def _pages(paths):
    pages = []
    for path in paths:
        if path.lower().endswith(".pdf"):
            with fitz.open(path) as doc:
                pages.extend((path, i) for i in range(len(doc)))
        else:
            pages.append((path, None))
    return pages


def _image_part(page):
    path, index = page
    if index is not None:
        with fitz.open(path) as doc:
            p = doc[index]
            scale = min(2.0, 1400 / max(p.rect.width, p.rect.height))
            pix = p.get_pixmap(matrix=fitz.Matrix(scale, scale), alpha=False,
                               colorspace=fitz.csRGB)
            data = pix.tobytes("jpeg", jpg_quality=80)
    else:
        with Image.open(path) as source:
            image = ImageOps.exif_transpose(source)
            image.thumbnail((1400, 1400))
            with io.BytesIO() as out:
                image.convert("RGB").save(out, format="JPEG", quality=80)
                data = out.getvalue()
    return {"type": "image_url", "image_url": {
        "url": "data:image/jpeg;base64," + base64.b64encode(data).decode("ascii")}}


async def classify(prompt, schema, *, model=None, paths=None, text=None):
    """Return validated batch results; failed/partial results are never cached."""
    if not _client:
        raise RuntimeError("GROQ_API_KEY is not configured")
    model = model or os.getenv("GROQ_DETECTION_MODEL", DEFAULT_VISION_MODEL)
    async with _slots:
        async with asyncio.timeout(60):
            pages = await asyncio.to_thread(_pages, paths) if paths else []
            if paths and not pages:
                raise ValueError("No pages available for classification")
            results = []
            batches = [pages[i:i + 3] for i in range(0, len(pages), 3)] or [[]]
            for batch in batches:
                instruction = (prompt + "\nReturn ONLY a JSON object matching this schema:\n"
                               + json.dumps(schema.model_json_schema(), ensure_ascii=False))
                if len(batches) > 1:
                    instruction += ("\nThese pages are part of one document. Classify only "
                                    "these pages and count only questions STARTING on these "
                                    "pages. Do not count continuation of earlier questions.")
                if text:
                    instruction += "\n" + text
                content = [{"type": "text", "text": instruction}]
                for page in batch:
                    content.append(await asyncio.to_thread(_image_part, page))
                response = await _client.chat.completions.create(
                    model=model, messages=[{"role": "user", "content": content}],
                    response_format={"type": "json_object"},
                    max_completion_tokens=1200, temperature=0,
                    extra_body={"reasoning_effort": "none"} if model.startswith("qwen/") else {},
                )
                results.append(schema.model_validate_json(response.choices[0].message.content))
            return results
