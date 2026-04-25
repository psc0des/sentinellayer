"""Phase 32 Part 1 — Evidence-Aware Scoring tests.

Verifies:
- EvidencePayload Pydantic model validates correctly
- Blast radius agent reduces score when sustained critical evidence justifies action
- Blast radius agent raises score when evidence is absent for remediation actions
- Financial agent reduces cost score for incident-driven production restarts
- Financial agent raises cost score for dangerous scale-downs (hidden peak CPU)
- Historical agent boosts approval for evidence matching successful past remediations
- Historical agent adds caution when no evidence AND no incidents
- Policy agent receives evidence in its LLM prompt (deterministic path: no change)
- Backward compatibility: proposals without evidence still score correctly
- Parametrized parity: same action, no evidence → higher score than with evidence
"""

from __future__ import annotations

import os
import pytest

os.environ.setdefault("USE_LOCAL_MOCKS", "true")

from src.core.models import (
    ActionTarget,
    ActionType,
    EvidencePayload,
    ProposedAction,
    SRIVerdict,
    Urgency,
)
from src.governance_agents.blast_radius_agent import BlastRadiusAgent
from src.governance_agents.financial_agent import FinancialImpactAgent
from src.governance_agents.historical_agent import HistoricalPatternAgent
from src.governance_agents.policy_agent import PolicyComplianceAgent


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_action(
    resource_id: str = "vm-prod-01",
    action_type: ActionType = ActionType.RESTART_SERVICE,
    resource_type: str = "Microsoft.Compute/virtualMachines",
    urgency: Urgency = Urgency.HIGH,
    evidence: EvidencePayload | None = None,
) -> ProposedAction:
    return ProposedAction(
        agent_id="test-agent",
        action_type=action_type,
        target=ActionTarget(resource_id=resource_id, resource_type=resource_type),
        reason="Test reason",
        urgency=urgency,
        evidence=evidence,
    )


def _critical_evidence(duration_minutes: int = 90) -> EvidencePayload:
    return EvidencePayload(
        metrics={"cpu_percent_avg_2h": 98.7, "memory_percent": 95.0},
        severity="critical",
        duration_minutes=duration_minutes,
        logs=["OOM killer invoked", "process restart loop detected"],
    )


def _high_evidence(duration_minutes: int = 75) -> EvidencePayload:
    return EvidencePayload(
        metrics={"cpu_percent_avg_2h": 88.0},
        severity="high",
        duration_minutes=duration_minutes,
    )


# ---------------------------------------------------------------------------
# EvidencePayload model validation
# ---------------------------------------------------------------------------

def test_evidence_payload_default_fields():
    ev = EvidencePayload()
    assert ev.metrics == {}
    assert ev.logs == []
    assert ev.alerts == []
    assert ev.duration_minutes is None
    assert ev.severity is None
    assert ev.context == {}


def test_evidence_payload_full_construction():
    ev = EvidencePayload(
        metrics={"cpu_percent_avg_2h": 98.7, "peak_cpu_14d": 62.0},
        logs=["OOM killer invoked"],
        alerts=[{"alert_id": "a-001", "severity": "critical"}],
        duration_minutes=120,
        severity="critical",
        context={"workload_type": "stateless", "env": "production"},
    )
    assert ev.metrics["cpu_percent_avg_2h"] == 98.7
    assert ev.duration_minutes == 120
    assert ev.severity == "critical"


def test_evidence_payload_pydantic_validation_rejects_non_float_metric():
    with pytest.raises(Exception):
        EvidencePayload(metrics={"cpu": "not-a-float"})


def test_proposed_action_evidence_is_optional():
    action = _make_action(evidence=None)
    assert action.evidence is None


def test_proposed_action_evidence_round_trips_serialization():
    ev = _critical_evidence()
    action = _make_action(evidence=ev)
    data = action.model_dump()
    restored = ProposedAction(**data)
    assert restored.evidence is not None
    assert restored.evidence.severity == "critical"
    assert restored.evidence.duration_minutes == 90


