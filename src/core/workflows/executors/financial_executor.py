"""Financial executor — wraps FinancialImpactAgent.evaluate() as a workflow handler."""

import agent_framework as af

from src.governance_agents.financial_agent import FinancialImpactAgent

from ..messages import GovernanceAgentResult, GovernanceInput


class FinancialExecutor(af.Executor):
    def __init__(self, agent: FinancialImpactAgent) -> None:
        super().__init__("financial")
        self._agent = agent

    @af.handler
    async def evaluate(
        self,
        message: GovernanceInput,
        ctx: af.WorkflowContext[GovernanceAgentResult],
    ) -> None:
        result = await self._agent.evaluate(
            message.action, force_deterministic=message.force_deterministic
        )
        await ctx.send_message(
            GovernanceAgentResult(
                agent_name="financial",
                action=message.action,
                result=result,
                triage_tier=message.triage_tier,
            )
        )
