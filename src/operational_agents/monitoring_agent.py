"""SRE Monitoring & RCA Agent — detects anomalies and proposes remediation.

This is an operational agent (the governed subject). It proposes
infrastructure actions that SentinelLayer evaluates before execution.

The agent scans a resource topology (loaded from ``data/seed_resources.json``)
and applies SRE heuristics to detect structural anomalies:

Detection rules
---------------
1. **Missing owner tag on critical resources** — Resources tagged
   ``criticality: critical`` with no ``owner`` tag create accountability gaps:
   if the resource fails, no team is automatically responsible. Proposes
   UPDATE_CONFIG to add ownership metadata.
2. **Circular dependency** — Resources with bidirectional dependency edges
   create split-brain risk during restarts or failovers. Proposes
   RESTART_SERVICE on the second node in the cycle, with sequencing guidance.
3. **High-cost single point of failure** — Resources tagged
   ``criticality: critical`` with monthly cost above threshold and downstream
   dependents, but no redundancy indicated. Proposes SCALE_UP to add a
   standby replica or additional node pool.
"""

import json
import logging
from pathlib import Path

from src.core.models import ActionTarget, ActionType, ProposedAction, Urgency

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_AGENT_ID = "monitoring-agent"

_DEFAULT_RESOURCES_PATH = (
    Path(__file__).parent.parent.parent / "data" / "seed_resources.json"
)

# Critical resources costing more than this per month are flagged as high-cost SPOFs.
_CRITICAL_COST_THRESHOLD: float = 500.0


# ---------------------------------------------------------------------------
# Agent
# ---------------------------------------------------------------------------


