"""UNIV-SEC-010 — App Service and Function Apps must enforce HTTPS-only.

httpsOnly=false allows browsers and clients to connect over plain HTTP, exposing
authentication tokens and request payloads in transit.
"""
from src.rules.base import Category, Finding, Severity, rule
from src.rules.inventory_index import InventoryIndex

_APPLICABLE_TYPES = [
    "microsoft.web/sites",
]


@rule(
    id="UNIV-SEC-010",
    name="App Service HTTPS-Only Not Enforced",
    category=Category.SECURITY,
    severity=Severity.HIGH,
    applies_to=_APPLICABLE_TYPES,
)
def evaluate(resource: dict, idx: InventoryIndex) -> Finding | None:
    props = resource.get("properties") or {}
    https_only = props.get("httpsOnly")
    if https_only is True or https_only is None:
        return None
    name = resource.get("name", "")
    return Finding(
        rule_id="UNIV-SEC-010",
        rule_name="App Service HTTPS-Only Not Enforced",
        category=Category.SECURITY,
        severity=Severity.HIGH,
        resource_id=resource["id"],
        resource_type=resource["type"],
        resource_name=name,
        reason=(
            f"App Service '{name}' has httpsOnly=false, allowing unencrypted HTTP connections. "
            "Set httpsOnly=true to redirect all HTTP traffic to HTTPS and prevent data exposure."
        ),
        recommended_action="update_config",
        evidence={"httpsOnly": False},
    )
