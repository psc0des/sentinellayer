"""UNIV-REL-002 — Container Apps with a single replica have no redundancy.

A Container App configured with maxReplicas=1 (or minReplicas=maxReplicas=1) cannot
handle pod restarts without downtime. Production workloads need at least 2 replicas.
"""
from src.rules.base import Category, Finding, Severity, rule
from src.rules.inventory_index import InventoryIndex


@rule(
    id="UNIV-REL-002",
    name="Container App Single-Replica Configuration",
    category=Category.RELIABILITY,
    severity=Severity.MEDIUM,
    applies_to=["microsoft.app/containerapps"],
)
def evaluate(resource: dict, idx: InventoryIndex) -> Finding | None:
    props = resource.get("properties") or {}
    template = props.get("template") or {}
    scale = template.get("scale") or {}
    max_replicas = scale.get("maxReplicas", 10)  # default 10 if not set
    min_replicas = scale.get("minReplicas", 0)
    if max_replicas > 1:
        return None
    name = resource.get("name", "")
    return Finding(
        rule_id="UNIV-REL-002",
        rule_name="Container App Single-Replica Configuration",
        category=Category.RELIABILITY,
        severity=Severity.MEDIUM,
        resource_id=resource["id"],
        resource_type=resource["type"],
        resource_name=name,
        reason=(
            f"Container App '{name}' has maxReplicas={max_replicas}. A single replica means "
            "any pod restart or rolling update causes downtime. Set maxReplicas >= 2 for "
            "production workloads."
        ),
        recommended_action="update_config",
        evidence={"maxReplicas": max_replicas, "minReplicas": min_replicas},
    )
