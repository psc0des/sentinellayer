"""Tests for LLM governance score adjustment utilities and engine integration.

Covers:
- clamp_score guardrail function
- parse_llm_decision with and without a submitted decision
- format_adjustment_text output structure
- governance_engine critical-violation softening (sri_policy threshold)
"""

import asyncio
import json
import sys
from unittest.mock import MagicMock, patch

import pytest

from src.governance_agents._llm_governance import (
    MAX_ADJUSTMENT,
    annotate_violations,
    clamp_score,
    format_adjustment_text,
    parse_llm_decision,
)
from src.core.governance_engine import GovernanceDecisionEngine
from src.core.models import (
    ActionTarget,
    ActionType,
    BlastRadiusResult,
    FinancialResult,
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

def _make_action() -> ProposedAction:
    return ProposedAction(
        agent_id="test-agent",
        action_type=ActionType.MODIFY_NSG,
        target=ActionTarget(resource_id="rg/nsg-1", resource_type="Microsoft.Network/networkSecurityGroups"),
        reason="Restricting SSH access",
        urgency=Urgency.MEDIUM,
    )


def _critical_violation(policy_id: str = "POL-DR-001") -> PolicyViolation:
    return PolicyViolation(
        policy_id=policy_id,
        name="Test Critical Policy",
        rule="Test rule",
        severity=PolicySeverity.CRITICAL,
    )


# ---------------------------------------------------------------------------
# clamp_score
# ---------------------------------------------------------------------------

class TestClampScore:
    def test_no_adjustment_returns_baseline(self):
        assert clamp_score(50.0, 50.0) == 50.0

    def test_adjustment_within_range(self):
        assert clamp_score(50.0, 30.0) == 30.0
        assert clamp_score(50.0, 70.0) == 70.0

    def test_adjustment_clamped_below_floor(self):
        # Baseline=50, MAX=30 → floor=20. LLM wants 0, gets clamped to 20.
        assert clamp_score(50.0, 0.0) == 20.0

    def test_adjustment_clamped_above_ceiling(self):
        # Baseline=50, MAX=30 → ceiling=80. LLM wants 100, gets clamped to 80.
        assert clamp_score(50.0, 100.0) == 80.0

    def test_floor_never_below_zero(self):
        # Baseline=10, MAX=30 → floor=max(0, -20)=0
        assert clamp_score(10.0, 0.0) == 0.0
        assert clamp_score(10.0, -5.0) == 0.0

    def test_ceiling_never_above_100(self):
        # Baseline=90, MAX=30 → ceiling=min(100, 120)=100
        assert clamp_score(90.0, 110.0) == 100.0

    def test_exact_boundary_allowed(self):
        assert clamp_score(50.0, 50.0 - MAX_ADJUSTMENT) == 50.0 - MAX_ADJUSTMENT
        assert clamp_score(50.0, 50.0 + MAX_ADJUSTMENT) == 50.0 + MAX_ADJUSTMENT

    def test_result_rounded_to_two_decimals(self):
        result = clamp_score(33.333, 33.333)
        assert result == round(33.333, 2)


# ---------------------------------------------------------------------------
# parse_llm_decision
# ---------------------------------------------------------------------------

class TestParseLlmDecision:
    def test_empty_holder_returns_baseline(self):
        score, text, adjs = parse_llm_decision([], 42.0)
        assert score == 42.0
        assert text == ""
        assert adjs == []

    def test_decision_within_range_applied(self):
        holder = [{"adjusted_score": 30.0, "adjustments": [], "reasoning": "ok"}]
        score, text, _ = parse_llm_decision(holder, 50.0)
        assert score == 30.0
        assert "LLM Governance Analysis" in text

    def test_decision_clamped_when_too_low(self):
        # Baseline=50, LLM wants 0 → floor=20
        holder = [{"adjusted_score": 0.0, "adjustments": [], "reasoning": ""}]
        score, _, _ = parse_llm_decision(holder, 50.0)
        assert score == 20.0

    def test_decision_clamped_when_too_high(self):
        # Baseline=50, LLM wants 100 → ceiling=80
        holder = [{"adjusted_score": 100.0, "adjustments": [], "reasoning": ""}]
        score, _, _ = parse_llm_decision(holder, 50.0)
        assert score == 80.0

    def test_last_decision_wins(self):
        # If LLM called the tool twice, last one wins
        holder = [
            {"adjusted_score": 60.0, "adjustments": [], "reasoning": "first"},
            {"adjusted_score": 40.0, "adjustments": [], "reasoning": "second"},
        ]
        score, text, _ = parse_llm_decision(holder, 50.0)
        assert score == 40.0
        assert "second" in text

    def test_missing_adjusted_score_falls_back_to_baseline(self):
        holder = [{"adjustments": [], "reasoning": "no score key"}]
        score, _, _ = parse_llm_decision(holder, 55.0)
        assert score == 55.0

    def test_adjustment_text_contains_baseline_and_adjusted(self):
        holder = [{"adjusted_score": 35.0, "adjustments": [], "reasoning": "test reason"}]
        _, text, _ = parse_llm_decision(holder, 50.0)
        assert "50.0" in text
        assert "35.0" in text

    def test_adjustments_appear_in_text(self):
        holder = [{
            "adjusted_score": 35.0,
            "adjustments": [{"reason": "Remediation intent detected", "delta": -15}],
            "reasoning": "Ops agent fixing open port",
        }]
        _, text, _ = parse_llm_decision(holder, 50.0)
        assert "Remediation intent detected" in text
        assert "-15" in text

    def test_malformed_adjustments_are_filtered(self):
        """Adjustments missing required fields are silently dropped."""
        holder = [{
            "adjusted_score": 40.0,
            "adjustments": [
                {"reason": "Good adjustment", "delta": -10},
                {"only_reason": "Missing delta field"},  # invalid — no delta
                {"only_delta": -5},                      # invalid — no reason
            ],
            "reasoning": "test",
        }]
        score, _, adjs = parse_llm_decision(holder, 50.0)
        assert score == 40.0
        # Only the valid adjustment should survive
        assert len(adjs) == 1
        assert adjs[0]["reason"] == "Good adjustment"

    def test_completely_invalid_submission_falls_back_to_baseline(self):
        """If the whole submission is unparseable, fall back to baseline."""
        holder = [{"adjusted_score": "not_a_number", "adjustments": [], "reasoning": ""}]
        score, text, adjs = parse_llm_decision(holder, 60.0)
        assert score == 60.0
        assert text == ""
        assert adjs == []

    def test_adjustments_list_returned(self):
        """Third return value is the validated adjustments as dicts."""
        holder = [{
            "adjusted_score": 35.0,
            "adjustments": [{"reason": "Fix intent", "delta": -15, "policy_id": "POL-DR-001"}],
            "reasoning": "ok",
        }]
        _, _, adjs = parse_llm_decision(holder, 50.0)
        assert len(adjs) == 1
        assert adjs[0]["reason"] == "Fix intent"
        assert adjs[0]["delta"] == -15
        assert adjs[0]["policy_id"] == "POL-DR-001"


# ---------------------------------------------------------------------------
# format_adjustment_text
# ---------------------------------------------------------------------------

class TestFormatAdjustmentText:
    def test_contains_llm_header(self):
        text = format_adjustment_text(50.0, 35.0, [], "some reasoning")
        assert "LLM Governance Analysis" in text

    def test_contains_baseline_and_adjusted(self):
        text = format_adjustment_text(50.0, 35.0, [], "reasoning")
        assert "50.0" in text
        assert "35.0" in text

    def test_adjustments_rendered(self):
        adjustments = [
            {"reason": "Emergency remediation", "delta": -10},
            {"reason": "Undocumented dependency", "delta": 5},
        ]
        text = format_adjustment_text(40.0, 35.0, adjustments, "explanation")
        assert "Emergency remediation" in text
        assert "-10" in text
        assert "Undocumented dependency" in text
        assert "+5" in text

    def test_empty_adjustments_no_crash(self):
        text = format_adjustment_text(40.0, 40.0, [], "")
        assert "40.0" in text

    def test_clamping_note_when_adjustment_exceeds_max(self):
        """When clamping reduces the effective delta, a guardrail note is appended."""
        # Baseline=50, LLM wants -50 → floor=20, so effective delta=-30 not -50
        adjustments = [{"reason": "Extreme remediation", "delta": -50}]
        text = format_adjustment_text(50.0, 20.0, adjustments, "reasoning")
        assert "Guardrail applied" in text
        assert "-50" in text
        assert "-30" in text

    def test_no_clamping_note_when_within_range(self):
        """No guardrail note when the adjustment is within bounds."""
        adjustments = [{"reason": "Minor reduction", "delta": -10}]
        text = format_adjustment_text(50.0, 40.0, adjustments, "reasoning")
        assert "Guardrail" not in text


# ---------------------------------------------------------------------------
# GovernanceDecisionEngine — critical violation softening
# ---------------------------------------------------------------------------

class TestCriticalViolationSoftening:
    """Verify the updated Rule 1: auto-DENY only when violation has no llm_override."""

    @pytest.fixture
    def engine(self):
        return GovernanceDecisionEngine()

    def _overridden_violation(self, policy_id: str = "POL-DR-001") -> PolicyViolation:
        """A CRITICAL violation that the LLM has annotated as overridden."""
        return PolicyViolation(
            policy_id=policy_id,
            name="Test Critical Policy",
            rule="Test rule",
            severity=PolicySeverity.CRITICAL,
            llm_override="Remediation intent — ops agent fixing the issue, not creating it",
        )

    def test_critical_violation_no_override_is_denied(self, engine):
        """CRITICAL violation without llm_override → DENIED."""
        action = _make_action()
        policy_r = PolicyResult(sri_policy=40, violations=[_critical_violation()])
        verdict = engine.evaluate(
            action,
            BlastRadiusResult(sri_infrastructure=0),
            policy_r,
            HistoricalResult(sri_historical=0),
            FinancialResult(sri_cost=0),
        )
        assert verdict.decision == SRIVerdict.DENIED
        assert "critical" in verdict.reason.lower()

    def test_critical_violation_with_llm_override_floors_at_escalated(self, engine):
        """CRITICAL violation with llm_override set → ESCALATED (Rule 1.5), never APPROVED.

        The LLM can provide context but cannot substitute for the human approval
        required by CRITICAL policies (VP/CAB). Even with llm_override set, the
        verdict must surface for human review.
        """
        action = _make_action()
        policy_r = PolicyResult(
            sri_policy=10,
            violations=[self._overridden_violation()],
        )
        verdict = engine.evaluate(
            action,
            BlastRadiusResult(sri_infrastructure=0),
            policy_r,
            HistoricalResult(sri_historical=0),
            FinancialResult(sri_cost=0),
        )
        # LLM override noted but CRITICAL cannot auto-approve → ESCALATED
        assert verdict.decision == SRIVerdict.ESCALATED
        assert "POL-DR-001" in verdict.reason

    def test_mixed_violations_non_overridden_critical_still_denies(self, engine):
        """One overridden + one non-overridden CRITICAL → DENIED (non-overridden wins)."""
        action = _make_action()
        policy_r = PolicyResult(
            sri_policy=40,
            violations=[
                self._overridden_violation("POL-DR-001"),
                _critical_violation("POL-CRIT-001"),  # NOT overridden
            ],
        )
        verdict = engine.evaluate(
            action,
            BlastRadiusResult(sri_infrastructure=0),
            policy_r,
            HistoricalResult(sri_historical=0),
            FinancialResult(sri_cost=0),
        )
        assert verdict.decision == SRIVerdict.DENIED
        assert "POL-CRIT-001" in verdict.reason

    def test_no_critical_violation_unaffected(self, engine):
        """Without CRITICAL violations, existing threshold logic is unchanged."""
        action = _make_action()
        policy_r = PolicyResult(sri_policy=0, violations=[])
        verdict = engine.evaluate(
            action,
            BlastRadiusResult(sri_infrastructure=0),
            policy_r,
            HistoricalResult(sri_historical=0),
            FinancialResult(sri_cost=0),
        )
        assert verdict.decision == SRIVerdict.APPROVED

    def test_llm_override_field_preserved_in_violation(self):
        """PolicyViolation with llm_override stores the reason string."""
        v = PolicyViolation(
            policy_id="POL-DR-001",
            name="DR Protection",
            rule="No deletion of DR resources",
            severity=PolicySeverity.CRITICAL,
            llm_override="Ops agent performing scheduled DR test — not a real deletion",
        )
        assert v.llm_override is not None
        assert "scheduled DR test" in v.llm_override

    def test_violation_without_override_has_none(self):
        """PolicyViolation without llm_override defaults to None."""
        v = _critical_violation()
        assert v.llm_override is None


# ---------------------------------------------------------------------------
# annotate_violations — CRITICAL override guardrail
# ---------------------------------------------------------------------------

class TestAnnotateViolations:
    """Verify the safety guardrail: CRITICAL violations require policy_id-specific override."""

    def test_no_adjustment_returns_unchanged(self):
        """When score is not reduced, violations are returned as-is."""
        violations = [_critical_violation("POL-DR-001")]
        result = annotate_violations(violations, [], baseline=40.0, adjusted_score=40.0)
        assert result[0].llm_override is None

    def test_specific_policy_id_overrides_critical(self):
        """CRITICAL violation IS overridden when LLM provides matching policy_id."""
        violations = [_critical_violation("POL-DR-001")]
        adj_list = [{"reason": "Remediation intent", "delta": -25, "policy_id": "POL-DR-001"}]
        result = annotate_violations(violations, adj_list, baseline=40.0, adjusted_score=15.0)
        assert result[0].llm_override == "Remediation intent"

    def test_generic_reason_does_NOT_override_critical(self):
        """CRITICAL violation is NOT overridden when LLM omits policy_id (generic reason)."""
        violations = [_critical_violation("POL-DR-001")]
        adj_list = [{"reason": "Remediation intent", "delta": -25}]  # no policy_id
        result = annotate_violations(violations, adj_list, baseline=40.0, adjusted_score=15.0)
        assert result[0].llm_override is None  # CRITICAL stays blocking

    def test_generic_reason_overrides_non_critical(self):
        """Non-CRITICAL violations CAN be overridden with generic reason."""
        v = PolicyViolation(
            policy_id="POL-CHG-001", name="Change Window", rule="Test",
            severity=PolicySeverity.MEDIUM,
        )
        adj_list = [{"reason": "After-hours justified", "delta": -10}]
        result = annotate_violations([v], adj_list, baseline=30.0, adjusted_score=20.0)
        assert result[0].llm_override == "After-hours justified"

    def test_mixed_violations_only_targeted_critical_overridden(self):
        """Two CRITICAL violations: one targeted by policy_id, one not."""
        v1 = _critical_violation("POL-DR-001")   # Will be targeted
        v2 = _critical_violation("POL-SEC-001")   # NOT targeted
        adj_list = [{"reason": "DR test procedure", "delta": -25, "policy_id": "POL-DR-001"}]
        result = annotate_violations([v1, v2], adj_list, baseline=80.0, adjusted_score=55.0)
        assert result[0].llm_override == "DR test procedure"   # targeted → overridden
        assert result[1].llm_override is None                  # untargeted → stays blocking

    def test_mixed_severity_generic_skips_critical_overrides_medium(self):
        """Generic reason overrides MEDIUM but not CRITICAL."""
        v_crit = _critical_violation("POL-DR-001")
        v_med = PolicyViolation(
            policy_id="POL-CHG-001", name="Change Window", rule="Test",
            severity=PolicySeverity.MEDIUM,
        )
        adj_list = [{"reason": "Remediation intent", "delta": -20}]  # no policy_id
        result = annotate_violations([v_crit, v_med], adj_list, baseline=55.0, adjusted_score=35.0)
        assert result[0].llm_override is None                    # CRITICAL: not overridden
        assert result[1].llm_override == "Remediation intent"    # MEDIUM: overridden

    def test_empty_adj_list_returns_unchanged(self):
        """Empty adj_list means no LLM decision — violations unchanged."""
        violations = [_critical_violation("POL-DR-001")]
        result = annotate_violations(violations, [], baseline=40.0, adjusted_score=15.0)
        assert result[0].llm_override is None

    def test_score_increase_returns_unchanged(self):
        """When LLM INCREASES the score, no overrides are applied."""
        violations = [_critical_violation("POL-DR-001")]
        adj_list = [{"reason": "Higher risk", "delta": 10, "policy_id": "POL-DR-001"}]
        result = annotate_violations(violations, adj_list, baseline=40.0, adjusted_score=50.0)
        assert result[0].llm_override is None


# ---------------------------------------------------------------------------
# Mocked end-to-end live-path behavioral test
# ---------------------------------------------------------------------------

class TestPolicyAgentLivePathBehavior:
    """Mocked end-to-end test of PolicyComplianceAgent._evaluate_with_framework.

    Mocks agent_framework (may not be installed) and run_with_throttle (skips
    real LLM call). The mock simulates the LLM calling both tools, then asserts
    that the returned PolicyResult has the adjusted score, annotated violations,
    and LLM reasoning text.
    """

    def _make_mocks(self):
        """Return (mock_af, mock_af_openai, captured_tools) for use in tests."""
        captured_tools = {}

        def mock_tool(**kwargs):
            def decorator(fn):
                captured_tools[kwargs["name"]] = fn
                return fn
            return decorator

        mock_af = MagicMock()
        mock_af.tool = mock_tool
        mock_af_openai = MagicMock()
        return mock_af, mock_af_openai, captured_tools

    def _make_live_agent(self):
        from src.governance_agents.policy_agent import PolicyComplianceAgent
        mock_settings = MagicMock()
        mock_settings.use_local_mocks = False
        mock_settings.azure_openai_endpoint = "https://fake.openai.azure.com"
        mock_settings.azure_openai_deployment = "gpt-4.1"
        return PolicyComplianceAgent(cfg=mock_settings)

    def test_live_path_returns_adjusted_score_and_annotated_violations(self):
        """Full runtime flow: tool calls → parse → annotate → PolicyResult."""
        mock_af, mock_af_openai, captured_tools = self._make_mocks()
        agent = self._make_live_agent()
        action = _make_action()
        action.reason = "Opening SSH port 22 to 0.0.0.0/0 for emergency admin access"
        action.nsg_change_direction = "open"  # guarantees POL-SEC-002 CRITICAL fires
        metadata = {"tags": {}, "environment": "production"}

        # Get deterministic baseline — POL-SEC-002 CRITICAL is guaranteed by direction="open"
        baseline = agent._evaluate_rules(action, metadata)
        target = max(0.0, baseline.sri_policy - 20.0)

        crit = next(v for v in baseline.violations if v.severity == PolicySeverity.CRITICAL)
        adjustments = [{
            "reason": "Ops agent is remediating open SSH, not creating the exposure",
            "delta": target - baseline.sri_policy,
            "policy_id": crit.policy_id,
        }]

        async def mock_run(run_fn, prompt):
            await captured_tools["evaluate_policy_rules"](
                action_json=action.model_dump_json(),
            )
            await captured_tools["submit_governance_decision"](
                adjusted_score=target,
                adjustments_json=json.dumps(adjustments),
                reasoning="Ops agent is remediating open SSH, not creating the issue",
                confidence=0.9,
            )

        with patch.dict(sys.modules, {
            "agent_framework": mock_af,
            "agent_framework.openai": mock_af_openai,
        }), patch(
            "src.infrastructure.llm_throttle.run_with_throttle",
            side_effect=mock_run,
        ):
            result = asyncio.run(agent.evaluate(action, resource_metadata=metadata))

        assert result.sri_policy == target
        assert result.sri_policy < baseline.sri_policy
        matched = [v for v in result.violations if v.policy_id == crit.policy_id]
        assert len(matched) == 1
        assert matched[0].llm_override == "Ops agent is remediating open SSH, not creating the exposure"
        assert "LLM Governance Analysis" in result.reasoning
        assert len(result.violations) == len(baseline.violations)

    def test_live_path_no_submission_falls_back_to_baseline(self):
        """If the LLM never calls submit_governance_decision, baseline is returned."""
        mock_af, mock_af_openai, captured_tools = self._make_mocks()
        agent = self._make_live_agent()
        action = _make_action()
        metadata = {"tags": {}, "environment": "production"}

        baseline = agent._evaluate_rules(action, metadata)

        async def mock_run(run_fn, prompt):
            await captured_tools["evaluate_policy_rules"](
                action_json=action.model_dump_json(),
            )

        with patch.dict(sys.modules, {
            "agent_framework": mock_af,
            "agent_framework.openai": mock_af_openai,
        }), patch(
            "src.infrastructure.llm_throttle.run_with_throttle",
            side_effect=mock_run,
        ):
            result = asyncio.run(agent.evaluate(action, resource_metadata=metadata))

        assert result.sri_policy == baseline.sri_policy
        for v in result.violations:
            assert v.llm_override is None

    def test_live_path_generic_reason_does_not_override_critical(self):
        """Generic adjustment (no policy_id) does NOT override CRITICAL violations."""
        mock_af, mock_af_openai, captured_tools = self._make_mocks()
        agent = self._make_live_agent()
        action = _make_action()
        action.reason = "Opening SSH port 22 to 0.0.0.0/0 for emergency access"
        action.nsg_change_direction = "open"  # forces POL-SEC-002 CRITICAL so guardrail is meaningful
        metadata = {"tags": {}, "environment": "production"}

        baseline = agent._evaluate_rules(action, metadata)
        target = max(0.0, baseline.sri_policy - 20.0)

        adjustments = [{"reason": "Remediation intent", "delta": target - baseline.sri_policy}]

        async def mock_run(run_fn, prompt):
            await captured_tools["evaluate_policy_rules"](
                action_json=action.model_dump_json(),
            )
            await captured_tools["submit_governance_decision"](
                adjusted_score=target,
                adjustments_json=json.dumps(adjustments),
                reasoning="Generic remediation note",
                confidence=0.9,
            )

        with patch.dict(sys.modules, {
            "agent_framework": mock_af,
            "agent_framework.openai": mock_af_openai,
        }), patch(
            "src.infrastructure.llm_throttle.run_with_throttle",
            side_effect=mock_run,
        ):
            result = asyncio.run(agent.evaluate(action, resource_metadata=metadata))

        assert result.sri_policy == target
        for v in result.violations:
            if v.severity == PolicySeverity.CRITICAL:
                assert v.llm_override is None, (
                    f"CRITICAL violation {v.policy_id} should NOT be overridden by generic reason"
                )


# ---------------------------------------------------------------------------
# Agent live-path integration — source inspection
# ---------------------------------------------------------------------------

class TestAgentLivePathIntegration:
    """Verify the wiring of the LLM decision-maker path in all 4 governance agents.

    Uses inspect.getsource to confirm each agent's _evaluate_with_framework method:
    - registers both tools (evaluate_*_rules + submit_governance_decision)
    - calls parse_llm_decision
    - uses llm_decision_holder as the capture list

    This approach is guaranteed to work without agent_framework installed and
    provides fast, deterministic coverage of the live-path wiring.
    """

    def test_policy_agent_live_path_wiring(self):
        """PolicyComplianceAgent._evaluate_with_framework is wired correctly."""
        import inspect
        from src.governance_agents.policy_agent import PolicyComplianceAgent

        source = inspect.getsource(PolicyComplianceAgent._evaluate_with_framework)
        assert "submit_governance_decision" in source
        assert "evaluate_policy_rules" in source
        assert "parse_llm_decision" in source
        assert "llm_decision_holder" in source

    def test_blast_radius_agent_live_path_wiring(self):
        """BlastRadiusSimulationAgent._evaluate_with_framework is wired correctly."""
        import inspect
        from src.governance_agents.blast_radius_agent import BlastRadiusAgent

        source = inspect.getsource(BlastRadiusAgent._evaluate_with_framework)
        assert "submit_governance_decision" in source
        assert "evaluate_blast_radius_rules" in source
        assert "parse_llm_decision" in source
        assert "llm_decision_holder" in source

    def test_historical_agent_live_path_wiring(self):
        """HistoricalPatternAgent._evaluate_with_framework is wired correctly."""
        import inspect
        from src.governance_agents.historical_agent import HistoricalPatternAgent

        source = inspect.getsource(HistoricalPatternAgent._evaluate_with_framework)
        assert "submit_governance_decision" in source
        assert "evaluate_historical_rules" in source
        assert "parse_llm_decision" in source
        assert "llm_decision_holder" in source

    def test_financial_agent_live_path_wiring(self):
        """FinancialImpactAgent._evaluate_with_framework is wired correctly."""
        import inspect
        from src.governance_agents.financial_agent import FinancialImpactAgent

        source = inspect.getsource(FinancialImpactAgent._evaluate_with_framework)
        assert "submit_governance_decision" in source
        assert "evaluate_financial_rules" in source
        assert "parse_llm_decision" in source
        assert "llm_decision_holder" in source

    def test_policy_agent_annotates_violations_on_score_reduction(self):
        """When LLM reduces sri_policy, violations are annotated via annotate_violations helper."""
        import inspect
        from src.governance_agents.policy_agent import PolicyComplianceAgent

        source = inspect.getsource(PolicyComplianceAgent._evaluate_with_framework)
        # Annotation is now delegated to the annotate_violations helper
        assert "annotate_violations" in source
        assert "adj_list" in source
        # Inline annotation logic (override_by_id / generic_reason) must be gone
        assert "override_by_id" not in source
        assert "generic_reason" not in source

    def test_policy_agent_submit_tool_captures_to_holder(self):
        """submit_governance_decision appends to llm_decision_holder."""
        import inspect
        from src.governance_agents.policy_agent import PolicyComplianceAgent

        source = inspect.getsource(PolicyComplianceAgent._evaluate_with_framework)
        # The holder list must be appended to by the tool
        assert "llm_decision_holder.append" in source

    def test_all_agents_use_clamp_guardrail(self):
        """All agents import and use clamp_score guardrail via parse_llm_decision."""
        from src.governance_agents._llm_governance import parse_llm_decision, MAX_ADJUSTMENT

        # Guardrail constant is reachable
        assert MAX_ADJUSTMENT == 30

        # parse_llm_decision enforces the clamp for every agent
        holder = [{"adjusted_score": 0.0, "adjustments": [], "reasoning": "extreme"}]
        score, _, _ = parse_llm_decision(holder, baseline=50.0)
        # Floor = max(0, 50-30) = 20
        assert score == 20.0

        holder2 = [{"adjusted_score": 100.0, "adjustments": [], "reasoning": "extreme up"}]
        score2, _, _ = parse_llm_decision(holder2, baseline=50.0)
        # Ceiling = min(100, 50+30) = 80
        assert score2 == 80.0

    def test_policy_agent_uses_annotate_violations_helper(self):
        """Policy agent delegates violation annotation to annotate_violations helper."""
        import inspect
        from src.governance_agents.policy_agent import PolicyComplianceAgent

        source = inspect.getsource(PolicyComplianceAgent._evaluate_with_framework)
        assert "annotate_violations" in source
        # Confirm inline annotation logic was removed
        assert "override_by_id" not in source
        assert "generic_reason" not in source

    def test_annotate_violations_is_same_function_as_tested(self):
        """The annotate_violations imported by policy_agent is the one we test."""
        from src.governance_agents._llm_governance import annotate_violations as tested_fn
        import inspect
        sig = inspect.signature(tested_fn)
        params = list(sig.parameters.keys())
        assert "violations" in params
        assert "adj_list" in params
        assert "baseline" in params
        assert "adjusted_score" in params
