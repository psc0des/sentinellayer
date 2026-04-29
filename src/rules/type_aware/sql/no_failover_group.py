"""TYPE-SQL-001 — SQL Server without a failover group.

Azure SQL failover groups provide automatic geo-redundancy and a single read/write
endpoint that survives a regional outage. Production SQL servers should have one.
We detect this by checking if no failoverGroup child resource exists in the inventory.
"""
from src.rules.base import Category, Finding, Severity, rule
from src.rules.inventory_index import InventoryIndex


@rule(
    id="TYPE-SQL-001",
    name="SQL Server Without Failover Group",
    category=Category.RELIABILITY,
    severity=Severity.HIGH,
    applies_to=["microsoft.sql/servers"],
)
def evaluate(resource: dict, idx: InventoryIndex) -> Finding | None:
    rid = (resource.get("id") or "").lower()
    # Check for a failoverGroup child resource
    has_failover = any(
        "/failovergroups/" in (r.get("id") or "").lower()
        for r in idx.by_type("microsoft.sql/servers/failovergroups")
        if (r.get("id") or "").lower().startswith(rid)
    )
    if has_failover:
        return None
    name = resource.get("name", "")
    return Finding(
        rule_id="TYPE-SQL-001",
        rule_name="SQL Server Without Failover Group",
        category=Category.RELIABILITY,
        severity=Severity.HIGH,
        resource_id=resource["id"],
        resource_type=resource["type"],
        resource_name=name,
        reason=(
            f"SQL Server '{name}' does not have a failover group configured. Without a failover "
            "group, a regional Azure outage would require a manual failover with potential data "
            "loss. Configure a failover group pointing to a secondary region."
        ),
        recommended_action="update_config",
        evidence={"failoverGroup": "not found"},
    )
