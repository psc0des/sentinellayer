"""Operational agent A2A client wrappers.

Each class wraps one of the three operational agents and uses the A2A protocol
to send governance requests to SentinelLayer.

Pattern (same for all three clients)
--------------------------------------
1. ``A2ACardResolver.get_agent_card()`` — download SentinelLayer's Agent Card
   from ``/.well-known/agent-card.json``.  This tells the client where to send
   requests and what the server can do.
2. ``agent.scan()`` — run the operational agent to generate proposals.
3. ``A2AClient.send_message_streaming()`` — send each proposal as an A2A task
   and receive streaming SSE progress updates plus the final verdict.
4. Parse the ``GovernanceVerdict`` from the artifact payload.
5. Call ``AgentRegistry.register_agent()`` + ``update_agent_stats()`` after each
   evaluation to keep the registry current.

Why streaming?
--------------
``send_message_streaming()`` returns an ``AsyncGenerator`` that yields events as
the server pushes them.  Each ``TaskStatusUpdateEvent`` contains a progress
message; the final ``TaskArtifactUpdateEvent`` contains the verdict JSON.
This is how a human at a dashboard would see live "Evaluating..." messages.

Why httpx.AsyncClient?
----------------------
A2AClient uses ``httpx`` as its transport layer.  We pass an
``httpx.AsyncClient`` so all network calls happen inside the same async event
loop — no blocking threads, consistent with the rest of SentinelLayer's
async-first design.
"""

import logging
import os
import uuid
from typing import Any

import httpx
from a2a.client import A2ACardResolver, A2AClient
from a2a.types import (
    Message,
    MessageSendParams,
    Part,
    Role,
    SendStreamingMessageRequest,
    TaskArtifactUpdateEvent,
    TaskStatusUpdateEvent,
    TextPart,
)
from a2a.utils import get_artifact_text, get_message_text

from src.core.models import GovernanceVerdict, ProposedAction
from src.operational_agents.cost_agent import CostOptimizationAgent
from src.operational_agents.deploy_agent import DeployAgent
from src.operational_agents.monitoring_agent import MonitoringAgent

logger = logging.getLogger(__name__)

_DEFAULT_SERVER_URL = os.getenv("A2A_SERVER_URL", "http://localhost:8000")


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _build_streaming_request(action: ProposedAction) -> SendStreamingMessageRequest:
    """Wrap a ProposedAction in an A2A streaming message request.

    The A2A protocol uses JSON-RPC 2.0.  A ``SendStreamingMessageRequest``
    contains a ``Message`` with one or more ``Part`` objects.  We use a single
    ``TextPart`` carrying the ProposedAction as a JSON string.

    Args:
        action: The governance action to evaluate.

    Returns:
        A ready-to-send A2A streaming request.
    """
    return SendStreamingMessageRequest(
        id=str(uuid.uuid4()),
        params=MessageSendParams(
            message=Message(
                role=Role.user,
                parts=[
                    Part(
                        root=TextPart(
                            kind="text",
                            text=action.model_dump_json(),
                        )
                    )
                ],
                message_id=str(uuid.uuid4()),
            )
        ),
    )


async def send_action_to_sentinel(
    action: ProposedAction,
    server_url: str,
    agent_name: str,
) -> GovernanceVerdict | None:
    """Send one ProposedAction to SentinelLayer via A2A streaming.

    Steps:
    1. Open an httpx.AsyncClient (context manager — auto-closes on exit).
    2. Resolve the Agent Card from /.well-known/agent-card.json.
    3. Create an A2AClient bound to that card.
    4. Stream the task and collect progress messages + the verdict artifact.
    5. Parse the final artifact JSON as a GovernanceVerdict and return it.

    Args:
        action: Proposed infrastructure action from the operational agent.
        server_url: Base URL of the SentinelLayer A2A server.
        agent_name: Human-readable name for logging (e.g. "cost-optimization-agent").

    Returns:
        Parsed ``GovernanceVerdict``, or ``None`` if the call failed.
    """
    async with httpx.AsyncClient(base_url=server_url, timeout=120.0) as http_client:
        # Step 1 — Discover SentinelLayer via its Agent Card
        resolver = A2ACardResolver(http_client=http_client, base_url=server_url)
        try:
            agent_card = await resolver.get_agent_card()
            logger.debug("[%s] A2A: discovered agent '%s'", agent_name, agent_card.name)
        except Exception as exc:
            logger.error(
                "[%s] A2A: failed to resolve Agent Card from %s — %s",
                agent_name,
                server_url,
                exc,
            )
            return None

        # Step 2 — Create the A2A client bound to the discovered agent card
        client = A2AClient(httpx_client=http_client, agent_card=agent_card)

        # Step 3 — Build and send the streaming request
        request = _build_streaming_request(action)
        logger.info(
            "[%s] A2A: sending action=%s resource=%s",
            agent_name,
            action.action_type.value,
            action.target.resource_id,
        )

        # Step 4 — Process the SSE event stream
        verdict_json: str | None = None

        try:
            async for event in client.send_message_streaming(request):
                # Each event has a .root that is either a TaskStatusUpdateEvent
                # (progress message) or a TaskArtifactUpdateEvent (final result).
                root = getattr(event, "root", event)

                if isinstance(root, TaskStatusUpdateEvent):
                    # Progress message — log it so the demo shows live updates
                    if root.status and root.status.message:
                        text = get_message_text(root.status.message)
                        if text:
                            logger.info("[%s] A2A progress: %s", agent_name, text)

                elif isinstance(root, TaskArtifactUpdateEvent):
                    # Artifact — this is the GovernanceVerdict JSON
                    text = get_artifact_text(root.artifact)
                    if text:
                        verdict_json = text
                        logger.debug(
                            "[%s] A2A: received artifact (%d chars)",
                            agent_name,
                            len(text),
                        )
        except Exception as exc:
            logger.error("[%s] A2A: streaming error — %s", agent_name, exc)
            return None

    # Step 5 — Parse the verdict
    if verdict_json:
        try:
            verdict = GovernanceVerdict.model_validate_json(verdict_json)
            logger.info(
                "[%s] A2A: verdict=%s sri=%.1f",
                agent_name,
                verdict.decision.value.upper(),
                verdict.sentinel_risk_index.sri_composite,
            )
            return verdict
        except Exception as exc:
            logger.error("[%s] A2A: failed to parse GovernanceVerdict — %s", agent_name, exc)

    return None


