"""SentinelLayer end-to-end governance pipeline.

Wires all four governance agents together with the GovernanceDecisionEngine,
running the agents in parallel for maximum throughput.

Microsoft Agent Framework integration (Phase 8)
------------------------------------------------
Each governance agent is now a Microsoft Agent Framework ``Agent`` backed
by Azure OpenAI GPT-4.1 (in live mode).  The framework agent calls our
deterministic rule-based tool, gets structured results, and synthesises an
expert reasoning narrative.

The pipeline itself continues to use ``ThreadPoolExecutor`` for parallelism:
each governance agent runs in its own worker thread.  Each agent internally
manages the async event loop required by the framework (via ``asyncio.run()``
called from within the thread — safe because ThreadPoolExecutor threads
have no pre-existing event loop).

In mock mode (USE_LOCAL_MOCKS=true), the framework is bypassed and only
deterministic rule-based scoring runs — identical to Phase 7 behaviour.

Data flow
---------
``ProposedAction``  (from an operational agent)
    │
    ├─ look up target resource metadata from seed_resources.json
    │
    └─ submit all four governance agents to a ThreadPoolExecutor simultaneously
            ├─ BlastRadiusAgent.evaluate(action)      → BlastRadiusResult
            ├─ PolicyComplianceAgent.evaluate(action) → PolicyResult
            ├─ HistoricalPatternAgent.evaluate(action) → HistoricalResult
            └─ FinancialImpactAgent.evaluate(action)  → FinancialResult
                │
                └─ GovernanceDecisionEngine.evaluate(...)
                        │
                        └─ GovernanceVerdict  (APPROVED / ESCALATED / DENIED)

Operational agents (governed subjects)
---------------------------------------
The pipeline exposes ``scan_operational_agents()`` which runs all three
operational agents and returns their raw proposals — before governance
evaluation.  Callers (e.g., demo.py) then pass each proposal through
``evaluate()`` to get the governance verdict.

Why parallel?
-------------
In production each governance agent calls a separate Azure service:
- BlastRadius  → Azure Cosmos DB (Gremlin graph)
- Policy       → local JSON (later: Azure Policy API)
- Historical   → Azure AI Search (vector index)
- Financial    → Azure Cost Management API

Those are all I/O-bound network calls. Running them in sequence would mean
waiting for four round-trips; running them in a ThreadPoolExecutor lets the
four network calls overlap, cutting wall-clock latency by ~75 %.

Even in the current mock implementation (all agents read local files), the
ThreadPoolExecutor pattern is correct to establish now — the code structure
will not change when we swap in real Azure clients.
"""

import asyncio
import json
import logging
from pathlib import Path

from src.core.governance_engine import GovernanceDecisionEngine
from src.core.models import GovernanceVerdict, ProposedAction
from src.governance_agents.blast_radius_agent import BlastRadiusAgent
from src.governance_agents.financial_agent import FinancialImpactAgent
from src.governance_agents.historical_agent import HistoricalPatternAgent
from src.governance_agents.policy_agent import PolicyComplianceAgent
from src.operational_agents.cost_agent import CostOptimizationAgent
from src.operational_agents.deploy_agent import DeployAgent
from src.operational_agents.monitoring_agent import MonitoringAgent

logger = logging.getLogger(__name__)

_DEFAULT_RESOURCES_PATH = (
    Path(__file__).parent.parent.parent / "data" / "seed_resources.json"
)


