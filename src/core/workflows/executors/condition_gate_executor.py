"""ConditionGate executor — promotes APPROVED_IF → APPROVED when all conditions are met.

This executor sits between ScoringExecutor and the workflow output.  It receives a
GovernanceVerdict from ScoringExecutor and:

1. If verdict.decision == APPROVED_IF:
   - Checks all auto-checkable conditions immediately.
   - If ALL auto-checkable conditions are already satisfied AND there are no
     human-required conditions, promotes the verdict to APPROVED in-flight.
   - Otherwise, passes the verdict through as APPROVED_IF (the execution gateway
     will set the record status to CONDITIONAL for watcher polling).

2. For any other verdict (APPROVED, ESCALATED, DENIED), passes through unchanged.

Phase 33 note: This executor requires USE_WORKFLOWS=true (it lives inside the
workflow graph).  The legacy pipeline path in pipeline.py keeps the existing
APPROVED-only flow; no APPROVED_IF conditions are derived there.
"""

import agent_framework as af

from src.core.condition_checkers import check_condition
from src.core.models import GovernanceVerdict, SRIVerdict


class ConditionGateExecutor(af.Executor):
    def __init__(self) -> None:
        super().__init__("condition_gate")

    @af.handler
    async def evaluate(
        self,
        verdict: GovernanceVerdict,
        ctx: af.WorkflowContext[None, GovernanceVerdict],
    ) -> None:
        promoted = self.maybe_promote(verdict)
        await ctx.yield_output(promoted)

    @staticmethod
    def maybe_promote(verdict: GovernanceVerdict) -> GovernanceVerdict:
        """Check conditions and promote APPROVED_IF → APPROVED if all auto-conditions met.

        Operates on a copy of the verdict's conditions in-place.  Returns the
        (possibly mutated) verdict.  Testable without a WorkflowContext.
        """
        if verdict.decision != SRIVerdict.APPROVED_IF:
            return verdict

        conditions = verdict.conditions
        auto_conditions = [c for c in conditions if c.auto_checkable]
        human_conditions = [c for c in conditions if not c.auto_checkable]

        for cond in auto_conditions:
            if not cond.satisfied and check_condition(cond):
                cond.satisfied = True

        all_auto_satisfied = all(c.satisfied for c in auto_conditions)

        if all_auto_satisfied and not human_conditions:
            verdict.decision = SRIVerdict.APPROVED
            verdict.reason += " (all conditions met at evaluation time — promoted to APPROVED)"

        return verdict
