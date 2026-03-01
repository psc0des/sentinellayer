"""Shared LLM rate-limit utilities — semaphore + exponential back-off retry.

Why this module exists
----------------------
All four governance agents call Azure OpenAI via ``agent.run()`` concurrently
(via ``asyncio.gather()`` in ``pipeline.py``).  Azure OpenAI enforces two quotas:

* **TPM** — Tokens Per Minute: total token throughput across all calls.
* **RPM** — Requests Per Minute: number of API calls per minute.

When either limit is exceeded the API returns HTTP 429 ("Too Many Requests").
Each agent catches the 429 in its ``evaluate()`` outer handler and silently
falls back to deterministic rules — meaning GPT-4.1 is **never used**.

Two complementary defences are implemented here:

1. **asyncio.Semaphore** — limits simultaneous LLM calls to
   ``settings.llm_concurrency_limit`` (default 3) across all agents in the
   same process.  The semaphore is module-level (one object per process), so
   all four agent instances share the same gate.  When a slot is available the
   agent proceeds immediately; when all slots are full it waits in a queue.

2. **Exponential back-off retry** — if Azure returns 429 despite the semaphore,
   ``run_with_throttle`` waits 2 s / 4 s / 8 s (+ random jitter) and retries up
   to 3 times.  If Azure supplies a ``Retry-After`` header, that value is used as
   the minimum wait time.  After 3 failures the original exception is re-raised
   so the agent's outer fallback handler can catch it and use rule-based scoring.

Usage in each governance agent::

    from src.infrastructure.llm_throttle import run_with_throttle

    # Replace:  response = await agent.run(prompt)
    # With:     response = await run_with_throttle(agent.run, prompt)

The wrapper is intentionally minimal — it does not change the return type or
alter the arguments passed to the underlying coroutine function.
"""

import asyncio
import logging
import random
import re

from src.config import settings

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Module-level semaphore (one instance shared across all agents in the process)
# ---------------------------------------------------------------------------

_semaphore: asyncio.Semaphore | None = None
_semaphore_limit: int = -1  # tracks the limit the semaphore was created with


def _get_semaphore() -> asyncio.Semaphore:
    """Return (lazily creating) the module-level concurrency semaphore.

    Lazy creation is required because ``asyncio.Semaphore`` must be
    instantiated inside a running event loop (Python 3.10+).  This function
    is only ever called from inside ``run_with_throttle``, which is always
    ``await``-ed, so the loop is always running when we get here.

    If ``settings.llm_concurrency_limit`` changes between calls (e.g. in tests
    with a patched config), a fresh semaphore is created.
    """
    global _semaphore, _semaphore_limit
    limit = settings.llm_concurrency_limit
    if _semaphore is None or _semaphore_limit != limit:
        _semaphore = asyncio.Semaphore(limit)
        _semaphore_limit = limit
        logger.info("LLM throttle: semaphore initialised (max_concurrent=%d)", limit)
    return _semaphore


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


async def run_with_throttle(coro_fn, *args, **kwargs):
    """Run an async LLM call with semaphore throttling + exponential back-off retry.

    The semaphore is **acquired for the duration of the call**, then released —
    even if the call raises.  Back-off sleep happens *outside* the semaphore so
    other agents can proceed while this one waits.

    Args:
        coro_fn: An async callable (e.g. ``agent.run`` from agent-framework).
        *args, **kwargs: Forwarded verbatim to ``coro_fn``.

    Returns:
        Whatever ``coro_fn`` returns on success.

    Raises:
        The last exception from ``coro_fn`` if all retries are exhausted.
    """
    sem = _get_semaphore()
    max_retries = 3
    base_delay = 2.0  # seconds — doubles each retry: 2 s, 4 s, 8 s

    for attempt in range(max_retries + 1):
        # ------------------------------------------------------------------
        # Acquire semaphore slot — blocks if LLM_CONCURRENCY_LIMIT slots are
        # already in use.  Released automatically when the `async with` block
        # exits, whether by return or exception.
        # ------------------------------------------------------------------
        logger.debug(
            "LLM throttle: waiting for semaphore slot (attempt %d/%d)",
            attempt + 1, max_retries + 1,
        )
        async with sem:
            logger.debug("LLM throttle: semaphore acquired — calling LLM")
            try:
                result = await coro_fn(*args, **kwargs)
                logger.debug("LLM throttle: LLM call succeeded")
                return result
            except Exception as exc:  # noqa: BLE001
                is_429 = _is_rate_limit_error(exc)

                if not is_429 or attempt == max_retries:
                    # Not a rate-limit error, or we've exhausted retries — re-raise
                    # so the agent's outer except handler can fall back to rules.
                    raise

                # Rate-limited — calculate wait time before next attempt
                retry_after = _extract_retry_after(exc)
                jitter = random.uniform(0.0, 1.0)
                wait = max(retry_after, base_delay * (2 ** attempt)) + jitter

                logger.warning(
                    "LLM throttle: 429 rate limit hit (attempt %d/%d) — "
                    "waiting %.1f s before retry (Retry-After header: %.0f s).",
                    attempt + 1, max_retries, wait, retry_after,
                )
        # Sleep *outside* the semaphore so the slot is free for other agents
        # during the back-off period.
        await asyncio.sleep(wait)

    # Should never reach here (the loop always raises or returns inside)
    raise RuntimeError("run_with_throttle: retry loop exited without result or exception")


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------


def _is_rate_limit_error(exc: Exception) -> bool:
    """Return True if *exc* represents an HTTP 429 rate-limit response.

    The ``agent-framework-core`` library wraps ``openai.RateLimitError``
    before it surfaces from ``agent.run()``, so we use string inspection
    rather than isinstance checks to remain robust against wrapper changes.
    """
    text = str(exc).lower()
    return "429" in text or "rate limit" in text or "too many requests" in text


def _extract_retry_after(exc: Exception) -> float:
    """Return the ``Retry-After`` value (seconds) from a 429 exception.

    Azure OpenAI sets ``Retry-After`` in the response headers as an integer
    number of seconds.  The openai SDK may expose this as
    ``exc.response.headers['Retry-After']``.  Some wrapper messages also
    embed it as plain text ("retry after 30 seconds").

    Returns 0.0 when not found — callers add their own base back-off on top.
    """
    # openai.RateLimitError exposes .response (httpx.Response)
    try:
        headers = exc.response.headers  # type: ignore[union-attr]
        value = headers.get("Retry-After") or headers.get("retry-after")
        if value:
            return float(value)
    except AttributeError:
        pass

    # Fallback: scan the string representation
    match = re.search(r"retry.after[:\s]+(\d+)", str(exc), re.IGNORECASE)
    if match:
        return float(match.group(1))

    return 0.0
