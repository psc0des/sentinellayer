"""Tests for Pydantic data models."""

import pytest
from datetime import datetime
from src.core.models import (
    ProposedAction,
    ActionTarget,
    ActionType,
    Urgency,
    SRIBreakdown,
    GovernanceVerdict,
    SRIVerdict,
)


class TestProposedAction:
    """Test action proposal model validation."""

    def test_valid_action(self):
        action = ProposedAction(
            agent_id="cost-optimization-agent",
            action_type=ActionType.DELETE_RESOURCE,
            target=ActionTarget(
                resource_id="/subscriptions/demo/resourceGroups/prod/providers/Microsoft.Compute/virtualMachines/vm-23",
                resource_type="Microsoft.Compute/virtualMachines",
                current_monthly_cost=847.00,
            ),
            reason="VM idle for 30 days",
            urgency=Urgency.LOW,
        )
        assert action.agent_id == "cost-optimization-agent"
        assert action.action_type == ActionType.DELETE_RESOURCE

    def test_action_default_timestamp(self):
        action = ProposedAction(
            agent_id="test",
            action_type=ActionType.SCALE_UP,
            target=ActionTarget(
                resource_id="test-id",
                resource_type="Microsoft.Compute/virtualMachines",
            ),
            reason="test",
        )
        assert isinstance(action.timestamp, datetime)


class TestSRIBreakdown:
    """Test SRI™ score model validation."""

    def test_valid_sri(self):
        sri = SRIBreakdown(
            sri_infrastructure=32,
            sri_policy=40,
            sri_historical=15,
            sri_cost=10,
            sri_composite=72,
        )
        assert sri.sri_composite == 72

    def test_sri_score_bounds(self):
        """Scores must be between 0 and 100."""
        with pytest.raises(Exception):
            SRIBreakdown(
                sri_infrastructure=150,  # Invalid
                sri_policy=40,
                sri_historical=15,
                sri_cost=10,
                sri_composite=72,
            )

    def test_sri_zero_scores(self):
        """All-zero SRI is valid (perfectly safe action)."""
        sri = SRIBreakdown(
            sri_infrastructure=0,
            sri_policy=0,
            sri_historical=0,
            sri_cost=0,
            sri_composite=0,
        )
        assert sri.sri_composite == 0
