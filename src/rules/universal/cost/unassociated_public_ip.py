"""UNIV-COST-003 — Flag public IP addresses not associated with any resource.

Static public IPs reserved but not attached to a NIC, load balancer, or gateway
still incur the idle IP charge (~$3.65/month) and fragment your address space.
"""
from src.rules.base import Category, Finding, Severity, rule
from src.rules.inventory_index import InventoryIndex


@rule(
    id="UNIV-COST-003",
    name="Unassociated Public IP Address",
    category=Category.COST,
    severity=Severity.LOW,
    applies_to=["microsoft.network/publicipaddresses"],
)
def evaluate(resource: dict, idx: InventoryIndex) -> Finding | None:
    props = resource.get("properties") or {}
    # If there is an ipConfiguration, the IP is attached
    if props.get("ipConfiguration"):
        return None
    # Dynamic IPs that are unallocated are free; only static ones cost money
    alloc_method = (props.get("publicIPAllocationMethod") or "").lower()
    if alloc_method == "dynamic" and not props.get("ipAddress"):
        return None
    name = resource.get("name", "")
    ip_addr = props.get("ipAddress", "not yet allocated")
    return Finding(
        rule_id="UNIV-COST-003",
        rule_name="Unassociated Public IP Address",
        category=Category.COST,
        severity=Severity.LOW,
        resource_id=resource["id"],
        resource_type=resource["type"],
        resource_name=name,
        reason=(
            f"Public IP '{name}' ({ip_addr}) is not associated with any NIC, load balancer, "
            "or gateway. Reserved IPs incur an idle charge. Release or reassign it."
        ),
        recommended_action="delete_resource",
        evidence={"publicIPAllocationMethod": alloc_method, "ipAddress": ip_addr},
    )
