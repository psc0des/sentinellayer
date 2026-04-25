"""Policy executor — wraps PolicyComplianceAgent.evaluate() as a workflow handler."""

import agent_framework as af

from src.governance_agents.policy_agent import PolicyComplianceAgent

from ..messages import GovernanceAgentResult, GovernanceInput


class PolicyExecutor(af.Executor):
    def __init__(self, agent: PolicyComplianceAgent) -> None:
        super().__init__("policy")
        self._agent = agent

    @af.handler
    async def evaluate(
        self,
        message: GovernanceInput,
        ctx: af.WorkflowContext[GovernanceAgentResult],
    ) -> None:
        result = await self._agent.evaluate(
            message.action,
            message.resource_metadata,
            force_deterministic=message.force_deterministic,
        )
        await ctx.send_message(
            GovernanceAgentResult(
                agent_name="policy",
                action=message.action,
                result=result,
                triage_tier=message.triage_tier,
            )
        )
