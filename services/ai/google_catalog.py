"""Discover and probe Google Gemini API models for the private admin catalog.

The catalog cannot establish pricing/free-tier status. Live probes use synthetic
educational inputs and never log prompts, image data, responses, or API secrets.
"""
import asyncio
import hashlib
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
PROBE_TIMEOUT = 24.0
# The ordinary catalog should only show current, image-capable classifiers.
# A fresh discovery probes each of the small shortlist with one synthetic
# image+text JSON request; never blindly probe every item returned by Google.
PREFLIGHT_TIMEOUT = 24.0
PREFLIGHT_CONCURRENCY = 2
MAX_PREFLIGHT_MODELS = 6
PREFLIGHT_SUCCESS_TTL = 30 * 60
PREFLIGHT_FAILURE_TTL = 2 * 60
_PREFLIGHT_RESULTS = {}  # (sha256(key), model): (monotonic_time, passed, reason)
_GEMINI_CLASSIFIER = re.compile(r"^gemini-(\d+)(?:\.(\d+))?-(flash-lite|flash)(-preview)?$")
_GEMMA_CLASSIFIER = re.compile(r"^gemma-(\d+)-([a-z0-9-]+)-it$")


def _model_family_version(name: str):
    """Fail closed: omit older generations, audio/image generators and unknown types.

    Return family and version to keep newest listed generation of Flash/Flash-Lite;
    keep each current Gemma instruction variant (different sizes/architectures).
    """
    match = _GEMINI_CLASSIFIER.fullmatch(name)
    if match and int(match.group(1)) >= 3:
        return ("gemini:" + match.group(3),
                (int(match.group(1)), int(match.group(2) or 0)),
                not bool(match.group(4)))
    match = _GEMMA_CLASSIFIER.fullmatch(name)
    if match and int(match.group(1)) >= 4:
        return ("gemma:" + match.group(2), (int(match.group(1)), 0), True)
    return None


def _choose_current_classifiers(all_models: list[dict]) -> list[dict]:
    """Latest per serving family, preferring stable over preview at equal version."""
    chosen = {}
    for model in all_models:
        details = _model_family_version(model["name"])
        if details is None:
            continue
        family, version, stable = details
        priority = (version, stable)
        old = chosen.get(family)
        if old is None or priority > old[0]:
            chosen[family] = (priority, model)
    # Avoid a prompt-heavy Pro/Argon or generated-media model on the free classifier path.
    candidates = [x[1] for x in chosen.values()]
    return sorted(candidates, key=lambda row: (
        0 if "flash-lite" in row["name"] else 1 if "-flash" in row["name"] else 2,
        row["name"]))[:MAX_PREFLIGHT_MODELS]



class CatalogError(RuntimeError):
    """A sanitized catalog/network failure visible to the admin."""


def classification_config(model, schema, types):
    """Use the same fast, structured config in both probe and live detection.

    Gemini 3.8 Flash defaults to medium thinking. Google recommends low for
    latency-sensitive work. Do not apply it to Gemma or other model families
    whose thinking-level support differs.
    """
    values = {"response_mime_type": "application/json", "response_schema": schema}
    if model == "gemini-3.8-flash":
        values["thinking_config"] = types.ThinkingConfig(thinking_level="LOW")
    return types.GenerateContentConfig(**values)


@dataclass(frozen=True)
class ProbeResult:
    model: str
    text_ok: bool
    image_ok: bool
    text_reason: str
    image_reason: str
    elapsed_ms: int
    text_elapsed_ms: int = 0
    image_elapsed_ms: int = 0

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
                        return _choose_current_classifiers(list(names.values()))
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
    return _choose_current_classifiers(list(names.values()))


def _synthetic_equation_image() -> bytes:
    """A tiny synthetic image: never read actual student material."""
    from PIL import Image, ImageDraw, ImageFont

    picture = Image.new("RGB", (384, 176), "white")
    draw = ImageDraw.Draw(picture)
    draw.text((29, 47), "2 + 2 = 4", fill="black", font=ImageFont.load_default(size=42))
    target = io.BytesIO()
    picture.save(target, format="PNG")
    return target.getvalue()


async def _probe_catalog_candidate(api_key: str, model: str) -> tuple[bool, str]:
    """One inexpensive synthetic vision+Arabic+JSON probe before listing a model.

    Do not mark quota/timeout errors as permanent removal: only the current refresh
    is affected. The full two-stage validation remains mandatory before activation.
    """
    from google import genai
    from google.genai import types
    from services.subject_classifier import SubjectClassification

    client = None
    try:
        http_options = types.HttpOptions(retry_options=types.HttpRetryOptions(attempts=1))
        client = genai.Client(api_key=api_key, http_options=http_options)
        image = types.Part.from_bytes(data=_synthetic_equation_image(), mime_type="image/png")
        response = await asyncio.wait_for(
            client.aio.models.generate_content(
                model=model,
                contents=[
                    "صنّف المادة الظاهرة بالصورة مباشرة. أعد JSON حسب المخطط، "
                    "واجعل subject=math فقط إن كانت الصورة تتضمن معادلة رياضية.",
                    image,
                ],
                config=classification_config(model, SubjectClassification, types),
            ),
            timeout=PREFLIGHT_TIMEOUT,
        )
        parsed = SubjectClassification.model_validate_json(response.text or "")
        return (True, "working") if parsed.subject == "math" else (False, "wrong_subject")
    except (asyncio.TimeoutError, TimeoutError):
        return False, "timeout"
    except Exception as exc:
        # Never log exception strings: vendor responses can include private data.
        status = getattr(exc, "code", None)
        if type(status) is int and 400 <= status <= 599:
            return False, "http_" + str(status)
        if isinstance(exc, (ValueError, TypeError, KeyError)):
            return False, "invalid_json"
        logger.info("GOOGLE_CATALOG_PREFLIGHT_FAILURE model=%s error_type=%s",
                    model, type(exc).__name__)
        return False, "request_error"
    finally:
        if client is not None:
            try:
                await client.aio.aclose()
            finally:
                client.close()


