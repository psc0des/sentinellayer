"""TYPE-NSG-001 — NSG rules allowing internet access to management ports.

Any inbound rule with source '*' or 'Internet' on ports 22 (SSH) or 3389 (RDP)
exposes the resource to brute-force and ransomware attacks from the public internet.
"""
from src.rules.base import Category, Finding, Severity, rule
from src.rules.inventory_index import InventoryIndex

_MGMT_PORTS = {22, 3389, 5985, 5986}  # SSH, RDP, WinRM
_OPEN_SOURCES = {"*", "internet", "any"}


def _port_in_range(port_range: str, target_ports: set) -> bool:
    pr = port_range.strip()
    if pr == "*":
        return True
    if "-" in pr:
        try:
            lo, hi = pr.split("-")
            return any(int(lo) <= p <= int(hi) for p in target_ports)
        except ValueError:
            return False
    try:
        return int(pr) in target_ports
    except ValueError:
        return False


@rule(
    id="TYPE-NSG-001",
    name="NSG Exposes Management Port to Internet",
    category=Category.SECURITY,
    severity=Severity.CRITICAL,
    applies_to=["microsoft.network/networksecuritygroups"],
)
def evaluate(resource: dict, idx: InventoryIndex) -> Finding | None:
    props = resource.get("properties") or {}
    rules = props.get("securityRules") or []
    exposed = []
    for r in rules:
        rprops = r.get("properties") or {}
        if rprops.get("direction", "").lower() != "inbound":
            continue
        if rprops.get("access", "").lower() != "allow":
            continue
        src = (rprops.get("sourceAddressPrefix") or "").lower()
        if src not in _OPEN_SOURCES:
            continue
        port_range = rprops.get("destinationPortRange") or ""
        ranges = rprops.get("destinationPortRanges") or [port_range]
        if any(_port_in_range(pr, _MGMT_PORTS) for pr in ranges if pr):
            exposed.append({"rule": r.get("name"), "ports": port_range or ranges})
    if not exposed:
        return None
    name = resource.get("name", "")
    return Finding(
        rule_id="TYPE-NSG-001",
        rule_name="NSG Exposes Management Port to Internet",
        category=Category.SECURITY,
        severity=Severity.CRITICAL,
        resource_id=resource["id"],
        resource_type=resource["type"],
        resource_name=name,
        reason=(
            f"NSG '{name}' has {len(exposed)} inbound rule(s) that allow the public internet "
            "(source='*' or 'Internet') to reach management ports (SSH/RDP/WinRM). "
            "This is a critical attack vector. Restrict source to specific IP ranges or use "
            "Azure Bastion / Just-in-Time access."
        ),
        recommended_action="modify_nsg",
        evidence={"exposed_rules": exposed},
    )
