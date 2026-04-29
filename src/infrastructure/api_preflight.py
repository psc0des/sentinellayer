"""API Preflight — checks connectivity and permission to each Microsoft API.

Results are cached by coverage_status.py and surfaced via /api/coverage/status.
A 403 from any API means the service principal is missing a role — the error
field describes which role is likely needed.
"""

import asyncio
import logging
import time
from typing import Optional

logger = logging.getLogger(__name__)

# Known role requirements for each API when a 403 is returned
_ROLE_HINTS: dict[str, str] = {
    "resource_graph": "Reader on the subscription",
    "advisor": "Cost Management Reader or Contributor",
    "policy_insights": "Microsoft.PolicyInsights/policyStates/read (built-in: Security Reader)",
    "defender": "Security Reader",
    "resource_health": "Reader on the subscription",
}


async def _check_resource_graph(settings) -> dict:
    t0 = time.monotonic()
    try:
        from src.infrastructure.resource_graph import query_resources_async
        results = await query_resources_async("Resources | take 1")
        latency = int((time.monotonic() - t0) * 1000)
        return {"ok": True, "latency_ms": latency, "error": None}
    except Exception as exc:
        latency = int((time.monotonic() - t0) * 1000)
        msg = str(exc)
        if "403" in msg or "AuthorizationFailed" in msg or "Forbidden" in msg:
            msg = f"403 Forbidden — missing role: {_ROLE_HINTS['resource_graph']}"
        return {"ok": False, "latency_ms": latency, "error": msg}


async def _check_advisor(settings) -> dict:
    t0 = time.monotonic()
    try:
        from src.infrastructure.azure_tools import list_advisor_recommendations_async
        await list_advisor_recommendations_async(scope=None, category="Cost")
        latency = int((time.monotonic() - t0) * 1000)
        return {"ok": True, "latency_ms": latency, "error": None}
    except Exception as exc:
        latency = int((time.monotonic() - t0) * 1000)
        msg = str(exc)
        if "403" in msg or "AuthorizationFailed" in msg or "Forbidden" in msg:
            msg = f"403 Forbidden — missing role: {_ROLE_HINTS['advisor']}"
        return {"ok": False, "latency_ms": latency, "error": msg}


async def _check_policy_insights(settings) -> dict:
    t0 = time.monotonic()
    try:
        from src.infrastructure.azure_tools import list_policy_violations_async
        await list_policy_violations_async(scope=None)
        latency = int((time.monotonic() - t0) * 1000)
        return {"ok": True, "latency_ms": latency, "error": None}
    except Exception as exc:
        latency = int((time.monotonic() - t0) * 1000)
        msg = str(exc)
        if "403" in msg or "AuthorizationFailed" in msg or "Forbidden" in msg:
            msg = f"403 Forbidden — missing role: {_ROLE_HINTS['policy_insights']}"
        return {"ok": False, "latency_ms": latency, "error": msg}


async def _check_defender(settings) -> dict:
    t0 = time.monotonic()
    try:
        from src.infrastructure.azure_tools import list_defender_assessments_async
        await list_defender_assessments_async(scope=None)
        latency = int((time.monotonic() - t0) * 1000)
        return {"ok": True, "latency_ms": latency, "error": None}
    except Exception as exc:
        latency = int((time.monotonic() - t0) * 1000)
        msg = str(exc)
        if "403" in msg or "AuthorizationFailed" in msg or "Forbidden" in msg:
            msg = f"403 Forbidden — missing role: {_ROLE_HINTS['defender']}"
        return {"ok": False, "latency_ms": latency, "error": msg}


async def _check_resource_health(settings) -> dict:
    t0 = time.monotonic()
    try:
        from src.infrastructure.azure_tools import get_resource_health_async
        # Use a known-safe placeholder; the API rejects invalid IDs with 404 not 403.
        # We parse the response to distinguish permission errors from expected 404s.
        await get_resource_health_async("/subscriptions/preflight-check/probe")
        latency = int((time.monotonic() - t0) * 1000)
        return {"ok": True, "latency_ms": latency, "error": None}
    except Exception as exc:
        latency = int((time.monotonic() - t0) * 1000)
        msg = str(exc)
        # 404 is expected for the probe resource — that still means permission is OK
        if "404" in msg or "NotFound" in msg or "ResourceNotFound" in msg:
            return {"ok": True, "latency_ms": latency, "error": None}
        if "403" in msg or "AuthorizationFailed" in msg or "Forbidden" in msg:
            msg = f"403 Forbidden — missing role: {_ROLE_HINTS['resource_health']}"
        return {"ok": False, "latency_ms": latency, "error": msg}


async def run_preflight(settings=None) -> dict:
    """Run all API checks concurrently and return a per-API status dict.

    Returns:
        {
            "resource_graph": {"ok": bool, "latency_ms": int, "error": str|None},
            "advisor": {...},
            "policy_insights": {...},
            "defender": {...},
            "resource_health": {...},
        }
    """
    if settings is None:
        from src.config import settings as _settings
        settings = _settings

    checks = await asyncio.gather(
        _check_resource_graph(settings),
        _check_advisor(settings),
        _check_policy_insights(settings),
        _check_defender(settings),
        _check_resource_health(settings),
        return_exceptions=True,
    )

    keys = ["resource_graph", "advisor", "policy_insights", "defender", "resource_health"]
    result = {}
    for key, val in zip(keys, checks):
        if isinstance(val, Exception):
            result[key] = {"ok": False, "latency_ms": 0, "error": str(val)}
        else:
            result[key] = val
        logger.info("preflight %s: ok=%s", key, result[key]["ok"])

    return result
