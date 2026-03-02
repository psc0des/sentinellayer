"""Tests for Decision Explanation Engine (Phase 17B).

Five tests verify the explanation and counterfactual analysis:

1. test_explanation_returns_all_fields — DecisionExplanation has all required fields
2. test_counterfactual_denied_shows_path_to_approval — DENIED shows path to lower verdicts
3. test_counterfactual_approved_shows_what_would_trigger — APPROVED shows escalation triggers
4. test_explanation_api_endpoint — GET /api/evaluations/{id}/explanation returns 200
5. test_explanation_handles_missing_evaluation — returns 404 for unknown ID
"""

from datetime import datetime, timezone

import pytest

from src.core.explanation_engine import DecisionExplainer
from src.core.models import (
    ActionTarget,
    ActionType,
    GovernanceVerdict,
    ProposedAction,
    SRIBreakdown,
    SRIVerdict,
    Urgency,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_action(resource_id: str = "vm-dr-01") -> ProposedAction:
    return ProposedAction(
        agent_id="cost-optimization-agent",
        action_type=ActionType.DELETE_RESOURCE,
        target=ActionTarget(
            resource_id=resource_id,
            resource_type="Microsoft.Compute/virtualMachines",
            current_monthly_cost=847.0,
        ),
        reason="VM idle for 30 days — estimated savings $847/month",
        urgency=Urgency.HIGH,
    )


def _make_verdict(
    decision: SRIVerdict = SRIVerdict.DENIED,
    composite: float = 77.0,
    infra: float = 65.0,
    policy: float = 95.0,
    historical: float = 50.0,
    cost: float = 63.0,
    action_id: str = "explain-test-001",
    violations: list | None = None,
) -> GovernanceVerdict:
    if violations is None:
        if decision == SRIVerdict.DENIED:
            violations = [
                {
                    "policy_id": "POL-DR-001",
                    "name": "Disaster Recovery Protection",
                    "rule": "Cannot delete disaster-recovery tagged resources",
                    "severity": "critical",
                }
            ]
        else:
            violations = []

    return GovernanceVerdict(
        action_id=action_id,
        timestamp=datetime.now(timezone.utc),
        proposed_action=_make_action(),
        skry_risk_index=SRIBreakdown(
            sri_infrastructure=infra,
            sri_policy=policy,
            sri_historical=historical,
            sri_cost=cost,
            sri_composite=composite,
        ),
        decision=decision,
        reason=f"{decision.value.upper()} — test verdict",
        agent_results={
            "blast_radius": {
                "agent": "blast_radius",
                "sri_infrastructure": infra,
                "affected_resources": ["vm-web-01", "payment-api"],
                "reasoning": "High blast radius due to 2 dependent services.",
            },
            "policy": {
                "agent": "policy_compliance",
                "sri_policy": policy,
                "violations": violations,
                "reasoning": "POL-DR-001 triggered for disaster-recovery tagged resource.",
            },
            "historical": {
                "agent": "historical_pattern",
                "sri_historical": historical,
                "reasoning": "Similar deletion caused outage on 2025-12-10.",
            },
            "financial": {
                "agent": "financial_impact",
                "sri_cost": cost,
                "reasoning": "Estimated savings $847/month but carries budget risk.",
            },
        },
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestDecisionExplainer:
    """Tests for the DecisionExplainer engine."""

    @pytest.mark.asyncio
    async def test_explanation_returns_all_fields(self):
        """DecisionExplanation must have all required fields with correct types."""
        explainer = DecisionExplainer()
        verdict = _make_verdict(SRIVerdict.DENIED, action_id="all-fields-test")
        action = _make_action()

        explanation = await explainer.explain(verdict, action)

        # summary — non-empty string
        assert isinstance(explanation.summary, str)
        assert len(explanation.summary) > 20, "Summary should be at least a meaningful sentence"

        # primary_factor — non-empty string
        assert isinstance(explanation.primary_factor, str)
        assert len(explanation.primary_factor) > 0

        # contributing_factors — list of 4 Factor objects
        assert len(explanation.contributing_factors) == 4
        for f in explanation.contributing_factors:
            assert f.dimension
            assert 0 <= f.score <= 100
            assert 0 < f.weight <= 1.0
            assert f.weighted_contribution >= 0

        # contributing_factors should be sorted by weighted_contribution descending
        contributions = [f.weighted_contribution for f in explanation.contributing_factors]
        assert contributions == sorted(contributions, reverse=True)

        # policy_violations — list of strings
        assert isinstance(explanation.policy_violations, list)
        assert len(explanation.policy_violations) >= 1  # DENIED with violations

        # risk_highlights — list of strings
        assert isinstance(explanation.risk_highlights, list)
        assert len(explanation.risk_highlights) > 0

        # counterfactuals — list of Counterfactual objects
        assert isinstance(explanation.counterfactuals, list)
        assert len(explanation.counterfactuals) > 0
        for cf in explanation.counterfactuals:
            assert cf.change_description
            assert isinstance(cf.predicted_new_score, float)
            assert cf.predicted_new_verdict in ("APPROVED", "ESCALATED", "DENIED")
            assert cf.explanation

    @pytest.mark.asyncio
    async def test_counterfactual_denied_shows_path_to_approval(self):
        """DENIED verdict should show counterfactuals with lower scores."""
        explainer = DecisionExplainer()
        verdict = _make_verdict(SRIVerdict.DENIED, composite=77.0, action_id="denied-cf-test")
        action = _make_action()

        explanation = await explainer.explain(verdict, action)

        # Should have counterfactuals
        assert len(explanation.counterfactuals) >= 1

        # At least one counterfactual should have a lower score than original
        original_score = verdict.skry_risk_index.sri_composite
        lower_scores = [cf for cf in explanation.counterfactuals if cf.predicted_new_score < original_score]
        assert len(lower_scores) >= 1, "DENIED should show at least one path to lower score"

        # At least one should show a different (better) verdict
        better_verdicts = [
            cf for cf in explanation.counterfactuals
            if cf.predicted_new_verdict in ("APPROVED", "ESCALATED")
        ]
        assert len(better_verdicts) >= 1, "DENIED should show at least one path to ESCALATED or APPROVED"

    @pytest.mark.asyncio
    async def test_counterfactual_approved_shows_what_would_trigger(self):
        """APPROVED verdict should show what would have triggered escalation/denial."""
        explainer = DecisionExplainer()
        verdict = _make_verdict(
            SRIVerdict.APPROVED,
            composite=14.1,
            infra=10.0,
            policy=0.0,
            historical=20.0,
            cost=30.0,
            action_id="approved-cf-test",
            violations=[],
        )
        action = _make_action("vm-web-01")

        explanation = await explainer.explain(verdict, action)

        # Should have counterfactuals
        assert len(explanation.counterfactuals) >= 1

        # At least one counterfactual should show ESCALATED or DENIED
        escalation_triggers = [
            cf for cf in explanation.counterfactuals
            if cf.predicted_new_verdict in ("ESCALATED", "DENIED")
        ]
        assert len(escalation_triggers) >= 1, "APPROVED should show what would trigger escalation"

        # At least one should have a higher score than original
        original_score = verdict.skry_risk_index.sri_composite
        higher_scores = [cf for cf in explanation.counterfactuals if cf.predicted_new_score > original_score]
        assert len(higher_scores) >= 1

    def test_explanation_api_endpoint(self):
        """GET /api/evaluations/{id}/explanation should return 200."""
        from src.api.dashboard_api import app, _get_tracker
        from starlette.testclient import TestClient

        # Record a real verdict via the tracker
        tracker = _get_tracker()
        verdict = _make_verdict(SRIVerdict.DENIED, action_id="api-explain-test-001")
        tracker.record(verdict)

        with TestClient(app) as client:
            resp = client.get("/api/evaluations/api-explain-test-001/explanation")

        assert resp.status_code == 200
        data = resp.json()
        assert "summary" in data
        assert "primary_factor" in data
        assert "contributing_factors" in data
        assert "counterfactuals" in data
        assert len(data["contributing_factors"]) == 4
        assert len(data["counterfactuals"]) >= 1

    @pytest.mark.asyncio
    async def test_explanation_handles_missing_evaluation(self):
        """GET /api/evaluations/{id}/explanation should return 404 for unknown ID."""
        from src.api.dashboard_api import app
        from starlette.testclient import TestClient

        with TestClient(app) as client:
            resp = client.get("/api/evaluations/nonexistent-id-99999/explanation")

        assert resp.status_code == 404
        assert "not found" in resp.json()["detail"].lower()
