"""UNIV-REL-005 — VMs missing the Azure Monitor Agent (AMA) extension.

The legacy Microsoft Monitoring Agent (MMA/OMS Agent) is being retired. VMs that
have neither AMA nor are managed by Arc should have the AzureMonitorWindowsAgent
or AzureMonitorLinuxAgent extension installed for metrics and log collection.
"""
from src.rules.base import Category, Finding, Severity, rule
from src.rules.inventory_index import InventoryIndex

_AMA_EXTENSIONS = {
    "azuremonitorwindowsagent",
    "azuremonitorlinuxagent",
}


@rule(
    id="UNIV-REL-005",
    name="VM Missing Azure Monitor Agent Extension",
    category=Category.RELIABILITY,
    severity=Severity.MEDIUM,
    applies_to=["microsoft.compute/virtualmachines"],
)
def evaluate(resource: dict, idx: InventoryIndex) -> Finding | None:
    rid = (resource.get("id") or "").lower()
    # Check for AMA extension as a child resource in the inventory
    has_ama = any(
        any(ext in (r.get("name") or "").lower() for ext in _AMA_EXTENSIONS)
        for r in idx.by_type("microsoft.compute/virtualmachines/extensions")
        if (r.get("id") or "").lower().startswith(rid)
    )
    if has_ama:
        return None
    # Also check extensions embedded in properties.resources (some inventory builders include them)
    props = resource.get("properties") or {}
    ext_profile = props.get("extensionProfile") or {}
    embedded_exts = ext_profile.get("extensions") or []
    if any(
        any(ama in (e.get("name") or "").lower() for ama in _AMA_EXTENSIONS)
        for e in embedded_exts
    ):
        return None
    name = resource.get("name", "")
    return Finding(
        rule_id="UNIV-REL-005",
        rule_name="VM Missing Azure Monitor Agent Extension",
        category=Category.RELIABILITY,
        severity=Severity.MEDIUM,
        resource_id=resource["id"],
        resource_type=resource["type"],
        resource_name=name,
        reason=(
            f"VM '{name}' does not appear to have the Azure Monitor Agent (AMA) extension "
            "installed. Without AMA, performance counters, syslog, and Windows event logs "
            "are not collected. Install AzureMonitorWindowsAgent or AzureMonitorLinuxAgent."
        ),
        recommended_action="update_config",
        evidence={"amaExtension": "not found"},
    )