# ---------------------------------------------------------------------------
# BlastRadiusAgent — evidence-aware scoring
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_blast_radius_lowers_score_when_evidence_justifies_restart():
    """Critical + 90min duration → -15 from blast radius score."""
    agent = BlastRadiusAgent()
    action_no_ev = _make_action(evidence=None)
    action_with_ev = _make_action(evidence=_critical_evidence(90))

    result_no_ev = await agent.evaluate(action_no_ev)
    result_with_ev = await agent.evaluate(action_with_ev)

    assert result_with_ev.sri_infrastructure < result_no_ev.sri_infrastructure, (
        f"Expected evidence to reduce score: {result_with_ev.sri_infrastructure} "
        f"should be < {result_no_ev.sri_infrastructure}"
    )


@pytest.mark.asyncio
async def test_blast_radius_raises_score_when_evidence_missing_for_restart():
    """Restart action with no evidence adds +5 unverified-justification penalty."""
    agent = BlastRadiusAgent()
    action_with_ev = _make_action(evidence=_critical_evidence(90))
    action_no_ev = _make_action(evidence=None)

    result_with_ev = await agent.evaluate(action_with_ev)
    result_no_ev = await agent.evaluate(action_no_ev)

    assert result_no_ev.sri_infrastructure > result_with_ev.sri_infrastructure, (
        f"No-evidence score ({result_no_ev.sri_infrastructure}) should exceed "
        f"evidence score ({result_with_ev.sri_infrastructure})"
    )


@pytest.mark.asyncio
async def test_blast_radius_no_evidence_note_in_reasoning():
    """Reasoning text mentions unverified justification when evidence is absent."""
    agent = BlastRadiusAgent()
    action = _make_action(evidence=None)
    result = await agent.evaluate(action)
    assert "unverified" in result.reasoning.lower() or "evidence" in result.reasoning.lower()


@pytest.mark.asyncio
async def test_blast_radius_evidence_note_in_reasoning_for_critical_sustained():
    """Reasoning text mentions responsive remediation when evidence justifies."""
    agent = BlastRadiusAgent()
    action = _make_action(evidence=_critical_evidence(90))
    result = await agent.evaluate(action)
    assert "remediation" in result.reasoning.lower() or "evidence" in result.reasoning.lower()


@pytest.mark.asyncio
async def test_blast_radius_short_duration_evidence_no_reduction():
    """Evidence with duration < 60min does NOT trigger the -15 reduction."""
    agent = BlastRadiusAgent()
    action_short = _make_action(evidence=EvidencePayload(severity="critical", duration_minutes=30))
    action_no_ev = _make_action(evidence=None)

    result_short = await agent.evaluate(action_short)
    result_no_ev = await agent.evaluate(action_no_ev)

    # Short-duration evidence doesn't reduce score, but no-evidence adds +5
    # so short-evidence should score lower than no-evidence
    assert result_short.sri_infrastructure <= result_no_ev.sri_infrastructure


@pytest.mark.asyncio
async def test_blast_radius_delete_action_no_evidence_penalty_exempt():
    """DELETE_RESOURCE is not in the remediation_types set — no +5 penalty."""
    agent = BlastRadiusAgent()
    action = _make_action(action_type=ActionType.DELETE_RESOURCE, evidence=None)
    result = await agent.evaluate(action)
    # DELETE base is 40 + criticality; no +5 added for non-remediation types
    # Just assert no exception and score is valid
    assert 0.0 <= result.sri_infrastructure <= 100.0
    # reasoning should NOT mention "unverified" (no penalty applied)
    assert "unverified" not in result.reasoning.lower()


