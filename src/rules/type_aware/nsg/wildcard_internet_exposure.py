"""TYPE-NSG-002 — NSG inbound rules with wildcard source AND wildcard port.

A rule that allows source='*', protocol='*', destinationPortRange='*' permits
all traffic from any IP on any port — effectively no firewall at all.
"""
from src.rules.base import Category, Finding, Severity, rule
from src.rules.inventory_index import InventoryIndex

_OPEN_SOURCES = {"*", "internet", "any"}


@rule(
    id="TYPE-NSG-002",
    name="NSG Wildcard Inbound Rule (Any → Any)",
    category=Category.SECURITY,
    severity=Severity.CRITICAL,
    applies_to=["microsoft.network/networksecuritygroups"],
)
def evaluate(resource: dict, idx: InventoryIndex) -> Finding | None:
    props = resource.get("properties") or {}
    rules = props.get("securityRules") or []
    wildcard_rules = []
    for r in rules:
        rprops = r.get("properties") or {}
        if rprops.get("direction", "").lower() != "inbound":
            continue
        if rprops.get("access", "").lower() != "allow":
            continue
        src = (rprops.get("sourceAddressPrefix") or "").lower()
        port = (rprops.get("destinationPortRange") or "").strip()
        protocol = (rprops.get("protocol") or "").strip()
        if src in _OPEN_SOURCES and port == "*" and protocol in ("*", ""):
            wildcard_rules.append(r.get("name"))
    if not wildcard_rules:
        return None
    name = resource.get("name", "")
    return Finding(
        rule_id="TYPE-NSG-002",
        rule_name="NSG Wildcard Inbound Rule (Any → Any)",
        category=Category.SECURITY,
        severity=Severity.CRITICAL,
        resource_id=resource["id"],
        resource_type=resource["type"],
        resource_name=name,
        reason=(
            f"NSG '{name}' has {len(wildcard_rules)} rule(s) ({', '.join(wildcard_rules)}) "
            "that allow all protocols from any source to any port. This is equivalent to "
            "no firewall. Remove these rules and replace with specific allow-listing."
        ),
        recommended_action="modify_nsg",
        evidence={"wildcard_rules": wildcard_rules},
    )
