"""UNIV-COST-006 — Flag Recovery Services vaults with no protected items.

An empty vault incurs the base vault fee without providing any backup protection.
Either configure backup policies or delete the vault.
"""
from src.rules.base import Category, Finding, Severity, rule
from src.rules.inventory_index import InventoryIndex


@rule(
    id="UNIV-COST-006",
    name="Empty Recovery Services Vault",
    category=Category.COST,
    severity=Severity.LOW,
    applies_to=["microsoft.recoveryservices/vaults"],
)
def evaluate(resource: dict, idx: InventoryIndex) -> Finding | None:
    props = resource.get("properties") or {}
    # The inventory builder captures protectedItemCount from the vault properties
    protected_count = props.get("protectedItemCount")
    if protected_count is None:
        # Can't determine — skip; don't false-flag
        return None
    if protected_count > 0:
        return None
    name = resource.get("name", "")
    return Finding(
        rule_id="UNIV-COST-006",
        rule_name="Empty Recovery Services Vault",
        category=Category.COST,
        severity=Severity.LOW,
        resource_id=resource["id"],
        resource_type=resource["type"],
        resource_name=name,
        reason=(
            f"Recovery Services vault '{name}' has {protected_count} protected items. "
            "An empty vault incurs a base fee with no backup value. "
            "Configure backup policies or delete the vault."
        ),
        recommended_action="delete_resource",
        evidence={"protectedItemCount": protected_count},
    )
