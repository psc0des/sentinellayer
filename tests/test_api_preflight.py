"""Phase 40F — API preflight and coverage status tests."""

import asyncio
import pytest
from unittest.mock import AsyncMock, patch

from src.infrastructure.api_preflight import run_preflight
from src.infrastructure import coverage_status


# ---------------------------------------------------------------------------
# Preflight — success path
# ---------------------------------------------------------------------------

class TestApiPreflightSuccess:
    @pytest.mark.asyncio
    async def test_all_ok_when_calls_succeed(self):
        async def _ok_rg(*a, **kw):
            return [{"id": "/sub/probe"}]

        async def _ok_advisor(*a, **kw):
            return []

        async def _ok_policy(*a, **kw):
            return []

        async def _ok_defender(*a, **kw):
            return []

        async def _ok_health(*a, **kw):
            # 404 is treated as OK (probe resource doesn't exist)
            raise Exception("404 ResourceNotFound")

        with (
            patch("src.infrastructure.api_preflight._check_resource_graph", AsyncMock(return_value={"ok": True, "latency_ms": 10, "error": None})),
            patch("src.infrastructure.api_preflight._check_advisor", AsyncMock(return_value={"ok": True, "latency_ms": 8, "error": None})),
            patch("src.infrastructure.api_preflight._check_policy_insights", AsyncMock(return_value={"ok": True, "latency_ms": 12, "error": None})),
            patch("src.infrastructure.api_preflight._check_defender", AsyncMock(return_value={"ok": True, "latency_ms": 9, "error": None})),
            patch("src.infrastructure.api_preflight._check_resource_health", AsyncMock(return_value={"ok": True, "latency_ms": 5, "error": None})),
        ):
            result = await run_preflight()

        for key in ("resource_graph", "advisor", "policy_insights", "defender", "resource_health"):
            assert result[key]["ok"] is True
            assert result[key]["error"] is None


# ---------------------------------------------------------------------------
# Preflight — 403 from a single API
# ---------------------------------------------------------------------------

class TestApiPreflight403:
    @pytest.mark.asyncio
    async def test_advisor_403_sets_error_others_unaffected(self):
        ok_result = {"ok": True, "latency_ms": 5, "error": None}
        advisor_403 = {
            "ok": False,
            "latency_ms": 3,
            "error": "403 Forbidden — missing role: Cost Management Reader or Contributor",
        }
        with (
            patch("src.infrastructure.api_preflight._check_resource_graph", AsyncMock(return_value=ok_result)),
            patch("src.infrastructure.api_preflight._check_advisor", AsyncMock(return_value=advisor_403)),
            patch("src.infrastructure.api_preflight._check_policy_insights", AsyncMock(return_value=ok_result)),
            patch("src.infrastructure.api_preflight._check_defender", AsyncMock(return_value=ok_result)),
            patch("src.infrastructure.api_preflight._check_resource_health", AsyncMock(return_value=ok_result)),
        ):
            result = await run_preflight()

        assert result["advisor"]["ok"] is False
        assert "403" in result["advisor"]["error"]
        assert result["resource_graph"]["ok"] is True
        assert result["policy_insights"]["ok"] is True
        assert result["defender"]["ok"] is True

    @pytest.mark.asyncio
    async def test_policy_insights_403(self):
        ok_result = {"ok": True, "latency_ms": 5, "error": None}
        policy_403 = {
            "ok": False,
            "latency_ms": 3,
            "error": "403 Forbidden — missing role: Microsoft.PolicyInsights/policyStates/read",
        }
        with (
            patch("src.infrastructure.api_preflight._check_resource_graph", AsyncMock(return_value=ok_result)),
            patch("src.infrastructure.api_preflight._check_advisor", AsyncMock(return_value=ok_result)),
            patch("src.infrastructure.api_preflight._check_policy_insights", AsyncMock(return_value=policy_403)),
            patch("src.infrastructure.api_preflight._check_defender", AsyncMock(return_value=ok_result)),
            patch("src.infrastructure.api_preflight._check_resource_health", AsyncMock(return_value=ok_result)),
        ):
            result = await run_preflight()

        assert result["policy_insights"]["ok"] is False
        assert "403" in result["policy_insights"]["error"]


# ---------------------------------------------------------------------------
# Coverage status cache
# ---------------------------------------------------------------------------

class TestCoverageStatusCache:
    def setup_method(self):
        coverage_status.invalidate()
        coverage_status._cached_result = None
        coverage_status._refresh_lock = None

    @pytest.mark.asyncio
    async def test_cache_miss_calls_preflight(self):
        mock_result = {
            api: {"ok": True, "latency_ms": 1, "error": None}
            for api in ("resource_graph", "advisor", "policy_insights", "defender", "resource_health")
        }
        with patch("src.infrastructure.api_preflight.run_preflight", AsyncMock(return_value=mock_result)):
            with patch("src.infrastructure.coverage_status.run_preflight", AsyncMock(return_value=mock_result), create=True) as mock_pf:
                result = await coverage_status.get_or_refresh()
                assert result == mock_result

    @pytest.mark.asyncio
    async def test_cache_hit_does_not_call_preflight(self):
        mock_result = {
            api: {"ok": True, "latency_ms": 1, "error": None}
            for api in ("resource_graph", "advisor", "policy_insights", "defender", "resource_health")
        }
        # Manually pre-populate the cache
        coverage_status._cached_result = mock_result
        import time
        coverage_status._cached_at = time.monotonic()  # fresh
        result2 = await coverage_status.get_or_refresh()
        assert result2 == mock_result
        # Cache was not re-fetched — coverage_status._cached_result still same object
        assert coverage_status._cached_result is mock_result

    def test_invalidate_forces_stale(self):
        coverage_status._cached_at = 999999999.0  # far future — would normally be fresh
        coverage_status._cached_result = {"dummy": True}
        coverage_status.invalidate()
        assert coverage_status.get_cached() is None

    @pytest.mark.asyncio
    async def test_get_coverage_status_endpoint_returns_correct_shape(self):
        from fastapi.testclient import TestClient
        from src.api.dashboard_api import app
        client = TestClient(app)
        response = client.get("/api/coverage/status")
        assert response.status_code == 200
        data = response.json()
        assert "apis" in data
        assert "rules" in data
        assert "total" in data["rules"]
        assert "by_category" in data["rules"]
        assert data["rules"]["total"] > 0
