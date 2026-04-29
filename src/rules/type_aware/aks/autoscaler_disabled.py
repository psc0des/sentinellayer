"""TYPE-AKS-001 — AKS node pools with cluster autoscaler disabled.

Node pools without autoscaler cannot scale to absorb traffic spikes and
cannot shrink during off-peak to save costs. Production clusters should
have autoscaler enabled on all node pools.
"""
from src.rules.base import Category, Finding, Severity, rule
from src.rules.inventory_index import InventoryIndex


@rule(
    id="TYPE-AKS-001",
    name="AKS Node Pool Autoscaler Disabled",
    category=Category.RELIABILITY,
    severity=Severity.MEDIUM,
    applies_to=["microsoft.containerservice/managedclusters"],
)
def evaluate(resource: dict, idx: InventoryIndex) -> Finding | None:
    props = resource.get("properties") or {}
    agent_pools = props.get("agentPoolProfiles") or []
    disabled_pools = [
        p.get("name", "?")
        for p in agent_pools
        if not p.get("enableAutoScaling")
    ]
    if not disabled_pools:
        return None
    name = resource.get("name", "")
    return Finding(
        rule_id="TYPE-AKS-001",
        rule_name="AKS Node Pool Autoscaler Disabled",
        category=Category.RELIABILITY,
        severity=Severity.MEDIUM,
        resource_id=resource["id"],
        resource_type=resource["type"],
        resource_name=name,
        reason=(
            f"AKS cluster '{name}' has autoscaler disabled on node pool(s): "
            f"{', '.join(disabled_pools)}. These pools cannot scale to handle traffic spikes "
            "or shrink during off-peak periods. Enable cluster autoscaler with appropriate "
            "minCount/maxCount values."
        ),
        recommended_action="update_config",
        evidence={"disabled_pools": disabled_pools},
    )
