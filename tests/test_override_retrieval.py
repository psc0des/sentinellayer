"""Tests for Phase 35B: Override Retrieval.

Covers:
1. test_retrieve_returns_empty_when_no_overrides
2. test_retrieve_tier1_exact_fingerprint
3. test_retrieve_tier2_action_resource_fallback
4. test_retrieve_tier3_action_type_fallback
5. test_retrieve_returns_empty_on_cosmos_error
6. test_format_overrides_for_prompt_empty_list
7. test_format_overrides_for_prompt_with_overrides
8. test_to_models_skips_malformed_records
"""

import json
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.core.models import (
    ActionTarget,
    ActionType,
    OverrideType,
    ProposedAction,
    VerdictOverride,
)
from src.core.override_retrieval import (
    _get_client,
    _to_models,
    retrieve_relevant_overrides,
)
from src.governance_agents._llm_governance import format_overrides_for_prompt


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_action(
    action_type: ActionType = ActionType.RESTART_SERVICE,
    resource_id: str = "/subscriptions/sub/resourceGroups/rg-dev/providers/Microsoft.Compute/virtualMachines/vm-01",
    resource_type: str = "Microsoft.Compute/virtualMachines",
) -> ProposedAction:
    return ProposedAction(
        agent_id="test-agent",
        action_type=action_type,
        target=ActionTarget(resource_id=resource_id, resource_type=resource_type),
        reason="test reason",
    )


def _make_override_dict(
    action_type: str = "restart_service",
    resource_type: str = "Microsoft.Compute/virtualMachines",
    fingerprint_hash: str = "abc123def456abcd",
    override_type: str = "dismiss_escalated",
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
        "original_verdict": "escalated",
        "original_sri": 72.5,
        "original_sri_breakdown": {"infrastructure": 30.0, "policy": 0.0, "historical": 0.0, "cost": 0.0},
        "override_type": override_type,
        "operator_id": "user@example.com",
        "operator_reason": "Routine maintenance — escalation threshold too aggressive",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "fingerprint_hash": fingerprint_hash,
        "record_type": "verdict_override",
    }


def _mock_cosmos_client(
    fingerprint_results: list | None = None,
    action_resource_results: list | None = None,
    action_type_results: list | None = None,
) -> MagicMock:
    client = MagicMock()
    client.get_by_fingerprint.return_value = fingerprint_results or []
    client.get_by_action_resource.return_value = action_resource_results or []
    client.get_by_action_type.return_value = action_type_results or []
    return client


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_retrieve_returns_empty_when_no_overrides():
    """Returns [] when all 3 tiers find nothing."""
    action = _make_action()
    client = _mock_cosmos_client()

    import src.core.override_retrieval as mod
    with patch.object(mod, "_get_client", return_value=client):
        result = await retrieve_relevant_overrides(action)

    assert result == []
    client.get_by_fingerprint.assert_called_once()
    client.get_by_action_resource.assert_called_once()
    client.get_by_action_type.assert_called_once()


@pytest.mark.asyncio
async def test_retrieve_tier1_exact_fingerprint():
    """Tier 1 match stops the fallback chain — tier 2 and 3 never called."""
    action = _make_action()
    override_dict = _make_override_dict()
    client = _mock_cosmos_client(fingerprint_results=[override_dict])

    import src.core.override_retrieval as mod
    with patch.object(mod, "_get_client", return_value=client):
        result = await retrieve_relevant_overrides(action)

    assert len(result) == 1
    assert isinstance(result[0], VerdictOverride)
    assert result[0].action_type == "restart_service"
    client.get_by_action_resource.assert_not_called()
    client.get_by_action_type.assert_not_called()


@pytest.mark.asyncio
async def test_retrieve_tier2_action_resource_fallback():
    """Tier 1 empty → tier 2 returns results → tier 3 never called."""
    action = _make_action()
    override_dict = _make_override_dict()
    client = _mock_cosmos_client(action_resource_results=[override_dict])

    import src.core.override_retrieval as mod
    with patch.object(mod, "_get_client", return_value=client):
        result = await retrieve_relevant_overrides(action)

    assert len(result) == 1
    assert result[0].override_type == OverrideType.DISMISS_ESCALATED
    client.get_by_fingerprint.assert_called_once()
    client.get_by_action_resource.assert_called_once()
    client.get_by_action_type.assert_not_called()


@pytest.mark.asyncio
async def test_retrieve_tier3_action_type_fallback():
    """Tier 1 and 2 empty → tier 3 returns results."""
    action = _make_action()
    override_dict = _make_override_dict()
    client = _mock_cosmos_client(action_type_results=[override_dict])

    import src.core.override_retrieval as mod
    with patch.object(mod, "_get_client", return_value=client):
        result = await retrieve_relevant_overrides(action)

    assert len(result) == 1
    client.get_by_fingerprint.assert_called_once()
    client.get_by_action_resource.assert_called_once()
    client.get_by_action_type.assert_called_once()


@pytest.mark.asyncio
async def test_retrieve_returns_empty_on_cosmos_error():
    """Any exception during retrieval returns [] — never propagates."""
    action = _make_action()
    client = MagicMock()
    client.get_by_fingerprint.side_effect = ConnectionError("Cosmos unavailable")

    import src.core.override_retrieval as mod
    with patch.object(mod, "_get_client", return_value=client):
        result = await retrieve_relevant_overrides(action)

    assert result == []


def test_format_overrides_for_prompt_empty_list():
    """Empty list returns empty string — no section added to prompt."""
    result = format_overrides_for_prompt([])
    assert result == ""


def test_format_overrides_for_prompt_with_overrides():
    """Non-empty list produces a prompt section with each override rendered."""
    ov = VerdictOverride(
        override_id="ov-001",
        decision_id="dec-001",
        action_id="act-001",
        action_type="restart_service",
        resource_type="Microsoft.Compute/virtualMachines",
        resource_id="/subs/sub/vm-01",
        is_production=True,
        is_critical=False,
        original_verdict="escalated",
        original_sri=72.5,
        original_sri_breakdown={"infrastructure": 30.0, "policy": 0.0, "historical": 0.0, "cost": 0.0},
        override_type=OverrideType.DISMISS_ESCALATED,
        operator_id="user@example.com",
        operator_reason="Routine maintenance window — escalation threshold too aggressive",
        timestamp=datetime(2026, 4, 15, 12, 0, 0, tzinfo=timezone.utc),
        fingerprint_hash="abc123def456abcd",
    )

    result = format_overrides_for_prompt([ov])

    assert "## Recent Operator Overrides" in result
    assert "restart_service" in result
    assert "Microsoft.Compute/virtualMachines" in result
    assert "escalated" in result
    assert "72.5" in result
    assert "dismiss_escalated" in result
    assert "Routine maintenance window" in result
    assert "2026-04-15" in result


def test_to_models_skips_malformed_records():
    """_to_models silently skips records that fail Pydantic validation."""
    good = _make_override_dict()
    bad = {"not": "a valid override record"}

    result = _to_models([good, bad, good], VerdictOverride)

    assert len(result) == 2
    assert all(isinstance(r, VerdictOverride) for r in result)
