"""UNIV-SEC-003 — Storage accounts must enforce HTTPS-only traffic.

Allowing HTTP transfers exposes credentials and data in transit. The supportsHttpsTrafficOnly
property must be true for all storage accounts.
"""
from src.rules.base import Category, Finding, Severity, rule
from src.rules.inventory_index import InventoryIndex


@rule(
    id="UNIV-SEC-003",
    name="Storage Account Allows HTTP Traffic",
    category=Category.SECURITY,
    severity=Severity.HIGH,
    applies_to=["microsoft.storage/storageaccounts"],
)
def evaluate(resource: dict, idx: InventoryIndex) -> Finding | None:
    props = resource.get("properties") or {}
    https_only = props.get("supportsHttpsTrafficOnly")
    if https_only is True or https_only is None:
        return None
    name = resource.get("name", "")
    return Finding(
        rule_id="UNIV-SEC-003",
        rule_name="Storage Account Allows HTTP Traffic",
        category=Category.SECURITY,
        severity=Severity.HIGH,
        resource_id=resource["id"],
        resource_type=resource["type"],
        resource_name=name,
        reason=(
            f"Storage account '{name}' has supportsHttpsTrafficOnly=false, allowing "
            "unencrypted HTTP connections. Set this to true to prevent data exposure in transit."
        ),
        recommended_action="update_config",
        evidence={"supportsHttpsTrafficOnly": False},
    )
