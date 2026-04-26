"""RuriSkry end-to-end governance pipeline.

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
evaluation.  Callers (e.g., examples/demo.py) then pass each proposal through
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

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncGenerator
from pathlib import Path
from typing import TYPE_CHECKING, Any

from src.config import settings
from src.core.governance_engine import GovernanceDecisionEngine
from src.core.models import GovernanceVerdict, ProposedAction

if TYPE_CHECKING:
    from agent_framework import CheckpointStorage, Workflow
from src.core.risk_triage import build_org_context, classify_tier, compute_fingerprint
from src.governance_agents.blast_radius_agent import BlastRadiusAgent
from src.governance_agents.financial_agent import FinancialImpactAgent
from src.governance_agents.historical_agent import HistoricalPatternAgent
from src.governance_agents.policy_agent import PolicyComplianceAgent
from src.notifications.slack_notifier import send_verdict_notification
from src.operational_agents.cost_agent import CostOptimizationAgent
from src.operational_agents.deploy_agent import DeployAgent
from src.operational_agents.monitoring_agent import MonitoringAgent

logger = logging.getLogger(__name__)

_DEFAULT_RESOURCES_PATH = (
    Path(__file__).parent.parent.parent / "data" / "seed_resources.json"
)


class RuriSkryPipeline:
    """End-to-end governance pipeline for RuriSkry.

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

        pipeline = RuriSkryPipeline()
        verdict: GovernanceVerdict = pipeline.evaluate(action)
        print(verdict.decision.value, verdict.skry_risk_index.sri_composite)
    """

    def __init__(self, inventory: list[dict] | None = None) -> None:
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
        # The PolicyComplianceAgent needs resource tags (e.g. disaster-recovery=true)
        # to evaluate tag-based policies correctly.
        self._resources: dict[str, dict] = self._load_resource_graph()

        # ── Inventory merge (live Azure tags) ─────────────────────────────
        # When the dashboard runs a live scan it passes the Cosmos inventory
        # snapshot here so _find_resource() can locate REAL Azure resources
        # by ARM ID. Without this, _find_resource only sees the static seed
        # topology and tag-based policies (POL-DR-001, POL-CRIT-001,
        # POL-PROD-001) silently fail to fire on live resources because
        # tags={} reaches the policy agent.
        seed_count = len(self._resources)
        inventory_count = len(inventory) if inventory else 0
        if inventory:
            for r in inventory:
                arm_id = r.get("id", "")
                name = r.get("name") or (arm_id.split("/")[-1] if arm_id else "")
                if arm_id:
                    self._resources[arm_id] = r
                    self._resources[arm_id.lower()] = r
                if name:
                    self._resources.setdefault(name, r)

        # ── Org context (Phase 26 — Risk Triage) ──────────────────────────
        # Built once from config and reused for every evaluate() call.
        # Provides compliance frameworks, risk tolerance, and critical RG list
        # to the triage fingerprint engine.
        self._org_context = build_org_context()

        # ── Agent Framework Workflow (Phase 33) ───────────────────────────
        # Built once, reused for all evaluate() calls when USE_WORKFLOWS=true.
        # Executors share the same agent instances as the legacy path — no
        # duplication of model state.
        self._workflow: Workflow | None = self._build_governance_workflow()

        logger.info(
            "RuriSkryPipeline initialised — %d seed + %d inventory resources | "
            "org=%s frameworks=%s tolerance=%s",
            seed_count,
            inventory_count,
            self._org_context.org_name,
            self._org_context.compliance_frameworks or "none",
            self._org_context.risk_tolerance,
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

        # ── Risk Triage (Phases 26 + 27A) ─────────────────────────────────
        # Classify the action before running any governance agent (<1 ms).
        # Phase 26: fingerprint derived, tier computed, stamped on verdict.
        # Phase 27A: Tier 1 short-circuits LLM — force_deterministic=True
        #            skips the Agent Framework in all 4 governance agents.
        # Phase 27B (future): Tier 2 single consolidated LLM call;
        #            Decision Memory — precedent matching for repeat actions.
        fingerprint = compute_fingerprint(action, resource_metadata, self._org_context)
        triage_tier = classify_tier(fingerprint)

        force_deterministic = (triage_tier == 1)

        if force_deterministic:
            logger.info(
                "Pipeline: Tier 1 short-circuit — skipping LLM for '%s' on '%s' "
                "(env=%s, blast_radius=%s)",
                action.action_type.value,
                action.target.resource_id,
                fingerprint.environment,
                fingerprint.estimated_blast_radius,
            )

        logger.info(
            "Pipeline: evaluating '%s' on '%s' (agent=%s, tier=%d, "
            "sequential_llm=%s)",
            action.action_type.value,
            action.target.resource_id,
            action.agent_id,
            triage_tier,
            settings.sequential_llm,
        )

        if settings.use_workflows:
            # ------------------------------------------------------------------
            # Workflow path (USE_WORKFLOWS=true, Phase 33)
            # ------------------------------------------------------------------
            # Delegates to the WorkflowBuilder graph.  Behavior is identical to
            # the legacy gather path; the workflow handles fan-out and fan-in.
            # ScoringExecutor stamps triage_tier/triage_mode before yielding.
            # ------------------------------------------------------------------
            verdict = await self._evaluate_via_workflow(
                action, resource_metadata, triage_tier, force_deterministic
            )
            logger.info(
                "Pipeline: verdict=%s composite=%.1f tier=%d agent=%s (workflow)",
                verdict.decision.value,
                verdict.skry_risk_index.sri_composite,
                triage_tier,
                action.agent_id,
            )
        else:
            # ------------------------------------------------------------------
            # Legacy path — sequential or parallel asyncio.gather() [DEPRECATED]
            # This path is deprecated as of Phase 33D. Set USE_WORKFLOWS=true
            # (now the default) to use the WorkflowBuilder graph instead.
            # The legacy path will be removed in a future release.
            # ------------------------------------------------------------------
            logger.warning(
                "Pipeline: running via deprecated legacy asyncio.gather() path "
                "(USE_WORKFLOWS=false). Switch to the workflow path — it is now the "
                "default. The legacy path will be removed in a future release."
            )
            if settings.sequential_llm:
                blast_result = await self._blast.evaluate(action, force_deterministic=force_deterministic)
                policy_result = await self._policy.evaluate(action, resource_metadata, force_deterministic=force_deterministic)
                historical_result = await self._historical.evaluate(action, force_deterministic=force_deterministic)
                financial_result = await self._financial.evaluate(action, force_deterministic=force_deterministic)
            else:
                (
                    blast_result,
                    policy_result,
                    historical_result,
                    financial_result,
                ) = await asyncio.gather(
                    self._blast.evaluate(action, force_deterministic=force_deterministic),
                    self._policy.evaluate(action, resource_metadata, force_deterministic=force_deterministic),
                    self._historical.evaluate(action, force_deterministic=force_deterministic),
                    self._financial.evaluate(action, force_deterministic=force_deterministic),
                )

            verdict = self._engine.evaluate(
                action, blast_result, policy_result, historical_result, financial_result
            )
            verdict.triage_tier = triage_tier
            verdict.triage_mode = "deterministic" if force_deterministic else "full"

            logger.info(
                "Pipeline: verdict=%s composite=%.1f tier=%d (infra=%.1f policy=%.1f "
                "hist=%.1f cost=%.1f) agent=%s",
                verdict.decision.value,
                verdict.skry_risk_index.sri_composite,
                triage_tier,
                blast_result.sri_infrastructure,
                policy_result.sri_policy,
                historical_result.sri_historical,
                financial_result.sri_cost,
                action.agent_id,
            )

        # ------------------------------------------------------------------
        # Fire-and-forget Slack notification (Phase 32A)
        # ------------------------------------------------------------------
        # For DENIED or ESCALATED verdicts, send a Slack Block Kit message
        # asynchronously.  The task runs in the background — the pipeline
        # returns the verdict immediately.  Errors are caught inside
        # send_verdict_notification() so they never affect governance.
        # ------------------------------------------------------------------
        try:
            asyncio.create_task(send_verdict_notification(verdict, action))
        except Exception:
            logger.debug("Slack notification task could not be created.", exc_info=True)

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

    def _build_governance_workflow(self) -> "Workflow":
        """Build (once) the WorkflowBuilder governance graph wired to existing agents."""
        from src.core.workflows.governance_workflow import build_governance_workflow
        return build_governance_workflow(
            blast=self._blast,
            policy=self._policy,
            historical=self._historical,
            financial=self._financial,
            engine=self._engine,
        )

    async def _evaluate_via_workflow(
        self,
        action: ProposedAction,
        resource_metadata: dict | None,
        triage_tier: int,
        force_deterministic: bool,
        checkpoint_storage: CheckpointStorage | None = None,
    ) -> GovernanceVerdict:
        """Run the governance workflow and return the single yielded verdict."""
        from src.core.workflows.messages import GovernanceInput
        inp = GovernanceInput(
            action=action,
            resource_metadata=resource_metadata,
            force_deterministic=force_deterministic,
            triage_tier=triage_tier,
        )
        result = await self._workflow.run(inp, checkpoint_storage=checkpoint_storage)
        outputs = result.get_outputs()
        return outputs[0]

    async def evaluate_streaming(
        self,
        action: ProposedAction,
        checkpoint_id: str | None = None,
        checkpoint_storage: CheckpointStorage | None = None,
    ) -> AsyncGenerator[tuple[str, dict[str, Any]] | GovernanceVerdict, None]:
        """Streaming variant of evaluate() for use when USE_WORKFLOWS=true.

        Yields ``(event_type, kwargs)`` tuples for per-agent SSE events, then
        finally yields the ``GovernanceVerdict`` as the last item.

        Callers distinguish the two kinds of yields with ``isinstance``:

            async for item in pipeline.evaluate_streaming(action):
                if isinstance(item, GovernanceVerdict):
                    verdict = item
                else:
                    event_type, kwargs = item
                    await emit(event_type, **kwargs)

        The verdict already has ``triage_tier`` and ``triage_mode`` stamped
        by ``ScoringExecutor``.  Slack notifications fire inside this method.
        """
        from src.core.workflows.governance_workflow import stream_governance_evaluation
        from src.core.workflows.messages import GovernanceInput

        resource = self._find_resource(action.target.resource_id)
        resource_metadata = self._build_policy_metadata(resource)
        fingerprint = compute_fingerprint(action, resource_metadata, self._org_context)
        triage_tier = classify_tier(fingerprint)
        force_deterministic = (triage_tier == 1)

        resource_name = action.target.resource_id.split("/")[-1]

        if checkpoint_id is not None:
            # Resume from checkpoint — no GovernanceInput needed
            inp = None
        else:
            inp = GovernanceInput(
                action=action,
                resource_metadata=resource_metadata,
                force_deterministic=force_deterministic,
                triage_tier=triage_tier,
            )

        verdict: GovernanceVerdict | None = None
        async for item in stream_governance_evaluation(
            self._workflow,
            inp,
            resource_name=resource_name,
            action_type=action.action_type.value,
            checkpoint_id=checkpoint_id,
            checkpoint_storage=checkpoint_storage,
        ):
            if isinstance(item, GovernanceVerdict):
                verdict = item
            else:
                yield item  # (event_type, kwargs) for SSE

        if verdict is None:
            raise RuntimeError("Governance workflow streaming ended without a verdict")

        logger.info(
            "Pipeline (streaming): verdict=%s composite=%.1f tier=%d agent=%s",
            verdict.decision.value,
            verdict.skry_risk_index.sri_composite,
            triage_tier,
            action.agent_id,
        )

        try:
            asyncio.create_task(send_verdict_notification(verdict, action))
        except Exception:
            logger.debug("Slack notification task could not be created.", exc_info=True)

        yield verdict  # Final yield — always the last item

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
        # Live inventory ARM IDs may differ in case (Microsoft.Compute vs microsoft.compute)
        rid_lower = resource_id.lower()
        if rid_lower in self._resources:
            return self._resources[rid_lower]
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
        tags = resource.get("tags") or {}  # `or {}` handles "tags": null from Azure API
        return {
            "tags": tags,
            "environment": tags.get("environment"),
        }