class SentinelLayerPipeline:
    """End-to-end governance pipeline for SentinelLayer.

    Instantiates all four governance agents, all three operational agents,
    and the decision engine once at startup (each agent loads its data file
    into memory during ``__init__``).  Subsequent calls to ``evaluate()``
    are fast — all data is already in RAM.

    In live mode each governance agent is backed by a Microsoft Agent
    Framework agent that calls GPT-4.1 via ``AsyncAzureOpenAI`` with
    ``AzureCliCredential`` for token-based authentication.

    The four governance agents run in **parallel** using a
    ``ThreadPoolExecutor`` with four workers.  All four ``evaluate()`` calls
    are submitted before any ``.result()`` is awaited, so they execute
    concurrently in separate threads.

    Usage::

        pipeline = SentinelLayerPipeline()
        verdict: GovernanceVerdict = pipeline.evaluate(action)
        print(verdict.decision.value, verdict.sentinel_risk_index.sri_composite)
    """

    def __init__(self) -> None:
        # ── Governance agents (the governors) ──────────────────────────────
        # Each loads its data file (JSON) once here and keeps it in memory.
        # In live mode each is backed by a Microsoft Agent Framework agent.
        self._blast = BlastRadiusAgent()
        self._policy = PolicyComplianceAgent()
        self._historical = HistoricalPatternAgent()
        self._financial = FinancialImpactAgent()
        self._engine = GovernanceDecisionEngine()

        # ── Operational agents (the governed subjects) ─────────────────────
        # These propose actions that the governance agents evaluate.
        self._cost = CostOptimizationAgent()
        self._monitoring = MonitoringAgent()
        self._deploy = DeployAgent()

        # Load the resource graph for policy metadata enrichment.
        # The PolicyComplianceAgent needs resource tags (e.g. purpose=disaster-recovery)
        # to evaluate tag-based policies correctly.  Since those tags live in
        # seed_resources.json, we load them here and pass them per-call.
        self._resources: dict[str, dict] = self._load_resource_graph()

        logger.info(
            "SentinelLayerPipeline initialised — %d resources in topology graph",
            len(self._resources),
        )

    # ------------------------------------------------------------------
    # Public API — Governance
    # ------------------------------------------------------------------

    async def evaluate(self, action: ProposedAction) -> GovernanceVerdict:
        """Run the full governance pipeline for a single proposed action.

        Async-first: safe to call from FastAPI endpoints, MCP tools, and any
        async context.  Uses ``asyncio.gather()`` to run all four governance
        agents concurrently — equivalent parallelism to the former
        ``ThreadPoolExecutor`` pattern but without nested event-loop issues.

        Steps:
        1. Looks up the target resource in the local topology graph to extract
           its tags and environment (needed by the Policy agent).
        2. Concurrently awaits all four governance agents via ``asyncio.gather()``.
        3. Each agent uses the Microsoft Agent Framework in live mode, or
           deterministic rule-based logic in mock mode.
        4. Passes the four agent results to ``GovernanceDecisionEngine``.
        5. Returns the final ``GovernanceVerdict``.

        Args:
            action: A :class:`~src.core.models.ProposedAction` from any
                operational agent (``cost-optimization-agent``,
                ``monitoring-agent``, ``deploy-agent``, etc.).

        Returns:
            :class:`~src.core.models.GovernanceVerdict` containing the
            SRI™ breakdown, decision, and human-readable reason.
        """
        resource = self._find_resource(action.target.resource_id)
        resource_metadata = self._build_policy_metadata(resource)

        logger.info(
            "Pipeline: evaluating '%s' on '%s' (agent=%s)",
            action.action_type.value,
            action.target.resource_id,
            action.agent_id,
        )

        # ------------------------------------------------------------------
        # Concurrent agent evaluation via asyncio.gather()
        # ------------------------------------------------------------------
        # asyncio.gather() runs all four coroutines concurrently inside the
        # same event loop.  No nested asyncio.run() calls are needed — each
        # agent's evaluate() is a native coroutine that simply awaits the
        # framework call (or returns the rule-based result immediately).
        # ------------------------------------------------------------------
        (
            blast_result,
            policy_result,
            historical_result,
            financial_result,
        ) = await asyncio.gather(
            self._blast.evaluate(action),
            self._policy.evaluate(action, resource_metadata),
            self._historical.evaluate(action),
            self._financial.evaluate(action),
        )

        # ------------------------------------------------------------------
        # Composite scoring + verdict
        # ------------------------------------------------------------------
        verdict = self._engine.evaluate(
            action, blast_result, policy_result, historical_result, financial_result
        )

        logger.info(
            "Pipeline: verdict=%s composite=%.1f (infra=%.1f policy=%.1f "
            "hist=%.1f cost=%.1f) agent=%s",
            verdict.decision.value,
            verdict.sentinel_risk_index.sri_composite,
            blast_result.sri_infrastructure,
            policy_result.sri_policy,
            historical_result.sri_historical,
            financial_result.sri_cost,
            action.agent_id,
        )

        return verdict

    # ------------------------------------------------------------------
    # Public API — Operational agent orchestration
    # ------------------------------------------------------------------

    async def scan_operational_agents(self) -> list[ProposedAction]:
        """Run all three operational agents and return their combined proposals.

        This method orchestrates the three operational agents using the
        Microsoft Agent Framework pattern:
        - :class:`~src.operational_agents.cost_agent.CostOptimizationAgent`
        - :class:`~src.operational_agents.monitoring_agent.MonitoringAgent`
        - :class:`~src.operational_agents.deploy_agent.DeployAgent`

        Each agent scans the resource topology for opportunities or anomalies
        and returns :class:`~src.core.models.ProposedAction` objects.  The
        returned proposals are raw — pass each through ``evaluate()`` to get
        a governance verdict before any execution.

        Returns:
            Combined list of proposals from all three operational agents.
        """
        cost_proposals, monitoring_proposals, deploy_proposals = await asyncio.gather(
            self._cost.scan(),
            self._monitoring.scan(),
            self._deploy.scan(),
        )

        proposals: list[ProposedAction] = [
            *cost_proposals,
            *monitoring_proposals,
            *deploy_proposals,
        ]

        logger.info(
            "Pipeline: operational scan — cost=%d monitoring=%d deploy=%d total=%d",
            len(cost_proposals),
            len(monitoring_proposals),
            len(deploy_proposals),
            len(proposals),
        )

        return proposals

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _load_resource_graph(self) -> dict[str, dict]:
        """Load seed_resources.json and index resources by name."""
        with open(_DEFAULT_RESOURCES_PATH, encoding="utf-8") as fh:
            data: dict = json.load(fh)
        return {r["name"]: r for r in data.get("resources", [])}

    def _find_resource(self, resource_id: str) -> dict | None:
        """Look up a resource by name or the last segment of its Azure resource ID.

        Azure resource IDs follow the pattern::

            /subscriptions/{sub}/resourceGroups/{rg}/providers/{type}/{name}

        We first try matching the full string as a resource name (for short
        names like ``"vm-23"``), then fall back to splitting on ``/`` and
        using the final segment (for full Azure IDs).
        """
        if resource_id in self._resources:
            return self._resources[resource_id]
        name = resource_id.split("/")[-1]
        return self._resources.get(name)

    def _build_policy_metadata(self, resource: dict | None) -> dict | None:
        """Build the ``resource_metadata`` dict expected by PolicyComplianceAgent.

        The policy agent uses this to evaluate tag-based policies like
        POL-DR-001 (disaster-recovery protection) and POL-CRIT-001
        (critical resource protection).  Without it, those policies cannot
        fire because the tags are not part of the ProposedAction itself.

        Returns ``None`` when the resource is unknown — the policy agent
        will then infer environment from the resource ID alone.
        """
        if resource is None:
            return None
        tags = resource.get("tags", {})
        return {
            "tags": tags,
            "environment": tags.get("environment"),
        }
