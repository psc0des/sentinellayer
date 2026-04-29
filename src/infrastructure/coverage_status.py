"""Coverage status cache — TTL-backed in-memory cache of preflight results.

Built once per /api/coverage/status request; refreshes after TTL expires.
Thread-safe for async access patterns (single asyncio event loop).
"""

import asyncio
import logging
import time
from typing import Optional

logger = logging.getLogger(__name__)

_TTL_SECONDS = 300  # 5 minutes

# Module-level cache state
_cached_result: Optional[dict] = None
_cached_at: float = 0.0
_refresh_lock: asyncio.Lock | None = None


def _get_lock() -> asyncio.Lock:
    global _refresh_lock
    if _refresh_lock is None:
        _refresh_lock = asyncio.Lock()
    return _refresh_lock


def _is_stale() -> bool:
    return time.monotonic() - _cached_at > _TTL_SECONDS


def get_cached() -> Optional[dict]:
    """Return the cached result if still fresh, else None."""
    if _cached_result is not None and not _is_stale():
        return _cached_result
    return None


async def refresh(settings=None) -> dict:
    """Refresh the cache by running a full preflight. Returns the new result."""
    global _cached_result, _cached_at
    lock = _get_lock()
    async with lock:
        # Double-check under the lock — another coroutine may have already refreshed.
        if _cached_result is not None and not _is_stale():
            return _cached_result

        from src.infrastructure.api_preflight import run_preflight
        result = await run_preflight(settings)
        _cached_result = result
        _cached_at = time.monotonic()
        return result


async def get_or_refresh(settings=None) -> dict:
    """Return cached result if fresh; otherwise refresh and return new result."""
    cached = get_cached()
    if cached is not None:
        return cached
    return await refresh(settings)


def invalidate() -> None:
    """Force-expire the cache (e.g. after permission changes)."""
    global _cached_at
    _cached_at = 0.0
