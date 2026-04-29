"""UNIV-SEC-006 — Key Vault must have purge-protection enabled.

Purge-protection prevents a vault from being permanently deleted (purged) during the
soft-delete retention period — even by an administrator. Required for FIPS-compliant
and HSM-backed vaults.
"""
from src.rules.base import Category, Finding, Severity, rule
from src.rules.inventory_index import InventoryIndex


@rule(
    id="UNIV-SEC-006",
    name="Key Vault Purge-Protection Disabled",
    category=Category.SECURITY,
    severity=Severity.HIGH,
    applies_to=["microsoft.keyvault/vaults"],
)
def evaluate(resource: dict, idx: InventoryIndex) -> Finding | None:
    props = resource.get("properties") or {}
    purge_protection = props.get("enablePurgeProtection")
    if purge_protection is True:
        return None
    name = resource.get("name", "")
    return Finding(
        rule_id="UNIV-SEC-006",
        rule_name="Key Vault Purge-Protection Disabled",
        category=Category.SECURITY,
        severity=Severity.HIGH,
        resource_id=resource["id"],
        resource_type=resource["type"],
        resource_name=name,
        reason=(
            f"Key Vault '{name}' does not have purge-protection enabled. "
            "Without this, a privileged user can permanently delete the vault and all its "
            "secrets during the soft-delete retention window. Enable enablePurgeProtection."
        ),
        recommended_action="update_config",
        evidence={"enablePurgeProtection": purge_protection},
    )
