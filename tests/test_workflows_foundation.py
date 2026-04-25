"""Phase 33A — Workflow foundation tests.

Verifies:
- The workflow graph builds without error with all 7 executors reachable.
- With USE_WORKFLOWS=false (legacy / opt-out), pipeline uses the asyncio.gather path.
- With USE_WORKFLOWS=true (Phase 33D default), pipeline produces a verdict that matches
  the legacy verdict for the same input across multiple representative actions.

Phase 32 Part 2 (ConditionGate) adds the 7th executor between ScoringExecutor and
the workflow output.  Tests updated to reflect the new executor count and output node.
Phase 33D: USE_WORKFLOWS=true is now the production default; USE_WORKFLOWS=false is
the deprecated opt-out. Tests use monkeypatch to isolate each path explicitly.
"""

from __future__ import annotations

import os
import pytest

# Force mock mode for all tests in this module — no Azure credentials needed.
os.environ.setdefault("USE_LOCAL_MOCKS", "true")

from src.core.governance_engine import GovernanceDecisionEngine
from src.core.models import (
    ActionTarget,
    ActionType,
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
    reason: str = "High CPU",
) -> ProposedAction:
    return ProposedAction(
        agent_id="test-agent",
        action_type=action_type,
        target=ActionTarget(resource_id=resource_id, resource_type=resource_type),
        reason=reason,
        urgency=urgency,
    )


def _make_agents():
    return dict(
        blast=BlastRadiusAgent(),
        policy=PolicyComplianceAgent(),
        historical=HistoricalPatternAgent(),
        financial=FinancialImpactAgent(),
        engine=GovernanceDecisionEngine(),
    )


# ---------------------------------------------------------------------------
# Graph construction tests
# ---------------------------------------------------------------------------

def test_workflow_builds_without_error():
    from src.core.workflows.governance_workflow import build_governance_workflow
    wf = build_governance_workflow(**_make_agents())
    assert wf is not None


def test_workflow_has_seven_executors():
    from src.core.workflows.governance_workflow import build_governance_workflow
    wf = build_governance_workflow(**_make_agents())
    executor_ids = {e.id for e in wf.get_executors_list()}
    assert executor_ids == {
        "dispatch", "blast_radius", "policy", "historical", "financial",
        "scoring", "condition_gate",
    }


def test_workflow_start_executor_is_dispatch():
    from src.core.workflows.governance_workflow import build_governance_workflow
    wf = build_governance_workflow(**_make_agents())
    assert wf.get_start_executor().id == "dispatch"


def test_workflow_output_executor_is_condition_gate():
    from src.core.workflows.governance_workflow import build_governance_workflow
    wf = build_governance_workflow(**_make_agents())
    output_ids = {e.id for e in wf.get_output_executors()}
    assert "condition_gate" in output_ids


# ---------------------------------------------------------------------------
# Feature-flag routing tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_legacy_path_when_use_workflows_false(monkeypatch):
    monkeypatch.setattr("src.config.settings.use_workflows", False)
    from src.core.pipeline import RuriSkryPipeline
    pipeline = RuriSkryPipeline()
    action = _make_action()
    verdict = await pipeline.evaluate(action)
    assert verdict.decision in (SRIVerdict.APPROVED, SRIVerdict.ESCALATED, SRIVerdict.DENIED)
    assert verdict.triage_tier is not None


@pytest.mark.asyncio
async def test_workflow_path_when_use_workflows_true(monkeypatch):
    monkeypatch.setattr("src.config.settings.use_workflows", True)
    from src.core.pipeline import RuriSkryPipeline
    pipeline = RuriSkryPipeline()
    action = _make_action()
    verdict = await pipeline.evaluate(action)
    assert verdict.decision in (SRIVerdict.APPROVED, SRIVerdict.ESCALATED, SRIVerdict.DENIED)
    assert verdict.triage_tier is not None
    assert verdict.triage_mode is not None


# ---------------------------------------------------------------------------
# Parity tests — workflow path must produce the same verdict as legacy path
# ---------------------------------------------------------------------------