async def list_working_google_models(api_key: str):
    """Return only models validated for the catalog's intended classification path.

    Caller must inform admin that this costs 1 short inference per shortlisted
    model on a cold refresh, and Google offers no free-tier eligibility field.
    """
    candidates = await list_google_models(api_key)
    fingerprint = hashlib.sha256(api_key.encode("utf-8")).hexdigest()
    now = time.monotonic()
    # bound process memory; never persist or log API keys / digests
    if len(_PREFLIGHT_RESULTS) > 500:
        _PREFLIGHT_RESULTS.clear()
    semaphore = asyncio.Semaphore(PREFLIGHT_CONCURRENCY)

    async def check(entry):
        model = entry["name"]
        cache_key = (fingerprint, model)
        cached = _PREFLIGHT_RESULTS.get(cache_key)
        if cached:
            stamped, passed, reason = cached
            ttl = PREFLIGHT_SUCCESS_TTL if passed else PREFLIGHT_FAILURE_TTL
            if now - stamped < ttl:
                return entry, passed, reason
        async with semaphore:
            started = time.monotonic()
            try:
                passed, reason = await _probe_catalog_candidate(api_key, model)
            except Exception as exc:
                # A failed cleanup / SDK exception must not break catalog loading.
                logger.info("GOOGLE_CATALOG_PREFLIGHT_UNEXPECTED model=%s type=%s",
                            model, type(exc).__name__)
                passed, reason = False, "request_error"
            _PREFLIGHT_RESULTS[cache_key] = (time.monotonic(), passed, reason)
            logger.info("GOOGLE_CATALOG_PREFLIGHT model=%s status=%s elapsed_ms=%d",
                        model, reason, round((time.monotonic() - started) * 1000))
            return entry, passed, reason

    results = await asyncio.gather(*(check(entry) for entry in candidates))
    healthy = [entry for entry, passed, _ in results if passed]
    stats = {
        "candidates": len(candidates),
        "available": len(healthy),
        "quota": sum(1 for _, passed, reason in results if not passed and reason == "http_429"),
        "timeout": sum(1 for _, passed, reason in results if not passed and reason == "timeout"),
        "other": sum(1 for _, passed, reason in results
                     if not passed and reason not in ("http_429", "timeout")),
    }
    logger.info("GOOGLE_CATALOG_PREFLIGHT_SUMMARY candidates=%d available=%d quota=%d timeout=%d other=%d",
                stats["candidates"], stats["available"], stats["quota"],
                stats["timeout"], stats["other"])
    return healthy, stats


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
    stage_elapsed = []
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
            stage_start = time.monotonic()
            status_code = "success"
            try:
                response = await asyncio.wait_for(
                    client.aio.models.generate_content(
                        model=model, contents=contents,
                        config=classification_config(model, SubjectClassification, types),
                    ),
                    timeout=timeout_s,
                )
                parsed = SubjectClassification.model_validate_json(response.text or "")
                if parsed.subject == "math":
                    checks.append((True, "نجح"))
                else:
                    checks.append((False, "تصنيف غير صحيح"))
                    status_code = "wrong_subject"
            except (asyncio.TimeoutError, TimeoutError):
                status_code = "timeout"
                checks.append((False, "انتهت المهلة دون استجابة من النموذج"))
            except Exception as exc:
                # Never log raw vendor errors: some echo credentials or request bodies.
                code = getattr(exc, "code", None)
                if type(code) is int and 400 <= code <= 599:
                    status_code = f"http_{code}"
                    if code == 429:
                        checks.append((False, "حد طلبات أو حصة Google (HTTP 429)"))
                    elif code in (401, 403):
                        checks.append((False, f"مفتاح أو صلاحيات Google (HTTP {code})"))
                    elif code == 404:
                        checks.append((False, "النموذج غير متاح لهذا المفتاح (HTTP 404)"))
                    else:
                        checks.append((False, f"رفض API: HTTP {code}"))
                elif isinstance(exc, (ValueError, TypeError, KeyError)):
                    status_code = "invalid_json"
                    checks.append((False, "JSON غير صالح"))
                else:
                    status_code = "request_error"
                    logger.info("GOOGLE_MODEL_PROBE_FAILURE model=%s stage=%s error_type=%s",
                                model, "image" if image_mode else "text", type(exc).__name__)
                    checks.append((False, "تعذّر الاختبار"))
            finally:
                elapsed = round((time.monotonic() - stage_start) * 1000)
                stage_elapsed.append(elapsed)
                logger.info(
                    "GOOGLE_MODEL_PROBE_STAGE model=%s stage=%s result=%s elapsed_ms=%d",
                    model, "image" if image_mode else "text", status_code, elapsed,
                )
    finally:
        await client.aio.aclose()
        client.close()
    duration = round((time.monotonic() - started) * 1000)
    logger.info("GOOGLE_MODEL_PROBE model=%s text_ok=%s image_ok=%s elapsed_ms=%d",
                model, checks[0][0], checks[1][0], duration)
    return ProbeResult(model, checks[0][0], checks[1][0],
                       checks[0][1], checks[1][1], duration,
                       stage_elapsed[0], stage_elapsed[1])
