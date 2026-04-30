"""UNIV-HYG-002 — Resources missing required organisational tags.

Checks for the three most common mandatory tags: 'owner', 'environment', and
'costcenter' (case-insensitive). Resources that have tags but are missing one or
more of these required keys are flagged.
"""
from src.rules.base import Category, Finding, Severity, rule
from src.rules.inventory_index import InventoryIndex

_REQUIRED_TAGS = {"owner", "environment", "costcenter"}

_SKIP_TYPES = {
    "microsoft.compute/virtualmachines/extensions",
    "microsoft.insights/diagnosticsettings",
    "microsoft.authorization/roleassignments",
    "microsoft.authorization/roledefinitions",
    "microsoft.operationsmanagement/solutions",
    "microsoft.network/networkwatchers/flowlogs",
    # Child resources — inherit ownership from parent, tagging independently is noise
    "microsoft.compute/disks",
    "microsoft.network/networkinterfaces",
    "microsoft.insights/scheduledqueryrules",
    "microsoft.insights/activitylogalerts",
    "microsoft.insights/datacollectionrules",
    "microsoft.devtestlab/schedules",
    "microsoft.maintenance/maintenanceconfigurations",
    "microsoft.compute/snapshots",
}


@rule(
    id="UNIV-HYG-002",
    name="Missing Required Organisational Tags",
    category=Category.HYGIENE,
    severity=Severity.LOW,
    applies_to=["*"],
)
def evaluate(resource: dict, idx: InventoryIndex) -> Finding | None:
    rtype = (resource.get("type") or "").lower()
    if rtype in _SKIP_TYPES:
        return None
    tags = resource.get("tags") or {}
    if not tags:
        return None  # UNIV-HYG-001 handles fully untagged resources
    existing_keys = {k.lower() for k in tags}
    missing = _REQUIRED_TAGS - existing_keys
    if not missing:
        return None
    name = resource.get("name", "")
    missing_str = ", ".join(sorted(missing))
    return Finding(
        rule_id="UNIV-HYG-002",
        rule_name="Missing Required Organisational Tags",
        category=Category.HYGIENE,
        severity=Severity.LOW,
        resource_id=resource["id"],
        resource_type=resource["type"],
        resource_name=name,
        reason=(
            f"'{name}' is missing required tags: {missing_str}. "
            "These tags are needed for cost allocation and ownership attribution. "
            "Add the missing tags to comply with the tagging policy."
        ),
        recommended_action="update_config",
        evidence={"missing_tags": sorted(missing), "present_tags": sorted(existing_keys)},
    )
