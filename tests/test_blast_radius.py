"""Tests for Blast Radius Simulation Agent (SRI:Infrastructure)."""

import pytest

from src.core.models import ActionTarget, ActionType, BlastRadiusResult, ProposedAction, Urgency
from src.governance_agents.blast_radius_agent import BlastRadiusAgent


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_action(
    resource_id: str,
    action_type: ActionType = ActionType.SCALE_DOWN,
    resource_type: str = "Microsoft.Compute/virtualMachines",
) -> ProposedAction:
    """Create a minimal ProposedAction for testing."""
    return ProposedAction(
        agent_id="test-agent",
        action_type=action_type,
        target=ActionTarget(
            resource_id=resource_id,
            resource_type=resource_type,
        ),
        reason="Test action",
        urgency=Urgency.LOW,
    )


# ---------------------------------------------------------------------------
# Test class
# ---------------------------------------------------------------------------


class TestBlastRadiusAgent:

    @pytest.fixture(scope="class")
    def agent(self):
        return BlastRadiusAgent()

    # ------------------------------------------------------------------
    # Return type and field validity
    # ------------------------------------------------------------------

    def test_returns_blast_radius_result_model(self, agent):
        """evaluate() always returns a BlastRadiusResult instance."""
        action = _make_action("vm-23", ActionType.RESTART_SERVICE)
        result = agent.evaluate(action)
        assert isinstance(result, BlastRadiusResult)
        assert result.agent == "blast_radius"

    def test_score_within_bounds_for_all_action_types(self, agent):
        """SRI:Infrastructure must always be in [0, 100] for every action type."""
        for action_type in ActionType:
            action = _make_action("api-server-03", action_type)
            result = agent.evaluate(action)
            assert 0.0 <= result.sri_infrastructure <= 100.0

    def test_result_lists_are_always_lists(self, agent):
        """All list fields must be list instances even when empty."""
        action = _make_action("vm-23", ActionType.RESTART_SERVICE)
        result = agent.evaluate(action)
        assert isinstance(result.affected_resources, list)
        assert isinstance(result.affected_services, list)
        assert isinstance(result.single_points_of_failure, list)
        assert isinstance(result.availability_zones_impacted, list)

    def test_reasoning_is_non_empty_string(self, agent):
        """Reasoning field must always be a non-empty string."""
        action = _make_action("api-server-03", ActionType.DELETE_RESOURCE)
        result = agent.evaluate(action)
        assert isinstance(result.reasoning, str)
        assert len(result.reasoning) > 0

    # ------------------------------------------------------------------
    # Scoring direction — destructive > conservative
    # ------------------------------------------------------------------

    def test_delete_scores_higher_than_scale_up_same_resource(self, agent):
        """DELETE_RESOURCE must produce a higher score than SCALE_UP."""
        scale_up = _make_action("api-server-03", ActionType.SCALE_UP)
        delete = _make_action("api-server-03", ActionType.DELETE_RESOURCE)
        assert agent.evaluate(scale_up).sri_infrastructure < agent.evaluate(delete).sri_infrastructure

    def test_create_resource_scores_low_for_unknown_resource(self, agent):
        """CREATE_RESOURCE on a brand-new resource has minimal blast radius."""
        action = _make_action("brand-new-vm-99", ActionType.CREATE_RESOURCE)
        result = agent.evaluate(action)
        # Only the base action score (3.0) — resource not in graph
        assert result.sri_infrastructure <= 10.0

    # ------------------------------------------------------------------
    # High-risk scenarios — should score > 60 (DENIED band)
    # ------------------------------------------------------------------

    def test_delete_critical_nsg_scores_above_60(self, agent):
        """Deleting nsg-east (criticality=critical, governs 3 subnets) → DENIED band."""
        action = _make_action(
            "nsg-east",
            ActionType.DELETE_RESOURCE,
            resource_type="Microsoft.Network/networkSecurityGroups",
        )
        result = agent.evaluate(action)
        assert result.sri_infrastructure > 60.0

    def test_delete_aks_prod_scores_above_60(self, agent):
        """Deleting aks-prod (critical, hosts 4 services) → DENIED band."""
        action = _make_action(
            "aks-prod",
            ActionType.DELETE_RESOURCE,
            resource_type="Microsoft.ContainerService/managedClusters",
        )
        result = agent.evaluate(action)
        assert result.sri_infrastructure > 60.0

    # ------------------------------------------------------------------
    # Low-risk scenario — should score ≤ 25 (auto-approve band)
    # ------------------------------------------------------------------

    def test_scale_down_medium_resource_with_no_dependents_scores_low(self, agent):
        """SCALE_DOWN on web-tier-01 (medium criticality, zero dependents) → auto-approve."""
        action = _make_action("web-tier-01", ActionType.SCALE_DOWN)
        result = agent.evaluate(action)
        # base 15 + medium 10 = 25; no dependents, no services, no extra SPOFs
        assert result.sri_infrastructure <= 25.0

    # ------------------------------------------------------------------
    # Affected resource detection
    # ------------------------------------------------------------------

    def test_delete_api_server_includes_all_dependents(self, agent):
        """Deleting api-server-03 must list all three downstream dependents."""
        action = _make_action("api-server-03", ActionType.DELETE_RESOURCE)
        result = agent.evaluate(action)
        expected_downstream = {"web-frontend", "mobile-backend", "payment-api"}
        assert expected_downstream.issubset(set(result.affected_resources))

    def test_delete_api_server_includes_upstream_dependencies(self, agent):
        """Deleting api-server-03 must also list its infrastructure dependencies."""
        action = _make_action("api-server-03", ActionType.DELETE_RESOURCE)
        result = agent.evaluate(action)
        # api-server-03 depends on nsg-east and vnet-prod-subnet-api
        assert "nsg-east" in result.affected_resources

    def test_nsg_governed_subnets_appear_in_affected_resources(self, agent):
        """Modifying nsg-east must expose the subnets it governs."""
        action = _make_action(
            "nsg-east",
            ActionType.MODIFY_NSG,
            resource_type="Microsoft.Network/networkSecurityGroups",
        )
        result = agent.evaluate(action)
        assert "vnet-prod-subnet-api" in result.affected_resources

    # ------------------------------------------------------------------
    # Affected services detection
    # ------------------------------------------------------------------

    def test_delete_aks_prod_surfaces_hosted_services(self, agent):
        """Deleting aks-prod must list all hosted Kubernetes workloads."""
        action = _make_action(
            "aks-prod",
            ActionType.DELETE_RESOURCE,
            resource_type="Microsoft.ContainerService/managedClusters",
        )
        result = agent.evaluate(action)
        expected = {"payment-api", "notification-service", "order-processing", "user-auth"}
        assert expected.issubset(set(result.affected_services))

    def test_delete_storage_account_surfaces_consumers(self, agent):
        """Deleting storageshared01 must list all four consumer services."""
        action = _make_action("storageshared01", ActionType.DELETE_RESOURCE)
        result = agent.evaluate(action)
        expected = {"order-processing", "notification-service", "analytics-pipeline", "audit-logger"}
        assert expected.issubset(set(result.affected_services))

    def test_no_affected_services_for_plain_vm(self, agent):
        """api-server-03 has no hosted services — affected_services should be empty."""
        action = _make_action("api-server-03", ActionType.RESTART_SERVICE)
        result = agent.evaluate(action)
        assert result.affected_services == []

    # ------------------------------------------------------------------
    # Single point of failure detection
    # ------------------------------------------------------------------

    def test_critical_target_appears_in_spofs(self, agent):
        """nsg-east (criticality=critical) must be in single_points_of_failure."""
        action = _make_action(
            "nsg-east",
            ActionType.MODIFY_NSG,
            resource_type="Microsoft.Network/networkSecurityGroups",
        )
        result = agent.evaluate(action)
        assert "nsg-east" in result.single_points_of_failure

    def test_critical_resource_in_blast_radius_also_flagged_as_spof(self, agent):
        """api-server-03 blast radius hits nsg-east (critical) — should be in SPOFs."""
        action = _make_action("api-server-03", ActionType.DELETE_RESOURCE)
        result = agent.evaluate(action)
        # nsg-east is a dependency of api-server-03 and is criticality=critical
        assert "nsg-east" in result.single_points_of_failure

    def test_medium_criticality_resource_not_flagged_as_spof(self, agent):
        """web-tier-01 (criticality=medium) should not appear as an SPOF."""
        action = _make_action("web-tier-01", ActionType.SCALE_DOWN)
        result = agent.evaluate(action)
        assert "web-tier-01" not in result.single_points_of_failure

    def test_no_spofs_for_isolated_unknown_resource(self, agent):
        """A resource not in the graph cannot produce any SPOF entries."""
        action = _make_action("totally-unknown-resource", ActionType.DELETE_RESOURCE)
        result = agent.evaluate(action)
        assert result.single_points_of_failure == []

    # ------------------------------------------------------------------
    # Availability zone detection
    # ------------------------------------------------------------------

    def test_affected_zones_captured_for_known_resource(self, agent):
        """Zones should be non-empty when the target is in the graph."""
        action = _make_action("aks-prod", ActionType.RESTART_SERVICE)
        result = agent.evaluate(action)
        assert len(result.availability_zones_impacted) > 0
        assert "eastus" in result.availability_zones_impacted

    # ------------------------------------------------------------------
    # Unknown / unregistered resource handling
    # ------------------------------------------------------------------

    def test_unknown_resource_returns_valid_result_without_crash(self, agent):
        """An unrecognized resource_id must return a valid result, not raise."""
        action = _make_action(
            "/subscriptions/demo/providers/unknown/resource/does-not-exist",
            ActionType.DELETE_RESOURCE,
        )
        result = agent.evaluate(action)
        assert isinstance(result, BlastRadiusResult)
        assert 0.0 <= result.sri_infrastructure <= 100.0
        assert result.affected_resources == []
        assert result.affected_services == []

    def test_unknown_resource_reasoning_mentions_not_found(self, agent):
        """Reasoning for an unknown resource should explain it wasn't in the graph."""
        action = _make_action("ghost-resource", ActionType.DELETE_RESOURCE)
        result = agent.evaluate(action)
        assert "not found" in result.reasoning.lower()

    # ------------------------------------------------------------------
    # Resource lookup by full Azure resource ID path
    # ------------------------------------------------------------------

    def test_resource_lookup_works_with_full_azure_id(self, agent):
        """Agent resolves api-server-03 from its full Azure resource path."""
        full_id = (
            "/subscriptions/demo/resourceGroups/prod/providers/"
            "Microsoft.Compute/virtualMachines/api-server-03"
        )
        action = _make_action(full_id, ActionType.RESTART_SERVICE)
        result = agent.evaluate(action)
        # Should resolve the resource and produce a non-trivial blast radius
        assert len(result.affected_resources) > 0

    def test_resource_lookup_works_with_short_name(self, agent):
        """Agent resolves 'vm-23' directly by short name."""
        action = _make_action("vm-23", ActionType.SCALE_DOWN)
        result = agent.evaluate(action)
        assert len(result.affected_resources) > 0

    # ------------------------------------------------------------------
    # Custom resources path
    # ------------------------------------------------------------------

    def test_custom_resources_path(self, tmp_path):
        """Agent can load from a custom JSON file for testing isolation."""
        custom = tmp_path / "resources.json"
        custom.write_text("""{
            "resources": [
                {
                    "name": "test-vm",
                    "type": "Microsoft.Compute/virtualMachines",
                    "location": "westus",
                    "tags": {"criticality": "high"},
                    "dependencies": [],
                    "dependents": ["test-app"]
                }
            ],
            "dependency_edges": []
        }""")
        agent = BlastRadiusAgent(resources_path=custom)
        action = _make_action("test-vm", ActionType.DELETE_RESOURCE)
        result = agent.evaluate(action)
        assert result.sri_infrastructure > 0
        assert "test-app" in result.affected_resources
        assert "westus" in result.availability_zones_impacted