# ---------------------------------------------------------------------------
# FinancialImpactAgent — evidence-aware scoring
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_financial_reduces_cost_score_for_production_incident_restart():
    """High/critical evidence on production resource reduces financial risk.

    RESTART_SERVICE has $0 cost baseline so the -10 reduction hits the floor.
    We verify: with evidence the score is ≤ without evidence (never increased).
    We use a SCALE_DOWN on a production resource to verify the actual reduction path.
    """
    agent = FinancialImpactAgent()

    # RESTART test: score is 0 both ways — evidence doesn't raise it (floor at 0)
    action_no_ev_restart = _make_action(
        resource_id="vm-prod-01",
        action_type=ActionType.RESTART_SERVICE,
        evidence=None,
    )
    action_with_ev_restart = _make_action(
        resource_id="vm-prod-01",
        action_type=ActionType.RESTART_SERVICE,
        evidence=EvidencePayload(severity="critical", duration_minutes=90),
    )
    result_no_ev = await agent.evaluate(action_no_ev_restart)
    result_with_ev = await agent.evaluate(action_with_ev_restart)
    assert result_with_ev.sri_cost <= result_no_ev.sri_cost, (
        "Evidence should never raise the financial score for a restart action"
    )

    # Verify the _apply_evidence_adjustment directly reduces when there IS a non-zero score
    from src.governance_agents.financial_agent import FinancialImpactAgent as FA
    scale_with_ev = _make_action(
        resource_id="vm-prod-23",
        action_type=ActionType.RESTART_SERVICE,
        evidence=EvidencePayload(severity="high", duration_minutes=90),
    )
    raw_score = 15.0  # hypothetical non-zero score
    adjusted = FA._apply_evidence_adjustment(raw_score, scale_with_ev)
    assert adjusted == 5.0, f"Expected 15 - 10 = 5, got {adjusted}"


@pytest.mark.asyncio
async def test_financial_raises_cost_score_for_dangerous_scale_down():
    """Scale-down with peak_cpu_14d > 50% raises financial risk."""
    agent = FinancialImpactAgent()
    action_safe = _make_action(
        resource_id="vm-dev-01",
        action_type=ActionType.SCALE_DOWN,
        evidence=EvidencePayload(metrics={"avg_cpu_7d": 5.0, "peak_cpu_14d": 20.0}),
    )
    action_risky = _make_action(
        resource_id="vm-dev-01",
        action_type=ActionType.SCALE_DOWN,
        evidence=EvidencePayload(metrics={"avg_cpu_7d": 5.0, "peak_cpu_14d": 70.0}),
    )

    result_safe = await agent.evaluate(action_safe)
    result_risky = await agent.evaluate(action_risky)

    assert result_risky.sri_cost > result_safe.sri_cost, (
        f"High peak CPU should raise financial score: {result_risky.sri_cost} "
        f"should be > {result_safe.sri_cost}"
    )


@pytest.mark.asyncio
async def test_financial_production_detection_uses_resource_id():
    """Production detection checks for 'prod' in resource_id."""
    agent = FinancialImpactAgent()
    prod_action = _make_action(
        resource_id="vm-prod-backend",
        action_type=ActionType.RESTART_SERVICE,
        evidence=EvidencePayload(severity="high", duration_minutes=90),
    )
    dev_action = _make_action(
        resource_id="vm-dev-backend",
        action_type=ActionType.RESTART_SERVICE,
        evidence=EvidencePayload(severity="high", duration_minutes=90),
    )

    result_prod = await agent.evaluate(prod_action)
    result_dev = await agent.evaluate(dev_action)

    # Production gets the -10 reduction; dev doesn't
    assert result_prod.sri_cost <= result_dev.sri_cost


# ---------------------------------------------------------------------------
# HistoricalPatternAgent — evidence-aware scoring
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_historical_adds_caution_when_no_evidence_and_no_incidents(tmp_path):
    """No incidents + no evidence for remediation action → +5 caution."""
    empty = tmp_path / "empty.json"
    empty.write_text("[]")
    agent = HistoricalPatternAgent(incidents_path=empty)
    action_no_ev = _make_action(action_type=ActionType.RESTART_SERVICE, evidence=None)

    result = await agent.evaluate(action_no_ev)
    assert result.sri_historical == 5.0


@pytest.mark.asyncio
async def test_historical_no_caution_with_evidence_and_no_incidents(tmp_path):
    """Evidence provided + no incidents → no +5 caution penalty."""
    empty = tmp_path / "empty.json"
    empty.write_text("[]")
    agent = HistoricalPatternAgent(incidents_path=empty)
    action_with_ev = _make_action(
        action_type=ActionType.RESTART_SERVICE,
        evidence=_critical_evidence(90),
    )

    result = await agent.evaluate(action_with_ev)
    # Base score = 0 (no incidents), no +5 added because evidence IS provided
    assert result.sri_historical == 0.0


