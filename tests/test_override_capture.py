"""Tests for Phase 35A: Operator Override Capture.

Covers:
1. test_capture_override_force_execute_creates_record
2. test_capture_override_dismiss_approved_creates_record
3. test_capture_override_force_execute_requires_reason
4. test_fingerprint_hash_stable_across_runs
5. test_capture_override_idempotent
6. test_endpoint_force_execute_persists_override (integration via TestClient)
"""

import json
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from src.core.models import (
    ApprovalCondition,
    ConditionType,
    ExecutionRecord,
    ExecutionStatus,
    OverrideType,
    SRIVerdict,
    VerdictOverride,
)
from src.core.override_capture import capture_override, compute_fingerprint_hash
from src.infrastructure.cosmos_client import CosmosOverrideClient


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _mock_cfg(use_local_mocks: bool = True) -> MagicMock:
    cfg = MagicMock()
    cfg.use_local_mocks = use_local_mocks
    cfg.cosmos_endpoint = ""
    cfg.cosmos_key = ""
    return cfg


def _make_execution_record(
    execution_id: str = "exec-001",
    verdict: SRIVerdict = SRIVerdict.APPROVED_IF,
    resource_id: str = (
        "/subscriptions/sub/resourceGroups/rg-prod"
        "/providers/Microsoft.Compute/virtualMachines/vm-web-01"
    ),
    resource_type: str = "Microsoft.Compute/virtualMachines",
    action_type: str = "restart_service",
    action_id: str = "action-001",
    triage_tier: int = 2,
    status: ExecutionStatus = ExecutionStatus.conditional,
) -> ExecutionRecord:
    """Build a minimal ExecutionRecord with a populated verdict_snapshot."""
    verdict_snapshot = {
        "action_id": action_id,
        "proposed_action": {
            "action_type": action_type,
            "target": {
                "resource_id": resource_id,
                "resource_type": resource_type,
            },
        },
        "skry_risk_index": {
            "sri_infrastructure": 30.0,
            "sri_policy": 20.0,
            "sri_historical": 25.0,
            "sri_cost": 15.0,
            "sri_composite": 23.0,
        },
        "decision": verdict.value,
        "triage_tier": triage_tier,
    }
    return ExecutionRecord(
        execution_id=execution_id,
        action_id=action_id,
        verdict=verdict,
        status=status,
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
        verdict_snapshot=verdict_snapshot,
    )


def _make_cosmos(tmp_path: Path) -> CosmosOverrideClient:
    """CosmosOverrideClient backed by a temp directory so tests don't touch data/."""
    client = CosmosOverrideClient.__new__(CosmosOverrideClient)
    client._cfg = _mock_cfg()
    client._dir = tmp_path / "overrides"
    client._dir.mkdir(parents=True, exist_ok=True)
    client._is_mock = True
    client._container = None
    return client


# ---------------------------------------------------------------------------
# 1. test_capture_override_force_execute_creates_record
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_capture_override_force_execute_creates_record(tmp_path: Path) -> None:
    """force_execute override produces a VerdictOverride and writes one JSON file."""
    rec = _make_execution_record()
    cosmos = _make_cosmos(tmp_path)

    override = await capture_override(
        rec,
        OverrideType.FORCE_EXECUTE,
        "alice",
        "Scheduled maintenance window — traffic <5% at 2am UTC",
        cosmos_client=cosmos,
    )

    assert isinstance(override, VerdictOverride)
    assert override.override_type == OverrideType.FORCE_EXECUTE
    assert override.record_type == "verdict_override"
    assert override.operator_id == "alice"
    assert override.action_type == "restart_service"
    assert override.resource_type == "Microsoft.Compute/virtualMachines"
    assert override.is_production is True   # resource_id contains "rg-prod"
    assert override.original_verdict == "approved_if"
    assert override.fingerprint_hash  # must be non-empty

    written = list(cosmos._dir.glob("*.json"))
    assert len(written) == 1
    doc = json.loads(written[0].read_text())
    assert doc["override_type"] == "force_execute"
    assert doc["operator_id"] == "alice"


# ---------------------------------------------------------------------------
# 2. test_capture_override_dismiss_approved_creates_record
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_capture_override_dismiss_approved_creates_record(tmp_path: Path) -> None:
    """dismiss_approved override on an APPROVED verdict writes a distinct OverrideType."""
    rec = _make_execution_record(
        execution_id="exec-002",
        verdict=SRIVerdict.APPROVED,
        resource_id=(
            "/subscriptions/sub/resourceGroups/rg"
            "/providers/Microsoft.Compute/virtualMachines/vm-01"
        ),
        status=ExecutionStatus.manual_required,
    )
    cosmos = _make_cosmos(tmp_path)

    override = await capture_override(
        rec,
        OverrideType.DISMISS_APPROVED,
        "bob",
        "Not needed this sprint",
        cosmos_client=cosmos,
    )

    assert override.override_type == OverrideType.DISMISS_APPROVED
    assert override.original_verdict == "approved"
    assert override.operator_id == "bob"
    assert len(list(cosmos._dir.glob("*.json"))) == 1


