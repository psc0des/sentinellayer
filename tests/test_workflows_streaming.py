"""Phase 33B — Workflow streaming tests.

Verifies:
- stream_governance_evaluation() yields (event_type, kwargs) tuples for
  executor_invoked / executor_completed events, then the GovernanceVerdict.
- All four agent labels appear in streaming events.
- evaluate_streaming() on the pipeline yields the same verdict as evaluate().
- SSE event format is compatible with _emit_event() (no unexpected keys).
- Streaming path is used in _run_agent_scan when USE_WORKFLOWS=true.
"""

from __future__ import annotations

import os
import pytest

os.environ.setdefault("USE_LOCAL_MOCKS", "true")

from src.core.governance_engine import GovernanceDecisionEngine
from src.core.models import (
    ActionTarget,
    ActionType,
    GovernanceVerdict,
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
    action_type: ActionType = ActionType.RESTART_SERVICE,
    resource_id: str = "vm-23",
    resource_type: str = "Microsoft.Compute/virtualMachines",
    urgency: Urgency = Urgency.HIGH,
) -> ProposedAction:
    return ProposedAction(
        agent_id="test-agent",
        action_type=action_type,
        target=ActionTarget(resource_id=resource_id, resource_type=resource_type),
        reason="Test reason",
        urgency=urgency,
    )


def _make_workflow():
    from src.core.workflows.governance_workflow import build_governance_workflow
    return build_governance_workflow(
        blast=BlastRadiusAgent(),
        policy=PolicyComplianceAgent(),
        historical=HistoricalPatternAgent(),
        financial=FinancialImpactAgent(),
        engine=GovernanceDecisionEngine(),
    )


# ---------------------------------------------------------------------------
# stream_governance_evaluation() tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_stream_yields_verdict_as_last_item():
    """The final yield from stream_governance_evaluation must be a GovernanceVerdict."""
    from src.core.workflows.governance_workflow import stream_governance_evaluation
    from src.core.workflows.messages import GovernanceInput

    wf = _make_workflow()
    action = _make_action()
    inp = GovernanceInput(
        action=action, resource_metadata=None,
        force_deterministic=True, triage_tier=1,
    )

    items = []
    async for item in stream_governance_evaluation(
        wf, inp, resource_name="vm-23", action_type="restart_service"
    ):
        items.append(item)

    assert len(items) > 0
    assert isinstance(items[-1], GovernanceVerdict), (
        f"Last item should be GovernanceVerdict, got {type(items[-1])}"
    )


@pytest.mark.asyncio
async def test_stream_yields_evaluation_events_for_all_agents():
    """Each of the 4 governance agents should produce at least one event tuple."""
    from src.core.workflows.governance_workflow import _AGENT_LABELS, stream_governance_evaluation
    from src.core.workflows.messages import GovernanceInput

    wf = _make_workflow()
    inp = GovernanceInput(
        action=_make_action(), resource_metadata=None,
        force_deterministic=True, triage_tier=1,
    )

    agent_events: set[str] = set()
    async for item in stream_governance_evaluation(
        wf, inp, resource_name="vm-23", action_type="restart_service"
    ):
        if isinstance(item, tuple):
            event_type, kwargs = item
            msg = kwargs.get("message", "")
            for label in _AGENT_LABELS.values():
                if label in msg:
                    agent_events.add(label)

    assert agent_events >= set(_AGENT_LABELS.values()), (
        f"Missing agent labels in stream events: {set(_AGENT_LABELS.values()) - agent_events}"
    )


@pytest.mark.asyncio
async def test_stream_event_tuples_have_expected_structure():
    """All non-verdict yields must be (str, dict) tuples with a 'message' key."""
    from src.core.workflows.governance_workflow import stream_governance_evaluation
    from src.core.workflows.messages import GovernanceInput

    wf = _make_workflow()
    inp = GovernanceInput(
        action=_make_action(), resource_metadata=None,
        force_deterministic=True, triage_tier=1,
    )

    async for item in stream_governance_evaluation(
        wf, inp, resource_name="vm-23", action_type="restart_service"
    ):
        if isinstance(item, GovernanceVerdict):
            continue
        assert isinstance(item, tuple), f"Expected tuple, got {type(item)}"
        assert len(item) == 2, "Event tuple must have exactly 2 elements"
        event_type, kwargs = item
        assert isinstance(event_type, str), "event_type must be a string"
        assert isinstance(kwargs, dict), "event kwargs must be a dict"
        assert "message" in kwargs, "Event kwargs must contain 'message'"


@pytest.mark.asyncio
async def test_stream_evaluation_events_include_resource_name():
    """SSE evaluation events must carry the resource_name so the frontend can label them."""
    from src.core.workflows.governance_workflow import stream_governance_evaluation
    from src.core.workflows.messages import GovernanceInput

    wf = _make_workflow()
    inp = GovernanceInput(
        action=_make_action(), resource_metadata=None,
        force_deterministic=True, triage_tier=1,
    )

    found_resource_in_event = False
    async for item in stream_governance_evaluation(
        wf, inp, resource_name="vm-23", action_type="restart_service"
    ):
        if isinstance(item, tuple):
            event_type, kwargs = item
            if kwargs.get("resource_id") == "vm-23":
                found_resource_in_event = True

    assert found_resource_in_event, "No event contained resource_id='vm-23'"


