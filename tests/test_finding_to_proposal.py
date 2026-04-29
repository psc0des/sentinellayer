"""Phase 40D — Finding → ProposedAction adapter tests."""

import pytest

from src.governance.finding_to_proposal import finding_to_proposal
from src.rules.base import Category, Finding, Severity
from src.core.models import ActionType, Urgency

_ARM_RID = (
    "/subscriptions/00000000-0000-0000-0000-000000000001"
    "/resourceGroups/rg-prod-data"
    "/providers/Microsoft.Compute/virtualMachines/vm-test"
)


def _finding(
    rule_id: str = "TEST-001",
    severity: Severity = Severity.MEDIUM,
    recommended_action: str = "delete_resource",
    resource_id: str = _ARM_RID,
    savings: float | None = None,
) -> Finding:
    return Finding(
        rule_id=rule_id,
        rule_name="Test Rule",
        category=Category.COST,
        severity=severity,
        resource_id=resource_id,
        resource_type="Microsoft.Compute/virtualMachines",
        resource_name="vm-test",
        reason="Something is wrong.",
        recommended_action=recommended_action,
        evidence={"key": "value"},
        estimated_savings_monthly=savings,
    )


class TestSeverityToUrgencyMapping:
    def test_critical_maps_to_critical(self):
        p = finding_to_proposal(_finding(severity=Severity.CRITICAL), "test-agent")
        assert p.urgency == Urgency.CRITICAL

    def test_high_maps_to_high(self):
        p = finding_to_proposal(_finding(severity=Severity.HIGH), "test-agent")
        assert p.urgency == Urgency.HIGH

    def test_medium_maps_to_medium(self):
        p = finding_to_proposal(_finding(severity=Severity.MEDIUM), "test-agent")
        assert p.urgency == Urgency.MEDIUM

    def test_low_maps_to_low(self):
        p = finding_to_proposal(_finding(severity=Severity.LOW), "test-agent")
        assert p.urgency == Urgency.LOW


class TestActionTypeMapping:
    def test_delete_resource(self):
        p = finding_to_proposal(_finding(recommended_action="delete_resource"), "a")
        assert p.action_type == ActionType.DELETE_RESOURCE

    def test_update_config(self):
        p = finding_to_proposal(_finding(recommended_action="update_config"), "a")
        assert p.action_type == ActionType.UPDATE_CONFIG

    def test_modify_nsg(self):
        p = finding_to_proposal(_finding(recommended_action="modify_nsg"), "a")
        assert p.action_type == ActionType.MODIFY_NSG

    def test_restart_service(self):
        p = finding_to_proposal(_finding(recommended_action="restart_service"), "a")
        assert p.action_type == ActionType.RESTART_SERVICE

    def test_scale_up(self):
        p = finding_to_proposal(_finding(recommended_action="scale_up"), "a")
        assert p.action_type == ActionType.SCALE_UP

    def test_scale_down(self):
        p = finding_to_proposal(_finding(recommended_action="scale_down"), "a")
        assert p.action_type == ActionType.SCALE_DOWN

    def test_create_resource(self):
        p = finding_to_proposal(_finding(recommended_action="create_resource"), "a")
        assert p.action_type == ActionType.CREATE_RESOURCE

    def test_unknown_action_raises(self):
        with pytest.raises(ValueError, match="Unknown recommended_action"):
            finding_to_proposal(_finding(recommended_action="explode_universe"), "a")


class TestResourceGroupParsing:
    def test_parses_rg_from_arm_id(self):
        p = finding_to_proposal(_finding(resource_id=_ARM_RID), "a")
        assert p.target.resource_group == "rg-prod-data"

    def test_rg_none_when_id_missing_rg(self):
        p = finding_to_proposal(_finding(resource_id="/no/resource/group/here"), "a")
        assert p.target.resource_group is None

    def test_rg_case_insensitive(self):
        rid = "/subscriptions/x/RESOURCEGROUPS/rg-UPPER/providers/Microsoft.Compute/virtualMachines/vm"
        p = finding_to_proposal(_finding(resource_id=rid), "a")
        assert p.target.resource_group == "rg-UPPER"


class TestProvenanceStamping:
    def test_reason_includes_rule_id(self):
        f = _finding(rule_id="UNIV-SEC-001")
        p = finding_to_proposal(f, "cost-agent")
        assert "[UNIV-SEC-001]" in p.reason
        assert f.reason in p.reason

    def test_agent_id_set(self):
        p = finding_to_proposal(_finding(), "deploy-agent")
        assert p.agent_id == "deploy-agent"

    def test_savings_passed_through(self):
        p = finding_to_proposal(_finding(savings=42.50), "a")
        assert p.projected_savings_monthly == 42.50

    def test_no_savings_when_none(self):
        p = finding_to_proposal(_finding(savings=None), "a")
        assert p.projected_savings_monthly is None

    def test_evidence_context_contains_finding_evidence(self):
        p = finding_to_proposal(_finding(), "a")
        assert p.evidence is not None
        assert p.evidence.context.get("key") == "value"
