"""Tests for CostOptimizationAgent."""

import json
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.core.models import ActionType, ProposedAction, Urgency
from src.operational_agents.cost_agent import (
    CostOptimizationAgent,
    _AKS_SCALE_DOWN_NODE_THRESHOLD,
    _AKS_SCALE_DOWN_SAVINGS_RATE,
    _AGENT_ID,
    _HIGH_COST_THRESHOLD,
    _MIN_COST_THRESHOLD,
    _VM_DOWNSIZE_SAVINGS_RATE,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _resource_names(proposals: list[ProposedAction]) -> list[str]:
    """Extract the last path segment (resource name) from each proposal's resource_id."""
    return [p.target.resource_id.split("/")[-1] for p in proposals]


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestCostOptimizationAgent:

    @pytest.fixture(scope="class")
    def agent(self):
        return CostOptimizationAgent()

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
        """The seed resources contain optimisation candidates."""
        assert len(proposals) >= 1

    # ------------------------------------------------------------------
    # Agent identity
    # ------------------------------------------------------------------

    def test_all_proposals_have_correct_agent_id(self, proposals):
        """Every proposal is tagged with the cost agent's ID."""
        for p in proposals:
            assert p.agent_id == _AGENT_ID

    # ------------------------------------------------------------------
    # Specific resource detection — VM oversized SKU
    # ------------------------------------------------------------------

    def test_vm23_is_flagged(self, proposals):
        """vm-23 (D8s_v3, $847) should be a scale-down candidate."""
        names = _resource_names(proposals)
        assert "vm-23" in names

    def test_api_server_03_is_flagged(self, proposals):
        """api-server-03 (D8s_v3, $847) should be a scale-down candidate."""
        names = _resource_names(proposals)
        assert "api-server-03" in names

    def test_vm23_action_type_is_scale_down(self, proposals):
        """VM proposals must be SCALE_DOWN actions."""
        vm23 = next(p for p in proposals if "vm-23" in p.target.resource_id)
        assert vm23.action_type == ActionType.SCALE_DOWN

    def test_vm23_proposed_sku_is_d4s(self, proposals):
        """Downsizing D8s_v3 should propose D4s_v3."""
        vm23 = next(p for p in proposals if "vm-23" in p.target.resource_id)
        assert vm23.target.proposed_sku == "Standard_D4s_v3"

    def test_vm23_current_sku_is_d8s(self, proposals):
        """Current SKU should be preserved on the proposal."""
        vm23 = next(p for p in proposals if "vm-23" in p.target.resource_id)
        assert vm23.target.current_sku == "Standard_D8s_v3"

    def test_vm23_projected_savings_are_correct(self, proposals):
        """Savings should be 45 % of $847 = $381.15."""
        vm23 = next(p for p in proposals if "vm-23" in p.target.resource_id)
        expected = round(847.0 * _VM_DOWNSIZE_SAVINGS_RATE, 2)
        assert vm23.projected_savings_monthly == pytest.approx(expected)

    def test_vm23_reason_mentions_disaster_recovery(self, proposals):
        """Idle DR VMs should have that context in the reason string."""
        vm23 = next(p for p in proposals if "vm-23" in p.target.resource_id)
        assert "disaster-recovery" in vm23.reason.lower()

    # ------------------------------------------------------------------
    # Specific resource detection — AKS cluster
    # ------------------------------------------------------------------

    def test_aks_prod_is_flagged(self, proposals):
        """aks-prod (5 nodes, $2100) should be a scale-down candidate."""
        names = _resource_names(proposals)
        assert "aks-prod" in names

    def test_aks_prod_action_type_is_scale_down(self, proposals):
        """AKS proposals must be SCALE_DOWN actions."""
        aks = next(p for p in proposals if "aks-prod" in p.target.resource_id)
        assert aks.action_type == ActionType.SCALE_DOWN

    def test_aks_prod_proposed_nodes_reduced_by_two(self, proposals):
        """Node count should be reduced by 2 (5 → 3)."""
        aks = next(p for p in proposals if "aks-prod" in p.target.resource_id)
        assert aks.target.proposed_sku == "3 nodes"

    def test_aks_prod_current_sku_is_five_nodes(self, proposals):
        """Current node count should be preserved on the proposal."""
        aks = next(p for p in proposals if "aks-prod" in p.target.resource_id)
        assert aks.target.current_sku == "5 nodes"

    def test_aks_prod_projected_savings_are_correct(self, proposals):
        """Savings should be 35 % of $2100 = $735."""
        aks = next(p for p in proposals if "aks-prod" in p.target.resource_id)
        expected = round(2100.0 * _AKS_SCALE_DOWN_SAVINGS_RATE, 2)
        assert aks.projected_savings_monthly == pytest.approx(expected)

    def test_aks_prod_has_medium_urgency(self, proposals):
        """AKS scale-down always gets MEDIUM urgency."""
        aks = next(p for p in proposals if "aks-prod" in p.target.resource_id)
        assert aks.urgency == Urgency.MEDIUM

    # ------------------------------------------------------------------
    # Resources that should NOT be flagged
    # ------------------------------------------------------------------

    def test_storageshared01_not_flagged(self, proposals):
        """Storage account ($125) is below the minimum cost threshold."""
        names = _resource_names(proposals)
        assert "storageshared01" not in names

    def test_nsg_east_not_flagged(self, proposals):
        """NSG has no monthly cost and should be skipped."""
        names = _resource_names(proposals)
        assert "nsg-east" not in names

    def test_web_tier_01_not_flagged(self, proposals):
        """web-tier-01 uses D4s_v3 — not in the oversized SKU list."""
        names = _resource_names(proposals)
        assert "web-tier-01" not in names

    # ------------------------------------------------------------------
    # Urgency thresholds
    # ------------------------------------------------------------------

    def test_high_cost_vm_gets_medium_urgency(self, proposals):
        """VMs with monthly cost > $500 should get MEDIUM urgency."""
        vm23 = next(p for p in proposals if "vm-23" in p.target.resource_id)
        assert vm23.target.current_monthly_cost is not None
        assert vm23.target.current_monthly_cost >= _HIGH_COST_THRESHOLD
        assert vm23.urgency == Urgency.MEDIUM

    # ------------------------------------------------------------------
    # Projected savings always set
    # ------------------------------------------------------------------

    def test_all_proposals_have_projected_savings(self, proposals):
        """Every cost proposal must include a projected_savings_monthly value."""
        for p in proposals:
            assert p.projected_savings_monthly is not None
            assert p.projected_savings_monthly > 0

    # ------------------------------------------------------------------
    # Reason strings are non-empty and informative
    # ------------------------------------------------------------------

    def test_all_proposals_have_non_empty_reason(self, proposals):
        """Every proposal must explain why it was raised."""
        for p in proposals:
            assert len(p.reason) > 20

    def test_reason_mentions_resource_name(self, proposals):
        """Reason strings should reference the specific resource."""
        for p in proposals:
            resource_name = p.target.resource_id.split("/")[-1]
            assert resource_name in p.reason

    # ------------------------------------------------------------------
    # Custom resources path
    # ------------------------------------------------------------------

    def test_custom_resources_path_empty_returns_no_proposals(self):
        """With no resources in the file, _scan_rules() returns an empty list."""
        minimal = {"resources": [], "dependency_edges": []}
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".json", delete=False
        ) as f:
            json.dump(minimal, f)
            tmp_path = f.name

        agent = CostOptimizationAgent(resources_path=tmp_path)
        assert agent._scan_rules() == []

    def test_custom_resources_path_cheap_resource_returns_no_proposals(self):
        """Resources below the cost threshold produce no proposals."""
        data = {
            "resources": [
                {
                    "id": "/subscriptions/demo/providers/vm/cheap-vm",
                    "name": "cheap-vm",
                    "type": "Microsoft.Compute/virtualMachines",
                    "sku": "Standard_D8s_v3",
                    "monthly_cost": _MIN_COST_THRESHOLD - 1,
                    "tags": {},
                }
            ],
            "dependency_edges": [],
        }
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".json", delete=False
        ) as f:
            json.dump(data, f)
            tmp_path = f.name

        agent = CostOptimizationAgent(resources_path=tmp_path)
        assert agent._scan_rules() == []

    def test_custom_resources_path_oversized_vm_flagged(self):
        """An oversized VM in a custom file should still be detected by the rule engine."""
        data = {
            "resources": [
                {
                    "id": "/subscriptions/demo/providers/vm/big-vm",
                    "name": "big-vm",
                    "type": "Microsoft.Compute/virtualMachines",
                    "sku": "Standard_D8s_v3",
                    "monthly_cost": 800.0,
                    "tags": {"environment": "production"},
                }
            ],
            "dependency_edges": [],
        }
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".json", delete=False
        ) as f:
            json.dump(data, f)
            tmp_path = f.name

        agent = CostOptimizationAgent(resources_path=tmp_path)
        proposals = agent._scan_rules()
        assert len(proposals) == 1
        assert proposals[0].action_type == ActionType.SCALE_DOWN

    def test_aks_below_node_threshold_not_flagged(self):
        """AKS cluster with fewer nodes than the threshold should be skipped."""
        data = {
            "resources": [
                {
                    "id": "/subscriptions/demo/providers/aks/small-aks",
                    "name": "small-aks",
                    "type": "Microsoft.ContainerService/managedClusters",
                    "sku": "Standard",
                    "node_count": _AKS_SCALE_DOWN_NODE_THRESHOLD - 1,
                    "monthly_cost": 1000.0,
                    "tags": {},
                }
            ],
            "dependency_edges": [],
        }
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".json", delete=False
        ) as f:
            json.dump(data, f)
            tmp_path = f.name

        agent = CostOptimizationAgent(resources_path=tmp_path)
        assert agent._scan_rules() == []


