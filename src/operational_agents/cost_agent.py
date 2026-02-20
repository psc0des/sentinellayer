"""Cost Optimization Agent — identifies wasteful resources and proposes savings.

This is an operational agent (the governed subject). It proposes
infrastructure actions that SentinelLayer evaluates before execution.

The agent scans a resource topology (loaded from ``data/seed_resources.json``)
and applies heuristics to identify overprovisioned or idle resources:

Detection rules
---------------
1. **Oversized VM SKU** — VMs running on D8s_v3 or larger are candidates for
   downsizing to the next smaller SKU tier, saving ~45 % of monthly cost.
2. **Large AKS cluster** — Kubernetes clusters with ≥ 4 nodes are candidates
   for a node-count reduction, saving ~35 % of monthly cost.

Resources with monthly cost below ``_MIN_COST_THRESHOLD`` ($200) are skipped
because the operational overhead of changing them exceeds the benefit.

Resources above ``_HIGH_COST_THRESHOLD`` ($500) get MEDIUM urgency so they
rise to the top of the human review queue.
"""

import json
import logging
from pathlib import Path

from src.core.models import ActionTarget, ActionType, ProposedAction, Urgency

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_AGENT_ID = "cost-optimization-agent"

_DEFAULT_RESOURCES_PATH = (
    Path(__file__).parent.parent.parent / "data" / "seed_resources.json"
)

# Only worth analysing resources that cost more than this per month.
_MIN_COST_THRESHOLD: float = 200.0

# Resources costing more than this receive MEDIUM urgency.
_HIGH_COST_THRESHOLD: float = 500.0

# Estimated monthly savings when downsizing a VM (e.g. D8s → D4s).
# In practice the ratio is ~48 %; we use 45 % as a conservative estimate.
_VM_DOWNSIZE_SAVINGS_RATE: float = 0.45

# Propose AKS scale-down only when node count is at or above this value.
_AKS_SCALE_DOWN_NODE_THRESHOLD: int = 4

# Estimated monthly savings when reducing AKS node count by 2.
_AKS_SCALE_DOWN_SAVINGS_RATE: float = 0.35

# SKUs considered oversized for general-purpose workloads.
_OVERSIZED_SKUS: set[str] = {
    "Standard_D8s_v3",
    "Standard_D16s_v3",
    "Standard_D32s_v3",
}

# Suggested replacement SKU for each oversized tier.
_DOWNSIZE_MAP: dict[str, str] = {
    "Standard_D8s_v3": "Standard_D4s_v3",
    "Standard_D16s_v3": "Standard_D8s_v3",
    "Standard_D32s_v3": "Standard_D16s_v3",
}


# ---------------------------------------------------------------------------
# Agent
# ---------------------------------------------------------------------------


