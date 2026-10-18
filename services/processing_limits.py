"""Shared per-process admission limits for a single 512 MB bot worker.

Always acquire EXPORT_SLOT, then HEAVY_PROCESSING_SEMAPHORE, then rendering.
Waiting exports must not occupy the heavy-processing slots.
"""
import asyncio

HEAVY_PROCESSING_SEMAPHORE = asyncio.Semaphore(2)
EXPORT_SLOT = asyncio.Lock()
MAX_PENDING_EXPORTS = 8

