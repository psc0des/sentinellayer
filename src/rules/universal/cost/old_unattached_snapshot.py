"""UNIV-COST-005 — Flag disk snapshots that are old and unattached.

Snapshots older than 90 days that are not referenced by any image or disk are
probably forgotten backups. They incur standard storage billing indefinitely.
"""
from datetime import datetime, timezone
from src.rules.base import Category, Finding, Severity, rule
from src.rules.inventory_index import InventoryIndex

_MAX_AGE_DAYS = 90


@rule(
    id="UNIV-COST-005",
    name="Old Unattached Disk Snapshot",
    category=Category.COST,
    severity=Severity.LOW,
    applies_to=["microsoft.compute/snapshots"],
)
def evaluate(resource: dict, idx: InventoryIndex) -> Finding | None:
    props = resource.get("properties") or {}
    # Skip if referenced (e.g., used as image source)
    rid = (resource.get("id") or "").lower()
    if idx.is_referenced(rid):
        return None
    created_raw = props.get("timeCreated") or resource.get("createdTime") or ""
    if not created_raw:
        return None
    try:
        created = datetime.fromisoformat(created_raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    age_days = (datetime.now(timezone.utc) - created).days
    if age_days < _MAX_AGE_DAYS:
        return None
    name = resource.get("name", "")
    size_gb = props.get("diskSizeGB", 0)
    return Finding(
        rule_id="UNIV-COST-005",
        rule_name="Old Unattached Disk Snapshot",
        category=Category.COST,
        severity=Severity.LOW,
        resource_id=resource["id"],
        resource_type=resource["type"],
        resource_name=name,
        reason=(
            f"Snapshot '{name}' ({size_gb} GiB) is {age_days} days old and not referenced "
            "by any disk or image. Old forgotten snapshots accumulate storage costs. "
            "Verify it is no longer needed and delete it."
        ),
        recommended_action="delete_resource",
        evidence={"agedays": age_days, "diskSizeGB": size_gb, "timeCreated": created_raw},
    )
