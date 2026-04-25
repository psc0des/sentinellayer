"""Builds the governance workflow graph used when USE_WORKFLOWS=true.

Also exposes ``stream_governance_evaluation()`` — an async generator that runs
the workflow with ``stream=True`` and translates WorkflowEvents into the
dashboard's existing SSE event format.  Frontend requires no changes.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from typing import TYPE_CHECKING, Any

from agent_framework import Workflow, WorkflowBuilder

if TYPE_CHECKING:
    from agent_framework import CheckpointStorage

    from src.core.models import GovernanceVerdict
    from src.core.workflows.messages import GovernanceInput

from src.core.governance_engine import GovernanceDecisionEngine
from src.governance_agents.blast_radius_agent import BlastRadiusAgent
from src.governance_agents.financial_agent import FinancialImpactAgent
from src.governance_agents.historical_agent import HistoricalPatternAgent
from src.governance_agents.policy_agent import PolicyComplianceAgent

from .executors.blast_radius_executor import BlastRadiusExecutor
from .executors.condition_gate_executor import ConditionGateExecutor
from .executors.dispatch_executor import DispatchExecutor
from .executors.financial_executor import FinancialExecutor
from .executors.historical_executor import HistoricalExecutor
from .executors.policy_executor import PolicyExecutor
from .executors.scoring_executor import ScoringExecutor

# Human-readable labels for each governance agent executor.
_AGENT_LABELS: dict[str, str] = {
    "blast_radius": "Blast Radius",
    "policy": "Policy Compliance",
    "historical": "Historical Patterns",
    "financial": "Financial Impact",
}


async def stream_governance_evaluation(
    workflow: Workflow,
    inp: GovernanceInput | None,
    *,
    resource_name: str,
    action_type: str,
    checkpoint_id: str | None = None,
    checkpoint_storage: CheckpointStorage | None = None,
) -> AsyncGenerator[tuple[str, dict[str, Any]] | GovernanceVerdict, None]:
    """Async generator: yields SSE event tuples then the final GovernanceVerdict.

    Translates ``WorkflowEvent`` objects from ``workflow.run(stream=True)`` into
    the dashboard's existing event format so the React frontend requires no changes.

    Each yield is one of:
    - ``(event_type: str, kwargs: dict)`` — emittable via ``_emit_event()``
    - ``GovernanceVerdict`` — the final result; always the last item yielded

    Args:
        workflow: The built ``Workflow`` instance.
        inp: ``GovernanceInput`` for a fresh run; ``None`` when resuming from checkpoint.
        resource_name: Short name for SSE event labels (last ARM ID segment).
        action_type: Action type string for SSE event labels.
        checkpoint_id: If provided (and ``inp`` is None), resumes from this checkpoint.
        checkpoint_storage: Optional ``CheckpointStorage`` backend.
    """
    if checkpoint_id is not None:
        # Resume path — message and checkpoint_id are mutually exclusive
        stream = workflow.run(
            checkpoint_id=checkpoint_id,
            checkpoint_storage=checkpoint_storage,
            stream=True,
        )
    else:
        stream = workflow.run(
            inp,
            checkpoint_storage=checkpoint_storage,
            stream=True,
        )

    verdict: GovernanceVerdict | None = None

    async for event in stream:
        if event.type == "executor_invoked" and event.executor_id in _AGENT_LABELS:
            label = _AGENT_LABELS[event.executor_id]
            yield (
                "evaluation",
                {
                    "resource_id": resource_name,
                    "action_type": action_type,
                    "message": f"  → {label} agent evaluating…",
                },
            )
        elif event.type == "executor_completed" and event.executor_id in _AGENT_LABELS:
            label = _AGENT_LABELS[event.executor_id]
            yield (
                "reasoning",
                {
                    "resource_id": resource_name,
                    "action_type": action_type,
                    "message": f"  ✓ {label} agent complete",
                },
            )
        elif event.type == "output" and event.executor_id == "condition_gate":
            verdict = event.data
        elif event.type in ("failed", "executor_failed"):
            details = getattr(event, "details", None)
            msg = details.message if details else "Unknown error"
            yield (
                "scan_error",
                {"message": f"Governance workflow error: {msg}"},
            )

    if verdict is None:
        raise RuntimeError("Governance workflow ended without producing a verdict")

    yield verdict  # Always the last item


def build_governance_workflow(
    *,
    blast: BlastRadiusAgent,
    policy: PolicyComplianceAgent,
    historical: HistoricalPatternAgent,
    financial: FinancialImpactAgent,
    engine: GovernanceDecisionEngine,
) -> Workflow:
    """Wire up the five-node governance workflow and return a ready-to-run Workflow.

    Topology (single superstep):

        [DispatchExecutor]
              ├──→ BlastRadiusExecutor ─┐
              ├──→ PolicyExecutor       ├──→ [ScoringExecutor] → [ConditionGateExecutor] → GovernanceVerdict
              ├──→ HistoricalExecutor   │
              └──→ FinancialExecutor   ─┘

    The four governance executors run in parallel.  ScoringExecutor is invoked
    only after all four have sent their GovernanceAgentResult (fan-in barrier).
    ConditionGateExecutor may promote APPROVED_IF → APPROVED when all conditions
    are already satisfied at evaluation time.
    """
    dispatch = DispatchExecutor()
    blast_ex = BlastRadiusExecutor(blast)
    policy_ex = PolicyExecutor(policy)
    historical_ex = HistoricalExecutor(historical)
    financial_ex = FinancialExecutor(financial)
    scoring = ScoringExecutor(engine)
    gate = ConditionGateExecutor()

    return (
        WorkflowBuilder(start_executor=dispatch, output_executors=[gate])
        .add_fan_out_edges(dispatch, [blast_ex, policy_ex, historical_ex, financial_ex])
        .add_fan_in_edges([blast_ex, policy_ex, historical_ex, financial_ex], scoring)
        .add_edge(scoring, gate)
        .build()
    )
