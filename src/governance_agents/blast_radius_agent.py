"""Blast Radius Simulation Agent — SRI:Infrastructure dimension.

Simulates the infrastructure impact of a proposed action by traversing
a resource dependency graph loaded from ``data/seed_resources.json``.

The agent identifies:

* **Affected resources** — direct dependencies and dependents of the target
* **Affected services** — workloads (e.g., Kubernetes pods) running on the target
* **Single points of failure** — resources tagged ``criticality: critical`` in
  the blast radius
* **Availability zones impacted** — Azure regions that would be affected

Microsoft Agent Framework integration (Phase 8)
------------------------------------------------
In live mode (USE_LOCAL_MOCKS=false), this agent is driven by a
Microsoft Agent Framework ``Agent`` backed by Azure OpenAI GPT-4.1.

The LLM agent receives the proposed action and calls our deterministic
``evaluate_blast_radius_rules`` tool, which runs the full rule-based
scoring logic.  The LLM then synthesises an expert narrative reasoning
paragraph from the tool output.

In mock mode (USE_LOCAL_MOCKS=true, or missing endpoint), the framework
is skipped entirely and only the deterministic rule-based path runs.
This preserves fully-offline behaviour for development and CI.

Score semantics (SRI:Infrastructure)
--------------------------------------
* 0–25   — minimal blast radius (auto-approve band)
* 26–60  — moderate blast radius (escalate for human review)
* 61–100 — significant blast radius (deny / require CAB approval)

Score components
-----------------
1. **Action type base score** — destructive actions (DELETE, MODIFY_NSG)
   start with a higher base to reflect inherent irreversibility.
2. **Resource criticality** — ``critical / high / medium / low`` tags from
   Azure resource metadata contribute 30 / 20 / 10 / 5 pts respectively.
3. **Downstream dependents** — resources that rely on the target: +5 per
   item, capped at 25 pts.
4. **Hosted services** — workloads disrupted (e.g., AKS pods): +5 per
   item, capped at 20 pts.
5. **Extra SPOFs in blast radius** — additional critical resources caught in
   the blast radius beyond the target itself: +10 per item.

All component scores accumulate and are capped at 100.
"""

import json
import logging
from pathlib import Path

from src.config import settings as _default_settings
from src.core.models import ActionType, BlastRadiusResult, ProposedAction

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_DEFAULT_RESOURCES_PATH = (
    Path(__file__).parent.parent.parent / "data" / "seed_resources.json"
)

# Base risk contribution by action type.
# Destructive / irreversible actions start higher.
_ACTION_BASE_SCORE: dict[ActionType, float] = {
    ActionType.DELETE_RESOURCE: 40.0,
    ActionType.MODIFY_NSG: 35.0,
    ActionType.RESTART_SERVICE: 20.0,
    ActionType.SCALE_DOWN: 15.0,
    ActionType.UPDATE_CONFIG: 10.0,
    ActionType.SCALE_UP: 5.0,
    ActionType.CREATE_RESOURCE: 3.0,
}

# Criticality tag value → score contribution
_CRITICALITY_SCORE: dict[str, float] = {
    "critical": 30.0,
    "high": 20.0,
    "medium": 10.0,
    "low": 5.0,
}

_DEPENDENT_SCORE_PER_ITEM: float = 5.0
_MAX_DEPENDENT_SCORE: float = 25.0

_SERVICE_SCORE_PER_ITEM: float = 5.0
_MAX_SERVICE_SCORE: float = 20.0

_EXTRA_SPOF_SCORE: float = 10.0

# System instructions for the framework agent (live mode only).
_AGENT_INSTRUCTIONS = """\
You are SentinelLayer's Blast Radius Evaluator — a specialist in cloud
infrastructure dependency analysis and risk assessment.

Your job:
1. Call the `evaluate_blast_radius_rules` tool with the action JSON provided.
2. Receive the deterministic risk score and affected-resource analysis.
3. Write a concise, expert 2-3 sentence narrative that explains the blast radius
   risk in plain English, highlighting the most important SPOFs or downstream
   impacts.  Do NOT restate raw numbers; interpret them.

Always call the tool first before providing any analysis.
"""


# ---------------------------------------------------------------------------
# Agent
# ---------------------------------------------------------------------------


