"""Tests for MonitoringAgent."""

import json
import tempfile
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.core.models import ActionType, ProposedAction, Urgency
from src.operational_agents.monitoring_agent import (
    MonitoringAgent,
    _AGENT_ID,
    _CRITICAL_COST_THRESHOLD,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _proposals_for_action(
    proposals: list[ProposedAction], action_type: ActionType
) -> list[ProposedAction]:
    return [p for p in proposals if p.action_type == action_type]


def _target_ids(proposals: list[ProposedAction]) -> list[str]:
    return [p.target.resource_id for p in proposals]


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestMonitoringAgent:

    @pytest.fixture(scope="class")
    def agent(self):
        return MonitoringAgent()

    @pytest.fixture(scope="class")
    def proposals(self, agent):
        return agent._scan_rules()

    # ------------------------------------------------------------------
    # Return type and basic structure
    # ------------------------------------------------------------------

    def test_scan_returns_list(self, proposals):
        """scan() always returns a list."""
        assert isinstance(proposals, list)

    def test_all_proposals_are_proposed_action(self, proposals):
        """Every item in the list is a ProposedAction."""
        for p in proposals:
            assert isinstance(p, ProposedAction)

    def test_returns_at_least_one_proposal(self, proposals):
        """The seed topology contains at least one anomaly."""
        assert len(proposals) >= 1

    # ------------------------------------------------------------------
    # Agent identity
    # ------------------------------------------------------------------

    def test_all_proposals_have_correct_agent_id(self, proposals):
        """Every proposal is tagged with the monitoring agent's ID."""
        for p in proposals:
            assert p.agent_id == _AGENT_ID

    # ------------------------------------------------------------------
    # Rule 1 — Untagged critical resources (UPDATE_CONFIG)
    # ------------------------------------------------------------------

    def test_nsg_east_flagged_for_missing_owner(self, proposals):
        """nsg-east is critical with no owner tag — should produce UPDATE_CONFIG."""
        update_proposals = _proposals_for_action(proposals, ActionType.UPDATE_CONFIG)
        target_ids = _target_ids(update_proposals)
        assert any("nsg-east" in t for t in target_ids)

    def test_aks_prod_flagged_for_missing_owner(self, proposals):
        """aks-prod is critical with no owner tag — should produce UPDATE_CONFIG."""
        update_proposals = _proposals_for_action(proposals, ActionType.UPDATE_CONFIG)
        target_ids = _target_ids(update_proposals)
        assert any("aks-prod" in t for t in target_ids)

    async def test_missing_owner_proposals_have_medium_urgency(self, proposals):
        """Unowned critical resources get MEDIUM urgency."""
        update_proposals = _proposals_for_action(proposals, ActionType.UPDATE_CONFIG)
        for p in update_proposals:
            assert p.urgency == Urgency.MEDIUM

    async def test_non_critical_resources_not_flagged_for_missing_owner(self):
        """Resources that are not critical should not trigger rule 1."""
        data = {
            "resources": [
                {
                    "id": "/subscriptions/demo/providers/vm/medium-vm",
                    "name": "medium-vm",
                    "type": "Microsoft.Compute/virtualMachines",
                    "tags": {"criticality": "medium"},  # not critical
                }
            ],
            "dependency_edges": [],
        }
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".json", delete=False
        ) as f:
            json.dump(data, f)
            tmp_path = f.name

        agent = MonitoringAgent(resources_path=tmp_path)
        update_proposals = _proposals_for_action(
            agent._scan_rules(), ActionType.UPDATE_CONFIG
        )
        assert len(update_proposals) == 0

    def test_critical_resource_with_owner_not_flagged(self):
        """A critical resource that already has an owner tag should be skipped."""
        data = {
            "resources": [
                {
                    "id": "/subscriptions/demo/providers/vm/owned-vm",
                    "name": "owned-vm",
                    "type": "Microsoft.Compute/virtualMachines",
                    "tags": {"criticality": "critical", "owner": "platform-team"},
                }
            ],
            "dependency_edges": [],
        }
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".json", delete=False
        ) as f:
            json.dump(data, f)
            tmp_path = f.name

        agent = MonitoringAgent(resources_path=tmp_path)
        update_proposals = _proposals_for_action(
            agent._scan_rules(), ActionType.UPDATE_CONFIG
        )
        assert len(update_proposals) == 0

    def test_missing_owner_reason_mentions_owner_tag(self, proposals):
        """Reason string should explain what tag is missing."""
        update_proposals = _proposals_for_action(proposals, ActionType.UPDATE_CONFIG)
        for p in update_proposals:
            assert "owner" in p.reason.lower()

    # ------------------------------------------------------------------
    # Rule 2 — Circular dependencies (RESTART_SERVICE)
    # ------------------------------------------------------------------

    def test_circular_dependency_produces_restart_proposal(self, proposals):
        """payment-api ↔ notification-service circular dep triggers RESTART_SERVICE."""
        restart_proposals = _proposals_for_action(proposals, ActionType.RESTART_SERVICE)
        assert len(restart_proposals) >= 1

    def test_circular_dependency_not_duplicated(self, proposals):
        """Each circular pair should only produce one proposal, not two."""
        restart_proposals = _proposals_for_action(proposals, ActionType.RESTART_SERVICE)
        # The seed data has exactly one circular pair
        assert len(restart_proposals) == 1

    def test_circular_dependency_targets_second_node(self, proposals):
        """The target should be 'notification-service' (second in the edge list)."""
        restart_proposals = _proposals_for_action(proposals, ActionType.RESTART_SERVICE)
        assert len(restart_proposals) == 1
        assert restart_proposals[0].target.resource_id == "notification-service"

    def test_circular_dependency_has_high_urgency(self, proposals):
        """Circular dependencies get HIGH urgency."""
        restart_proposals = _proposals_for_action(proposals, ActionType.RESTART_SERVICE)
        for p in restart_proposals:
            assert p.urgency == Urgency.HIGH

    async def test_circular_dependency_reason_mentions_both_services(self, proposals):
        """Reason should name both nodes in the cycle."""
        restart_proposals = _proposals_for_action(proposals, ActionType.RESTART_SERVICE)
        assert len(restart_proposals) == 1
        reason = restart_proposals[0].reason
        assert "payment-api" in reason
        assert "notification-service" in reason

    def test_no_circular_dep_in_clean_topology(self):
        """A topology with no circular edges should produce no RESTART proposals."""
        data = {
            "resources": [],
            "dependency_edges": [
                {"from": "a", "to": "b", "type": "http"},
                {"from": "b", "to": "c", "type": "http"},
            ],
        }
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".json", delete=False
        ) as f:
            json.dump(data, f)
            tmp_path = f.name

        agent = MonitoringAgent(resources_path=tmp_path)
        restart_proposals = _proposals_for_action(
            agent._scan_rules(), ActionType.RESTART_SERVICE
        )
        assert len(restart_proposals) == 0

    def test_multiple_circular_pairs_each_produce_one_proposal(self):
        """Two independent circular pairs should produce two RESTART proposals."""
        data = {
            "resources": [],
            "dependency_edges": [
                {"from": "svc-a", "to": "svc-b", "type": "circ"},
                {"from": "svc-b", "to": "svc-a", "type": "circ"},
                {"from": "svc-c", "to": "svc-d", "type": "circ"},
                {"from": "svc-d", "to": "svc-c", "type": "circ"},
            ],
        }
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".json", delete=False
        ) as f:
            json.dump(data, f)
            tmp_path = f.name

        agent = MonitoringAgent(resources_path=tmp_path)
        restart_proposals = _proposals_for_action(
            agent._scan_rules(), ActionType.RESTART_SERVICE
        )
        assert len(restart_proposals) == 2

    # ------------------------------------------------------------------
    # Rule 3 — High-cost SPOFs (SCALE_UP)
    # ------------------------------------------------------------------

    def test_aks_prod_flagged_as_high_cost_spof(self, proposals):
        """aks-prod ($2100, critical, 4 hosted services) triggers SCALE_UP."""
        scale_up_proposals = _proposals_for_action(proposals, ActionType.SCALE_UP)
        target_ids = _target_ids(scale_up_proposals)
        assert any("aks-prod" in t for t in target_ids)

    def test_high_cost_spof_has_high_urgency(self, proposals):
        """High-cost SPOFs get HIGH urgency."""
        scale_up_proposals = _proposals_for_action(proposals, ActionType.SCALE_UP)
        for p in scale_up_proposals:
            assert p.urgency == Urgency.HIGH

    def test_high_cost_spof_reason_mentions_dependents(self, proposals):
        """Reason should reference the number or names of dependents."""
        scale_up_proposals = _proposals_for_action(proposals, ActionType.SCALE_UP)
        aks = next(p for p in scale_up_proposals if "aks-prod" in p.target.resource_id)
        # Reason should mention at least one of the hosted services
        assert any(
            svc in aks.reason
            for svc in ["payment-api", "notification-service", "order-processing"]
        )

    async def test_cheap_critical_resource_not_flagged_as_spof(self):
        """A critical resource below the cost threshold is skipped by rule 3."""
        data = {
            "resources": [
                {
                    "id": "/subscriptions/demo/providers/vm/cheap-critical",
                    "name": "cheap-critical",
                    "type": "Microsoft.Compute/virtualMachines",
                    "monthly_cost": _CRITICAL_COST_THRESHOLD - 1,
                    "tags": {"criticality": "critical"},
                    "services_hosted": ["svc-a", "svc-b"],
                }
            ],
            "dependency_edges": [],
        }
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".json", delete=False
        ) as f:
            json.dump(data, f)
            tmp_path = f.name

        agent = MonitoringAgent(resources_path=tmp_path)
        scale_up_proposals = _proposals_for_action(
            agent._scan_rules(), ActionType.SCALE_UP
        )
        assert len(scale_up_proposals) == 0

    def test_critical_with_no_dependents_not_flagged_as_spof(self):
        """A critical resource with zero dependents is not a blast-radius SPOF."""
        data = {
            "resources": [
                {
                    "id": "/subscriptions/demo/providers/vm/isolated-critical",
                    "name": "isolated-critical",
                    "type": "Microsoft.Compute/virtualMachines",
                    "monthly_cost": 2000.0,
                    "tags": {"criticality": "critical"},
                    # no services_hosted, no dependents
                }
            ],
            "dependency_edges": [],
        }
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".json", delete=False
        ) as f:
            json.dump(data, f)
            tmp_path = f.name

        agent = MonitoringAgent(resources_path=tmp_path)
        scale_up_proposals = _proposals_for_action(
            agent._scan_rules(), ActionType.SCALE_UP
        )
        assert len(scale_up_proposals) == 0

    # ------------------------------------------------------------------
    # Reason strings
    # ------------------------------------------------------------------

    def test_all_proposals_have_non_empty_reason(self, proposals):
        """Every proposal must have a non-empty reason string."""
        for p in proposals:
            assert len(p.reason) > 20

    # ------------------------------------------------------------------
    # Custom resources path
    # ------------------------------------------------------------------

    def test_empty_topology_returns_no_proposals(self):
        """An empty resource file produces no proposals."""
        minimal = {"resources": [], "dependency_edges": []}
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".json", delete=False
        ) as f:
            json.dump(minimal, f)
            tmp_path = f.name

        agent = MonitoringAgent(resources_path=tmp_path)
        assert agent._scan_rules() == []