# ---------------------------------------------------------------------------
# Phase 1A: Safety net tests (Advisor + Policy deterministic post-scan checks)
# ---------------------------------------------------------------------------

def _advisor_rec_cost(
    resource_id: str, name: str, impact: str = "High", desc: str = "Rightsize VM"
) -> dict:
    """Minimal Advisor recommendation dict."""
    return {
        "id": f"{resource_id}/providers/Microsoft.Advisor/recommendations/abc",
        "impactedValue": name,
        "impactedField": "Microsoft.Compute/virtualMachines",
        "impact": impact,
        "shortDescription": {"problem": desc},
    }


def _policy_violation_cost(resource_id: str, name: str, policy: str = "cost-tagging") -> dict:
    """Minimal Policy non-compliant resource dict."""
    return {
        "resourceId": resource_id,
        "resourceName": name,
        "policyDefinitionName": policy,
        "policyAssignmentName": "org-cost-baseline",
    }


def _make_cost_agent() -> CostOptimizationAgent:
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
    return CostOptimizationAgent(resources_path=path, cfg=cfg)


async def _run_cost_safety_nets(
    advisor_recs: list[dict],
    policy_violations: list[dict],
) -> tuple[CostOptimizationAgent, list[ProposedAction]]:
    """Run cost agent scan with LLM calls mocked out.

    Returns (agent, proposals) so tests can inspect both agent.scan_notes and
    the returned proposals list.
    """
    agent = _make_cost_agent()
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
            "src.infrastructure.azure_tools.list_policy_violations_async",
            new=AsyncMock(return_value=policy_violations),
        ),
    ):
        mock_agent_obj = MagicMock()
        mock_agent_obj.run = AsyncMock()
        mock_oir.return_value.as_agent.return_value = mock_agent_obj
        proposals = await agent.scan(target_resource_group="test-rg")
    return agent, proposals


