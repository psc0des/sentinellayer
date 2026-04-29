"""TYPE-COSMOS-001 — Cosmos DB without automatic failover enabled.

Even with multiple regions configured, Cosmos requires enableAutomaticFailover=true
for the service to automatically promote a secondary region as the new write region
during an outage without manual intervention.
"""
from src.rules.base import Category, Finding, Severity, rule
from src.rules.inventory_index import InventoryIndex


@rule(
    id="TYPE-COSMOS-001",
    name="Cosmos DB Automatic Failover Disabled",
    category=Category.RELIABILITY,
    severity=Severity.HIGH,
    applies_to=["microsoft.documentdb/databaseaccounts"],
)
def evaluate(resource: dict, idx: InventoryIndex) -> Finding | None:
    props = resource.get("properties") or {}
    locations = props.get("locations") or []
    if len(locations) <= 1:
        return None  # UNIV-REL-006 covers single-region already
    if props.get("enableAutomaticFailover"):
        return None
    name = resource.get("name", "")
    return Finding(
        rule_id="TYPE-COSMOS-001",
        rule_name="Cosmos DB Automatic Failover Disabled",
        category=Category.RELIABILITY,
        severity=Severity.HIGH,
        resource_id=resource["id"],
        resource_type=resource["type"],
        resource_name=name,
        reason=(
            f"Cosmos DB account '{name}' has multiple regions ({len(locations)}) but "
            "enableAutomaticFailover=false. During a regional outage, failover to a secondary "
            "region requires manual intervention. Enable automatic failover for production "
            "databases."
        ),
        recommended_action="update_config",
        evidence={"enableAutomaticFailover": False, "locationCount": len(locations)},
    )
