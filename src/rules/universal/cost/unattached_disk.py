"""UNIV-COST-002 — Flag managed disks not attached to any VM.

Unattached disks continue to incur storage costs (typically $5–$50/month each for
P10–P30 premium SSDs) with zero utility. They are safe to delete once confirmed unused.
"""
from src.rules.base import Category, Finding, Severity, rule
from src.rules.inventory_index import InventoryIndex


@rule(
    id="UNIV-COST-002",
    name="Unattached Managed Disk",
    category=Category.COST,
    severity=Severity.MEDIUM,
    applies_to=["microsoft.compute/disks"],
)
def evaluate(resource: dict, idx: InventoryIndex) -> Finding | None:
    props = resource.get("properties") or {}
    disk_state = (props.get("diskState") or "").lower()
    if disk_state not in ("unattached", ""):
        return None
    # Skip OS disks that are unattached because their VM was deallocated
    # (they're flagged by UNIV-COST-001 already). Only flag truly orphaned disks
    # that are NOT referenced by any resource in the inventory.
    rid = (resource.get("id") or "").lower()
    if idx.is_referenced(rid):
        return None
    if not disk_state:
        return None
    name = resource.get("name", "")
    sku = (resource.get("sku") or {}).get("name", "unknown")
    size_gb = props.get("diskSizeGB", 0)
    return Finding(
        rule_id="UNIV-COST-002",
        rule_name="Unattached Managed Disk",
        category=Category.COST,
        severity=Severity.MEDIUM,
        resource_id=resource["id"],
        resource_type=resource["type"],
        resource_name=name,
        reason=(
            f"Managed disk '{name}' ({sku}, {size_gb} GiB) is in 'Unattached' state and is "
            "not referenced by any other resource. It is incurring storage costs with no "
            "consumer. Delete or snapshot it."
        ),
        recommended_action="delete_resource",
        evidence={"diskState": "Unattached", "sku": sku, "diskSizeGB": size_gb},
    )
