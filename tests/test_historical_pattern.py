"""Tests for Historical Pattern Agent (SRI:Historical)."""

import pytest

from src.core.models import ActionTarget, ActionType, HistoricalResult, ProposedAction, Urgency
from src.governance_agents.historical_agent import (
    HistoricalPatternAgent,
    _SEVERITY_WEIGHT,
    _W_ACTION,
    _W_RESOURCE_TYPE,
    _W_RESOURCE_NAME,
    _W_TAGS,
)


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


class TestHistoricalPatternAgent:

    @pytest.fixture(scope="class")
    def agent(self):
        return HistoricalPatternAgent()

    # ------------------------------------------------------------------
    # Return type and field validity
    # ------------------------------------------------------------------

    def test_returns_historical_result_model(self, agent):
        """evaluate() always returns a HistoricalResult instance."""
        action = _make_action("some-resource", ActionType.SCALE_DOWN)
        result = agent.evaluate(action)
        assert isinstance(result, HistoricalResult)
        assert result.agent == "historical_pattern"

    def test_score_within_bounds_for_all_action_types(self, agent):
        """SRI:Historical must always be in [0, 100] regardless of action type."""
        for action_type in ActionType:
            action = _make_action("api-server-03", action_type)
            result = agent.evaluate(action)
            assert 0.0 <= result.sri_historical <= 100.0, (
                f"Score out of bounds for {action_type}: {result.sri_historical}"
            )

    def test_result_fields_are_correct_types(self, agent):
        """All HistoricalResult fields must have the expected Python types."""
        action = _make_action("vm-23", ActionType.DELETE_RESOURCE)
        result = agent.evaluate(action)
        assert isinstance(result.similar_incidents, list)
        assert isinstance(result.reasoning, str)
        # most_relevant_incident and recommended_procedure are Optional
        # (can be None or a value — just check no AttributeError)
        _ = result.most_relevant_incident
        _ = result.recommended_procedure

    def test_reasoning_is_non_empty_string(self, agent):
        """Reasoning must always be populated."""
        action = _make_action("vm-23", ActionType.DELETE_RESOURCE)
        result = agent.evaluate(action)
        assert len(result.reasoning) > 0

    # ------------------------------------------------------------------
    # No-match scenario
    # ------------------------------------------------------------------

    def test_unknown_resource_type_scores_zero(self, agent):
        """An action on a resource type absent from incident history scores 0."""
        action = _make_action(
            "logic-app-01",
            ActionType.CREATE_RESOURCE,
            resource_type="Microsoft.Logic/workflows",  # not in seed_incidents.json
        )
        result = agent.evaluate(action)
        assert result.sri_historical == 0.0
        assert result.similar_incidents == []
        assert result.most_relevant_incident is None
        assert result.recommended_procedure is None

    def test_no_match_reasoning_says_no_history(self, agent):
        """When no incidents match, reasoning must explain there's no history."""
        action = _make_action(
            "logic-app-01",
            ActionType.CREATE_RESOURCE,
            resource_type="Microsoft.Logic/workflows",
        )
        result = agent.evaluate(action)
        assert "no historical" in result.reasoning.lower()

    # ------------------------------------------------------------------
    # High-risk scenarios — critical or high severity near-perfect matches
    # ------------------------------------------------------------------

    def test_restart_payment_api_finds_critical_incident(self, agent):
        """RESTART_SERVICE on payment-api should surface INC-2025-0847 (critical)."""
        action = _make_action(
            "payment-api",
            ActionType.RESTART_SERVICE,
            resource_type="Microsoft.ContainerService/managedClusters",
        )
        result = agent.evaluate(action)
        incident_ids = [i.incident_id for i in result.similar_incidents]
        assert "INC-2025-0847" in incident_ids

    def test_restart_payment_api_scores_maximum(self, agent):
        """RESTART_SERVICE on payment-api has similarity=1.0 × critical=100 → score=100."""
        action = _make_action(
            "payment-api",
            ActionType.RESTART_SERVICE,
            resource_type="Microsoft.ContainerService/managedClusters",
        )
        result = agent.evaluate(action)
        assert result.sri_historical == 100.0

    def test_modify_nsg_east_finds_security_incident(self, agent):
        """MODIFY_NSG on nsg-east should surface INC-2025-0923 (critical security exposure)."""
        action = _make_action(
            "nsg-east",
            ActionType.MODIFY_NSG,
            resource_type="Microsoft.Network/networkSecurityGroups",
        )
        result = agent.evaluate(action)
        incident_ids = [i.incident_id for i in result.similar_incidents]
        assert "INC-2025-0923" in incident_ids

    def test_modify_nsg_east_scores_maximum(self, agent):
        """MODIFY_NSG on nsg-east has similarity=1.0 × critical=100 → score=100."""
        action = _make_action(
            "nsg-east",
            ActionType.MODIFY_NSG,
            resource_type="Microsoft.Network/networkSecurityGroups",
        )
        result = agent.evaluate(action)
        assert result.sri_historical == 100.0

    def test_delete_vm23_finds_dr_incident(self, agent):
        """DELETE_RESOURCE on vm-23 must surface INC-2025-1204 (DR VM deletion)."""
        action = _make_action(
            "vm-23",
            ActionType.DELETE_RESOURCE,
            resource_type="Microsoft.Compute/virtualMachines",
        )
        result = agent.evaluate(action)
        incident_ids = [i.incident_id for i in result.similar_incidents]
        assert "INC-2025-1204" in incident_ids

    def test_delete_vm23_scores_above_60(self, agent):
        """Deleting the DR VM (similarity 0.90, severity high) → DENIED band."""
        action = _make_action(
            "vm-23",
            ActionType.DELETE_RESOURCE,
            resource_type="Microsoft.Compute/virtualMachines",
        )
        result = agent.evaluate(action)
        assert result.sri_historical > 60.0

    def test_scale_down_aks_finds_traffic_incident(self, agent):
        """SCALE_DOWN on aks-prod should surface INC-2025-1089 (Black Friday incident)."""
        action = _make_action(
            "aks-prod",
            ActionType.SCALE_DOWN,
            resource_type="Microsoft.ContainerService/managedClusters",
        )
        result = agent.evaluate(action)
        incident_ids = [i.incident_id for i in result.similar_incidents]
        assert "INC-2025-1089" in incident_ids

    def test_scale_down_aks_scores_high(self, agent):
        """SCALE_DOWN on aks-prod (similarity=1.0, severity=high) → score ≥ 60."""
        action = _make_action(
            "aks-prod",
            ActionType.SCALE_DOWN,
            resource_type="Microsoft.ContainerService/managedClusters",
        )
        result = agent.evaluate(action)
        assert result.sri_historical >= 60.0

    # ------------------------------------------------------------------
    # Low-risk scenario — low severity successful precedent
    # ------------------------------------------------------------------

    def test_scale_up_vm_finds_successful_precedent(self, agent):
        """SCALE_UP on a VM should surface INC-2026-0012 (successful, low severity)."""
        action = _make_action(
            "web-tier-01",
            ActionType.SCALE_UP,
            resource_type="Microsoft.Compute/virtualMachines",
        )
        result = agent.evaluate(action)
        incident_ids = [i.incident_id for i in result.similar_incidents]
        assert "INC-2026-0012" in incident_ids

    def test_scale_up_vm_scores_low(self, agent):
        """SCALE_UP with only low-severity historical precedent → low score."""
        action = _make_action(
            "web-tier-01",
            ActionType.SCALE_UP,
            resource_type="Microsoft.Compute/virtualMachines",
        )
        result = agent.evaluate(action)
        # INC-2026-0012: sim=0.80, severity=low → 0.80 × 10 = 8.0
        assert result.sri_historical < 26.0  # stays in auto-approve band

    def test_update_config_sql_finds_credential_incident(self, agent):
        """UPDATE_CONFIG on sql-prod finds INC-2025-0634 (credential rotation failure)."""
        action = _make_action(
            "sql-prod",
            ActionType.UPDATE_CONFIG,
            resource_type="Microsoft.Sql/servers",
        )
        result = agent.evaluate(action)
        incident_ids = [i.incident_id for i in result.similar_incidents]
        assert "INC-2025-0634" in incident_ids

    def test_update_config_sql_scores_in_escalation_band(self, agent):
        """UPDATE_CONFIG on sql-prod (similarity=1.0, severity=medium) → 26–60 band."""
        action = _make_action(
            "sql-prod",
            ActionType.UPDATE_CONFIG,
            resource_type="Microsoft.Sql/servers",
        )
        result = agent.evaluate(action)
        # sim=1.0 × medium=40 = 40.0 → ESCALATED
        assert 26.0 <= result.sri_historical <= 60.0

    # ------------------------------------------------------------------
    # Output fields — most_relevant_incident and recommended_procedure
    # ------------------------------------------------------------------

    def test_most_relevant_incident_is_highest_similarity(self, agent):
        """most_relevant_incident must be the incident with the highest similarity."""
        action = _make_action(
            "payment-api",
            ActionType.RESTART_SERVICE,
            resource_type="Microsoft.ContainerService/managedClusters",
        )
        result = agent.evaluate(action)
        assert result.most_relevant_incident is not None
        # It must be the same as the first in the sorted list
        assert result.most_relevant_incident.incident_id == result.similar_incidents[0].incident_id

    def test_recommended_procedure_comes_from_top_incident_lesson(self, agent):
        """recommended_procedure must equal the lesson of the most relevant incident."""
        action = _make_action(
            "nsg-east",
            ActionType.MODIFY_NSG,
            resource_type="Microsoft.Network/networkSecurityGroups",
        )
        result = agent.evaluate(action)
        assert result.recommended_procedure is not None
        assert result.recommended_procedure == result.most_relevant_incident.lesson

    def test_most_relevant_is_none_when_no_matches(self, agent):
        """When no incidents qualify, most_relevant_incident must be None."""
        action = _make_action(
            "some-resource",
            ActionType.CREATE_RESOURCE,
            resource_type="Microsoft.Logic/workflows",
        )
        result = agent.evaluate(action)
        assert result.most_relevant_incident is None
        assert result.recommended_procedure is None

    # ------------------------------------------------------------------
    # SimilarIncident fields
    # ------------------------------------------------------------------

    def test_all_similar_incidents_have_similarity_score(self, agent):
        """Every SimilarIncident returned must have similarity_score populated."""
        action = _make_action(
            "vm-23",
            ActionType.DELETE_RESOURCE,
            resource_type="Microsoft.Compute/virtualMachines",
        )
        result = agent.evaluate(action)
        for inc in result.similar_incidents:
            assert inc.similarity_score is not None
            assert 0.0 <= inc.similarity_score <= 1.0

    def test_similar_incidents_sorted_by_similarity_descending(self, agent):
        """Incidents must be returned from highest to lowest similarity."""
        action = _make_action(
            "vm-23",
            ActionType.DELETE_RESOURCE,
            resource_type="Microsoft.Compute/virtualMachines",
        )
        result = agent.evaluate(action)
        scores = [i.similarity_score or 0 for i in result.similar_incidents]
        assert scores == sorted(scores, reverse=True)

    def test_similar_incident_fields_are_populated(self, agent):
        """All mandatory SimilarIncident fields must be non-empty strings."""
        action = _make_action(
            "nsg-east",
            ActionType.MODIFY_NSG,
            resource_type="Microsoft.Network/networkSecurityGroups",
        )
        result = agent.evaluate(action)
        best = result.most_relevant_incident
        assert best is not None
        assert best.incident_id
        assert best.description
        assert best.action_taken
        assert best.outcome
        assert best.lesson
        assert best.service
        assert best.severity
        assert best.date

    # ------------------------------------------------------------------
    # Reasoning
    # ------------------------------------------------------------------

    def test_reasoning_mentions_incident_id_when_match_found(self, agent):
        """Reasoning must reference the top incident ID when a match is found."""
        action = _make_action(
            "nsg-east",
            ActionType.MODIFY_NSG,
            resource_type="Microsoft.Network/networkSecurityGroups",
        )
        result = agent.evaluate(action)
        assert "INC-2025-0923" in result.reasoning

    def test_reasoning_mentions_count_of_similar_incidents(self, agent):
        """Reasoning should mention how many similar incidents were found."""
        action = _make_action(
            "vm-23",
            ActionType.DELETE_RESOURCE,
            resource_type="Microsoft.Compute/virtualMachines",
        )
        result = agent.evaluate(action)
        count = len(result.similar_incidents)
        assert str(count) in result.reasoning

    # ------------------------------------------------------------------
    # Similarity dimension unit tests (whitebox)
    # ------------------------------------------------------------------

    def test_action_type_contributes_0_40(self, agent):
        """Matching action type alone contributes exactly 0.40 to similarity."""
        # restart_service action on a type with no incidents → only action match
        # We need a resource type present in one incident to isolate just action match
        # Use Microsoft.Sql/servers — INC-2025-0634 is update_config on sql type
        # Use RESTART_SERVICE (not update_config) to get only resource type match if any
        # Actually let's use the compute method directly via a known combo:
        # DELETE_RESOURCE on Microsoft.Logic/workflows → no type match, no name, no tag
        # INC-2025-0456 (delete_resource, Microsoft.Storage/storageAccounts):
        #   action match 0.40 + type no 0 + name no 0 + tag match 0.10 = 0.50
        # INC-2025-1204 (delete_resource, Microsoft.Compute/virtualMachines):
        #   action match 0.40 + type no 0 + name no 0 + tag no 0 = 0.40
        sim = agent._compute_similarity(
            {
                "action_taken": "delete_resource:some-other-resource",
                "resource_type": "Microsoft.Logic/workflows",
                "tags": [],
            },
            _make_action("brand-new-vm", ActionType.DELETE_RESOURCE, "Microsoft.Compute/virtualMachines"),
        )
        assert sim == pytest.approx(_W_ACTION)

    def test_resource_type_match_contributes_0_30(self, agent):
        """Matching resource type alone contributes exactly 0.30 to similarity."""
        sim = agent._compute_similarity(
            {
                "action_taken": "scale_up:other-resource",
                "resource_type": "Microsoft.Compute/virtualMachines",
                "tags": [],
            },
            _make_action("brand-new-vm", ActionType.DELETE_RESOURCE, "Microsoft.Compute/virtualMachines"),
        )
        assert sim == pytest.approx(_W_RESOURCE_TYPE)

    def test_resource_name_match_contributes_0_20(self, agent):
        """Matching resource name alone contributes exactly 0.20 to similarity."""
        sim = agent._compute_similarity(
            {
                "action_taken": "scale_up:my-special-vm:some-detail",
                "resource_type": "Microsoft.Network/differentType",
                "tags": [],
            },
            _make_action(
                "my-special-vm",
                ActionType.DELETE_RESOURCE,
                "Microsoft.Compute/virtualMachines",
            ),
        )
        assert sim == pytest.approx(_W_RESOURCE_NAME)

    def test_tag_match_contributes_0_10(self, agent):
        """Matching a tag keyword alone contributes exactly 0.10 to similarity."""
        sim = agent._compute_similarity(
            {
                "action_taken": "some_other_action:other-resource",
                "resource_type": "Microsoft.Logic/differentType",
                "tags": ["scale-down", "premature-optimization"],
            },
            _make_action("vm-x", ActionType.SCALE_DOWN, "Microsoft.Compute/virtualMachines"),
        )
        assert sim == pytest.approx(_W_TAGS)

    def test_full_match_similarity_is_1_0(self, agent):
        """All four dimensions matching gives similarity = 1.0."""
        sim = agent._compute_similarity(
            {
                "action_taken": "restart_service:payment-api",
                "resource_type": "Microsoft.ContainerService/managedClusters",
                "tags": ["restart"],
            },
            _make_action(
                "payment-api",
                ActionType.RESTART_SERVICE,
                "Microsoft.ContainerService/managedClusters",
            ),
        )
        assert sim == pytest.approx(1.0)

    def test_no_match_similarity_is_0(self, agent):
        """Nothing matching gives similarity = 0.0."""
        sim = agent._compute_similarity(
            {
                "action_taken": "update_config:sql-server",
                "resource_type": "Microsoft.Sql/servers",
                "tags": ["config-change"],
            },
            _make_action("web-tier-01", ActionType.SCALE_UP, "Microsoft.Compute/virtualMachines"),
        )
        assert sim == 0.0

    # ------------------------------------------------------------------
    # Resource ID lookup — full Azure path vs short name
    # ------------------------------------------------------------------

    def test_resource_lookup_with_full_azure_id(self, agent):
        """Full Azure resource ID is resolved to its short name for matching."""
        full_id = (
            "/subscriptions/demo/resourceGroups/prod/providers/"
            "Microsoft.Network/networkSecurityGroups/nsg-east"
        )
        action = _make_action(
            full_id,
            ActionType.MODIFY_NSG,
            resource_type="Microsoft.Network/networkSecurityGroups",
        )
        result = agent.evaluate(action)
        # Should match INC-2025-0923 which has "nsg-east" in action_taken
        incident_ids = [i.incident_id for i in result.similar_incidents]
        assert "INC-2025-0923" in incident_ids

    # ------------------------------------------------------------------
    # Custom incidents path
    # ------------------------------------------------------------------

    def test_custom_incidents_path(self, tmp_path):
        """Agent can load from a custom JSON file for isolated testing."""
        custom = tmp_path / "incidents.json"
        custom.write_text("""[
            {
                "incident_id": "TEST-001",
                "description": "Test scale down incident",
                "action_taken": "scale_down:test-vm",
                "outcome": "Test outage",
                "lesson": "Do not scale down test-vm",
                "service": "test-service",
                "severity": "medium",
                "date": "2026-01-01",
                "resource_type": "Microsoft.Compute/virtualMachines",
                "tags": ["scale-down"]
            }
        ]""")
        agent = HistoricalPatternAgent(incidents_path=custom)
        action = _make_action(
            "test-vm",
            ActionType.SCALE_DOWN,
            resource_type="Microsoft.Compute/virtualMachines",
        )
        result = agent.evaluate(action)
        assert result.sri_historical > 0.0
        assert any(i.incident_id == "TEST-001" for i in result.similar_incidents)
        assert result.recommended_procedure == "Do not scale down test-vm"

    def test_empty_incidents_file_scores_zero(self, tmp_path):
        """An empty incident history produces a score of 0 for any action."""
        empty = tmp_path / "empty.json"
        empty.write_text("[]")
        agent = HistoricalPatternAgent(incidents_path=empty)
        action = _make_action(
            "payment-api",
            ActionType.RESTART_SERVICE,
            resource_type="Microsoft.ContainerService/managedClusters",
        )
        result = agent.evaluate(action)
        assert result.sri_historical == 0.0
        assert result.similar_incidents == []
