"""Tests for Financial Impact Agent (SRI:Cost)."""

import pytest

from src.core.models import ActionTarget, ActionType, FinancialResult, ProposedAction, Urgency
from src.governance_agents.financial_agent import (
    FinancialImpactAgent,
    _COST_UNCERTAINTY_PENALTY,
    _OVER_OPTIMISATION_PENALTY,
    _RECOVERY_COST_PER_SERVICE,
    _SCALE_DOWN_ESTIMATE,
    _SCALE_UP_ESTIMATE,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_action(
    resource_id: str,
    action_type: ActionType = ActionType.SCALE_DOWN,
    resource_type: str = "Microsoft.Compute/virtualMachines",
    current_monthly_cost: float | None = None,
    projected_savings_monthly: float | None = None,
) -> ProposedAction:
    """Create a minimal ProposedAction for testing."""
    return ProposedAction(
        agent_id="test-agent",
        action_type=action_type,
        target=ActionTarget(
            resource_id=resource_id,
            resource_type=resource_type,
            current_monthly_cost=current_monthly_cost,
        ),
        reason="Test action",
        urgency=Urgency.LOW,
        projected_savings_monthly=projected_savings_monthly,
    )


# ---------------------------------------------------------------------------
# Test class
# ---------------------------------------------------------------------------


class TestFinancialImpactAgent:

    @pytest.fixture(scope="class")
    def agent(self):
        return FinancialImpactAgent()

    # ------------------------------------------------------------------
    # Return type and field validity
    # ------------------------------------------------------------------

    def test_returns_financial_result_model(self, agent):
        """evaluate() always returns a FinancialResult instance."""
        action = _make_action("api-server-03", ActionType.SCALE_DOWN)
        result = agent.evaluate(action)
        assert isinstance(result, FinancialResult)
        assert result.agent == "financial_impact"

    def test_score_within_bounds_for_all_action_types(self, agent):
        """SRI:Cost must always be in [0, 100] for every action type."""
        for action_type in ActionType:
            action = _make_action("api-server-03", action_type)
            result = agent.evaluate(action)
            assert 0.0 <= result.sri_cost <= 100.0, (
                f"Score out of bounds for {action_type}: {result.sri_cost}"
            )

    def test_reasoning_is_non_empty_string(self, agent):
        """Reasoning must always be a non-empty string."""
        action = _make_action("api-server-03", ActionType.DELETE_RESOURCE)
        result = agent.evaluate(action)
        assert isinstance(result.reasoning, str)
        assert len(result.reasoning) > 0

    def test_reasoning_mentions_action_type(self, agent):
        """Reasoning must reference the action type."""
        action = _make_action("api-server-03", ActionType.DELETE_RESOURCE)
        result = agent.evaluate(action)
        assert "delete_resource" in result.reasoning.lower()

    # ------------------------------------------------------------------
    # Zero-cost actions
    # ------------------------------------------------------------------

    def test_restart_service_has_zero_cost_change(self, agent):
        """RESTART_SERVICE has no billing impact — monthly change must be 0."""
        action = _make_action("api-server-03", ActionType.RESTART_SERVICE)
        result = agent.evaluate(action)
        assert result.immediate_monthly_change == 0.0

    def test_restart_service_scores_zero(self, agent):
        """RESTART_SERVICE carries zero financial risk — SRI:Cost should be 0."""
        action = _make_action("api-server-03", ActionType.RESTART_SERVICE)
        result = agent.evaluate(action)
        assert result.sri_cost == 0.0

    def test_modify_nsg_has_zero_cost_change(self, agent):
        """MODIFY_NSG does not change billing — monthly change must be 0."""
        action = _make_action(
            "nsg-east",
            ActionType.MODIFY_NSG,
            resource_type="Microsoft.Network/networkSecurityGroups",
        )
        result = agent.evaluate(action)
        assert result.immediate_monthly_change == 0.0

    # ------------------------------------------------------------------
    # Cost estimation — DELETE
    # ------------------------------------------------------------------

    def test_delete_uses_graph_monthly_cost(self, agent):
        """DELETE with no explicit cost falls back to the resource graph."""
        # api-server-03 has monthly_cost: 847.00 in seed_resources.json
        action = _make_action("api-server-03", ActionType.DELETE_RESOURCE)
        result = agent.evaluate(action)
        assert result.immediate_monthly_change == pytest.approx(-847.0)

    def test_delete_uses_target_current_monthly_cost(self, agent):
        """DELETE uses target.current_monthly_cost when provided."""
        action = _make_action(
            "api-server-03",
            ActionType.DELETE_RESOURCE,
            current_monthly_cost=500.0,
        )
        result = agent.evaluate(action)
        assert result.immediate_monthly_change == pytest.approx(-500.0)

    def test_immediate_monthly_change_is_negative_for_delete(self, agent):
        """DELETE should produce a negative monthly change (cost reduction)."""
        action = _make_action("api-server-03", ActionType.DELETE_RESOURCE)
        result = agent.evaluate(action)
        assert result.immediate_monthly_change < 0.0

    def test_delete_unknown_resource_reports_zero_change(self, agent):
        """DELETE on an unknown resource with no cost data returns 0 change."""
        action = _make_action("ghost-vm-xyz", ActionType.DELETE_RESOURCE)
        result = agent.evaluate(action)
        assert result.immediate_monthly_change == 0.0

    # ------------------------------------------------------------------
    # Cost estimation — SCALE operations
    # ------------------------------------------------------------------

    def test_scale_down_estimates_30_percent_reduction(self, agent):
        """SCALE_DOWN estimates a 30 % cost reduction of the current monthly cost."""
        # api-server-03: monthly_cost = 847.00 → 30% = 254.10
        action = _make_action("api-server-03", ActionType.SCALE_DOWN)
        result = agent.evaluate(action)
        assert result.immediate_monthly_change == pytest.approx(-847.0 * _SCALE_DOWN_ESTIMATE)

    def test_scale_up_estimates_50_percent_increase(self, agent):
        """SCALE_UP estimates a 50 % cost increase of the current monthly cost."""
        # api-server-03: monthly_cost = 847.00 → 50% = 423.50
        action = _make_action("api-server-03", ActionType.SCALE_UP)
        result = agent.evaluate(action)
        assert result.immediate_monthly_change == pytest.approx(847.0 * _SCALE_UP_ESTIMATE)

    def test_scale_up_monthly_change_is_positive(self, agent):
        """SCALE_UP should produce a positive monthly change (cost increase)."""
        action = _make_action("api-server-03", ActionType.SCALE_UP)
        result = agent.evaluate(action)
        assert result.immediate_monthly_change > 0.0

    # ------------------------------------------------------------------
    # projected_savings_monthly takes priority
    # ------------------------------------------------------------------

    def test_projected_savings_overrides_graph_cost(self, agent):
        """projected_savings_monthly takes priority over the resource graph lookup."""
        # api-server-03 costs $847 in graph, but agent says it saves $300
        action = _make_action(
            "api-server-03",
            ActionType.SCALE_DOWN,
            projected_savings_monthly=300.0,
        )
        result = agent.evaluate(action)
        assert result.immediate_monthly_change == pytest.approx(-300.0)

    def test_projected_savings_marked_as_not_uncertain(self, agent):
        """An action with explicit savings should NOT incur the uncertainty penalty."""
        # With explicit savings (not uncertain): same magnitude, no +10 penalty
        certain = _make_action(
            "web-tier-01",
            ActionType.SCALE_DOWN,
            projected_savings_monthly=126.0,
        )
        # Without explicit savings (estimated 30% of $420 = $126, uncertain): +10 penalty
        uncertain = _make_action("web-tier-01", ActionType.SCALE_DOWN)

        certain_result = agent.evaluate(certain)
        uncertain_result = agent.evaluate(uncertain)

        assert uncertain_result.sri_cost == pytest.approx(
            certain_result.sri_cost + _COST_UNCERTAINTY_PENALTY
        )

    # ------------------------------------------------------------------
    # Scoring bands — high risk
    # ------------------------------------------------------------------

    def test_delete_aks_prod_scores_above_60(self, agent):
        """Deleting aks-prod ($2100/month, 4 hosted services) → DENIED band."""
        # monthly_cost = 2100 → magnitude 70 × 1.5 = 105, +20 over-opt → 100 (cap)
        action = _make_action(
            "aks-prod",
            ActionType.DELETE_RESOURCE,
            resource_type="Microsoft.ContainerService/managedClusters",
        )
        result = agent.evaluate(action)
        assert result.sri_cost > 60.0

    def test_delete_api_server_scores_high(self, agent):
        """Deleting api-server-03 ($847, 3 dependents) → high financial risk."""
        action = _make_action("api-server-03", ActionType.DELETE_RESOURCE)
        result = agent.evaluate(action)
        assert result.sri_cost > 60.0

    # ------------------------------------------------------------------
    # Scoring bands — low risk
    # ------------------------------------------------------------------

    def test_small_explicit_savings_scores_low(self, agent):
        """Modest explicit savings with no dependents → auto-approve band."""
        # $50 savings, web-tier-01 has no dependents → score should be ≤ 25
        action = _make_action(
            "web-tier-01",
            ActionType.SCALE_DOWN,
            projected_savings_monthly=50.0,
        )
        result = agent.evaluate(action)
        assert result.sri_cost <= 25.0

    def test_scale_up_scores_lower_than_delete_same_resource(self, agent):
        """SCALE_UP should score lower than DELETE_RESOURCE for the same resource."""
        scale_up = _make_action("api-server-03", ActionType.SCALE_UP)
        delete = _make_action("api-server-03", ActionType.DELETE_RESOURCE)
        assert agent.evaluate(scale_up).sri_cost < agent.evaluate(delete).sri_cost

    # ------------------------------------------------------------------
    # Over-optimisation detection
    # ------------------------------------------------------------------

    def test_delete_with_dependents_triggers_over_optimisation(self, agent):
        """DELETE on api-server-03 (3 dependents) must trigger over-optimisation."""
        action = _make_action("api-server-03", ActionType.DELETE_RESOURCE)
        result = agent.evaluate(action)
        assert result.over_optimization_risk is not None
        assert result.over_optimization_risk["detected"] is True

    def test_delete_aks_prod_triggers_over_optimisation(self, agent):
        """DELETE on aks-prod (4 hosted services) must trigger over-optimisation."""
        action = _make_action(
            "aks-prod",
            ActionType.DELETE_RESOURCE,
            resource_type="Microsoft.ContainerService/managedClusters",
        )
        result = agent.evaluate(action)
        assert result.over_optimization_risk is not None

    def test_scale_down_with_dependents_triggers_over_optimisation(self, agent):
        """SCALE_DOWN on api-server-03 (3 dependents) also triggers over-optimisation."""
        action = _make_action("api-server-03", ActionType.SCALE_DOWN)
        result = agent.evaluate(action)
        assert result.over_optimization_risk is not None

    def test_delete_resource_without_dependents_no_over_optimisation(self, agent):
        """DELETE on web-tier-01 (0 dependents) must NOT trigger over-optimisation."""
        action = _make_action("web-tier-01", ActionType.DELETE_RESOURCE)
        result = agent.evaluate(action)
        assert result.over_optimization_risk is None

    def test_restart_never_triggers_over_optimisation(self, agent):
        """RESTART_SERVICE must never trigger over-optimisation (not a cost-reducer)."""
        action = _make_action("api-server-03", ActionType.RESTART_SERVICE)
        result = agent.evaluate(action)
        assert result.over_optimization_risk is None

    def test_scale_up_never_triggers_over_optimisation(self, agent):
        """SCALE_UP increases cost — it is not an over-optimisation action."""
        action = _make_action("api-server-03", ActionType.SCALE_UP)
        result = agent.evaluate(action)
        assert result.over_optimization_risk is None

    def test_over_optimisation_risk_structure(self, agent):
        """over_optimization_risk dict must have the expected keys."""
        action = _make_action("api-server-03", ActionType.DELETE_RESOURCE)
        result = agent.evaluate(action)
        over_opt = result.over_optimization_risk
        assert over_opt is not None
        assert "detected" in over_opt
        assert "affected_services" in over_opt
        assert "affected_count" in over_opt
        assert "monthly_savings" in over_opt
        assert "estimated_recovery_cost" in over_opt
        assert "reason" in over_opt

    def test_over_optimisation_affected_count_matches_dependents(self, agent):
        """affected_count must equal the number of dependents of the resource."""
        # api-server-03 has 3 dependents in seed_resources.json
        action = _make_action("api-server-03", ActionType.DELETE_RESOURCE)
        result = agent.evaluate(action)
        assert result.over_optimization_risk["affected_count"] == 3

    def test_over_optimisation_recovery_cost_is_count_times_rate(self, agent):
        """estimated_recovery_cost = affected_count × _RECOVERY_COST_PER_SERVICE."""
        action = _make_action("api-server-03", ActionType.DELETE_RESOURCE)
        result = agent.evaluate(action)
        over_opt = result.over_optimization_risk
        assert over_opt["estimated_recovery_cost"] == pytest.approx(
            over_opt["affected_count"] * _RECOVERY_COST_PER_SERVICE
        )

    def test_over_optimisation_adds_penalty_to_score(self, agent):
        """Over-optimisation detection must add exactly 20 pts to the score."""
        # web-tier-01: no dependents → no over-opt penalty
        no_dep = _make_action(
            "web-tier-01", ActionType.DELETE_RESOURCE, current_monthly_cost=847.0
        )
        # api-server-03: 3 dependents → over-opt penalty
        with_dep = _make_action(
            "api-server-03", ActionType.DELETE_RESOURCE, current_monthly_cost=847.0
        )
        no_dep_result = agent.evaluate(no_dep)
        with_dep_result = agent.evaluate(with_dep)
        # Both have same cost ($847, exact), same action — only difference is the penalty
        assert with_dep_result.sri_cost == pytest.approx(
            no_dep_result.sri_cost + _OVER_OPTIMISATION_PENALTY
        )

    def test_storage_consumers_trigger_over_optimisation(self, agent):
        """storageshared01 has 4 consumers — DELETE should trigger over-optimisation."""
        action = _make_action("storageshared01", ActionType.DELETE_RESOURCE)
        result = agent.evaluate(action)
        assert result.over_optimization_risk is not None
        assert result.over_optimization_risk["affected_count"] == 4

    # ------------------------------------------------------------------
    # Cost uncertainty
    # ------------------------------------------------------------------

    def test_delete_unknown_resource_is_uncertain(self, agent):
        """DELETE on an unrecognised resource with no cost data incurs the uncertainty penalty."""
        action = _make_action("totally-unknown-vm", ActionType.DELETE_RESOURCE)
        result = agent.evaluate(action)
        # score should include the uncertainty penalty (no magnitude, but +10)
        assert result.sri_cost >= _COST_UNCERTAINTY_PENALTY

    def test_scale_down_without_cost_data_is_uncertain(self, agent):
        """SCALE_DOWN with no graph entry and no target cost gets the uncertainty penalty."""
        action = _make_action(
            "totally-unknown-vm",
            ActionType.SCALE_DOWN,
        )
        result = agent.evaluate(action)
        assert result.sri_cost >= _COST_UNCERTAINTY_PENALTY

    # ------------------------------------------------------------------
    # 90-day projection
    # ------------------------------------------------------------------

    def test_projection_90_day_is_always_populated(self, agent):
        """projection_90_day must be a non-None dict for every action type."""
        for action_type in ActionType:
            action = _make_action("api-server-03", action_type)
            result = agent.evaluate(action)
            assert result.projection_90_day is not None
            assert isinstance(result.projection_90_day, dict)

    def test_projection_90_day_has_required_keys(self, agent):
        """projection_90_day must contain the expected financial forecast keys."""
        action = _make_action("api-server-03", ActionType.DELETE_RESOURCE)
        result = agent.evaluate(action)
        proj = result.projection_90_day
        for key in ("month_1", "month_2", "month_3", "total_90_day", "annualized", "note"):
            assert key in proj

    def test_projection_total_is_3x_monthly_change(self, agent):
        """total_90_day must equal 3 × immediate_monthly_change."""
        action = _make_action("api-server-03", ActionType.DELETE_RESOURCE)
        result = agent.evaluate(action)
        assert result.projection_90_day["total_90_day"] == pytest.approx(
            result.immediate_monthly_change * 3
        )

    def test_projection_annualized_is_12x_monthly_change(self, agent):
        """annualized must equal 12 × immediate_monthly_change."""
        action = _make_action("api-server-03", ActionType.DELETE_RESOURCE)
        result = agent.evaluate(action)
        assert result.projection_90_day["annualized"] == pytest.approx(
            result.immediate_monthly_change * 12
        )

    def test_projection_all_months_equal_monthly_change(self, agent):
        """Each month in the projection must equal the immediate_monthly_change."""
        action = _make_action("api-server-03", ActionType.DELETE_RESOURCE)
        result = agent.evaluate(action)
        proj = result.projection_90_day
        change = result.immediate_monthly_change
        assert proj["month_1"] == pytest.approx(change)
        assert proj["month_2"] == pytest.approx(change)
        assert proj["month_3"] == pytest.approx(change)

    # ------------------------------------------------------------------
    # Resource lookup
    # ------------------------------------------------------------------

    def test_resource_lookup_by_full_azure_id(self, agent):
        """Full Azure resource ID is resolved to short name for graph lookup."""
        full_id = (
            "/subscriptions/demo/resourceGroups/prod/providers/"
            "Microsoft.Compute/virtualMachines/api-server-03"
        )
        action = _make_action(full_id, ActionType.DELETE_RESOURCE)
        result = agent.evaluate(action)
        # Should resolve to api-server-03 (monthly_cost $847) → non-zero change
        assert result.immediate_monthly_change == pytest.approx(-847.0)

    # ------------------------------------------------------------------
    # Custom resources path
    # ------------------------------------------------------------------

    def test_custom_resources_path(self, tmp_path):
        """Agent can load from a custom JSON file for isolated testing."""
        custom = tmp_path / "resources.json"
        custom.write_text("""{
            "resources": [
                {
                    "name": "test-vm",
                    "type": "Microsoft.Compute/virtualMachines",
                    "monthly_cost": 200.0,
                    "tags": {},
                    "dependencies": [],
                    "dependents": []
                }
            ],
            "dependency_edges": []
        }""")
        agent = FinancialImpactAgent(resources_path=custom)
        action = _make_action("test-vm", ActionType.DELETE_RESOURCE)
        result = agent.evaluate(action)
        assert result.immediate_monthly_change == pytest.approx(-200.0)
        assert result.sri_cost > 0.0
