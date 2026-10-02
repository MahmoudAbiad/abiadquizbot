"""Resolve an async SDK constructor without nesting event loops at import time."""
import asyncio
from concurrent.futures import ThreadPoolExecutor


def resolve_client(client_or_coro):
    if not asyncio.iscoroutine(client_or_coro):
        return client_or_coro
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(client_or_coro)
    # Same synchronous initialization contract, but run its coroutine on a separate
    # thread when import is triggered by an already-running event loop.
    with ThreadPoolExecutor(max_workers=1) as executor:
        return executor.submit(asyncio.run, client_or_coro).result()
