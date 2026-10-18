"""Discover and probe Google Gemini API models for the private admin catalog.

The catalog cannot establish pricing/free-tier status. Live probes use synthetic
educational inputs and never log prompts, image data, responses, or API secrets.
"""
import asyncio
import io
import json
import re
import time
from dataclasses import dataclass

import httpx

from logger import get_logger

logger = get_logger(__name__)

MODELS_URL = "https://generativelanguage.googleapis.com/v1beta/models"
MODEL_PATTERN = re.compile(r"^[A-Za-z0-9._/:-]{1,100}$")
PAGE_SIZE = 500
MAX_PAGES = 8
MAX_MODELS = 600
PROBE_TIMEOUT = 13.0


class CatalogError(RuntimeError):
    """A sanitized catalog/network failure visible to the admin."""


@dataclass(frozen=True)
class ProbeResult:
    model: str
    text_ok: bool
    image_ok: bool
    text_reason: str
    image_reason: str
    elapsed_ms: int

    @property
    def ready(self):
        return self.text_ok and self.image_ok


async def list_google_models(api_key: str) -> list[dict]:
    """Query all models for *this key*, including paged responses. No billing inference."""
    if not api_key:
        raise CatalogError("مفتاح Google غير موجود في بيئة الخدمة.")
    token = None
    seen_pages = set()
    names = {}
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(12.0), follow_redirects=False) as client:
            for _ in range(MAX_PAGES):
                params = {"pageSize": PAGE_SIZE}
                if token:
                    params["pageToken"] = token
                response = await client.get(
                    MODELS_URL, params=params, headers={"x-goog-api-key": api_key})
                if response.status_code != 200:
                    status = response.status_code
                    logger.warning("GOOGLE_CATALOG_FAILED status=%s", status)
                    raise CatalogError(f"تعذّر جلب نماذج Google (HTTP {status}). تحقق من المفتاح والحصة.")
                if len(response.content) > 2_000_000:
                    raise CatalogError("استجابة كتالوج Google أكبر من الحد المسموح.")
                payload = response.json()
                if not isinstance(payload, dict) or not isinstance(payload.get("models", []), list):
                    raise CatalogError("استجابة كتالوج Google غير متوقعة.")
                for entry in payload.get("models", []):
                    if not isinstance(entry, dict):
                        continue
                    name = entry.get("name")
                    if not isinstance(name, str) or not name.startswith("models/"):
                        continue
                    model = name.removeprefix("models/")
                    if not MODEL_PATTERN.fullmatch(model):
                        continue
                    methods = entry.get("supportedGenerationMethods") or []
                    if "generateContent" not in methods:
                        continue
                    label = entry.get("displayName")
                    label = label[:85] if isinstance(label, str) else model
                    names[model] = {
                        "name": model, "label": label,
                        "input_token_limit": entry.get("inputTokenLimit")
                        if type(entry.get("inputTokenLimit")) is int else None,
                        "output_token_limit": entry.get("outputTokenLimit")
                        if type(entry.get("outputTokenLimit")) is int else None,
                    }
                    if len(names) >= MAX_MODELS:
                        return sorted(names.values(), key=lambda row: row["name"])
                next_token = payload.get("nextPageToken")
                if not next_token:
                    break
                if not isinstance(next_token, str) or next_token in seen_pages:
                    raise CatalogError("Google أرجعت رابط متابعة غير صالح.")
                seen_pages.add(next_token)
                token = next_token
            else:
                raise CatalogError("كتالوج Google تجاوز الحد الأقصى للصفحات.")
    except (httpx.TimeoutException, httpx.NetworkError, httpx.ProtocolError):
        raise CatalogError("تعذّر الاتصال بـGoogle. حاول مرة ثانية.") from None
    except (ValueError, TypeError) as exc:
        logger.warning("GOOGLE_CATALOG_FAILED error_type=%s", type(exc).__name__)
        raise CatalogError("استجابة كتالوج Google غير قابلة للقراءة.") from None
    return sorted(names.values(), key=lambda row: row["name"])


def _synthetic_equation_image() -> bytes:
    """A tiny synthetic image: never read actual student material."""
    from PIL import Image, ImageDraw, ImageFont

    picture = Image.new("RGB", (384, 176), "white")
    draw = ImageDraw.Draw(picture)
    draw.text((29, 47), "2 + 2 = 4", fill="black", font=ImageFont.load_default(size=42))
    target = io.BytesIO()
    picture.save(target, format="PNG")
    return target.getvalue()


async def probe_google_model(api_key: str, model: str, timeout_s: float = PROBE_TIMEOUT) -> ProbeResult:
    """Test the production JSON schema against an Arabic text and a real PNG.

    Probe only Google direct-model API, using the same config path as GoogleAdapter.
    Every inference may consume provider quota or incur charges.
    """
    if not api_key or not MODEL_PATTERN.fullmatch(model or ""):
        raise CatalogError("مفتاح أو اسم موديل غير صالح.")
    from google import genai
    from google.genai import types
    from services.subject_classifier import SubjectClassification

    started = time.monotonic()
    checks = []
    http_options = types.HttpOptions(retry_options=types.HttpRetryOptions(attempts=1))
    client = genai.Client(api_key=api_key, http_options=http_options)
    try:
        for image_mode in (False, True):
            if image_mode:
                contents = [
                    "افحص الصورة نفسها. حدّد نوع المادة العلمية المكتوبة فيها. "
                    "رد كـJSON، math للرياضيات، other لغيرها. لا تعتمد على التخمين.",
                    types.Part.from_bytes(data=_synthetic_equation_image(), mime_type="image/png"),
                ]
            else:
                contents = (
                    "هذه مسألة جبر: أوجد س في المعادلة 2س + 3 = 7. "
                    "صنّف مادة النص ورد بالحقول المطلوبة: math للرياضيات."
                )
            try:
                response = await asyncio.wait_for(
                    client.aio.models.generate_content(
                        model=model, contents=contents,
                        config=types.GenerateContentConfig(
                            response_mime_type="application/json",
                            response_schema=SubjectClassification,
                        ),
                    ),
                    timeout=timeout_s,
                )
                parsed = SubjectClassification.model_validate_json(response.text or "")
                if parsed.subject == "math":
                    checks.append((True, "نجح"))
                else:
                    checks.append((False, "تصنيف غير صحيح"))
            except (asyncio.TimeoutError, TimeoutError):
                checks.append((False, "انتهت مهلة الاختبار"))
            except Exception as exc:
                # Do not include exception messages; providers may echo sensitive data.
                code = getattr(exc, "code", None)
                if type(code) is int and 400 <= code <= 599:
                    checks.append((False, f"رفض API: HTTP {code}"))
                elif isinstance(exc, (ValueError, TypeError, KeyError)):
                    checks.append((False, "JSON غير صالح"))
                else:
                    logger.info("GOOGLE_MODEL_PROBE_FAILURE model=%s stage=%s error_type=%s",
                                model, "image" if image_mode else "text", type(exc).__name__)
                    checks.append((False, "تعذّر الاختبار"))
    finally:
        await client.aio.aclose()
        client.close()
    duration = round((time.monotonic() - started) * 1000)
    logger.info("GOOGLE_MODEL_PROBE model=%s text_ok=%s image_ok=%s elapsed_ms=%d",
                model, checks[0][0], checks[1][0], duration)
    return ProbeResult(model, checks[0][0], checks[1][0],
                       checks[0][1], checks[1][1], duration)