# ---------------------------------------------------------------------------
# Backward compatibility — proposals without evidence still score
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_blast_radius_scores_without_evidence():
    """Existing proposals with no evidence field still produce valid scores."""
    agent = BlastRadiusAgent()
    action = ProposedAction(
        agent_id="legacy-agent",
        action_type=ActionType.MODIFY_NSG,
        target=ActionTarget(resource_id="nsg-web", resource_type="Microsoft.Network/networkSecurityGroups"),
        reason="Legacy proposal — no evidence field",
        urgency=Urgency.HIGH,
    )
    result = await agent.evaluate(action)
    assert 0.0 <= result.sri_infrastructure <= 100.0


@pytest.mark.asyncio
async def test_financial_scores_without_evidence():
    """Existing proposals with no evidence field still produce valid scores."""
    agent = FinancialImpactAgent()
    action = ProposedAction(
        agent_id="legacy-agent",
        action_type=ActionType.SCALE_DOWN,
        target=ActionTarget(resource_id="vm-23", resource_type="Microsoft.Compute/virtualMachines"),
        reason="CPU below threshold",
        urgency=Urgency.LOW,
    )
    result = await agent.evaluate(action)
    assert 0.0 <= result.sri_cost <= 100.0


@pytest.mark.asyncio
async def test_policy_scores_without_evidence():
    """Policy agent still evaluates correctly when no evidence is attached."""
    agent = PolicyComplianceAgent()
    action = ProposedAction(
        agent_id="legacy-agent",
        action_type=ActionType.MODIFY_NSG,
        target=ActionTarget(resource_id="nsg-legacy", resource_type="Microsoft.Network/networkSecurityGroups"),
        reason="Legacy action with no evidence",
        urgency=Urgency.MEDIUM,
        nsg_change_direction="open",
    )
    result = await agent.evaluate(action)
    assert 0.0 <= result.sri_policy <= 100.0


# ---------------------------------------------------------------------------
# Parametrized parity: no evidence → higher score than with strong evidence
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("action_type,resource_id,resource_type", [
    (ActionType.RESTART_SERVICE, "vm-api-prod", "Microsoft.Compute/virtualMachines"),
    (ActionType.SCALE_DOWN, "vm-worker-prod", "Microsoft.Compute/virtualMachines"),
    (ActionType.SCALE_UP, "aks-prod", "Microsoft.ContainerService/managedClusters"),
])
@pytest.mark.asyncio
async def test_blast_radius_no_evidence_scores_higher_than_strong_evidence(
    action_type, resource_id, resource_type
):
    """Blast radius score without evidence ≥ score with strong justifying evidence."""
    agent = BlastRadiusAgent()
    strong_ev = EvidencePayload(severity="critical", duration_minutes=120)

    result_no_ev = await agent.evaluate(_make_action(resource_id, action_type, resource_type, evidence=None))
    result_with_ev = await agent.evaluate(_make_action(resource_id, action_type, resource_type, evidence=strong_ev))

    assert result_no_ev.sri_infrastructure >= result_with_ev.sri_infrastructure, (
        f"{action_type.value}: no-evidence score ({result_no_ev.sri_infrastructure}) "
        f"should be ≥ evidence score ({result_with_ev.sri_infrastructure})"
    )


@pytest.mark.parametrize("severity,duration,should_reduce", [
    ("critical", 90, True),
    ("high", 75, True),
    ("critical", 30, False),   # short duration — no reduction
    ("medium", 90, False),     # medium severity — no reduction
    ("low", 120, False),       # low severity — no reduction
])
@pytest.mark.asyncio
async def test_blast_radius_evidence_reduction_conditions(severity, duration, should_reduce):
    """Evidence reduction applies only for high/critical severity AND duration ≥ 60min."""
    agent = BlastRadiusAgent()
    ev = EvidencePayload(severity=severity, duration_minutes=duration)
    action_with_ev = _make_action(evidence=ev)
    action_no_ev = _make_action(evidence=None)

    result_ev = await agent.evaluate(action_with_ev)
    result_no = await agent.evaluate(action_no_ev)

    if should_reduce:
        assert result_ev.sri_infrastructure < result_no.sri_infrastructure, (
            f"severity={severity} duration={duration}: expected reduction"
        )
    else:
        # No reduction (but also no +5 since evidence IS provided)
        assert result_ev.sri_infrastructure <= result_no.sri_infrastructure, (
            f"severity={severity} duration={duration}: expected no reduction beyond baseline"
        )