# ---------------------------------------------------------------------------
# Phase 1A: Safety net tests (Advisor + Defender + Policy post-scan checks)
# ---------------------------------------------------------------------------

def _advisor_rec_mon(
    resource_id: str, name: str, impact: str = "High", desc: str = "Enable HA"
) -> dict:
    """Minimal Advisor recommendation dict."""
    return {
        "id": f"{resource_id}/providers/Microsoft.Advisor/recommendations/abc",
        "impactedValue": name,
        "impactedField": "Microsoft.Compute/virtualMachines",
        "impact": impact,
        "shortDescription": {"problem": desc},
    }


def _defender_assessment(resource_id: str, name: str, severity: str = "High") -> dict:
    """Minimal Defender for Cloud assessment dict."""
    return {
        "resourceId": resource_id,
        "resourceName": name,
        "assessmentName": "Enable VM backup",
        "severity": severity,
        "remediation": "Enable Azure Backup on this VM.",
    }


def _policy_violation_mon(resource_id: str, name: str, policy: str = "ha-policy") -> dict:
    """Minimal Policy non-compliant resource dict."""
    return {
        "resourceId": resource_id,
        "resourceName": name,
        "policyDefinitionName": policy,
        "policyAssignmentName": "org-ha-baseline",
    }


def _make_monitoring_agent() -> MonitoringAgent:
    """Agent with _use_framework=True and minimal seed data."""
    data = {"resources": [], "dependency_edges": []}
    with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
        json.dump(data, f)
        path = f.name
    cfg = MagicMock()
    cfg.azure_openai_endpoint = "https://fake.openai.azure.com"
    cfg.azure_openai_deployment = "gpt-4o"
    cfg.llm_timeout = 600
    cfg.demo_mode = False
    return MonitoringAgent(resources_path=path, cfg=cfg)


