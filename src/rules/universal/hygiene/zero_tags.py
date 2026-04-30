"""UNIV-HYG-001 — Resources with zero tags have no ownership metadata.

Untagged resources cannot be attributed to a cost center, owner, or environment.
This makes cost allocation, access reviews, and decommissioning harder.
Exclude resource types that cannot be tagged (e.g., extension child resources).
"""
from src.rules.base import Category, Finding, Severity, rule
from src.rules.inventory_index import InventoryIndex

# Resource types that legitimately have no tags and should not be flagged.
# Child/infrastructure resources inherit ownership from their parent and adding
# tags independently would create maintenance burden without adding signal.
_SKIP_TYPES = {
    "microsoft.compute/virtualmachines/extensions",
    "microsoft.insights/diagnosticsettings",
    "microsoft.operationsmanagement/solutions",
    "microsoft.authorization/roleassignments",
    "microsoft.authorization/roledefinitions",
    "microsoft.network/networkwatchers/flowlogs",
    # Child resources that inherit ownership from their parent VM/resource
    "microsoft.compute/disks",                        # OS/data disks (child of VM)
    "microsoft.network/networkinterfaces",            # NICs (child of VM)
    # Monitoring infrastructure — auto-created, not independently ownable
    "microsoft.insights/scheduledqueryrules",         # Alert rules
    "microsoft.insights/activitylogalerts",           # Activity log alerts
    "microsoft.insights/datacollectionrules",         # DCRs
    "microsoft.devtestlab/schedules",                 # VM auto-shutdown schedules
    "microsoft.maintenance/maintenanceconfigurations", # Maintenance configs
    "microsoft.compute/snapshots",                    # Snapshots (point-in-time, ephemeral)
}


@rule(
    id="UNIV-HYG-001",
    name="Resource Has No Tags",
    category=Category.HYGIENE,
    severity=Severity.LOW,
    applies_to=["*"],
)
def evaluate(resource: dict, idx: InventoryIndex) -> Finding | None:
    rtype = (resource.get("type") or "").lower()
    if rtype in _SKIP_TYPES:
        return None
    tags = resource.get("tags") or {}
    if tags:
        return None
    name = resource.get("name", "")
    return Finding(
        rule_id="UNIV-HYG-001",
        rule_name="Resource Has No Tags",
        category=Category.HYGIENE,
        severity=Severity.LOW,
        resource_id=resource["id"],
        resource_type=resource["type"],
        resource_name=name,
        reason=(
            f"'{name}' has no tags. Untagged resources cannot be attributed to a cost center, "
            "team, or environment. Add at minimum: owner, environment, and costcenter tags."
        ),
        recommended_action="update_config",
        evidence={"tags": {}},
    )
