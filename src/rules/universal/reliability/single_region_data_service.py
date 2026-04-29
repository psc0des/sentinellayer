"""UNIV-REL-006 — Cosmos DB and SQL without geo-redundancy in production.

Single-region data services cannot survive a regional outage. Production databases
should have geo-redundancy or failover groups configured.
"""
from src.rules.base import Category, Finding, Severity, rule
from src.rules.inventory_index import InventoryIndex


@rule(
    id="UNIV-REL-006",
    name="Cosmos DB Without Geo-Redundancy",
    category=Category.RELIABILITY,
    severity=Severity.HIGH,
    applies_to=["microsoft.documentdb/databaseaccounts"],
)
def evaluate(resource: dict, idx: InventoryIndex) -> Finding | None:
    props = resource.get("properties") or {}
    # enableMultipleWriteLocations or multiple locations indicates geo-redundancy
    locations = props.get("locations") or []
    if len(locations) > 1:
        return None
    if props.get("enableMultipleWriteLocations"):
        return None
    name = resource.get("name", "")
    return Finding(
        rule_id="UNIV-REL-006",
        rule_name="Cosmos DB Without Geo-Redundancy",
        category=Category.RELIABILITY,
        severity=Severity.HIGH,
        resource_id=resource["id"],
        resource_type=resource["type"],
        resource_name=name,
        reason=(
            f"Cosmos DB account '{name}' is configured with a single region only. "
            "A regional Azure outage would make this database unavailable. "
            "Add at least one additional region as a read replica or enable multi-region writes."
        ),
        recommended_action="update_config",
        evidence={"locationCount": len(locations)},
    )
