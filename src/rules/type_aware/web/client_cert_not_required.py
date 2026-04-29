"""TYPE-WEB-001 — App Service not requiring client certificates.

For APIs that should only be called by known clients, clientCertEnabled=true enforces
mutual TLS, preventing unauthorised callers even if they know a valid API token.
"""
from src.rules.base import Category, Finding, Severity, rule
from src.rules.inventory_index import InventoryIndex


@rule(
    id="TYPE-WEB-001",
    name="App Service Client Certificate Not Required",
    category=Category.SECURITY,
    severity=Severity.MEDIUM,
    applies_to=["microsoft.web/sites"],
)
def evaluate(resource: dict, idx: InventoryIndex) -> Finding | None:
    props = resource.get("properties") or {}
    # Only flag API apps (kind contains 'api') or function apps (kind contains 'functionapp')
    kind = (resource.get("kind") or "").lower()
    if "api" not in kind and "functionapp" not in kind:
        return None
    if props.get("clientCertEnabled"):
        return None
    name = resource.get("name", "")
    return Finding(
        rule_id="TYPE-WEB-001",
        rule_name="App Service Client Certificate Not Required",
        category=Category.SECURITY,
        severity=Severity.MEDIUM,
        resource_id=resource["id"],
        resource_type=resource["type"],
        resource_name=name,
        reason=(
            f"App Service '{name}' (kind: {kind}) does not require client certificates. "
            "Enabling clientCertEnabled enforces mutual TLS for all incoming requests, "
            "preventing access from clients without a valid certificate."
        ),
        recommended_action="update_config",
        evidence={"clientCertEnabled": False, "kind": kind},
    )
