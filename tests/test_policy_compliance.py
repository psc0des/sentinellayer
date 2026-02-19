"""Tests for Policy & Compliance Agent (SRI:Policy)."""

from datetime import datetime, timezone

import pytest

from src.core.models import (
    ActionTarget,
    ActionType,
    PolicyResult,
    PolicySeverity,
    ProposedAction,
    Urgency,
)
from src.governance_agents.policy_agent import PolicyComplianceAgent


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_action(
    action_type: ActionType = ActionType.SCALE_DOWN,
    resource_id: str = "/subscriptions/demo/resourceGroups/dev/providers/Microsoft.Compute/virtualMachines/vm-dev-01",
    resource_type: str = "Microsoft.Compute/virtualMachines",
    resource_group: str = "dev",
    current_monthly_cost: float | None = None,
    projected_savings_monthly: float | None = None,
) -> ProposedAction:
    return ProposedAction(
        agent_id="test-agent",
        action_type=action_type,
        target=ActionTarget(
            resource_id=resource_id,
            resource_type=resource_type,
            resource_group=resource_group,
            current_monthly_cost=current_monthly_cost,
        ),
        reason="test",
        urgency=Urgency.LOW,
        projected_savings_monthly=projected_savings_monthly,
    )


# Fixed timestamps for deterministic change-window tests
# Saturday noon UTC — inside the Fri 17:00 → Mon 08:00 window
_SATURDAY_NOON = datetime(2025, 1, 4, 12, 0, tzinfo=timezone.utc)

# Wednesday noon UTC — outside the change window
_WEDNESDAY_NOON = datetime(2025, 1, 1, 12, 0, tzinfo=timezone.utc)

# Friday 16:59 UTC — just BEFORE the window opens
_FRIDAY_BEFORE = datetime(2025, 1, 3, 16, 59, tzinfo=timezone.utc)

# Friday 17:00 UTC — exactly at window start (inclusive)
_FRIDAY_AT_START = datetime(2025, 1, 3, 17, 0, tzinfo=timezone.utc)

# Monday 07:59 UTC — still inside window
_MONDAY_BEFORE_END = datetime(2025, 1, 6, 7, 59, tzinfo=timezone.utc)

