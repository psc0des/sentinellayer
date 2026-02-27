"""SentinelLayer A2A Server — exposes the governance engine via the A2A protocol.

What is A2A?
-----------
A2A (Agent-to-Agent) is an open protocol that lets AI agents talk to each other
using a standard HTTP + JSON-RPC interface.  Each agent publishes an **Agent Card**
(a machine-readable description of its capabilities) at a well-known URL.  Other
agents discover it, then send tasks and receive streaming progress updates via
Server-Sent Events (SSE).

SentinelLayer as an A2A Server
-------------------------------
SentinelLayer publishes its governance engine as an A2A server.  External
operational agents (cost-agent, monitoring-agent, deploy-agent) discover it via
the Agent Card, send ``ProposedAction`` JSON payloads as task messages, and
receive ``GovernanceVerdict`` results with streaming SRI progress updates.

Agent Card is served at:
  GET /.well-known/agent-card.json   (current A2A standard)
  GET /.well-known/agent.json        (legacy alias — also served by the SDK)

Run
---
    uvicorn src.a2a.sentinel_a2a_server:app --host 0.0.0.0 --port 8000
"""

import logging
import os
from typing import Any

from a2a.server.agent_execution import AgentExecutor, RequestContext
from a2a.server.apps import A2AFastAPIApplication
from a2a.server.events import EventQueue, InMemoryQueueManager
from a2a.server.request_handlers import DefaultRequestHandler
from a2a.server.tasks import InMemoryTaskStore, TaskUpdater
from a2a.types import AgentCapabilities, AgentCard, AgentSkill, Part, TextPart
from a2a.utils import new_agent_text_message

from src.core.decision_tracker import DecisionTracker
from src.core.models import GovernanceVerdict, ProposedAction
from src.core.pipeline import SentinelLayerPipeline

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Pipeline singleton — created once, shared across all requests.
# ---------------------------------------------------------------------------

_pipeline: SentinelLayerPipeline | None = None


def get_pipeline() -> SentinelLayerPipeline:
    """Return the module-level pipeline singleton, creating it on first call."""
    global _pipeline
    if _pipeline is None:
        _pipeline = SentinelLayerPipeline()
    return _pipeline


# ---------------------------------------------------------------------------
# Agent Executor — the core request handler
# ---------------------------------------------------------------------------


class SentinelAgentExecutor(AgentExecutor):
    """A2A AgentExecutor that routes governance requests through SentinelLayer.

    The A2A SDK calls ``execute()`` for every incoming task.  We:
    1. Parse the user message as a ``ProposedAction`` JSON string.
    2. Stream intermediate progress updates via SSE (Server-Sent Events).
    3. Run the full governance pipeline (all 4 agents in parallel).
    4. Return the ``GovernanceVerdict`` as an A2A artifact.

    Why streaming?
    --------------
    A2A supports Server-Sent Events — the server can push multiple messages
    *before* the final result.  Clients that call ``send_message_streaming()``
    receive these as a live feed.  This lets the user see "Evaluating blast
    radius..." and "Checking policy compliance..." while the pipeline runs.
    """

    def __init__(self) -> None:
        self._pipeline = get_pipeline()

    async def execute(
        self, context: RequestContext, event_queue: EventQueue
    ) -> None:
        """Evaluate a ProposedAction and stream governance progress to the client.

        Args:
            context: A2A request context — provides task_id, context_id,
                and ``get_user_input()`` which returns the text payload.
            event_queue: The SSE event queue — write events here to stream
                them to the client in real time.
        """
        # TaskUpdater is the helper that writes events to event_queue.
        # Think of it as the "output stream" for this task.
        updater = TaskUpdater(event_queue, context.task_id, context.context_id)

        # submit() → task received; start_work() → task is actively being processed
        updater.submit()
        updater.start_work()

        # ── Parse the incoming ProposedAction ─────────────────────────────
        user_text = context.get_user_input()
        logger.info(
            "A2A: received task %s (%d chars)", context.task_id, len(user_text)
        )

        try:
            action = ProposedAction.model_validate_json(user_text)
        except Exception as exc:
            logger.error("A2A: invalid ProposedAction payload: %s", exc)
            # complete() with an error message ends the task gracefully
            updater.complete(
                message=new_agent_text_message(
                    f"ERROR: invalid ProposedAction JSON — {exc}",
                    task_id=context.task_id,
                    context_id=context.context_id,
                )
            )
            return

        # ── Stream progress — these are sent to the client via SSE ─────────
        # new_agent_message() enqueues a TaskStatusUpdateEvent.  Clients that
        # called send_message_streaming() will receive each of these in order.
        updater.new_agent_message(
            [Part(root=TextPart(kind="text", text="Evaluating blast radius..."))]
        )
        updater.new_agent_message(
            [Part(root=TextPart(kind="text", text="Checking policy compliance..."))]
        )
        updater.new_agent_message(
            [Part(root=TextPart(kind="text", text="Querying historical incidents..."))]
        )
        updater.new_agent_message(
            [Part(root=TextPart(kind="text", text="Calculating financial impact..."))]
        )

        # ── Run the full governance pipeline (async, all 4 agents in parallel) ─
        verdict: GovernanceVerdict = await self._pipeline.evaluate(action)

        # ── Write to audit trail so A2A decisions appear in /api/evaluations ─
        try:
            DecisionTracker().record(verdict)
        except Exception as exc:
            logger.warning("A2A: failed to record verdict to audit trail — %s", exc)

        sri = verdict.sentinel_risk_index.sri_composite
        decision = verdict.decision.value.upper()

        # ── Stream final SRI summary ─────────────────────────────────────
        updater.new_agent_message(
            [
                Part(
                    root=TextPart(
                        kind="text",
                        text=f"SRI Composite: {sri:.1f} → {decision}",
                    )
                )
            ]
        )

        # ── Return full GovernanceVerdict as an artifact ──────────────────
        # add_artifact() attaches the result payload to the task.
        # complete() finalises the task — the client's stream ends here.
        updater.add_artifact(
            parts=[Part(root=TextPart(kind="text", text=verdict.model_dump_json()))],
            name="governance_verdict",
        )
        updater.complete()

        logger.info(
            "A2A: completed task %s — verdict=%s sri=%.1f agent=%s",
            context.task_id,
            decision,
            sri,
            action.agent_id,
        )

    async def cancel(
        self, context: RequestContext, event_queue: EventQueue
    ) -> None:
        """Cancel is not supported — governance evaluations are atomic."""
        logger.warning(
            "A2A: cancel requested for task %s — not supported", context.task_id
        )


