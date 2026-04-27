"""Tests for Phase 35C: Override History API endpoints.

Covers:
1. test_list_overrides_returns_empty_when_no_records
2. test_list_overrides_returns_records_and_strips_cosmos_fields
3. test_list_overrides_filter_by_action_type
4. test_override_metrics_aggregates_correctly
"""

import json
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from src.api.dashboard_api import app


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_override_dict(
    action_type: str = "restart_service",
    resource_type: str = "Microsoft.Compute/virtualMachines",
    override_type: str = "dismiss_escalated",
    original_verdict: str = "escalated",
    original_sri: float = 72.5,
) -> dict:
    return {
        "override_id": "ov-001",
        "decision_id": "dec-001",
        "action_id": "act-001",
        "action_type": action_type,
        "resource_type": resource_type,
        "resource_id": "/subs/sub/vm-01",
        "is_production": False,
        "is_critical": False,
        "original_verdict": original_verdict,
        "original_sri": original_sri,
        "original_sri_breakdown": {"infrastructure": 30.0, "policy": 0.0, "historical": 0.0, "cost": 0.0},
        "override_type": override_type,
        "operator_id": "user@example.com",
        "operator_reason": "Routine maintenance — escalation threshold too aggressive",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "fingerprint_hash": "abc123def456abcd",
        "record_type": "verdict_override",
        # Cosmos internal fields — should be stripped from API response
        "_rid": "abc",
        "_ts": 1234567890,
        "_etag": '"etag"',
        "id": "cosmos-internal-id",
    }


def _mock_cosmos_live(records: list[dict]) -> MagicMock:
    """Return a CosmosOverrideClient mock in live mode serving the given records."""
    client = MagicMock()
    client.is_mock = False
    client._container.query_items.return_value = iter(records)
    return client


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_list_overrides_returns_empty_when_no_records():
    """GET /api/overrides with no records returns count=0 and empty list."""
    mock_client = _mock_cosmos_live([])

    with patch("src.infrastructure.cosmos_client.CosmosOverrideClient", return_value=mock_client):
        with TestClient(app) as tc:
            resp = tc.get("/api/overrides")

    assert resp.status_code == 200
    data = resp.json()
    assert data["count"] == 0
    assert data["overrides"] == []


def test_list_overrides_returns_records_and_strips_cosmos_fields():
    """GET /api/overrides returns records with Cosmos internal fields (_rid, _ts, id) removed."""
    records = [_make_override_dict(), _make_override_dict(action_type="scale_up")]
    # Each call to query_items must return a fresh iterator
    mock_client = MagicMock()
    mock_client.is_mock = False
    mock_client._container.query_items.return_value = iter(records)

    with patch("src.infrastructure.cosmos_client.CosmosOverrideClient", return_value=mock_client):
        with TestClient(app) as tc:
            resp = tc.get("/api/overrides")

    assert resp.status_code == 200
    data = resp.json()
    assert data["count"] == 2
    assert len(data["overrides"]) == 2

    # Cosmos internal fields must be stripped
    for ov in data["overrides"]:
        assert "_rid" not in ov
        assert "_ts" not in ov
        assert "_etag" not in ov
        assert "id" not in ov
        # Business fields must be present
        assert "override_type" in ov
        assert "action_type" in ov


def test_list_overrides_filter_by_action_type():
    """GET /api/overrides?action_type=X returns only matching records."""
    records = [
        _make_override_dict(action_type="restart_service"),
        _make_override_dict(action_type="scale_up"),
        _make_override_dict(action_type="restart_service"),
    ]
    mock_client = MagicMock()
    mock_client.is_mock = False
    mock_client._container.query_items.return_value = iter(records)

    with patch("src.infrastructure.cosmos_client.CosmosOverrideClient", return_value=mock_client):
        with TestClient(app) as tc:
            resp = tc.get("/api/overrides?action_type=restart_service")

    assert resp.status_code == 200
    data = resp.json()
    assert data["count"] == 2
    assert all(ov["action_type"] == "restart_service" for ov in data["overrides"])


def test_override_metrics_aggregates_correctly():
    """GET /api/overrides/metrics returns correct totals and breakdowns."""
    records = [
        _make_override_dict(action_type="restart_service", override_type="dismiss_escalated", original_verdict="escalated"),
        _make_override_dict(action_type="restart_service", override_type="force_execute",     original_verdict="denied"),
        _make_override_dict(action_type="scale_up",        override_type="dismiss_escalated", original_verdict="escalated"),
    ]
    mock_client = MagicMock()
    mock_client.is_mock = False
    mock_client._container.query_items.return_value = iter(records)

    with patch("src.infrastructure.cosmos_client.CosmosOverrideClient", return_value=mock_client):
        with TestClient(app) as tc:
            resp = tc.get("/api/overrides/metrics")

    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] == 3
    assert data["by_override_type"]["dismiss_escalated"] == 2
    assert data["by_override_type"]["force_execute"] == 1
    # top_action_types: restart_service (2) > scale_up (1)
    assert data["top_action_types"][0]["action_type"] == "restart_service"
    assert data["top_action_types"][0]["count"] == 2
    # most_overridden_verdict: "escalated" appears twice
    assert data["most_overridden_verdict"] == "escalated"