class BlastRadiusAgent:
    """Simulates the infrastructure blast radius of a proposed action.

    Loads a resource dependency graph from a JSON file (mock for Azure
    Resource Graph / Cosmos DB Gremlin), then for any proposed action:

    1. Locates the target resource in the graph.
    2. Traverses dependencies, dependents, governed resources, and
       explicit edge relationships.
    3. Collects hosted/consuming services that would be disrupted.
    4. Detects single points of failure (``criticality: critical`` resources).
    5. Computes an SRI:Infrastructure score (0–100).

    In live mode the Microsoft Agent Framework drives GPT-4.1 to call the
    deterministic tool and synthesise expert reasoning.

    Usage::

        agent = BlastRadiusAgent()
        result: BlastRadiusResult = agent.evaluate(action)
        print(result.sri_infrastructure, result.single_points_of_failure)
    """

    def __init__(
        self,
        resources_path: str | Path | None = None,
        cfg=None,
    ) -> None:
        path = Path(resources_path) if resources_path else _DEFAULT_RESOURCES_PATH
        with open(path, encoding="utf-8") as fh:
            data: dict = json.load(fh)

        # Fast lookup: resource name → resource dict
        self._resources: dict[str, dict] = {
            r["name"]: r for r in data.get("resources", [])
        }
        # Directed dependency edges from the JSON
        self._edges: list[dict] = data.get("dependency_edges", [])

        self._cfg = cfg or _default_settings

        # Is the framework (live LLM) enabled?
        self._use_framework: bool = (
            not self._cfg.use_local_mocks
            and bool(self._cfg.azure_openai_endpoint)
        )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def evaluate(self, action: ProposedAction) -> BlastRadiusResult:
        """Evaluate the blast radius of a proposed infrastructure action.

        Async-first: safe to call from FastAPI, MCP, asyncio.gather(), or any
        async context.  Routes to the Microsoft Agent Framework agent in live
        mode, or to the deterministic rule-based engine in mock mode.

        Args:
            action: The proposed action from an operational agent.

        Returns:
            :class:`~src.core.models.BlastRadiusResult` containing:

            * ``sri_infrastructure`` — 0–100 risk score
            * ``affected_resources`` — resource names caught in the blast radius
            * ``affected_services`` — workloads disrupted by this action
            * ``single_points_of_failure`` — critical resources in blast radius
            * ``availability_zones_impacted`` — Azure regions affected
            * ``reasoning`` — human-readable explanation of the score
        """
        if not self._use_framework:
            return self._evaluate_rules(action)

        try:
            return await self._evaluate_with_framework(action)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "BlastRadiusAgent: framework call failed (%s) — falling back to rules.", exc
            )
            return self._evaluate_rules(action)

    # ------------------------------------------------------------------
    # Microsoft Agent Framework path (live mode)
    # ------------------------------------------------------------------

    async def _evaluate_with_framework(self, action: ProposedAction) -> BlastRadiusResult:
        """Run the framework agent with GPT-4.1 driving the tool call."""
        from openai import AsyncAzureOpenAI
        from azure.identity import DefaultAzureCredential, get_bearer_token_provider
        import agent_framework as af
        from agent_framework.openai import OpenAIResponsesClient

        # ── Credentials: DefaultAzureCredential (az login locally, MI in Azure) ─
        credential = DefaultAzureCredential()
        token_provider = get_bearer_token_provider(
            credential, "https://cognitiveservices.azure.com/.default"
        )
        azure_openai = AsyncAzureOpenAI(
            azure_endpoint=self._cfg.azure_openai_endpoint,
            azure_ad_token_provider=token_provider,
            api_version="2025-03-01-preview",  # Responses API requires >=2025-03-01-preview
        )
        client = OpenAIResponsesClient(
            async_client=azure_openai,
            model_id=self._cfg.azure_openai_deployment,
        )

        # ── Tool: deterministic rule-based evaluation ───────────────────
        result_holder: list[BlastRadiusResult] = []

        @af.tool(
            name="evaluate_blast_radius_rules",
            description=(
                "Run the deterministic blast radius evaluation. "
                "Returns a JSON object with sri_infrastructure score, "
                "affected_resources, affected_services, single_points_of_failure, "
                "availability_zones_impacted, and reasoning."
            ),
        )
        def evaluate_blast_radius_rules(action_json: str) -> str:
            """Evaluate infrastructure blast radius using rule-based scoring."""
            try:
                a = ProposedAction.model_validate_json(action_json)
            except Exception:
                a = action  # fallback to the outer action if JSON parse fails
            r = self._evaluate_rules(a)
            result_holder.append(r)
            return r.model_dump_json()

        # ── Agent: LLM orchestrates tool call + synthesises reasoning ───
        agent = client.as_agent(
            name="blast-radius-evaluator",
            instructions=_AGENT_INSTRUCTIONS,
            tools=[evaluate_blast_radius_rules],
        )

        from src.infrastructure.llm_throttle import run_with_throttle
        response = await run_with_throttle(
            agent.run,
            f"Evaluate the blast radius risk for this proposed action.\n"
            f"Action JSON: {action.model_dump_json()}",
        )

        if result_holder:
            base = result_holder[-1]
            # Enrich the rule-based reasoning with the LLM's synthesis
            enriched_reasoning = (
                base.reasoning
                + "\n\nAgent Framework Analysis (GPT-4.1): "
                + response.text
            )
            return BlastRadiusResult(
                **{**base.model_dump(), "reasoning": enriched_reasoning}
            )

        # Tool was never called — return plain rule-based result
        return self._evaluate_rules(action)

    # ------------------------------------------------------------------
    # Deterministic rule-based evaluation (used in both modes)
    # ------------------------------------------------------------------

    def _evaluate_rules(self, action: ProposedAction) -> BlastRadiusResult:
        """Run the full deterministic blast radius analysis."""
        resource = self._find_resource(action.target.resource_id)
        affected_resources = self._get_affected_resources(resource)
        affected_services = self._get_affected_services(resource)
        spofs = self._detect_spofs(resource, affected_resources)
        zones = self._get_affected_zones(resource, affected_resources)

        score = self._calculate_score(
            action=action,
            resource=resource,
            affected_resources=affected_resources,
            affected_services=affected_services,
            spofs=spofs,
        )

        logger.info(
            "BlastRadiusAgent: resource=%s action=%s score=%.1f spofs=%s",
            action.target.resource_id,
            action.action_type.value,
            score,
            spofs,
        )

        reasoning = self._build_reasoning(action, resource, score, affected_resources, spofs)

        return BlastRadiusResult(
            sri_infrastructure=score,
            affected_resources=affected_resources,
            affected_services=affected_services,
            single_points_of_failure=spofs,
            availability_zones_impacted=zones,
            reasoning=reasoning,
        )

    # ------------------------------------------------------------------
    # Graph traversal helpers
    # ------------------------------------------------------------------

    def _find_resource(self, resource_id: str) -> dict | None:
        """Look up a resource by name or the last segment of its Azure resource ID.

        Azure resource IDs follow the pattern::

            /subscriptions/{sub}/resourceGroups/{rg}/providers/{type}/{name}

        So we first try matching the full string as a resource name, then fall
        back to splitting on ``/`` and using the final segment.
        """
        if resource_id in self._resources:
            return self._resources[resource_id]
        name = resource_id.split("/")[-1]
        return self._resources.get(name)

    def _get_affected_resources(self, resource: dict | None) -> list[str]:
        """Collect all resource names directly linked to the target.

        Traverses four relationship types:

        * ``dependencies`` — resources the target relies on (upstream).
        * ``dependents`` — resources that rely on the target (downstream).
        * ``governs`` — resources controlled by the target (e.g., NSG → subnets).
        * ``dependency_edges`` — explicit directed edges in the graph.

        Returns a deduplicated list that preserves insertion order.
        """
        if resource is None:
            return []

        affected: list[str] = []
        name = resource["name"]

        for dep in resource.get("dependencies", []):
            affected.append(dep)
        for dep in resource.get("dependents", []):
            affected.append(dep)
        for governed in resource.get("governs", []):
            affected.append(governed)

        # Supplement with explicit edge relationships
        for edge in self._edges:
            if edge["from"] == name and edge["to"] not in affected:
                affected.append(edge["to"])
            elif edge["to"] == name and edge["from"] not in affected:
                affected.append(edge["from"])

        # dict.fromkeys preserves order while deduplicating
        return list(dict.fromkeys(affected))

    def _get_affected_services(self, resource: dict | None) -> list[str]:
        """Return workloads hosted on or consuming the target resource.

        Covers:

        * ``services_hosted`` — e.g., Kubernetes workloads on an AKS cluster.
        * ``consumers`` — e.g., services reading from a Storage Account.
        """
        if resource is None:
            return []

        services: list[str] = []
        services.extend(resource.get("services_hosted", []))
        services.extend(resource.get("consumers", []))
        return list(dict.fromkeys(services))

    def _detect_spofs(
        self, resource: dict | None, affected_resources: list[str]
    ) -> list[str]:
        """Identify single points of failure in the blast radius.

        A resource is flagged as an SPOF when its ``criticality`` tag equals
        ``"critical"``.  We check:

        1. The action target itself.
        2. Every resource in the blast radius that exists in our graph.
        """
        spofs: list[str] = []

        if resource and resource.get("tags", {}).get("criticality") == "critical":
            spofs.append(resource["name"])

        for name in affected_resources:
            r = self._resources.get(name)
            if r and r.get("tags", {}).get("criticality") == "critical":
                if name not in spofs:
                    spofs.append(name)

        return spofs

    def _get_affected_zones(
        self, resource: dict | None, affected_resources: list[str]
    ) -> list[str]:
        """Collect unique Azure availability zones impacted by the action."""
        zones: list[str] = []

        if resource:
            loc = resource.get("location")
            if loc:
                zones.append(loc)

        for name in affected_resources:
            r = self._resources.get(name)
            if r:
                loc = r.get("location")
                if loc and loc not in zones:
                    zones.append(loc)

        return zones

    # ------------------------------------------------------------------
    # Scoring
    # ------------------------------------------------------------------

    def _calculate_score(
        self,
        action: ProposedAction,
        resource: dict | None,
        affected_resources: list[str],
        affected_services: list[str],
        spofs: list[str],
    ) -> float:
        """Compute the SRI:Infrastructure score (0–100).

        Formula::

            score = action_base
                  + criticality_contribution
                  + min((dependents + governs) * 5, 25)
                  + min(services * 5, 20)
                  + extra_spof_count * 10
        """
        score = 0.0

        # 1. Base risk contribution from action type
        score += _ACTION_BASE_SCORE.get(action.action_type, 10.0)

        if resource:
            # 2. Criticality of the target resource
            criticality = resource.get("tags", {}).get("criticality", "")
            score += _CRITICALITY_SCORE.get(criticality, 0.0)

            # 3. Downstream dependents + governed resources
            downstream = resource.get("dependents", []) + resource.get("governs", [])
            score += min(
                len(downstream) * _DEPENDENT_SCORE_PER_ITEM, _MAX_DEPENDENT_SCORE
            )

        # 4. Hosted / consuming services disrupted by this action
        score += min(
            len(affected_services) * _SERVICE_SCORE_PER_ITEM, _MAX_SERVICE_SCORE
        )

        # 5. Additional critical resources caught in the blast radius
        target_name = resource["name"] if resource else None
        extra_spofs = [s for s in spofs if s != target_name]
        score += len(extra_spofs) * _EXTRA_SPOF_SCORE

        return round(min(score, 100.0), 2)

    # ------------------------------------------------------------------
    # Reasoning
    # ------------------------------------------------------------------

    def _build_reasoning(
        self,
        action: ProposedAction,
        resource: dict | None,
        score: float,
        affected_resources: list[str],
        spofs: list[str],
    ) -> str:
        """Build a human-readable explanation of the blast radius assessment."""
        if resource is None:
            return (
                f"Target '{action.target.resource_id}' not found in the dependency graph. "
                f"Blast radius cannot be fully simulated. "
                f"Assigned base score "
                f"{_ACTION_BASE_SCORE.get(action.action_type, 10.0):.0f} pts "
                "from action type alone."
            )

        name = resource["name"]
        criticality = resource.get("tags", {}).get("criticality", "unknown")
        base = _ACTION_BASE_SCORE.get(action.action_type, 10.0)
        preview = affected_resources[:3]
        ellipsis = "..." if len(affected_resources) > 3 else ""

        lines = [
            f"Blast radius analysis for '{action.action_type.value}' on '{name}' "
            f"(criticality: {criticality}).",
            f"Action base risk: {base:.0f} pts. "
            f"Affected resources ({len(affected_resources)}): "
            f"{', '.join(preview)}{ellipsis}.",
        ]

        if spofs:
            lines.append(
                f"Single points of failure in blast radius: {', '.join(spofs)}."
            )

        lines.append(f"SRI:Infrastructure score: {score:.1f}/100.")
        return "\n".join(lines)
