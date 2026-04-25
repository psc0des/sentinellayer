"""Dispatch executor — workflow entry point that fans out to governance agents."""

import agent_framework as af

from ..messages import GovernanceInput


class DispatchExecutor(af.Executor):
    def __init__(self) -> None:
        super().__init__("dispatch")

    @af.handler
    async def dispatch(
        self, message: GovernanceInput, ctx: af.WorkflowContext[GovernanceInput]
    ) -> None:
        await ctx.send_message(message)