# ---------------------------------------------------------------------------
# Client wrappers for each operational agent
# ---------------------------------------------------------------------------


class CostAgentA2AClient:
    """A2A client wrapper for the CostOptimizationAgent.

    Scans for cost proposals, then evaluates each via SentinelLayer using
    the A2A protocol.  Updates the agent registry after each evaluation.

    Usage::

        client = CostAgentA2AClient()
        results = await client.run()
        for r in results:
            print(r["verdict"]["decision"], r["action"]["target"]["resource_id"])
    """

    AGENT_NAME = "cost-optimization-agent"

    def __init__(
        self,
        server_url: str = _DEFAULT_SERVER_URL,
        cfg=None,
    ) -> None:
        self._server_url = server_url
        self._cost_agent = CostOptimizationAgent(cfg=cfg)

    async def run(self) -> list[dict[str, Any]]:
        """Scan for cost proposals and evaluate each via A2A.

        Returns:
            List of dicts with keys ``"action"`` and ``"verdict"``.
        """
        from src.a2a.agent_registry import AgentRegistry

        registry = AgentRegistry()
        registry.register_agent(self.AGENT_NAME, agent_card_url="")

        proposals: list[ProposedAction] = await self._cost_agent.scan()
        logger.info("[%s] scanned %d proposals", self.AGENT_NAME, len(proposals))

        results: list[dict[str, Any]] = []
        for action in proposals:
            verdict = await send_action_to_sentinel(
                action, self._server_url, self.AGENT_NAME
            )
            if verdict:
                registry.update_agent_stats(self.AGENT_NAME, verdict.decision.value)
                results.append(
                    {
                        "action": action.model_dump(),
                        "verdict": verdict.model_dump(),
                    }
                )

        return results


class MonitoringAgentA2AClient:
    """A2A client wrapper for the MonitoringAgent.

    Scans for anomaly-based proposals, then evaluates each via SentinelLayer.

    Usage::

        client = MonitoringAgentA2AClient()
        results = await client.run()
    """

    AGENT_NAME = "monitoring-agent"

    def __init__(
        self,
        server_url: str = _DEFAULT_SERVER_URL,
        cfg=None,
    ) -> None:
        self._server_url = server_url
        self._monitoring_agent = MonitoringAgent(cfg=cfg)

    async def run(self) -> list[dict[str, Any]]:
        """Scan for monitoring proposals and evaluate each via A2A.

        Returns:
            List of dicts with keys ``"action"`` and ``"verdict"``.
        """
        from src.a2a.agent_registry import AgentRegistry

        registry = AgentRegistry()
        registry.register_agent(self.AGENT_NAME, agent_card_url="")

        proposals: list[ProposedAction] = await self._monitoring_agent.scan()
        logger.info("[%s] scanned %d proposals", self.AGENT_NAME, len(proposals))

        results: list[dict[str, Any]] = []
        for action in proposals:
            verdict = await send_action_to_sentinel(
                action, self._server_url, self.AGENT_NAME
            )
            if verdict:
                registry.update_agent_stats(self.AGENT_NAME, verdict.decision.value)
                results.append(
                    {
                        "action": action.model_dump(),
                        "verdict": verdict.model_dump(),
                    }
                )

        return results


class DeployAgentA2AClient:
    """A2A client wrapper for the DeployAgent.

    Scans for infrastructure deployment proposals (NSG rules, lifecycle tags,
    sparse topology), then evaluates each via SentinelLayer.

    Usage::

        client = DeployAgentA2AClient()
        results = await client.run()
    """

    AGENT_NAME = "deploy-agent"

    def __init__(
        self,
        server_url: str = _DEFAULT_SERVER_URL,
        cfg=None,
    ) -> None:
        self._server_url = server_url
        self._deploy_agent = DeployAgent(cfg=cfg)

    async def run(self) -> list[dict[str, Any]]:
        """Scan for deploy proposals and evaluate each via A2A.

        Returns:
            List of dicts with keys ``"action"`` and ``"verdict"``.
        """
        from src.a2a.agent_registry import AgentRegistry

        registry = AgentRegistry()
        registry.register_agent(self.AGENT_NAME, agent_card_url="")

        proposals: list[ProposedAction] = await self._deploy_agent.scan()
        logger.info("[%s] scanned %d proposals", self.AGENT_NAME, len(proposals))

        results: list[dict[str, Any]] = []
        for action in proposals:
            verdict = await send_action_to_sentinel(
                action, self._server_url, self.AGENT_NAME
            )
            if verdict:
                registry.update_agent_stats(self.AGENT_NAME, verdict.decision.value)
                results.append(
                    {
                        "action": action.model_dump(),
                        "verdict": verdict.model_dump(),
                    }
                )

        return results
