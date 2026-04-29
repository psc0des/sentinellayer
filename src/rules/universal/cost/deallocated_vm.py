"""UNIV-COST-001 — Flag VMs in the deallocated power state.

A deallocated VM still incurs OS disk + reserved IP costs while delivering no
compute value. Either restart it (if needed) or delete it.
"""
from src.rules.base import Category, Finding, Severity, rule
from src.rules.inventory_index import InventoryIndex


@rule(
    id="UNIV-COST-001",
    name="Deallocated VM",
    category=Category.COST,
    severity=Severity.MEDIUM,
    applies_to=["microsoft.compute/virtualmachines"],
)
def evaluate(resource: dict, idx: InventoryIndex) -> Finding | None:
    power_state = (resource.get("powerState") or "").lower()
    if power_state != "vm deallocated":
        return None
    name = resource.get("name", "")
    return Finding(
        rule_id="UNIV-COST-001",
        rule_name="Deallocated VM",
        category=Category.COST,
        severity=Severity.MEDIUM,
        resource_id=resource["id"],
        resource_type=resource["type"],
        resource_name=name,
        reason=(
            f"VM '{name}' is in 'VM deallocated' power state. "
            "It incurs OS disk and reserved-IP costs while delivering no compute. "
            "Either delete it or assign it a clear standby/DR purpose with a documented runbook."
        ),
        recommended_action="delete_resource",
        evidence={"powerState": "VM deallocated"},
    )
