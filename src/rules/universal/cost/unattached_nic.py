"""UNIV-COST-004 — Flag network interfaces not attached to any virtual machine.

A NIC that was detached from a deleted VM still holds IP configuration, NSG
associations, and private IP reservations. It should be cleaned up.
"""
from src.rules.base import Category, Finding, Severity, rule
from src.rules.inventory_index import InventoryIndex


@rule(
    id="UNIV-COST-004",
    name="Unattached Network Interface",
    category=Category.COST,
    severity=Severity.LOW,
    applies_to=["microsoft.network/networkinterfaces"],
)
def evaluate(resource: dict, idx: InventoryIndex) -> Finding | None:
    props = resource.get("properties") or {}
    if props.get("virtualMachine"):
        return None
    name = resource.get("name", "")
    return Finding(
        rule_id="UNIV-COST-004",
        rule_name="Unattached Network Interface",
        category=Category.COST,
        severity=Severity.LOW,
        resource_id=resource["id"],
        resource_type=resource["type"],
        resource_name=name,
        reason=(
            f"Network interface '{name}' is not attached to any virtual machine. "
            "Orphaned NICs consume IP configuration and potentially NSG rules. Delete it."
        ),
        recommended_action="delete_resource",
        evidence={"virtualMachine": None},
    )
