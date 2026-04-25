"""Scoring executor — collects all four agent results and produces the governance verdict.

Phase 32 Part 2: ScoringExecutor now sends the verdict onward via send_message()
instead of yield_output(), so ConditionGateExecutor can inspect and potentially
promote APPROVED_IF → APPROVED before the workflow emits its final output.
"""

import agent_framework as af

from src.core.governance_engine import GovernanceDecisionEngine
from src.core.models import GovernanceVerdict

from ..messages import GovernanceAgentResult


class ScoringExecutor(af.Executor):
    def __init__(self, engine: GovernanceDecisionEngine) -> None:
        super().__init__("scoring")
        self._engine = engine

    @af.handler
    async def score(
        self,
        messages: list[GovernanceAgentResult],
        ctx: af.WorkflowContext[GovernanceVerdict, None],
    ) -> None:
        by_name = {r.agent_name: r.result for r in messages}
        first = messages[0]

        verdict = self._engine.evaluate(
            first.action,
            by_name["blast_radius"],
            by_name["policy"],
            by_name["historical"],
            by_name["financial"],
        )

        verdict.triage_tier = first.triage_tier
        verdict.triage_mode = "deterministic" if first.triage_tier == 1 else "full"

        # Send to ConditionGateExecutor — it decides whether to promote or pass through.
        await ctx.send_message(verdict)
