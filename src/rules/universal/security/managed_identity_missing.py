"""UNIV-SEC-007 — Compute and integration services should use managed identity.

Managed identity eliminates the need to store credentials in code or configuration.
Resources that interact with other Azure services (storage, key vault, databases) should
have either a system-assigned or user-assigned managed identity.
"""
from src.rules.base import Category, Finding, Severity, rule
from src.rules.inventory_index import InventoryIndex

_APPLICABLE_TYPES = [
    "microsoft.compute/virtualmachines",
    "microsoft.web/sites",
    "microsoft.containerinstance/containergroups",
    "microsoft.logic/workflows",
    "microsoft.datafactory/factories",
    "microsoft.appconfiguration/configurationstores",
]


@rule(
    id="UNIV-SEC-007",
    name="Managed Identity Not Configured",
    category=Category.SECURITY,
    severity=Severity.MEDIUM,
    applies_to=_APPLICABLE_TYPES,
)
def evaluate(resource: dict, idx: InventoryIndex) -> Finding | None:
    identity = resource.get("identity") or {}
    id_type = (identity.get("type") or "").lower()
    if id_type and id_type != "none":
        return None
    name = resource.get("name", "")
    return Finding(
        rule_id="UNIV-SEC-007",
        rule_name="Managed Identity Not Configured",
        category=Category.SECURITY,
        severity=Severity.MEDIUM,
        resource_id=resource["id"],
        resource_type=resource["type"],
        resource_name=name,
        reason=(
            f"'{name}' has no managed identity. Services that call other Azure APIs should "
            "use a system-assigned or user-assigned managed identity to avoid storing "
            "credentials in code or environment variables."
        ),
        recommended_action="update_config",
        evidence={"identity": identity},
    )