async def _run_monitoring_safety_nets(
    advisor_recs: list[dict],
    defender_assessments: list[dict],
    policy_violations: list[dict],
    alert_payload: dict | None = None,
) -> tuple[MonitoringAgent, list[ProposedAction]]:
    """Run monitoring agent scan with LLM calls mocked out.

    Returns (agent, proposals). Pass alert_payload to trigger alert mode.
    """
    agent = _make_monitoring_agent()
    with (
        patch("openai.AsyncAzureOpenAI"),
        patch("azure.identity.DefaultAzureCredential"),
        patch("azure.identity.get_bearer_token_provider"),
        patch("agent_framework.openai.OpenAIResponsesClient") as mock_oir,
        patch("src.infrastructure.llm_throttle.run_with_throttle", new=AsyncMock()),
        patch(
            "src.infrastructure.azure_tools.list_advisor_recommendations_async",
            new=AsyncMock(return_value=advisor_recs),
        ),
        patch(
            "src.infrastructure.azure_tools.list_defender_assessments_async",
            new=AsyncMock(return_value=defender_assessments),
        ),
        patch(
            "src.infrastructure.azure_tools.list_policy_violations_async",
            new=AsyncMock(return_value=policy_violations),
        ),
    ):
        mock_agent_obj = MagicMock()
        mock_agent_obj.run = AsyncMock()
        mock_oir.return_value.as_agent.return_value = mock_agent_obj
        proposals = await agent.scan(
            alert_payload=alert_payload,
            target_resource_group="test-rg",
        )
    return agent, proposals


