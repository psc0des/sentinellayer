"""Tests for CostOptimizationAgent."""

import json
import tempfile
from pathlib import Path

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
        return agent.scan()

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
        """With no resources in the file, scan() returns an empty list."""
        minimal = {"resources": [], "dependency_edges": []}
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".json", delete=False
        ) as f:
            json.dump(minimal, f)
            tmp_path = f.name

        agent = CostOptimizationAgent(resources_path=tmp_path)
        assert agent.scan() == []

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
        assert agent.scan() == []

    def test_custom_resources_path_oversized_vm_flagged(self):
        """An oversized VM in a custom file should still be detected."""
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
        proposals = agent.scan()
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
        assert agent.scan() == []
