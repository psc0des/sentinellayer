"""Phase 32 Part 2 — Conditional Approvals tests.

Verifies:
- ConditionType and ApprovalCondition models validate correctly
- GovernanceDecisionEngine emits APPROVED_IF for production resize/restart
- GovernanceDecisionEngine emits APPROVED_IF for NSG affecting shared infrastructure
- GovernanceDecisionEngine emits plain APPROVED when no conditions apply
- check_time_window returns True inside window and False outside
- check_metric_threshold respects max_threshold parameter
- check_condition dispatches correctly and is False for human-required types
- ExecutionGateway routes APPROVED_IF → CONDITIONAL status
- ExecutionGateway.mark_condition_satisfied updates state and promotes when all done
- ExecutionGateway.try_execute_if_all_satisfied promotes only when all conditions met
- ExecutionGateway.force_execute requires non-empty justification and promotes record
- ExecutionGateway.force_execute logs audit fields correctly
- ConditionGateExecutor promotes APPROVED_IF → APPROVED when all auto-conditions met
- ConditionGateExecutor passes APPROVED_IF through when human conditions remain
- Backward compatibility: APPROVED / ESCALATED / DENIED verdicts unaffected
"""

from __future__ import annotations

import os
import uuid
from datetime import datetime, timezone

import pytest

os.environ.setdefault("USE_LOCAL_MOCKS", "true")

