"""Tests for the Governance Decision Engine and SRI™ scoring."""

import pytest

from src.config import settings
from src.core.governance_engine import GovernanceDecisionEngine
from src.core.models import (
    ActionTarget,
    ActionType,
    BlastRadiusResult,
    FinancialResult,
    GovernanceVerdict,
    HistoricalResult,
    PolicyResult,
    PolicySeverity,
    PolicyViolation,
    ProposedAction,
    SRIVerdict,
    Urgency,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_action(action_type: ActionType = ActionType.SCALE_DOWN) -> ProposedAction:
    """Minimal valid ProposedAction for use in engine tests."""
    return ProposedAction(
        agent_id="test-agent",
        action_type=action_type,
        target=ActionTarget(
            resource_id="/subscriptions/demo/resourceGroups/dev/providers/Microsoft.Compute/virtualMachines/vm-01",
            resource_type="Microsoft.Compute/virtualMachines",
        ),
        reason="test",
        urgency=Urgency.LOW,
    )


def _make_results(
    infra: float = 0.0,
    policy: float = 0.0,
    historical: float = 0.0,
    cost: float = 0.0,
    violations: list | None = None,
) -> tuple[BlastRadiusResult, PolicyResult, HistoricalResult, FinancialResult]:
    """Build the four agent result objects with the given SRI dimension scores."""
    return (
        BlastRadiusResult(sri_infrastructure=infra),
        PolicyResult(sri_policy=policy, violations=violations or []),
        HistoricalResult(sri_historical=historical),
        FinancialResult(sri_cost=cost),
    )


def _critical_violation(policy_id: str = "POL-DR-001") -> PolicyViolation:
    return PolicyViolation(
        policy_id=policy_id,
        name="Disaster Recovery Protection",
        rule="Blocks destructive actions on DR resources",
        severity=PolicySeverity.CRITICAL,
    )


def _high_violation(policy_id: str = "POL-SEC-001") -> PolicyViolation:
    return PolicyViolation(
        policy_id=policy_id,
        name="Network Security Baseline",
        rule="NSG changes require security review",
        severity=PolicySeverity.HIGH,
    )


# ---------------------------------------------------------------------------
# Composite score calculation
# ---------------------------------------------------------------------------


class TestSRICompositeCalculation:
    """Validate the weighted-average formula for SRI Composite."""

    @pytest.fixture
    def engine(self):
        return GovernanceDecisionEngine()

    def test_all_zeros_gives_zero_composite(self, engine):
        action = _make_action()
        blast, policy, hist, fin = _make_results(0, 0, 0, 0)
        verdict = engine.evaluate(action, blast, policy, hist, fin)
        assert verdict.sentinel_risk_index.sri_composite == 0.0

    def test_all_100_gives_100_composite(self, engine):
        action = _make_action()
        blast, policy, hist, fin = _make_results(100, 100, 100, 100)
        verdict = engine.evaluate(action, blast, policy, hist, fin)
        assert verdict.sentinel_risk_index.sri_composite == 100.0

    def test_composite_formula_correct(self, engine):
        """30×0.30 + 20×0.25 + 40×0.25 + 50×0.20 = 9+5+10+10 = 34.0"""
        action = _make_action()
        blast, policy, hist, fin = _make_results(30, 20, 40, 50)
        verdict = engine.evaluate(action, blast, policy, hist, fin)
        expected = round(30 * 0.30 + 20 * 0.25 + 40 * 0.25 + 50 * 0.20, 2)
        assert verdict.sentinel_risk_index.sri_composite == expected

    def test_composite_within_bounds(self, engine):
        action = _make_action()
        blast, policy, hist, fin = _make_results(100, 100, 100, 100)
        verdict = engine.evaluate(action, blast, policy, hist, fin)
        assert 0.0 <= verdict.sentinel_risk_index.sri_composite <= 100.0

    def test_composite_preserves_dimension_scores(self, engine):
        """The breakdown must echo each agent's raw score unchanged."""
        action = _make_action()
        blast, policy, hist, fin = _make_results(30, 20, 40, 50)
        verdict = engine.evaluate(action, blast, policy, hist, fin)
        sri = verdict.sentinel_risk_index
        assert sri.sri_infrastructure == 30.0
        assert sri.sri_policy == 20.0
        assert sri.sri_historical == 40.0
        assert sri.sri_cost == 50.0

    def test_weights_sum_to_one(self):
        """The four configured weights must sum to exactly 1.0."""
        total = (
            settings.sri_weight_infrastructure
            + settings.sri_weight_policy
            + settings.sri_weight_historical
            + settings.sri_weight_cost
        )
        assert abs(total - 1.0) < 0.001


# ---------------------------------------------------------------------------
# Verdict decision rules
# ---------------------------------------------------------------------------


class TestSRIScoring:
    """Test approve / escalate / deny decision logic."""

    @pytest.fixture
    def engine(self):
        return GovernanceDecisionEngine()

    def test_low_risk_action_auto_approved(self, engine):
        """SRI = 0 → auto-approved."""
        action = _make_action()
        blast, policy, hist, fin = _make_results(0, 0, 0, 0)
        verdict = engine.evaluate(action, blast, policy, hist, fin)
        assert verdict.decision == SRIVerdict.APPROVED

    def test_moderate_risk_action_escalated(self, engine):
        """SRI = 40 (between 25 and 60) → escalated for human review."""
        # 60×0.30 + 60×0.25 + 28×0.25 + 0×0.20 = 18+15+7+0 = 40.0
        action = _make_action()
        blast = BlastRadiusResult(sri_infrastructure=60)
        policy_r = PolicyResult(sri_policy=60)
        hist = HistoricalResult(sri_historical=28)
        fin = FinancialResult(sri_cost=0)
        verdict = engine.evaluate(action, blast, policy_r, hist, fin)
        assert verdict.decision == SRIVerdict.ESCALATED

    def test_high_risk_action_denied(self, engine):
        """SRI = 100 → denied."""
        action = _make_action()
        blast, policy, hist, fin = _make_results(100, 100, 100, 100)
        verdict = engine.evaluate(action, blast, policy, hist, fin)
        assert verdict.decision == SRIVerdict.DENIED

    def test_critical_policy_violation_always_denied(self, engine):
        """Critical violation → DENIED even when composite score would be APPROVED."""
        action = _make_action()
        # composite = 0×0.30 + 40×0.25 + 0 + 0 = 10.0 → would normally be APPROVED
        blast = BlastRadiusResult(sri_infrastructure=0)
        policy_r = PolicyResult(
            sri_policy=40,
            violations=[_critical_violation()],
        )
        hist = HistoricalResult(sri_historical=0)
        fin = FinancialResult(sri_cost=0)
        verdict = engine.evaluate(action, blast, policy_r, hist, fin)
        assert verdict.decision == SRIVerdict.DENIED
        assert "critical" in verdict.reason.lower()

    def test_critical_violation_id_appears_in_reason(self, engine):
        """The denying policy ID must be named in the reason string."""
        action = _make_action()
        blast = BlastRadiusResult(sri_infrastructure=0)
        policy_r = PolicyResult(
            sri_policy=0,
            violations=[_critical_violation("POL-DR-001")],
        )
        hist = HistoricalResult(sri_historical=0)
        fin = FinancialResult(sri_cost=0)
        verdict = engine.evaluate(action, blast, policy_r, hist, fin)
        assert "POL-DR-001" in verdict.reason

    def test_high_violation_does_not_override_score(self, engine):
        """HIGH severity violation does not trigger the critical override rule."""
        action = _make_action()
        blast = BlastRadiusResult(sri_infrastructure=0)
        # sri_policy=0 → composite=0 → would be APPROVED without override
        policy_r = PolicyResult(sri_policy=0, violations=[_high_violation()])
        hist = HistoricalResult(sri_historical=0)
        fin = FinancialResult(sri_cost=0)
        verdict = engine.evaluate(action, blast, policy_r, hist, fin)
        assert verdict.decision == SRIVerdict.APPROVED

    def test_sri_composite_within_bounds(self, engine):
        action = _make_action()
        blast, policy, hist, fin = _make_results(100, 100, 100, 100)
        verdict = engine.evaluate(action, blast, policy, hist, fin)
        assert 0.0 <= verdict.sentinel_risk_index.sri_composite <= 100.0


# ---------------------------------------------------------------------------
# Threshold configuration
# ---------------------------------------------------------------------------


class TestSRIThresholds:
    """Validate the configured threshold values make sense."""

    def test_auto_approve_below_human_review(self):
        assert settings.sri_auto_approve_threshold < settings.sri_human_review_threshold

    def test_thresholds_within_valid_range(self):
        assert 0 <= settings.sri_auto_approve_threshold <= 100
        assert 0 <= settings.sri_human_review_threshold <= 100


# ---------------------------------------------------------------------------
# Exact boundary conditions
# ---------------------------------------------------------------------------


class TestBoundaryConditions:
    """Pin the exact values at threshold edges."""

    @pytest.fixture
    def engine(self):
        return GovernanceDecisionEngine()

    def test_sri_25_is_approved(self, engine):
        """SRI = 25.0 exactly → APPROVED (threshold is inclusive: ≤ 25)."""
        # 0×0.30 + 100×0.25 + 0×0.25 + 0×0.20 = 25.0 (0.25 is exact in binary)
        action = _make_action()
        blast = BlastRadiusResult(sri_infrastructure=0)
        policy_r = PolicyResult(sri_policy=100)
        hist = HistoricalResult(sri_historical=0)
        fin = FinancialResult(sri_cost=0)
        verdict = engine.evaluate(action, blast, policy_r, hist, fin)
        assert verdict.sentinel_risk_index.sri_composite == 25.0
        assert verdict.decision == SRIVerdict.APPROVED

    def test_sri_just_above_25_is_escalated(self, engine):
        """SRI = 26.5 (just above 25) → ESCALATED."""
        # 5×0.30 + 100×0.25 + 0 + 0 = 1.5 + 25.0 = 26.5
        action = _make_action()
        blast = BlastRadiusResult(sri_infrastructure=5)
        policy_r = PolicyResult(sri_policy=100)
        hist = HistoricalResult(sri_historical=0)
        fin = FinancialResult(sri_cost=0)
        verdict = engine.evaluate(action, blast, policy_r, hist, fin)
        assert verdict.sentinel_risk_index.sri_composite == 26.5
        assert verdict.decision == SRIVerdict.ESCALATED

    def test_sri_60_is_escalated(self, engine):
        """SRI = 60.0 exactly → ESCALATED (threshold is exclusive: > 60 is denied)."""
        # 100×0.30 + 100×0.25 + 20×0.25 + 0×0.20 = 30+25+5+0 = 60.0
        action = _make_action()
        blast = BlastRadiusResult(sri_infrastructure=100)
        policy_r = PolicyResult(sri_policy=100)
        hist = HistoricalResult(sri_historical=20)
        fin = FinancialResult(sri_cost=0)
        verdict = engine.evaluate(action, blast, policy_r, hist, fin)
        assert verdict.sentinel_risk_index.sri_composite == 60.0
        assert verdict.decision == SRIVerdict.ESCALATED

    def test_sri_61_is_denied(self, engine):
        """SRI = 61.0 (just above 60) → DENIED."""
        # 100×0.30 + 100×0.25 + 24×0.25 + 0×0.20 = 30+25+6+0 = 61.0
        action = _make_action()
        blast = BlastRadiusResult(sri_infrastructure=100)
        policy_r = PolicyResult(sri_policy=100)
        hist = HistoricalResult(sri_historical=24)
        fin = FinancialResult(sri_cost=0)
        verdict = engine.evaluate(action, blast, policy_r, hist, fin)
        assert verdict.sentinel_risk_index.sri_composite == 61.0
        assert verdict.decision == SRIVerdict.DENIED


# ---------------------------------------------------------------------------
# GovernanceVerdict structure
# ---------------------------------------------------------------------------


class TestGovernanceVerdictStructure:
    """Verify every field of the returned GovernanceVerdict is correctly set."""

    @pytest.fixture
    def engine(self):
        return GovernanceDecisionEngine()

    def test_returns_governance_verdict_instance(self, engine):
        action = _make_action()
        blast, policy, hist, fin = _make_results()
        verdict = engine.evaluate(action, blast, policy, hist, fin)
        assert isinstance(verdict, GovernanceVerdict)

    def test_verdict_contains_proposed_action(self, engine):
        action = _make_action()
        blast, policy, hist, fin = _make_results()
        verdict = engine.evaluate(action, blast, policy, hist, fin)
        assert verdict.proposed_action == action

    def test_verdict_has_unique_action_id_per_call(self, engine):
        """Each evaluate() call must produce a distinct action_id (UUID4)."""
        action = _make_action()
        blast, policy, hist, fin = _make_results()
        v1 = engine.evaluate(action, blast, policy, hist, fin)
        v2 = engine.evaluate(action, blast, policy, hist, fin)
        assert v1.action_id != v2.action_id

    def test_verdict_action_id_is_non_empty(self, engine):
        action = _make_action()
        blast, policy, hist, fin = _make_results()
        verdict = engine.evaluate(action, blast, policy, hist, fin)
        assert len(verdict.action_id) > 0

    def test_verdict_contains_all_agent_results(self, engine):
        action = _make_action()
        blast, policy, hist, fin = _make_results()
        verdict = engine.evaluate(action, blast, policy, hist, fin)
        assert "blast_radius" in verdict.agent_results
        assert "policy" in verdict.agent_results
        assert "historical" in verdict.agent_results
        assert "financial" in verdict.agent_results

    def test_verdict_reason_is_non_empty(self, engine):
        action = _make_action()
        blast, policy, hist, fin = _make_results()
        verdict = engine.evaluate(action, blast, policy, hist, fin)
        assert len(verdict.reason) > 0

    def test_verdict_thresholds_match_settings(self, engine):
        action = _make_action()
        blast, policy, hist, fin = _make_results()
        verdict = engine.evaluate(action, blast, policy, hist, fin)
        assert verdict.thresholds["auto_approve"] == settings.sri_auto_approve_threshold
        assert verdict.thresholds["human_review"] == settings.sri_human_review_threshold

    def test_approved_reason_mentions_approved(self, engine):
        action = _make_action()
        blast, policy, hist, fin = _make_results(0, 0, 0, 0)
        verdict = engine.evaluate(action, blast, policy, hist, fin)
        assert "APPROVED" in verdict.reason

    def test_escalated_reason_mentions_escalated(self, engine):
        # composite = 40 → ESCALATED
        action = _make_action()
        blast = BlastRadiusResult(sri_infrastructure=60)
        policy_r = PolicyResult(sri_policy=60)
        hist = HistoricalResult(sri_historical=28)
        fin = FinancialResult(sri_cost=0)
        verdict = engine.evaluate(action, blast, policy_r, hist, fin)
        assert "ESCALATED" in verdict.reason

    def test_denied_reason_mentions_denied(self, engine):
        action = _make_action()
        blast, policy, hist, fin = _make_results(100, 100, 100, 100)
        verdict = engine.evaluate(action, blast, policy, hist, fin)
        assert "DENIED" in verdict.reason

    def test_verdict_timestamp_is_set(self, engine):
        from datetime import datetime
        action = _make_action()
        blast, policy, hist, fin = _make_results()
        verdict = engine.evaluate(action, blast, policy, hist, fin)
        assert isinstance(verdict.timestamp, datetime)
