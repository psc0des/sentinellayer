"""TYPE-NSG-003 — NSG missing an explicit DenyAll inbound rule.

Azure NSGs have an implicit DenyAll at priority 65500, but explicit high-priority
DenyAll rules are a defence-in-depth control that documents intent and is visible
in compliance reports. Flag NSGs that have custom allow rules but no explicit Deny.
"""
from src.rules.base import Category, Finding, Severity, rule
from src.rules.inventory_index import InventoryIndex


@rule(
    id="TYPE-NSG-003",
    name="NSG Missing Explicit Deny-All Inbound Rule",
    category=Category.SECURITY,
    severity=Severity.LOW,
    applies_to=["microsoft.network/networksecuritygroups"],
)
def evaluate(resource: dict, idx: InventoryIndex) -> Finding | None:
    props = resource.get("properties") or {}
    rules = props.get("securityRules") or []
    custom_inbound = [
        r for r in rules
        if (r.get("properties") or {}).get("direction", "").lower() == "inbound"
    ]
    if not custom_inbound:
        return None  # No custom rules — implicit deny is sufficient
    has_deny_all = any(
        (r.get("properties") or {}).get("access", "").lower() == "deny"
        and (r.get("properties") or {}).get("sourceAddressPrefix", "") in ("*", "Any")
        for r in custom_inbound
    )
    if has_deny_all:
        return None
    name = resource.get("name", "")
    return Finding(
        rule_id="TYPE-NSG-003",
        rule_name="NSG Missing Explicit Deny-All Inbound Rule",
        category=Category.SECURITY,
        severity=Severity.LOW,
        resource_id=resource["id"],
        resource_type=resource["type"],
        resource_name=name,
        reason=(
            f"NSG '{name}' has {len(custom_inbound)} custom inbound rule(s) but no explicit "
            "Deny-All rule. While Azure adds an implicit deny at priority 65500, an explicit "
            "rule with a high priority number and source='*' / access='Deny' makes the intent "
            "visible in compliance tooling."
        ),
        recommended_action="modify_nsg",
        evidence={"custom_inbound_rules": len(custom_inbound)},
    )