# Monday 08:00 UTC — exactly at window end (exclusive)
_MONDAY_AT_END = datetime(2025, 1, 6, 8, 0, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# Test class
# ---------------------------------------------------------------------------


class TestPolicyComplianceAgent:

    @pytest.fixture(scope="class")
    def agent(self):
        return PolicyComplianceAgent()

    # ------------------------------------------------------------------
    # Baseline — fully compliant action
    # ------------------------------------------------------------------

    def test_compliant_action_returns_zero_score(self, agent):
        """A benign scale-up on a dev resource should clear all policies."""
        action = _make_action(
            action_type=ActionType.SCALE_UP,
            resource_id="/subscriptions/x/resourceGroups/dev/providers/Microsoft.Compute/vm/vm-dev",
            resource_group="dev",
        )
        result = agent.evaluate(action, now=_WEDNESDAY_NOON)

        assert isinstance(result, PolicyResult)
        assert result.sri_policy == 0.0
        assert result.violations == []
        assert result.policies_passed == result.total_policies_checked

    def test_returns_policy_result_model(self, agent):
        action = _make_action()
        result = agent.evaluate(action, now=_WEDNESDAY_NOON)
        assert isinstance(result, PolicyResult)
        assert result.agent == "policy_compliance"
        assert 0 <= result.sri_policy <= 100

    # ------------------------------------------------------------------
    # POL-DR-001 — Disaster Recovery Protection (critical)
    # ------------------------------------------------------------------

    def test_pol_dr001_delete_dr_resource(self, agent):
        """Deleting a disaster-recovery resource violates POL-DR-001."""
        action = _make_action(
            action_type=ActionType.DELETE_RESOURCE,
            resource_id="/subscriptions/demo/resourceGroups/prod/providers/Microsoft.Compute/virtualMachines/vm-23",
        )
        result = agent.evaluate(
            action,
            resource_metadata={"tags": {"purpose": "disaster-recovery"}, "environment": "production"},
            now=_WEDNESDAY_NOON,
        )

        ids = [v.policy_id for v in result.violations]
        assert "POL-DR-001" in ids
        assert result.sri_policy >= 40.0  # critical = 40 pts

    def test_pol_dr001_scale_down_dr_resource(self, agent):
        """Scaling down a disaster-recovery resource violates POL-DR-001."""
        action = _make_action(action_type=ActionType.SCALE_DOWN)
        result = agent.evaluate(
            action,
            resource_metadata={"tags": {"purpose": "disaster-recovery"}},
            now=_WEDNESDAY_NOON,
        )
        assert any(v.policy_id == "POL-DR-001" for v in result.violations)

    def test_pol_dr001_scale_up_dr_resource_allowed(self, agent):
        """Scale-up on a DR resource is not in the blocked list — should pass."""
        action = _make_action(action_type=ActionType.SCALE_UP)
        result = agent.evaluate(
            action,
            resource_metadata={"tags": {"purpose": "disaster-recovery"}},
            now=_WEDNESDAY_NOON,
        )
        assert not any(v.policy_id == "POL-DR-001" for v in result.violations)

    def test_pol_dr001_severity_is_critical(self, agent):
        action = _make_action(action_type=ActionType.DELETE_RESOURCE)
        result = agent.evaluate(
            action,
            resource_metadata={"tags": {"purpose": "disaster-recovery"}},
            now=_WEDNESDAY_NOON,
        )
        dr_violation = next(v for v in result.violations if v.policy_id == "POL-DR-001")
        assert dr_violation.severity == PolicySeverity.CRITICAL

    # ------------------------------------------------------------------
    # POL-SEC-001 — Network Security Baseline (high)
    # ------------------------------------------------------------------

    def test_pol_sec001_modify_nsg(self, agent):
        """Modifying an NSG resource requires security team review."""
        action = _make_action(
            action_type=ActionType.MODIFY_NSG,
            resource_type="Microsoft.Network/networkSecurityGroups",
        )
        result = agent.evaluate(action, now=_WEDNESDAY_NOON)
        assert any(v.policy_id == "POL-SEC-001" for v in result.violations)

    def test_pol_sec001_delete_nsg(self, agent):
        """Deleting an NSG also violates POL-SEC-001."""
        action = _make_action(
            action_type=ActionType.DELETE_RESOURCE,
            resource_type="Microsoft.Network/networkSecurityGroups",
        )
        result = agent.evaluate(action, now=_WEDNESDAY_NOON)
        assert any(v.policy_id == "POL-SEC-001" for v in result.violations)

    def test_pol_sec001_non_nsg_resource_not_blocked(self, agent):
        """modify_nsg on a VM (wrong resource type) should NOT trigger POL-SEC-001."""
        action = _make_action(
            action_type=ActionType.MODIFY_NSG,
            resource_type="Microsoft.Compute/virtualMachines",
        )
        result = agent.evaluate(action, now=_WEDNESDAY_NOON)
        assert not any(v.policy_id == "POL-SEC-001" for v in result.violations)

    def test_pol_sec001_severity_is_high(self, agent):
        action = _make_action(
            action_type=ActionType.MODIFY_NSG,
            resource_type="Microsoft.Network/networkSecurityGroups",
        )
        result = agent.evaluate(action, now=_WEDNESDAY_NOON)
        violation = next(v for v in result.violations if v.policy_id == "POL-SEC-001")
        assert violation.severity == PolicySeverity.HIGH

    # ------------------------------------------------------------------
    # POL-CHG-001 — Change Window Enforcement (medium)
    # ------------------------------------------------------------------

    def test_pol_chg001_triggers_during_weekend_window(self, agent):
        """Production changes during the weekend change window are blocked."""
        action = _make_action(
            action_type=ActionType.UPDATE_CONFIG,
            resource_id="/subscriptions/x/resourceGroups/prod/providers/Microsoft.Compute/vm/api",
        )
        result = agent.evaluate(action, now=_SATURDAY_NOON)
        assert any(v.policy_id == "POL-CHG-001" for v in result.violations)

    def test_pol_chg001_not_triggered_outside_window(self, agent):
        """Mid-week production changes are allowed."""
        action = _make_action(
            action_type=ActionType.UPDATE_CONFIG,
            resource_id="/subscriptions/x/resourceGroups/prod/providers/Microsoft.Compute/vm/api",
        )
        result = agent.evaluate(action, now=_WEDNESDAY_NOON)
        assert not any(v.policy_id == "POL-CHG-001" for v in result.violations)

    def test_pol_chg001_triggers_friday_at_window_start(self, agent):
        """Friday at exactly 17:00 UTC is the first blocked moment."""
        action = _make_action(
            resource_id="/subscriptions/x/resourceGroups/prod/providers/Microsoft.Compute/vm/api",
        )
        result = agent.evaluate(action, now=_FRIDAY_AT_START)
        assert any(v.policy_id == "POL-CHG-001" for v in result.violations)

    def test_pol_chg001_not_triggered_friday_before_window(self, agent):
        """Friday at 16:59 is just before the window — should be allowed."""
        action = _make_action(
            resource_id="/subscriptions/x/resourceGroups/prod/providers/Microsoft.Compute/vm/api",
        )
        result = agent.evaluate(action, now=_FRIDAY_BEFORE)
        assert not any(v.policy_id == "POL-CHG-001" for v in result.violations)

    def test_pol_chg001_triggers_monday_before_end(self, agent):
        """Monday 07:59 UTC is still within the blocked window."""
        action = _make_action(
            resource_id="/subscriptions/x/resourceGroups/prod/providers/Microsoft.Compute/vm/api",
        )
        result = agent.evaluate(action, now=_MONDAY_BEFORE_END)
        assert any(v.policy_id == "POL-CHG-001" for v in result.violations)

    def test_pol_chg001_not_triggered_monday_at_end(self, agent):
        """Monday 08:00 UTC the window has closed — changes are allowed again."""
        action = _make_action(
            resource_id="/subscriptions/x/resourceGroups/prod/providers/Microsoft.Compute/vm/api",
        )
        result = agent.evaluate(action, now=_MONDAY_AT_END)
        assert not any(v.policy_id == "POL-CHG-001" for v in result.violations)

    def test_pol_chg001_dev_environment_not_blocked(self, agent):
        """Dev environment changes are not subject to change windows."""
        action = _make_action(
            action_type=ActionType.UPDATE_CONFIG,
            resource_id="/subscriptions/x/resourceGroups/dev/providers/Microsoft.Compute/vm/dev-01",
            resource_group="dev",
        )
        result = agent.evaluate(action, now=_SATURDAY_NOON)
        assert not any(v.policy_id == "POL-CHG-001" for v in result.violations)

    # ------------------------------------------------------------------
    # POL-COST-001 — Cost Change Threshold (medium)
    # ------------------------------------------------------------------

    def test_pol_cost001_triggers_on_high_savings(self, agent):
        """$600/month savings exceeds the $500 threshold."""
        action = _make_action(
            action_type=ActionType.SCALE_DOWN,
            projected_savings_monthly=600.0,
        )
        result = agent.evaluate(action, now=_WEDNESDAY_NOON)
        assert any(v.policy_id == "POL-COST-001" for v in result.violations)

    def test_pol_cost001_triggers_on_delete_resource_cost(self, agent):
        """Deleting a $600/month VM triggers the cost threshold policy."""
        action = _make_action(
            action_type=ActionType.DELETE_RESOURCE,
            resource_id="/subscriptions/x/resourceGroups/dev/providers/Microsoft.Compute/vm/big-vm",
            current_monthly_cost=600.0,
        )
        result = agent.evaluate(action, now=_WEDNESDAY_NOON)
        assert any(v.policy_id == "POL-COST-001" for v in result.violations)

    def test_pol_cost001_not_triggered_below_threshold(self, agent):
        """$400/month savings is below the $500 threshold — no violation."""
        action = _make_action(
            action_type=ActionType.SCALE_DOWN,
            projected_savings_monthly=400.0,
        )
        result = agent.evaluate(action, now=_WEDNESDAY_NOON)
        assert not any(v.policy_id == "POL-COST-001" for v in result.violations)

    def test_pol_cost001_not_triggered_at_threshold(self, agent):
        """Exactly $500/month savings does NOT exceed the threshold."""
        action = _make_action(
            action_type=ActionType.SCALE_DOWN,
            projected_savings_monthly=500.0,
        )
        result = agent.evaluate(action, now=_WEDNESDAY_NOON)
        assert not any(v.policy_id == "POL-COST-001" for v in result.violations)

    def test_pol_cost001_not_triggered_without_cost_data(self, agent):
        """When cost data is unknown the threshold policy is not triggered."""
        action = _make_action(
            action_type=ActionType.UPDATE_CONFIG,
            current_monthly_cost=None,
            projected_savings_monthly=None,
        )
        result = agent.evaluate(action, now=_WEDNESDAY_NOON)
        assert not any(v.policy_id == "POL-COST-001" for v in result.violations)

    # ------------------------------------------------------------------
    # POL-CRIT-001 — Critical Resource Protection (high)
    # ------------------------------------------------------------------

    def test_pol_crit001_delete_critical_resource(self, agent):
        """Deleting a criticality:critical resource requires CAB approval."""
        action = _make_action(action_type=ActionType.DELETE_RESOURCE)
        result = agent.evaluate(
            action,
            resource_metadata={"tags": {"criticality": "critical"}},
            now=_WEDNESDAY_NOON,
        )
        assert any(v.policy_id == "POL-CRIT-001" for v in result.violations)

    def test_pol_crit001_restart_critical_resource(self, agent):
        """Restarting a critical resource is also blocked."""
        action = _make_action(action_type=ActionType.RESTART_SERVICE)
        result = agent.evaluate(
            action,
            resource_metadata={"tags": {"criticality": "critical"}},
            now=_WEDNESDAY_NOON,
        )
        assert any(v.policy_id == "POL-CRIT-001" for v in result.violations)

    def test_pol_crit001_scale_up_critical_resource_allowed(self, agent):
        """scale_up is not in the POL-CRIT-001 blocked list."""
        action = _make_action(action_type=ActionType.SCALE_UP)
        result = agent.evaluate(
            action,
            resource_metadata={"tags": {"criticality": "critical"}},
            now=_WEDNESDAY_NOON,
        )
        assert not any(v.policy_id == "POL-CRIT-001" for v in result.violations)

    def test_pol_crit001_non_critical_tag_not_blocked(self, agent):
        """criticality:high does not match the critical tag condition."""
        action = _make_action(action_type=ActionType.DELETE_RESOURCE)
        result = agent.evaluate(
            action,
            resource_metadata={"tags": {"criticality": "high"}},
            now=_WEDNESDAY_NOON,
        )
        assert not any(v.policy_id == "POL-CRIT-001" for v in result.violations)

    # ------------------------------------------------------------------
    # POL-SHARED-001 — Shared Resource Protection (high)
    # ------------------------------------------------------------------

    def test_pol_shared001_delete_shared_messaging_resource(self, agent):
        """Deleting a shared-messaging resource is blocked."""
        action = _make_action(action_type=ActionType.DELETE_RESOURCE)
        result = agent.evaluate(
            action,
            resource_metadata={"tags": {"purpose": "shared-messaging"}},
            now=_WEDNESDAY_NOON,
        )
        assert any(v.policy_id == "POL-SHARED-001" for v in result.violations)

    def test_pol_shared001_scale_down_shared_resource_allowed(self, agent):
        """scale_down is not in POL-SHARED-001 blocked list."""
        action = _make_action(action_type=ActionType.SCALE_DOWN)
        result = agent.evaluate(
            action,
            resource_metadata={"tags": {"purpose": "shared-messaging"}},
            now=_WEDNESDAY_NOON,
        )
        assert not any(v.policy_id == "POL-SHARED-001" for v in result.violations)

    # ------------------------------------------------------------------
    # Score calculation
    # ------------------------------------------------------------------

    def test_score_zero_for_no_violations(self, agent):
        action = _make_action(action_type=ActionType.SCALE_UP)
        result = agent.evaluate(action, now=_WEDNESDAY_NOON)
        assert result.sri_policy == 0.0

    def test_score_40_for_single_critical_violation(self, agent):
        action = _make_action(action_type=ActionType.DELETE_RESOURCE)
        result = agent.evaluate(
            action,
            resource_metadata={"tags": {"purpose": "disaster-recovery"}},
            now=_WEDNESDAY_NOON,
        )
        # POL-DR-001 (critical=40) only — dev environment, no cost data
        assert result.sri_policy == 40.0

    def test_score_compounds_across_violations(self, agent):
        """Multiple violations should add up (capped at 100)."""
        # Trigger POL-DR-001 (critical=40) + POL-CRIT-001 (high=25)
        action = _make_action(action_type=ActionType.DELETE_RESOURCE)
        result = agent.evaluate(
            action,
            resource_metadata={
                "tags": {"purpose": "disaster-recovery", "criticality": "critical"}
            },
            now=_WEDNESDAY_NOON,
        )
        policy_ids = [v.policy_id for v in result.violations]
        assert "POL-DR-001" in policy_ids
        assert "POL-CRIT-001" in policy_ids
        assert result.sri_policy == 65.0  # 40 + 25

    def test_score_capped_at_100(self, agent):
        """Score never exceeds 100 regardless of violation count."""
        # Trigger all possible violations: POL-DR-001 (40) + POL-SEC-001 (25)
        # + POL-CRIT-001 (25) + POL-SHARED-001 (25) + POL-COST-001 (15) = 130 → capped
        action = _make_action(
            action_type=ActionType.DELETE_RESOURCE,
            resource_type="Microsoft.Network/networkSecurityGroups",
            resource_id="/subscriptions/x/resourceGroups/prod/providers/Microsoft.Network/nsg/nsg-prod",
            projected_savings_monthly=600.0,
        )
        result = agent.evaluate(
            action,
            resource_metadata={
                "tags": {
                    "purpose": "disaster-recovery",
                    "criticality": "critical",
                    "shared": "true",
                },
                "environment": "production",
            },
            now=_SATURDAY_NOON,
        )
        assert result.sri_policy == 100.0

    def test_score_within_bounds(self, agent):
        """SRI:Policy must always be in [0, 100]."""
        for action_type in ActionType:
            action = _make_action(action_type=action_type)
            result = agent.evaluate(action, now=_WEDNESDAY_NOON)
            assert 0.0 <= result.sri_policy <= 100.0

    # ------------------------------------------------------------------
    # PolicyResult metadata
    # ------------------------------------------------------------------

    def test_total_policies_checked_matches_loaded(self, agent):
        action = _make_action()
        result = agent.evaluate(action, now=_WEDNESDAY_NOON)
        assert result.total_policies_checked == len(agent._policies)

    def test_policies_passed_plus_violations_equals_total(self, agent):
        action = _make_action(action_type=ActionType.DELETE_RESOURCE)
        result = agent.evaluate(
            action,
            resource_metadata={"tags": {"purpose": "disaster-recovery"}},
            now=_WEDNESDAY_NOON,
        )
        assert result.policies_passed + len(result.violations) == result.total_policies_checked

    def test_reasoning_mentions_violation_policy_id(self, agent):
        action = _make_action(action_type=ActionType.MODIFY_NSG,
                              resource_type="Microsoft.Network/networkSecurityGroups")
        result = agent.evaluate(action, now=_WEDNESDAY_NOON)
        assert "POL-SEC-001" in result.reasoning

    def test_reasoning_mentions_compliant_when_clean(self, agent):
        action = _make_action(action_type=ActionType.SCALE_UP)
        result = agent.evaluate(action, now=_WEDNESDAY_NOON)
        assert "compliant" in result.reasoning.lower()

    # ------------------------------------------------------------------
    # Environment inference
    # ------------------------------------------------------------------

    def test_infer_production_from_resource_id(self, agent):
        """'prod' in resource_id should trigger production environment inference."""
        action = _make_action(
            resource_id="/subscriptions/x/resourceGroups/prod-rg/providers/Microsoft.Compute/vm/api",
        )
        result = agent.evaluate(action, now=_SATURDAY_NOON)
        # Should detect production and apply change-window check
        assert any(v.policy_id == "POL-CHG-001" for v in result.violations)

    def test_infer_production_from_resource_group(self, agent):
        action = _make_action(
            resource_id="/subscriptions/x/providers/Microsoft.Compute/vm/api",
            resource_group="prod",
        )
        result = agent.evaluate(action, now=_SATURDAY_NOON)
        assert any(v.policy_id == "POL-CHG-001" for v in result.violations)

    def test_metadata_environment_overrides_inference(self, agent):
        """Explicit 'environment' in metadata takes precedence over heuristic."""
        # Resource ID contains 'prod' but metadata says 'staging'
        action = _make_action(
            resource_id="/subscriptions/x/resourceGroups/prod-like/providers/Microsoft.Compute/vm/svc",
        )
        result = agent.evaluate(
            action,
            resource_metadata={"environment": "staging", "tags": {}},
            now=_SATURDAY_NOON,
        )
        assert not any(v.policy_id == "POL-CHG-001" for v in result.violations)

    # ------------------------------------------------------------------
    # Custom policies path
    # ------------------------------------------------------------------

    def test_custom_policies_path(self, tmp_path):
        """Agent can load policies from a custom path."""
        custom = tmp_path / "policies.json"
        custom.write_text('[{"id": "TEST-001", "name": "Test Policy", '
                          '"description": "Always blocked", "severity": "low", '
                          '"conditions": {"blocked_actions": ["scale_up"]}}]')
        agent = PolicyComplianceAgent(policies_path=custom)
        action = _make_action(action_type=ActionType.SCALE_UP)
        result = agent.evaluate(action, now=_WEDNESDAY_NOON)
        assert any(v.policy_id == "TEST-001" for v in result.violations)
        assert result.sri_policy == 5.0  # low = 5 pts

    def test_empty_policies_returns_zero_score(self, tmp_path):
        """Zero policies loaded → always compliant."""
        empty = tmp_path / "empty.json"
        empty.write_text("[]")
        agent = PolicyComplianceAgent(policies_path=empty)
        action = _make_action(action_type=ActionType.DELETE_RESOURCE)
        result = agent.evaluate(action, now=_WEDNESDAY_NOON)
        assert result.sri_policy == 0.0
        assert result.total_policies_checked == 0