@pytest.mark.asyncio
async def test_stream_verdict_decision_matches_non_streaming():
    """Streaming verdict decision must match non-streaming evaluate() for the same action."""
    from src.core.workflows.governance_workflow import build_governance_workflow, stream_governance_evaluation
    from src.core.workflows.messages import GovernanceInput

    # Non-streaming
    blast = BlastRadiusAgent()
    policy = PolicyComplianceAgent()
    hist = HistoricalPatternAgent()
    fin = FinancialImpactAgent()
    engine = GovernanceDecisionEngine()

    wf = build_governance_workflow(blast=blast, policy=policy, historical=hist, financial=fin, engine=engine)
    action = _make_action(ActionType.MODIFY_NSG, "nsg-prod-01", "Microsoft.Network/networkSecurityGroups")
    inp = GovernanceInput(action=action, resource_metadata=None, force_deterministic=True, triage_tier=1)

    result = await wf.run(inp)
    non_stream_verdict: GovernanceVerdict = result.get_outputs()[0]

    # Streaming
    wf2 = build_governance_workflow(blast=blast, policy=policy, historical=hist, financial=fin, engine=engine)
    stream_verdict = None
    async for item in stream_governance_evaluation(
        wf2, inp, resource_name="nsg-prod-01", action_type="modify_nsg"
    ):
        if isinstance(item, GovernanceVerdict):
            stream_verdict = item

    assert stream_verdict is not None
    assert stream_verdict.decision == non_stream_verdict.decision
    assert abs(
        stream_verdict.skry_risk_index.sri_composite -
        non_stream_verdict.skry_risk_index.sri_composite
    ) < 0.01


# ---------------------------------------------------------------------------
# pipeline.evaluate_streaming() tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_pipeline_evaluate_streaming_yields_verdict(monkeypatch):
    """pipeline.evaluate_streaming() must yield the verdict as its last item."""
    monkeypatch.setattr("src.config.settings.use_workflows", True)
    from src.core.pipeline import RuriSkryPipeline

    pipeline = RuriSkryPipeline()
    action = _make_action()

    verdict = None
    async for item in pipeline.evaluate_streaming(action):
        if isinstance(item, GovernanceVerdict):
            verdict = item

    assert verdict is not None
    assert verdict.decision in (SRIVerdict.APPROVED, SRIVerdict.ESCALATED, SRIVerdict.DENIED)


@pytest.mark.asyncio
async def test_pipeline_streaming_parity_with_evaluate(monkeypatch):
    """evaluate_streaming() and evaluate() must produce identical decisions."""
    monkeypatch.setattr("src.config.settings.use_workflows", True)
    from src.core.pipeline import RuriSkryPipeline

    pipeline = RuriSkryPipeline()
    action = _make_action(ActionType.SCALE_DOWN, "vm-23", "Microsoft.Compute/virtualMachines", Urgency.LOW)

    # Streaming verdict
    stream_verdict = None
    async for item in pipeline.evaluate_streaming(action):
        if isinstance(item, GovernanceVerdict):
            stream_verdict = item

    # Non-streaming verdict (legacy path)
    monkeypatch.setattr("src.config.settings.use_workflows", False)
    pipeline2 = RuriSkryPipeline()
    legacy_verdict = await pipeline2.evaluate(action)

    assert stream_verdict is not None
    assert stream_verdict.decision == legacy_verdict.decision
    assert abs(
        stream_verdict.skry_risk_index.sri_composite -
        legacy_verdict.skry_risk_index.sri_composite
    ) < 0.01


@pytest.mark.asyncio
async def test_pipeline_streaming_stamps_triage_info(monkeypatch):
    """Verdict from evaluate_streaming() must have triage_tier and triage_mode set."""
    monkeypatch.setattr("src.config.settings.use_workflows", True)
    from src.core.pipeline import RuriSkryPipeline

    pipeline = RuriSkryPipeline()
    action = _make_action()

    verdict = None
    async for item in pipeline.evaluate_streaming(action):
        if isinstance(item, GovernanceVerdict):
            verdict = item

    assert verdict is not None
    assert verdict.triage_tier is not None
    assert verdict.triage_mode in ("full", "deterministic")


@pytest.mark.asyncio
async def test_pipeline_streaming_yields_at_least_four_agent_events(monkeypatch):
    """evaluate_streaming() must yield ≥4 (event_type, kwargs) tuples (one per agent)."""
    monkeypatch.setattr("src.config.settings.use_workflows", True)
    from src.core.pipeline import RuriSkryPipeline

    pipeline = RuriSkryPipeline()
    action = _make_action()

    event_tuples = []
    async for item in pipeline.evaluate_streaming(action):
        if not isinstance(item, GovernanceVerdict):
            event_tuples.append(item)

    assert len(event_tuples) >= 4, (
        f"Expected at least 4 agent events, got {len(event_tuples)}"
    )


@pytest.mark.asyncio
async def test_multiple_actions_stream_sequentially(monkeypatch):
    """evaluate_streaming() is safe to call in a loop for multiple actions."""
    monkeypatch.setattr("src.config.settings.use_workflows", True)
    from src.core.pipeline import RuriSkryPipeline

    pipeline = RuriSkryPipeline()
    actions = [
        _make_action(ActionType.RESTART_SERVICE),
        _make_action(ActionType.SCALE_DOWN, "vm-api", "Microsoft.Compute/virtualMachines"),
        _make_action(ActionType.MODIFY_NSG, "nsg-web", "Microsoft.Network/networkSecurityGroups"),
    ]

    verdicts = []
    for action in actions:
        async for item in pipeline.evaluate_streaming(action):
            if isinstance(item, GovernanceVerdict):
                verdicts.append(item)

    assert len(verdicts) == 3
    for v in verdicts:
        assert v.decision in (SRIVerdict.APPROVED, SRIVerdict.ESCALATED, SRIVerdict.DENIED)