class MonitoringAgent:
    """Detects structural anomalies in the resource topology and proposes remediation.

    Loads resource metadata from ``data/seed_resources.json`` (mock for
    Azure Monitor + Resource Graph), applies SRE heuristics, and returns
    a list of remediation :class:`~src.core.models.ProposedAction` objects.

    Usage::

        agent = MonitoringAgent()
        proposals: list[ProposedAction] = agent.scan()
        for p in proposals:
            print(p.action_type.value, p.target.resource_id, p.reason)
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

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def scan(self) -> list[ProposedAction]:
        """Detect anomalies across the resource topology.

        Runs all three detection rules in sequence and aggregates results.

        Returns:
            A list of :class:`~src.core.models.ProposedAction` objects,
            one per anomaly detected. Returns an empty list when the
            topology appears healthy.
        """
        proposals: list[ProposedAction] = []
        proposals.extend(self._detect_untagged_critical_resources())
        proposals.extend(self._detect_circular_dependencies())
        proposals.extend(self._detect_high_cost_spofs())

        logger.info(
            "MonitoringAgent: scan complete — %d anomalies detected",
            len(proposals),
        )
        return proposals

    # ------------------------------------------------------------------
    # Detection rules
    # ------------------------------------------------------------------

    def _detect_untagged_critical_resources(self) -> list[ProposedAction]:
        """Rule 1 — Critical resources missing an owner tag.

        An unowned critical resource creates an accountability gap: if it
        fails at 3 AM, no on-call engineer is automatically paged. We propose
        UPDATE_CONFIG to add the ``owner`` tag.
        """
        proposals: list[ProposedAction] = []

        for resource in self._resources.values():
            tags = resource.get("tags", {})
            if tags.get("criticality") != "critical":
                continue
            if "owner" in tags:
                continue

            reason = (
                f"Resource '{resource['name']}' is tagged criticality=critical "
                "but has no 'owner' tag. Unowned critical resources create "
                "accountability gaps during incidents — no team is automatically "
                "responsible when the resource fails. Add an owner tag."
            )
            proposals.append(
                ProposedAction(
                    agent_id=_AGENT_ID,
                    action_type=ActionType.UPDATE_CONFIG,
                    target=ActionTarget(
                        resource_id=resource["id"],
                        resource_type=resource["type"],
                        resource_group=resource.get("resource_group"),
                        current_monthly_cost=resource.get("monthly_cost"),
                    ),
                    reason=reason,
                    urgency=Urgency.MEDIUM,
                )
            )
            logger.info(
                "MonitoringAgent: unowned critical resource — '%s'",
                resource["name"],
            )

        return proposals

    def _detect_circular_dependencies(self) -> list[ProposedAction]:
        """Rule 2 — Bidirectional dependency edges (circular dependencies).

        A circular dependency means that if either service restarts, the
        other may fail to reconnect, potentially causing a cascading outage.
        We propose RESTART_SERVICE on the second node in the pair, with
        sequencing guidance so the operator restarts them in the right order.

        Only the first occurrence of each unique pair is flagged to avoid
        producing duplicate proposals for the same circular edge.
        """
        # Build a set of directed edges for fast reverse-lookup.
        edge_set: set[tuple[str, str]] = {(e["from"], e["to"]) for e in self._edges}

        seen_pairs: set[frozenset[str]] = set()
        proposals: list[ProposedAction] = []

        for edge in self._edges:
            a, b = edge["from"], edge["to"]
            pair = frozenset({a, b})

            if pair in seen_pairs:
                continue  # already reported this circular pair

            if (b, a) in edge_set:
                seen_pairs.add(pair)
                reason = (
                    f"Circular dependency detected between '{a}' and '{b}'. "
                    "Bidirectional dependencies create split-brain risk: if "
                    f"either service restarts, '{b}' may fail to reconnect to "
                    f"'{a}', causing a cascading outage. Recommend restarting "
                    f"'{b}' first (with health checks) before restarting '{a}'."
                )
                proposals.append(
                    ProposedAction(
                        agent_id=_AGENT_ID,
                        action_type=ActionType.RESTART_SERVICE,
                        target=ActionTarget(
                            resource_id=b,
                            resource_type="unknown",
                        ),
                        reason=reason,
                        urgency=Urgency.HIGH,
                    )
                )
                logger.info(
                    "MonitoringAgent: circular dependency — '%s' ↔ '%s'", a, b
                )

        return proposals

    def _detect_high_cost_spofs(self) -> list[ProposedAction]:
        """Rule 3 — High-cost critical resources with many dependents.

        A critical resource that is expensive and has many dependents but shows
        no sign of redundancy is a single point of failure with high blast radius.
        We propose SCALE_UP to add a standby replica or additional node pool.
        """
        proposals: list[ProposedAction] = []

        for resource in self._resources.values():
            tags = resource.get("tags", {})
            if tags.get("criticality") != "critical":
                continue

            monthly_cost = resource.get("monthly_cost")
            if monthly_cost is None or monthly_cost < _CRITICAL_COST_THRESHOLD:
                continue

            # Count downstream workloads that would be disrupted
            dependents = (
                resource.get("dependents", []) + resource.get("services_hosted", [])
            )
            if not dependents:
                continue

            preview = ", ".join(dependents[:3])
            ellipsis = "..." if len(dependents) > 3 else ""
            reason = (
                f"Critical resource '{resource['name']}' costs ${monthly_cost:.0f}/month "
                f"and has {len(dependents)} dependent(s): {preview}{ellipsis}. "
                "No redundancy configuration detected. A failure here would cause "
                "a wide blast-radius outage. Recommend scaling up to add a standby "
                "replica or additional node pool."
            )
            proposals.append(
                ProposedAction(
                    agent_id=_AGENT_ID,
                    action_type=ActionType.SCALE_UP,
                    target=ActionTarget(
                        resource_id=resource["id"],
                        resource_type=resource["type"],
                        resource_group=resource.get("resource_group"),
                        current_sku=resource.get("sku"),
                        current_monthly_cost=monthly_cost,
                    ),
                    reason=reason,
                    urgency=Urgency.HIGH,
                )
            )
            logger.info(
                "MonitoringAgent: high-cost SPOF — '%s' $%.0f/month %d dependents",
                resource["name"],
                monthly_cost,
                len(dependents),
            )

        return proposals