class TestMonitoringAgentSafetyNets:
    """Phase 1A: deterministic post-scan safety nets for Monitoring agent."""

    # --- Advisor safety net ---

    async def test_advisor_high_ha_rec_added_in_scan_mode(self):
        """A HIGH-impact HighAvailability Advisor recommendation produces a proposal."""
        rid = "/subscriptions/sub1/resourceGroups/test-rg/providers/Microsoft.Compute/virtualMachines/vm-1"
        _, proposals = await _run_monitoring_safety_nets(
            advisor_recs=[_advisor_rec_mon(rid, "vm-1", impact="High", desc="Enable HA")],
            defender_assessments=[],
            policy_violations=[],
        )
        assert len(proposals) == 1
        assert proposals[0].reason.startswith("ADVISOR-HIGH:")
        assert proposals[0].urgency == Urgency.HIGH
        assert proposals[0].action_type == ActionType.UPDATE_CONFIG

    async def test_advisor_medium_impact_rec_not_added(self):
        """Medium-impact recommendations are ignored — only HIGH triggers auto-propose."""
        rid = "/subscriptions/sub1/resourceGroups/test-rg/providers/Microsoft.Compute/virtualMachines/vm-1"
        _, proposals = await _run_monitoring_safety_nets(
            advisor_recs=[_advisor_rec_mon(rid, "vm-1", impact="Medium")],
            defender_assessments=[],
            policy_violations=[],
        )
        assert len(proposals) == 0

    async def test_advisor_safety_nets_skipped_in_alert_mode(self):
        """Safety nets must NOT run when alert_payload is provided (alert mode)."""
        rid = "/subscriptions/sub1/resourceGroups/test-rg/providers/Microsoft.Compute/virtualMachines/vm-alerted"
        _, proposals = await _run_monitoring_safety_nets(
            advisor_recs=[_advisor_rec_mon(rid, "vm-alerted", impact="High")],
            defender_assessments=[_defender_assessment(rid, "vm-alerted")],
            policy_violations=[_policy_violation_mon(rid, "vm-alerted")],
            alert_payload={"resource_id": rid, "metric": "Percentage CPU", "value": 99},
        )
        # In alert mode the LLM is mocked to produce nothing, safety nets skipped.
        assert len(proposals) == 0

    # --- Defender safety net ---

    async def test_defender_high_severity_added_in_scan_mode(self):
        """A HIGH-severity Defender assessment produces an UPDATE_CONFIG proposal."""
        rid = "/subscriptions/sub1/resourceGroups/test-rg/providers/Microsoft.Compute/virtualMachines/vm-backup"
        _, proposals = await _run_monitoring_safety_nets(
            advisor_recs=[],
            defender_assessments=[_defender_assessment(rid, "vm-backup", severity="High")],
            policy_violations=[],
        )
        assert len(proposals) == 1
        assert proposals[0].reason.startswith("DEFENDER-HIGH:")
        assert proposals[0].urgency == Urgency.HIGH
        assert proposals[0].action_type == ActionType.UPDATE_CONFIG

    async def test_defender_medium_severity_not_added(self):
        """Medium-severity Defender assessments are not auto-proposed."""
        rid = "/subscriptions/sub1/resourceGroups/test-rg/providers/Microsoft.Compute/virtualMachines/vm-m"
        _, proposals = await _run_monitoring_safety_nets(
            advisor_recs=[],
            defender_assessments=[_defender_assessment(rid, "vm-m", severity="Medium")],
            policy_violations=[],
        )
        assert len(proposals) == 0

    async def test_defender_assessment_includes_remediation_in_reason(self):
        """When remediation text is present it appears in the proposal reason."""
        rid = "/subscriptions/sub1/resourceGroups/test-rg/providers/Microsoft.Compute/virtualMachines/vm-r"
        _, proposals = await _run_monitoring_safety_nets(
            advisor_recs=[],
            defender_assessments=[_defender_assessment(rid, "vm-r", severity="High")],
            policy_violations=[],
        )
        assert len(proposals) == 1
        assert "Remediation" in proposals[0].reason

    # --- Policy safety net ---

    async def test_policy_violation_added_in_scan_mode(self):
        """A Policy non-compliant resource produces an UPDATE_CONFIG proposal."""
        rid = "/subscriptions/sub1/resourceGroups/test-rg/providers/Microsoft.Storage/storageAccounts/sa"
        _, proposals = await _run_monitoring_safety_nets(
            advisor_recs=[],
            defender_assessments=[],
            policy_violations=[_policy_violation_mon(rid, "sa", "ha-tags")],
        )
        assert len(proposals) == 1
        assert proposals[0].reason.startswith("POLICY-NONCOMPLIANT:")
        assert proposals[0].urgency == Urgency.MEDIUM

    async def test_policy_violation_missing_resource_id_skipped(self):
        """A violation without resourceId is silently skipped."""
        _, proposals = await _run_monitoring_safety_nets(
            advisor_recs=[],
            defender_assessments=[],
            policy_violations=[{"resourceName": "orphan", "policyDefinitionName": "some-policy"}],
        )
        assert len(proposals) == 0

    # --- scan_notes ---

    async def test_scan_notes_populated_in_scan_mode(self):
        """agent.scan_notes is set and non-empty after scan-mode run."""
        rid = "/subscriptions/sub1/resourceGroups/test-rg/providers/Microsoft.Compute/virtualMachines/vm-1"
        agent, _ = await _run_monitoring_safety_nets(
            advisor_recs=[_advisor_rec_mon(rid, "vm-1")],
            defender_assessments=[],
            policy_violations=[],
        )
        assert hasattr(agent, "scan_notes")
        assert isinstance(agent.scan_notes, list)
        assert len(agent.scan_notes) > 0

    async def test_scan_notes_empty_in_alert_mode(self):
        """In alert mode no safety nets run so scan_notes is an empty list."""
        rid = "/subscriptions/sub1/resourceGroups/test-rg/providers/Microsoft.Compute/virtualMachines/vm-alerted"
        agent, _ = await _run_monitoring_safety_nets(
            advisor_recs=[],
            defender_assessments=[],
            policy_violations=[],
            alert_payload={"resource_id": rid, "metric": "Percentage CPU", "value": 95},
        )
        assert hasattr(agent, "scan_notes")
        assert agent.scan_notes == []

    async def test_scan_notes_contain_scan_complete_in_scan_mode(self):
        """scan_notes always ends with a 'Scan complete' summary line in scan mode."""
        agent, _ = await _run_monitoring_safety_nets(
            advisor_recs=[], defender_assessments=[], policy_violations=[]
        )
        assert any("Scan complete" in note for note in agent.scan_notes)