# ---------------------------------------------------------------------------
# 3. test_capture_override_force_execute_requires_reason
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_capture_override_force_execute_requires_reason(tmp_path: Path) -> None:
    """force_execute with a reason shorter than 20 chars raises ValueError."""
    rec = _make_execution_record()
    cosmos = _make_cosmos(tmp_path)

    with pytest.raises(ValueError, match="at least 20 characters"):
        await capture_override(
            rec,
            OverrideType.FORCE_EXECUTE,
            "alice",
            "too short",          # 9 chars — must be rejected
            cosmos_client=cosmos,
        )

    # Nothing should have been written
    assert not list(cosmos._dir.glob("*.json"))


# ---------------------------------------------------------------------------
# 4. test_fingerprint_hash_stable_across_runs
# ---------------------------------------------------------------------------


def test_fingerprint_hash_stable_across_runs() -> None:
    """Same inputs always produce the same 16-char hex hash."""
    h1 = compute_fingerprint_hash(
        "restart_service", "Microsoft.Compute/virtualMachines", True, False
    )
    h2 = compute_fingerprint_hash(
        "restart_service", "Microsoft.Compute/virtualMachines", True, False
    )
    assert h1 == h2
    assert len(h1) == 16
    assert all(c in "0123456789abcdef" for c in h1)

    # Different inputs → different hash
    h3 = compute_fingerprint_hash(
        "restart_service", "Microsoft.Compute/virtualMachines", False, False
    )
    assert h1 != h3

    # Case-insensitive on action_type and resource_type
    h4 = compute_fingerprint_hash(
        "RESTART_SERVICE", "microsoft.compute/virtualmachines", True, False
    )
    assert h1 == h4


# ---------------------------------------------------------------------------
# 5. test_capture_override_idempotent
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_capture_override_idempotent(tmp_path: Path) -> None:
    """Replaying the same execution_id returns the existing record without writing a duplicate."""
    rec = _make_execution_record()
    cosmos = _make_cosmos(tmp_path)
    reason = "Scheduled maintenance window — traffic <5% at 2am UTC"

    override1 = await capture_override(
        rec, OverrideType.FORCE_EXECUTE, "alice", reason, cosmos_client=cosmos
    )
    override2 = await capture_override(
        rec, OverrideType.FORCE_EXECUTE, "alice", reason, cosmos_client=cosmos
    )

    # Same override_id returned both times
    assert override1.override_id == override2.override_id

    # Still exactly one file on disk
    assert len(list(cosmos._dir.glob("*.json"))) == 1


# ---------------------------------------------------------------------------
# 6. test_endpoint_force_execute_persists_override
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_endpoint_force_execute_persists_override(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """POST /api/execution/{id}/force-execute writes a VerdictOverride to Cosmos."""
    import src.core.override_capture as oc_mod
    from fastapi.testclient import TestClient
    from src.api.dashboard_api import app, _get_execution_gateway

    # Replace the module-level singleton so the API writes to tmp_path
    override_cosmos = _make_cosmos(tmp_path)
    monkeypatch.setattr(oc_mod, "_override_client", override_cosmos)

    gateway = _get_execution_gateway()
    gateway._ensure_loaded()

    # Build a conditional record with one unsatisfied human condition
    cond = ApprovalCondition(
        condition_type=ConditionType.BLAST_RADIUS_CONFIRMED,
        description="Blast radius sign-off required",
        auto_checkable=False,
        satisfied=False,
    )
    exec_rec = _make_execution_record(
        execution_id="exec-e2e-001",
        verdict=SRIVerdict.APPROVED_IF,
        status=ExecutionStatus.conditional,
    )
    exec_rec = exec_rec.model_copy(update={"conditions": [cond]})
    gateway._records["exec-e2e-001"] = exec_rec

    try:
        client = TestClient(app)
        resp = client.post(
            "/api/execution/exec-e2e-001/force-execute",
            json={
                "admin_user": "alice",
                "justification": "Scheduled maintenance window, low traffic at 2am UTC",
            },
        )
        assert resp.status_code == 200, resp.text

        # Verify the override record was written
        files = list(override_cosmos._dir.glob("*.json"))
        assert len(files) == 1
        doc = json.loads(files[0].read_text())
        assert doc["override_type"] == "force_execute"
        assert doc["operator_id"] == "alice"
        assert doc["execution_id"] == "exec-e2e-001"
        assert doc["record_type"] == "verdict_override"
    finally:
        # Clean up seeded state so other tests are not affected
        gateway._records.pop("exec-e2e-001", None)
        monkeypatch.undo()
