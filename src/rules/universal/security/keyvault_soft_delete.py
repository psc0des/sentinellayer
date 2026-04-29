"""UNIV-SEC-005 — Key Vault must have soft-delete enabled.

Soft-delete protects secrets, keys, and certificates from accidental or malicious
permanent deletion for a retention period. Without it, a deleted key vault is
unrecoverable.
"""
from src.rules.base import Category, Finding, Severity, rule
from src.rules.inventory_index import InventoryIndex


@rule(
    id="UNIV-SEC-005",
    name="Key Vault Soft-Delete Disabled",
    category=Category.SECURITY,
    severity=Severity.HIGH,
    applies_to=["microsoft.keyvault/vaults"],
)
def evaluate(resource: dict, idx: InventoryIndex) -> Finding | None:
    props = resource.get("properties") or {}
    soft_delete = props.get("enableSoftDelete")
    # None means not explicitly set (older vaults — treat as missing)
    if soft_delete is True:
        return None
    name = resource.get("name", "")
    return Finding(
        rule_id="UNIV-SEC-005",
        rule_name="Key Vault Soft-Delete Disabled",
        category=Category.SECURITY,
        severity=Severity.HIGH,
        resource_id=resource["id"],
        resource_type=resource["type"],
        resource_name=name,
        reason=(
            f"Key Vault '{name}' does not have soft-delete enabled. Without soft-delete, "
            "secrets, keys, and certificates can be permanently deleted with no recovery window. "
            "Enable enableSoftDelete and set a retention period of at least 7 days."
        ),
        recommended_action="update_config",
        evidence={"enableSoftDelete": soft_delete},
    )
