"""Historical executor — wraps HistoricalPatternAgent.evaluate() as a workflow handler."""

import agent_framework as af

from src.governance_agents.historical_agent import HistoricalPatternAgent

from ..messages import GovernanceAgentResult, GovernanceInput


class HistoricalExecutor(af.Executor):
    def __init__(self, agent: HistoricalPatternAgent) -> None:
        super().__init__("historical")
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
                agent_name="historical",
                action=message.action,
                result=result,
                triage_tier=message.triage_tier,
            )
        )