class TestCostAgentSafetyNets:
    """Phase 1A: deterministic post-scan safety nets for Cost agent."""

    async def test_advisor_high_impact_rec_added_when_llm_misses_it(self):
        """A HIGH-impact Advisor Cost recommendation produces a proposal."""
        rid = "/subscriptions/sub1/resourceGroups/test-rg/providers/Microsoft.Compute/virtualMachines/vm-idle"
        _, proposals = await _run_cost_safety_nets(
            advisor_recs=[_advisor_rec_cost(rid, "vm-idle", impact="High", desc="VM is undersized")],
            policy_violations=[],
        )
        assert len(proposals) == 1
        assert proposals[0].reason.startswith("ADVISOR-HIGH:")
        assert proposals[0].urgency == Urgency.HIGH
        assert proposals[0].action_type == ActionType.SCALE_DOWN

    async def test_advisor_medium_impact_rec_not_added(self):
        """Medium-impact Advisor recommendations are ignored — only HIGH triggers auto-propose."""
        rid = "/subscriptions/sub1/resourceGroups/test-rg/providers/Microsoft.Compute/virtualMachines/vm-medium"
        _, proposals = await _run_cost_safety_nets(
            advisor_recs=[_advisor_rec_cost(rid, "vm-medium", impact="Medium", desc="Idle VM")],
            policy_violations=[],
        )
        assert len(proposals) == 0

    async def test_advisor_empty_response_does_not_crash(self):
        """An empty Advisor response produces no proposals and no exception."""
        _, proposals = await _run_cost_safety_nets(advisor_recs=[], policy_violations=[])
        assert proposals == []

    async def test_advisor_rec_missing_description_is_skipped(self):
        """A recommendation without shortDescription is silently skipped."""
        rid = "/subscriptions/sub1/resourceGroups/test-rg/providers/Microsoft.Compute/virtualMachines/vm-nodesc"
        rec = {
            "id": f"{rid}/providers/Microsoft.Advisor/recommendations/abc",
            "impactedValue": "vm-nodesc",
            "impact": "High",
        }
        _, proposals = await _run_cost_safety_nets(advisor_recs=[rec], policy_violations=[])
        assert len(proposals) == 0

    async def test_policy_violation_added_when_llm_misses_it(self):
        """A Policy non-compliant resource produces an UPDATE_CONFIG proposal."""
        rid = "/subscriptions/sub1/resourceGroups/test-rg/providers/Microsoft.Storage/storageAccounts/sa-untagged"
        _, proposals = await _run_cost_safety_nets(
            advisor_recs=[],
            policy_violations=[_policy_violation_cost(rid, "sa-untagged", "require-cost-tags")],
        )
        assert len(proposals) == 1
        assert proposals[0].reason.startswith("POLICY-NONCOMPLIANT:")
        assert proposals[0].urgency == Urgency.MEDIUM
        assert proposals[0].action_type == ActionType.UPDATE_CONFIG

    async def test_policy_violation_missing_resource_id_skipped(self):
        """A violation record without resourceId is silently skipped."""
        _, proposals = await _run_cost_safety_nets(
            advisor_recs=[],
            policy_violations=[{"resourceName": "orphan", "policyDefinitionName": "some-policy"}],
        )
        assert len(proposals) == 0

    async def test_policy_violation_missing_policy_name_skipped(self):
        """A violation record without policyDefinitionName is silently skipped."""
        _, proposals = await _run_cost_safety_nets(
            advisor_recs=[],
            policy_violations=[{
                "resourceId": "/subscriptions/sub1/resourceGroups/test-rg/providers/Microsoft.Storage/storageAccounts/sa",
                "resourceName": "sa",
            }],
        )
        assert len(proposals) == 0

    async def test_scan_notes_populated_after_scan(self):
        """agent.scan_notes is set and non-empty after scan completes."""
        rid = "/subscriptions/sub1/resourceGroups/test-rg/providers/Microsoft.Compute/virtualMachines/vm-1"
        agent, _ = await _run_cost_safety_nets(
            advisor_recs=[_advisor_rec_cost(rid, "vm-1")],
            policy_violations=[],
        )
        assert hasattr(agent, "scan_notes")
        assert isinstance(agent.scan_notes, list)
        assert len(agent.scan_notes) > 0

    async def test_scan_notes_contain_scan_complete_entry(self):
        """scan_notes always contains a 'Scan complete' summary line."""
        agent, _ = await _run_cost_safety_nets(advisor_recs=[], policy_violations=[])
        assert any("Scan complete" in note for note in agent.scan_notes)