PARITY_ACTIONS = [
    _make_action(ActionType.RESTART_SERVICE, "vm-23", "Microsoft.Compute/virtualMachines", Urgency.HIGH),
    _make_action(ActionType.SCALE_DOWN, "vm-23", "Microsoft.Compute/virtualMachines", Urgency.LOW, "Cost saving"),
    _make_action(ActionType.DELETE_RESOURCE, "vm-23", "Microsoft.Compute/virtualMachines", Urgency.LOW, "Decommission"),
    _make_action(ActionType.MODIFY_NSG, "nsg-prod-01", "Microsoft.Network/networkSecurityGroups", Urgency.CRITICAL),
    _make_action(ActionType.SCALE_UP, "vm-api", "Microsoft.Compute/virtualMachines", Urgency.MEDIUM, "Traffic spike"),
    _make_action(ActionType.UPDATE_CONFIG, "vm-23", "Microsoft.Compute/virtualMachines", Urgency.LOW, "Config drift"),
    _make_action(ActionType.CREATE_RESOURCE, "new-vm", "Microsoft.Compute/virtualMachines", Urgency.LOW, "Provisioning"),
    _make_action(ActionType.RESTART_SERVICE, "aks-prod-01", "Microsoft.ContainerService/managedClusters", Urgency.CRITICAL),
    _make_action(ActionType.SCALE_DOWN, "sql-prod-01", "Microsoft.Sql/servers", Urgency.MEDIUM, "Downsize DB"),
    _make_action(ActionType.DELETE_RESOURCE, "vm-dev-99", "Microsoft.Compute/virtualMachines", Urgency.LOW, "Cleanup"),
    _make_action(ActionType.MODIFY_NSG, "nsg-web-01", "Microsoft.Network/networkSecurityGroups", Urgency.HIGH),
    _make_action(ActionType.RESTART_SERVICE, "storage-acct-01", "Microsoft.Storage/storageAccounts", Urgency.MEDIUM),
]


@pytest.mark.asyncio
@pytest.mark.parametrize("action", PARITY_ACTIONS)
async def test_workflow_parity_with_legacy(action: ProposedAction, monkeypatch):
    """Workflow path verdict must match legacy path verdict for the same action."""
    # Legacy path
    monkeypatch.setattr("src.config.settings.use_workflows", False)
    from src.core.pipeline import RuriSkryPipeline
    legacy_pipeline = RuriSkryPipeline()
    legacy_verdict = await legacy_pipeline.evaluate(action)

    # Workflow path
    monkeypatch.setattr("src.config.settings.use_workflows", True)
    wf_pipeline = RuriSkryPipeline()
    wf_verdict = await wf_pipeline.evaluate(action)

    assert wf_verdict.decision == legacy_verdict.decision, (
        f"Decision mismatch for {action.action_type.value} on {action.target.resource_id}: "
        f"workflow={wf_verdict.decision} legacy={legacy_verdict.decision}"
    )
    assert abs(wf_verdict.skry_risk_index.sri_composite - legacy_verdict.skry_risk_index.sri_composite) < 0.01, (
        f"Composite score mismatch: workflow={wf_verdict.skry_risk_index.sri_composite:.2f} "
        f"legacy={legacy_verdict.skry_risk_index.sri_composite:.2f}"
    )
    assert wf_verdict.triage_tier == legacy_verdict.triage_tier
    assert wf_verdict.triage_mode == legacy_verdict.triage_mode


# ---------------------------------------------------------------------------
# Triage tier 1 short-circuit parity
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_tier1_short_circuit_parity(monkeypatch):
    """Tier 1 (deterministic) verdict must be identical in both paths."""
    # dev resource → tier 1 (no LLM, isolated blast radius)
    action = _make_action(
        ActionType.SCALE_DOWN,
        "vm-dev-99",
        "Microsoft.Compute/virtualMachines",
        Urgency.LOW,
        "Cost saving",
    )

    monkeypatch.setattr("src.config.settings.use_workflows", False)
    from src.core.pipeline import RuriSkryPipeline
    legacy = await RuriSkryPipeline().evaluate(action)

    monkeypatch.setattr("src.config.settings.use_workflows", True)
    wf = await RuriSkryPipeline().evaluate(action)

    assert wf.decision == legacy.decision
    assert abs(wf.skry_risk_index.sri_composite - legacy.skry_risk_index.sri_composite) < 0.01


# ---------------------------------------------------------------------------
# GovernanceInput / GovernanceAgentResult message type tests
# ---------------------------------------------------------------------------

def test_governance_input_fields():
    from src.core.workflows.messages import GovernanceInput
    action = _make_action()
    inp = GovernanceInput(
        action=action,
        resource_metadata={"tags": {}},
        force_deterministic=True,
        triage_tier=1,
    )
    assert inp.action is action
    assert inp.force_deterministic is True
    assert inp.triage_tier == 1


def test_governance_agent_result_fields():
    from src.core.models import BlastRadiusResult
    from src.core.workflows.messages import GovernanceAgentResult
    action = _make_action()
    result = BlastRadiusResult(sri_infrastructure=30.0)
    msg = GovernanceAgentResult(
        agent_name="blast_radius",
        action=action,
        result=result,
        triage_tier=2,
    )
    assert msg.agent_name == "blast_radius"
    assert msg.result is result
    assert msg.triage_tier == 2
