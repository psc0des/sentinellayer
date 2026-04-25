"""Blast radius executor — wraps BlastRadiusAgent.evaluate() as a workflow handler."""

import agent_framework as af

from src.governance_agents.blast_radius_agent import BlastRadiusAgent

from ..messages import GovernanceAgentResult, GovernanceInput


class BlastRadiusExecutor(af.Executor):
    def __init__(self, agent: BlastRadiusAgent) -> None:
        super().__init__("blast_radius")
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
                agent_name="blast_radius",
                action=message.action,
                result=result,
                triage_tier=message.triage_tier,
            )
        )
