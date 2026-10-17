"""Bounded PDF rasterization for classifiers using vision-only models."""
import asyncio
import io
import fitz
from PIL import Image, ImageOps
from services.ai.contracts import Attachment


def _page_count(path):
    with fitz.open(path) as document:
        return len(document)


def _page_image(path, index):
    if index is not None:
        with fitz.open(path) as document:
            page = document[index]
            scale = min(2.0, 1400 / max(page.rect.width, page.rect.height))
            image = page.get_pixmap(matrix=fitz.Matrix(scale, scale), alpha=False, colorspace=fitz.csRGB)
            data = image.tobytes("jpeg", jpg_quality=80)
    else:
        with Image.open(path) as source:
            image = ImageOps.exif_transpose(source)
            image.thumbnail((1400, 1400))
            with io.BytesIO() as target:
                image.convert("RGB").save(target, format="JPEG", quality=80)
                data = target.getvalue()
    return Attachment("image/jpeg", data=data)


async def page_batches(paths):
    batch = []
    for path in paths:
        indices = range(await asyncio.to_thread(_page_count, path)) if path.lower().endswith(".pdf") else [None]
        for index in indices:
            batch.append(await asyncio.to_thread(_page_image, path, index))
            if len(batch) == 3:
                yield tuple(batch)
                batch.clear()
    if batch:
        yield tuple(batch)