# ---------------------------------------------------------------------------
# Agent Card builder
# ---------------------------------------------------------------------------


def _build_agent_card(server_url: str) -> AgentCard:
    """Build the A2A Agent Card that advertises SentinelLayer's capabilities.

    The Agent Card is a JSON document that other agents download to learn:
    - What this agent is called and what it does
    - Which skills it exposes
    - Where to send requests
    - What input/output formats it supports

    Think of it like a business card for an AI agent.
    """
    return AgentCard(
        name="SentinelLayer Governance Engine",
        description=(
            "AI Action Governance — evaluates proposed infrastructure actions "
            "using SRI™ scoring. Intercepts AI agent actions and scores them "
            "across infrastructure blast radius, policy compliance, historical "
            "incidents, and financial impact before any execution is allowed."
        ),
        url=server_url,
        version="1.0.0",
        # Streaming=True means we support SSE (Server-Sent Events) for live updates
        capabilities=AgentCapabilities(streaming=True),
        skills=[
            AgentSkill(
                id="evaluate_action",
                name="Evaluate Action",
                description=(
                    "Evaluate a ProposedAction JSON object against all governance "
                    "policies and return a GovernanceVerdict with full SRI™ breakdown "
                    "(infrastructure, policy, historical, cost dimensions)."
                ),
                tags=["governance", "risk", "SRI", "infrastructure"],
            ),
            AgentSkill(
                id="query_decision_history",
                name="Query Decision History",
                description=(
                    "Query past governance decisions from the Cosmos DB audit trail."
                ),
                tags=["history", "audit", "decisions"],
            ),
            AgentSkill(
                id="get_resource_risk_profile",
                name="Get Resource Risk Profile",
                description=(
                    "Get the aggregated SRI™ risk profile for a specific Azure resource "
                    "based on all historical evaluations."
                ),
                tags=["risk", "resource", "profile"],
            ),
        ],
        default_input_modes=["text/plain"],
        default_output_modes=["text/plain"],
    )


# ---------------------------------------------------------------------------
# App factory
# ---------------------------------------------------------------------------


def create_app() -> Any:
    """Create and return the A2A-compliant FastAPI application.

    This wires together:
    - The Agent Card (published at /.well-known/agent-card.json)
    - The AgentExecutor (SentinelAgentExecutor)
    - The DefaultRequestHandler (routes JSON-RPC calls to the executor)
    - InMemoryTaskStore (tracks in-flight tasks)
    - InMemoryQueueManager (manages SSE event queues)

    Returns:
        A FastAPI application ready to be served by uvicorn.
    """
    server_url = os.getenv("A2A_SERVER_URL", "http://localhost:8000")
    logger.info("A2A server: building app with URL=%s", server_url)

    agent_card = _build_agent_card(server_url)
    executor = SentinelAgentExecutor()

    handler = DefaultRequestHandler(
        agent_executor=executor,
        task_store=InMemoryTaskStore(),
        queue_manager=InMemoryQueueManager(),
    )

    a2a_app = A2AFastAPIApplication(
        agent_card=agent_card,
        http_handler=handler,
    )

    # .build() returns a fully-configured FastAPI app with all A2A routes:
    #   GET  /.well-known/agent-card.json  → Agent Card JSON
    #   GET  /.well-known/agent.json       → Agent Card JSON (legacy alias)
    #   POST /                             → JSON-RPC tasks/sendMessage endpoint
    #   GET  /                             → SSE streaming endpoint
    return a2a_app.build()


# ---------------------------------------------------------------------------
# Module-level ASGI app — used directly by uvicorn
# ---------------------------------------------------------------------------
# uvicorn src.a2a.sentinel_a2a_server:app --host 0.0.0.0 --port 8000

app = create_app()