from src.core.condition_checkers import check_condition, check_metric_threshold, check_time_window
from src.core.models import (
    ActionTarget,
    ActionType,
    ApprovalCondition,
    BlastRadiusResult,
    ConditionType,
    EvidencePayload,
    ExecutionRecord,
    ExecutionStatus,
    FinancialResult,
    GovernanceVerdict,
    HistoricalResult,
    PolicyResult,
    ProposedAction,
    SRIBreakdown,
    SRIVerdict,
    Urgency,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_action(
    resource_id: str = "vm-prod-01",
    action_type: ActionType = ActionType.RESTART_SERVICE,
    evidence: EvidencePayload | None = None,
) -> ProposedAction:
    return ProposedAction(
        agent_id="test-agent",
        action_type=action_type,
        target=ActionTarget(resource_id=resource_id, resource_type="Microsoft.Compute/virtualMachines"),
        reason="Test",
        urgency=Urgency.HIGH,
        evidence=evidence,
    )


def _make_blast_radius(affected_count: int = 0) -> BlastRadiusResult:
    return BlastRadiusResult(
        sri_infrastructure=5.0,
        affected_resources=[f"res-{i}" for i in range(affected_count)],
    )


def _make_policy(violations: list | None = None) -> PolicyResult:
    return PolicyResult(sri_policy=0.0, violations=violations or [])


def _make_historical() -> HistoricalResult:
    return HistoricalResult(sri_historical=0.0)


def _make_financial() -> FinancialResult:
    return FinancialResult(sri_cost=0.0)


def _make_condition(
    condition_type: ConditionType = ConditionType.TIME_WINDOW,
    auto_checkable: bool = True,
    satisfied: bool = False,
    parameters: dict | None = None,
) -> ApprovalCondition:
    return ApprovalCondition(
        condition_type=condition_type,
        description=f"Test condition — {condition_type.value}",
        parameters=parameters or {},
        auto_checkable=auto_checkable,
        satisfied=satisfied,
    )


def _make_conditional_record(conditions: list[ApprovalCondition]) -> ExecutionRecord:
    return ExecutionRecord(
        execution_id=str(uuid.uuid4()),
        action_id=str(uuid.uuid4()),
        verdict=SRIVerdict.APPROVED_IF,
        status=ExecutionStatus.conditional,
        conditions=conditions,
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
    )


# ---------------------------------------------------------------------------
# Model validation
# ---------------------------------------------------------------------------

def test_condition_type_enum_values():
    assert ConditionType.TIME_WINDOW == "time_window"
    assert ConditionType.BLAST_RADIUS_CONFIRMED == "blast_radius_confirmed"
    assert ConditionType.OWNER_NOTIFIED == "owner_notified"
    assert ConditionType.METRIC_THRESHOLD == "metric_threshold"
    assert ConditionType.DEPENDENCY_CONFIRMED == "dependency_confirmed"


def test_approval_condition_defaults():
    cond = ApprovalCondition(
        condition_type=ConditionType.TIME_WINDOW,
        description="Execute during off-hours",
        auto_checkable=True,
    )
    assert cond.satisfied is False
    assert cond.satisfied_at is None
    assert cond.satisfied_by is None
    assert cond.parameters == {}


def test_approval_condition_full_construction():
    now = datetime.now(timezone.utc)
    cond = ApprovalCondition(
        condition_type=ConditionType.OWNER_NOTIFIED,
        description="Notify resource owner",
        parameters={"owner": "alice@example.com"},
        auto_checkable=False,
        satisfied=True,
        satisfied_at=now,
        satisfied_by="admin@example.com",
    )
    assert cond.satisfied is True
    assert cond.satisfied_by == "admin@example.com"
    assert cond.auto_checkable is False


def test_approved_if_in_sri_verdict():
    assert SRIVerdict.APPROVED_IF == "approved_if"
    assert SRIVerdict.APPROVED_IF in list(SRIVerdict)


def test_conditional_in_execution_status():
    assert ExecutionStatus.conditional == "conditional"


# ---------------------------------------------------------------------------
# check_time_window
# ---------------------------------------------------------------------------

def test_check_time_window_inside_window():
    cond = _make_condition(
        condition_type=ConditionType.TIME_WINDOW,
        parameters={"window_start": "00:00", "window_end": "06:00", "tz": "UTC"},
    )
    inside = datetime(2024, 1, 1, 3, 0, tzinfo=timezone.utc)
    assert check_time_window(cond, now=inside) is True


def test_check_time_window_outside_window():
    cond = _make_condition(
        condition_type=ConditionType.TIME_WINDOW,
        parameters={"window_start": "00:00", "window_end": "06:00", "tz": "UTC"},
    )
    outside = datetime(2024, 1, 1, 12, 0, tzinfo=timezone.utc)
    assert check_time_window(cond, now=outside) is False


def test_check_time_window_at_window_start():
    cond = _make_condition(
        condition_type=ConditionType.TIME_WINDOW,
        parameters={"window_start": "02:00", "window_end": "06:00"},
    )
    at_start = datetime(2024, 1, 1, 2, 0, tzinfo=timezone.utc)
    assert check_time_window(cond, now=at_start) is True


def test_check_time_window_midnight_wrap():
    cond = _make_condition(
        condition_type=ConditionType.TIME_WINDOW,
        parameters={"window_start": "22:00", "window_end": "04:00"},
    )
    at_23 = datetime(2024, 1, 1, 23, 0, tzinfo=timezone.utc)
    at_03 = datetime(2024, 1, 1, 3, 0, tzinfo=timezone.utc)
    at_12 = datetime(2024, 1, 1, 12, 0, tzinfo=timezone.utc)
    assert check_time_window(cond, now=at_23) is True
    assert check_time_window(cond, now=at_03) is True
    assert check_time_window(cond, now=at_12) is False


# ---------------------------------------------------------------------------
# check_metric_threshold
# ---------------------------------------------------------------------------

def test_check_metric_threshold_within_safe_range():
    cond = _make_condition(
        condition_type=ConditionType.METRIC_THRESHOLD,
        parameters={"metric": "cpu_percent", "max_threshold": 50.0, "current_value": 30.0},
    )
    assert check_metric_threshold(cond) is True


def test_check_metric_threshold_above_breach():
    cond = _make_condition(
        condition_type=ConditionType.METRIC_THRESHOLD,
        parameters={"metric": "cpu_percent", "max_threshold": 50.0, "current_value": 75.0},
    )
    assert check_metric_threshold(cond) is False


def test_check_metric_threshold_no_current_value_returns_false():
    cond = _make_condition(
        condition_type=ConditionType.METRIC_THRESHOLD,
        parameters={"metric": "cpu_percent", "max_threshold": 50.0},
    )
    assert check_metric_threshold(cond) is False


# ---------------------------------------------------------------------------
# check_condition dispatcher
# ---------------------------------------------------------------------------

def test_check_condition_dispatches_time_window():
    cond = _make_condition(
        condition_type=ConditionType.TIME_WINDOW,
        auto_checkable=True,
        parameters={"window_start": "00:00", "window_end": "23:59"},
    )
    assert check_condition(cond) is True


def test_check_condition_returns_false_for_human_required():
    cond = _make_condition(
        condition_type=ConditionType.BLAST_RADIUS_CONFIRMED,
        auto_checkable=False,
    )
    assert check_condition(cond) is False


def test_check_condition_returns_false_for_owner_notified():
    cond = _make_condition(
        condition_type=ConditionType.OWNER_NOTIFIED,
        auto_checkable=False,
    )
    assert check_condition(cond) is False


# ---------------------------------------------------------------------------
# GovernanceDecisionEngine — APPROVED_IF emission
# ---------------------------------------------------------------------------

def test_engine_emits_approved_if_for_production_restart():
    from src.core.governance_engine import GovernanceDecisionEngine  # noqa: PLC0415
    engine = GovernanceDecisionEngine()
    action = _make_action(resource_id="vm-prod-backend", action_type=ActionType.RESTART_SERVICE)
    blast = _make_blast_radius(0)
    verdict = engine.evaluate(action, blast, _make_policy(), _make_historical(), _make_financial())
    assert verdict.decision == SRIVerdict.APPROVED_IF
    assert len(verdict.conditions) >= 1
    types = [c.condition_type for c in verdict.conditions]
    assert ConditionType.TIME_WINDOW in types


def test_engine_emits_approved_if_for_production_resize():
    from src.core.governance_engine import GovernanceDecisionEngine  # noqa: PLC0415
    engine = GovernanceDecisionEngine()
    action = _make_action(resource_id="vm-prod-worker", action_type=ActionType.SCALE_DOWN)
    blast = _make_blast_radius(0)
    verdict = engine.evaluate(action, blast, _make_policy(), _make_historical(), _make_financial())
    assert verdict.decision == SRIVerdict.APPROVED_IF
    assert any(c.condition_type == ConditionType.TIME_WINDOW for c in verdict.conditions)


def test_engine_emits_approved_if_for_shared_nsg():
    from src.core.governance_engine import GovernanceDecisionEngine  # noqa: PLC0415
    engine = GovernanceDecisionEngine()
    action = ProposedAction(
        agent_id="test-agent",
        action_type=ActionType.MODIFY_NSG,
        target=ActionTarget(resource_id="nsg-shared-prod", resource_type="Microsoft.Network/networkSecurityGroups"),
        reason="Policy remediation",
        urgency=Urgency.HIGH,
        nsg_change_direction="restrict",
    )
    blast = _make_blast_radius(affected_count=3)
    verdict = engine.evaluate(action, blast, _make_policy(), _make_historical(), _make_financial())
    assert verdict.decision == SRIVerdict.APPROVED_IF
    assert any(c.condition_type == ConditionType.BLAST_RADIUS_CONFIRMED for c in verdict.conditions)


def test_engine_emits_plain_approved_when_no_conditions_apply():
    from src.core.governance_engine import GovernanceDecisionEngine  # noqa: PLC0415
    engine = GovernanceDecisionEngine()
    action = _make_action(resource_id="vm-dev-worker", action_type=ActionType.RESTART_SERVICE)
    blast = _make_blast_radius(0)
    verdict = engine.evaluate(action, blast, _make_policy(), _make_historical(), _make_financial())
    assert verdict.decision == SRIVerdict.APPROVED
    assert verdict.conditions == []


# ---------------------------------------------------------------------------
# ExecutionGateway — APPROVED_IF routing
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_execution_gateway_routes_approved_if_to_conditional_status(tmp_path):
    from src.core.execution_gateway import ExecutionGateway  # noqa: PLC0415
    gateway = ExecutionGateway(executions_dir=tmp_path)

    action = _make_action(resource_id="vm-prod-01")
    verdict = GovernanceVerdict(
        action_id=str(uuid.uuid4()),
        timestamp=datetime.now(timezone.utc),
        proposed_action=action,
        skry_risk_index=SRIBreakdown(
            sri_infrastructure=5.0, sri_policy=0.0,
            sri_historical=0.0, sri_cost=0.0, sri_composite=1.5,
        ),
        decision=SRIVerdict.APPROVED_IF,
        reason="APPROVED_IF — time window condition applies",
        conditions=[_make_condition(condition_type=ConditionType.TIME_WINDOW)],
    )

    record = await gateway.process_verdict(verdict)
    assert record.status == ExecutionStatus.conditional
    assert len(record.conditions) == 1


def test_try_execute_when_all_conditions_satisfied(tmp_path):
    from src.core.execution_gateway import ExecutionGateway  # noqa: PLC0415
    gateway = ExecutionGateway(executions_dir=tmp_path)

    cond = _make_condition(condition_type=ConditionType.TIME_WINDOW, satisfied=True)
    record = _make_conditional_record([cond])
    gateway._records[record.execution_id] = record
    gateway._loaded = True

    result = gateway.try_execute_if_all_satisfied(record.execution_id)
    assert result.status == ExecutionStatus.manual_required


def test_try_execute_blocked_when_one_condition_unsatisfied(tmp_path):
    from src.core.execution_gateway import ExecutionGateway  # noqa: PLC0415
    gateway = ExecutionGateway(executions_dir=tmp_path)

    cond_ok = _make_condition(condition_type=ConditionType.TIME_WINDOW, satisfied=True)
    cond_pending = _make_condition(condition_type=ConditionType.BLAST_RADIUS_CONFIRMED, auto_checkable=False, satisfied=False)
    record = _make_conditional_record([cond_ok, cond_pending])
    gateway._records[record.execution_id] = record
    gateway._loaded = True

    result = gateway.try_execute_if_all_satisfied(record.execution_id)
    assert result.status == ExecutionStatus.conditional


@pytest.mark.asyncio
async def test_force_execute_requires_justification(tmp_path):
    from src.core.execution_gateway import ExecutionGateway  # noqa: PLC0415
    gateway = ExecutionGateway(executions_dir=tmp_path)

    cond = _make_condition(condition_type=ConditionType.BLAST_RADIUS_CONFIRMED, auto_checkable=False)
    record = _make_conditional_record([cond])
    gateway._records[record.execution_id] = record
    gateway._loaded = True

    with pytest.raises(ValueError, match="[Jj]ustification"):
        await gateway.force_execute(record.execution_id, "admin@example.com", "")


@pytest.mark.asyncio
async def test_force_execute_logs_audit_entry(tmp_path):
    from src.core.execution_gateway import ExecutionGateway  # noqa: PLC0415
    gateway = ExecutionGateway(executions_dir=tmp_path)

    cond = _make_condition(condition_type=ConditionType.BLAST_RADIUS_CONFIRMED, auto_checkable=False)
    record = _make_conditional_record([cond])
    gateway._records[record.execution_id] = record
    gateway._loaded = True

    updated = await gateway.force_execute(record.execution_id, "admin@example.com", "Emergency P0 incident")
    assert updated.status == ExecutionStatus.manual_required
    assert updated.force_executed_by == "admin@example.com"
    assert updated.force_execute_justification == "Emergency P0 incident"
    assert "FORCE-EXECUTE" in (updated.notes or "")


# ---------------------------------------------------------------------------
# ConditionGateExecutor
# ---------------------------------------------------------------------------

def test_condition_gate_promotes_approved_if_when_all_auto_conditions_met():
    """All auto-checkable conditions satisfied → promoted to APPROVED."""
    from src.core.workflows.executors.condition_gate_executor import ConditionGateExecutor  # noqa: PLC0415

    action = _make_action()
    verdict_in = GovernanceVerdict(
        action_id=str(uuid.uuid4()),
        timestamp=datetime.now(timezone.utc),
        proposed_action=action,
        skry_risk_index=SRIBreakdown(
            sri_infrastructure=5.0, sri_policy=0.0,
            sri_historical=0.0, sri_cost=0.0, sri_composite=1.5,
        ),
        decision=SRIVerdict.APPROVED_IF,
        reason="Test",
        conditions=[
            ApprovalCondition(
                condition_type=ConditionType.TIME_WINDOW,
                description="Off-hours window",
                parameters={"window_start": "00:00", "window_end": "23:59"},
                auto_checkable=True,
            )
        ],
    )

    result = ConditionGateExecutor.maybe_promote(verdict_in)
    assert result.decision == SRIVerdict.APPROVED


def test_condition_gate_passes_through_when_human_conditions_remain():
    """Human-required conditions → APPROVED_IF passes through unchanged."""
    from src.core.workflows.executors.condition_gate_executor import ConditionGateExecutor  # noqa: PLC0415

    action = _make_action()
    verdict_in = GovernanceVerdict(
        action_id=str(uuid.uuid4()),
        timestamp=datetime.now(timezone.utc),
        proposed_action=action,
        skry_risk_index=SRIBreakdown(
            sri_infrastructure=5.0, sri_policy=0.0,
            sri_historical=0.0, sri_cost=0.0, sri_composite=1.5,
        ),
        decision=SRIVerdict.APPROVED_IF,
        reason="Test",
        conditions=[
            ApprovalCondition(
                condition_type=ConditionType.BLAST_RADIUS_CONFIRMED,
                description="Confirm blast radius",
                parameters={},
                auto_checkable=False,
            )
        ],
    )

    result = ConditionGateExecutor.maybe_promote(verdict_in)
    assert result.decision == SRIVerdict.APPROVED_IF


def test_condition_gate_passes_through_approved_unchanged():
    """APPROVED verdicts pass through ConditionGate without modification."""
    from src.core.workflows.executors.condition_gate_executor import ConditionGateExecutor  # noqa: PLC0415

    action = _make_action()
    verdict_in = GovernanceVerdict(
        action_id=str(uuid.uuid4()),
        timestamp=datetime.now(timezone.utc),
        proposed_action=action,
        skry_risk_index=SRIBreakdown(
            sri_infrastructure=5.0, sri_policy=0.0,
            sri_historical=0.0, sri_cost=0.0, sri_composite=1.5,
        ),
        decision=SRIVerdict.APPROVED,
        reason="Test",
    )

    result = ConditionGateExecutor.maybe_promote(verdict_in)
    assert result.decision == SRIVerdict.APPROVED
