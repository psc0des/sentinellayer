"""UNIV-REL-004 — Key services should have diagnostic settings configured.

Without diagnostic settings, platform logs and metrics are not sent to Log Analytics,
Storage, or Event Hub. This makes incident investigation and alerting impossible.
We detect this indirectly: if the inventory has no diagnosticSettings child resource
for a key service, flag it.
"""
from src.rules.base import Category, Finding, Severity, rule
from src.rules.inventory_index import InventoryIndex

_APPLICABLE_TYPES = [
    "microsoft.keyvault/vaults",
    "microsoft.sql/servers/databases",
    "microsoft.network/applicationgateways",
    "microsoft.network/loadbalancers",
    "microsoft.servicebus/namespaces",
    "microsoft.eventhub/namespaces",
]


@rule(
    id="UNIV-REL-004",
    name="Diagnostic Settings Missing",
    category=Category.RELIABILITY,
    severity=Severity.MEDIUM,
    applies_to=_APPLICABLE_TYPES,
)
def evaluate(resource: dict, idx: InventoryIndex) -> Finding | None:
    rid = (resource.get("id") or "").lower()
    # Check if there's a diagnosticSettings child in the inventory
    diag_prefix = rid + "/providers/microsoft.insights/diagnosticsettings"
    has_diag = any(
        (r.get("id") or "").lower().startswith(diag_prefix)
        for r in idx.all()
    )
    if has_diag:
        return None
    # Also check the resource's own properties for a diagnosticsettings reference
    props = resource.get("properties") or {}
    if props.get("diagnosticsSettings") or props.get("diagnosticSettings"):
        return None
    name = resource.get("name", "")
    return Finding(
        rule_id="UNIV-REL-004",
        rule_name="Diagnostic Settings Missing",
        category=Category.RELIABILITY,
        severity=Severity.MEDIUM,
        resource_id=resource["id"],
        resource_type=resource["type"],
        resource_name=name,
        reason=(
            f"'{name}' has no diagnostic settings configured. Platform logs and metrics "
            "are not being sent to any Log Analytics workspace, storage account, or Event Hub. "
            "Add a diagnostic setting to enable alerting and incident investigation."
        ),
        recommended_action="update_config",
        evidence={"diagnosticSettings": "not found in inventory"},
    )