class CostOptimizationAgent:
    """Scans a resource topology and proposes cost-saving actions.

    Loads resource metadata from ``data/seed_resources.json`` (mock for
    Azure Cost Management + Resource Graph), then applies heuristic rules
    to identify overprovisioned or idle resources.

    Usage::

        agent = CostOptimizationAgent()
        proposals: list[ProposedAction] = agent.scan()
        for p in proposals:
            print(p.action_type.value, p.target.resource_id, p.projected_savings_monthly)
    """

    def __init__(self, resources_path: str | Path | None = None) -> None:
        path = Path(resources_path) if resources_path else _DEFAULT_RESOURCES_PATH
        with open(path, encoding="utf-8") as fh:
            data: dict = json.load(fh)

        self._resources: list[dict] = data.get("resources", [])

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def scan(self) -> list[ProposedAction]:
        """Scan all resources and return cost-optimisation proposals.

        Returns:
            A list of :class:`~src.core.models.ProposedAction` objects,
            one per resource that triggered a detection rule.
            Returns an empty list if no optimisation opportunities are found.
        """
        proposals: list[ProposedAction] = []
        for resource in self._resources:
            proposal = self._analyze_resource(resource)
            if proposal is not None:
                proposals.append(proposal)
                logger.info(
                    "CostOptimizationAgent: flagged '%s' (%s) — savings $%.0f/month",
                    resource["name"],
                    proposal.action_type.value,
                    proposal.projected_savings_monthly or 0,
                )
        return proposals

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _analyze_resource(self, resource: dict) -> ProposedAction | None:
        """Apply detection rules to a single resource.

        Returns a :class:`~src.core.models.ProposedAction` if the resource
        triggers a rule, or ``None`` if no optimisation is warranted.
        """
        monthly_cost: float | None = resource.get("monthly_cost")

        # Skip free or cheap resources — not worth the operational overhead.
        if monthly_cost is None or monthly_cost < _MIN_COST_THRESHOLD:
            return None

        resource_type: str = resource.get("type", "")

        # Rule 1 — Oversized VM SKU
        if "virtualMachines" in resource_type:
            return self._propose_vm_scale_down(resource, monthly_cost)

        # Rule 2 — Large AKS cluster
        if "managedClusters" in resource_type:
            return self._propose_aks_scale_down(resource, monthly_cost)

        return None

    def _propose_vm_scale_down(
        self, resource: dict, monthly_cost: float
    ) -> ProposedAction | None:
        """Propose a scale-down for an oversized VM.

        Only triggers when the current SKU is in ``_OVERSIZED_SKUS``.
        Returns ``None`` if the VM is already on a smaller SKU tier.
        """
        sku: str = resource.get("sku", "")
        if sku not in _OVERSIZED_SKUS:
            return None

        proposed_sku = _DOWNSIZE_MAP[sku]
        savings = round(monthly_cost * _VM_DOWNSIZE_SAVINGS_RATE, 2)
        tags = resource.get("tags", {})
        is_idle = tags.get("purpose") == "disaster-recovery"

        reason = (
            f"VM '{resource['name']}' is running SKU {sku} at ${monthly_cost:.0f}/month. "
        )
        if is_idle:
            reason += (
                "Tagged as disaster-recovery — expected to be idle most of the time. "
            )
        reason += (
            f"Downsizing to {proposed_sku} is estimated to save ${savings:.0f}/month."
        )

        urgency = Urgency.MEDIUM if monthly_cost >= _HIGH_COST_THRESHOLD else Urgency.LOW

        return ProposedAction(
            agent_id=_AGENT_ID,
            action_type=ActionType.SCALE_DOWN,
            target=ActionTarget(
                resource_id=resource["id"],
                resource_type=resource["type"],
                resource_group=resource.get("resource_group"),
                current_sku=sku,
                proposed_sku=proposed_sku,
                current_monthly_cost=monthly_cost,
            ),
            reason=reason,
            urgency=urgency,
            projected_savings_monthly=savings,
        )

    def _propose_aks_scale_down(
        self, resource: dict, monthly_cost: float
    ) -> ProposedAction | None:
        """Propose a node-count reduction for an over-provisioned AKS cluster.

        Only triggers when node count is at or above ``_AKS_SCALE_DOWN_NODE_THRESHOLD``.
        """
        node_count: int = resource.get("node_count", 0)
        if node_count < _AKS_SCALE_DOWN_NODE_THRESHOLD:
            return None

        proposed_nodes = node_count - 2
        savings = round(monthly_cost * _AKS_SCALE_DOWN_SAVINGS_RATE, 2)

        reason = (
            f"AKS cluster '{resource['name']}' is running {node_count} nodes "
            f"at ${monthly_cost:.0f}/month. Reducing to {proposed_nodes} nodes "
            f"is estimated to save ${savings:.0f}/month."
        )

        return ProposedAction(
            agent_id=_AGENT_ID,
            action_type=ActionType.SCALE_DOWN,
            target=ActionTarget(
                resource_id=resource["id"],
                resource_type=resource["type"],
                resource_group=resource.get("resource_group"),
                current_sku=f"{node_count} nodes",
                proposed_sku=f"{proposed_nodes} nodes",
                current_monthly_cost=monthly_cost,
            ),
            reason=reason,
            urgency=Urgency.MEDIUM,
            projected_savings_monthly=savings,
        )
