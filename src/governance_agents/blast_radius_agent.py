"""Blast Radius Simulation Agent — SRI:Infrastructure dimension.

Simulates the infrastructure impact of a proposed action by traversing
a resource dependency graph loaded from ``data/seed_resources.json``.

The agent identifies:

* **Affected resources** — direct dependencies and dependents of the target
* **Affected services** — workloads (e.g., Kubernetes pods) running on the target
* **Single points of failure** — resources tagged ``criticality: critical`` in
  the blast radius
* **Availability zones impacted** — Azure regions that would be affected

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

from src.core.models import ActionType, BlastRadiusResult, ProposedAction
from src.infrastructure.openai_client import AzureOpenAIClient

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

    Usage::

        agent = BlastRadiusAgent()
        result: BlastRadiusResult = agent.evaluate(action)
        print(result.sri_infrastructure, result.single_points_of_failure)
    """

    def __init__(self, resources_path: str | Path | None = None) -> None:
        path = Path(resources_path) if resources_path else _DEFAULT_RESOURCES_PATH
        with open(path, encoding="utf-8") as fh:
            data: dict = json.load(fh)

        # Fast lookup: resource name → resource dict
        self._resources: dict[str, dict] = {
            r["name"]: r for r in data.get("resources", [])
        }
        # Directed dependency edges from the JSON
        self._edges: list[dict] = data.get("dependency_edges", [])

        # LLM client — augments rule-based reasoning with GPT-4.1 analysis
        # when USE_LOCAL_MOCKS=false and Foundry credentials are available.
        self._llm = AzureOpenAIClient()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def evaluate(self, action: ProposedAction) -> BlastRadiusResult:
        """Evaluate the blast radius of a proposed infrastructure action.

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

        # Augment rule-based reasoning with GPT-4.1 analysis in live mode
        if not self._llm.is_mock:
            criticality = (
                resource.get("tags", {}).get("criticality", "unknown")
                if resource else "unknown"
            )
            llm_text = self._llm.analyze(
                action_context=(
                    f"Action: {action.action_type.value} on '{action.target.resource_id}'\n"
                    f"Resource criticality: {criticality}\n"
                    f"Affected resources ({len(affected_resources)}): "
                    f"{', '.join(affected_resources[:5])}\n"
                    f"Single points of failure: {', '.join(spofs) if spofs else 'none'}\n"
                    f"SRI:Infrastructure score: {score:.1f}/100\n"
                    f"Agent reason: {action.reason}"
                ),
                agent_role="blast radius and infrastructure dependency risk assessor",
            )
            reasoning = reasoning + "\n\nGPT-4.1 Analysis: " + llm_text

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
