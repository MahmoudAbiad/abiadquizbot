"""Request-scoped Gemini Files API uploads for multi-batch quiz generation.

Uploads are private to the credential that created them. Reuse lasts only for
this generation session; all uploaded remote files are deleted at its end.
"""

import asyncio
import hashlib
import os

from logger import get_logger

logger = get_logger(__name__)


class GeminiFileSession:
    def __init__(self):
        # Key by provider + credential fingerprint + local path. Never expose keys in logs.
        self._uploaded = {}

    async def get(self, client, provider_id: str, credential: str, path: str):
        key = (provider_id, hashlib.sha256(credential.encode()).digest(), os.path.abspath(path))
        cached = self._uploaded.get(key)
        if cached is not None:
            return cached[0]
        uploaded = await client.aio.files.upload(file=path)
        if not uploaded.name or not uploaded.uri:
            # Avoid storing a handle that cannot be used in a subsequent request.
            if uploaded.name:
                try:
                    await client.aio.files.delete(name=uploaded.name)
                except Exception:
                    logger.warning("GEMINI_BATCH_FILE_CLEANUP_FAILED reason=invalid_upload_response")
            raise ValueError("Gemini Files API returned an unusable file reference")
        self._uploaded[key] = (uploaded, credential)
        return uploaded

    async def aclose(self):
        uploaded, self._uploaded = list(self._uploaded.values()), {}
        if not uploaded:
            return
        from google import genai
        for file, credential in uploaded:
            client = None
            try:
                client = genai.Client(api_key=credential)
                await asyncio.wait_for(client.aio.files.delete(name=file.name), timeout=8)
            except Exception:
                # Files API uploads expire server-side; cleanup is best-effort.
                logger.warning("GEMINI_BATCH_FILE_CLEANUP_FAILED")
            finally:
                if client is not None:
                    try:
                        await client.aio.aclose()
                        client.close()
                    except Exception:
                        # Never fail a completed quiz just because cleanup failed.
                        logger.warning("GEMINI_BATCH_CLIENT_CLOSE_FAILED")
