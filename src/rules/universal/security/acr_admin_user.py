"""UNIV-SEC-009 — Container registry admin user account should be disabled.

The built-in admin user uses a single shared username/password with full registry access.
It cannot be audited per-user and is incompatible with least-privilege. Use managed
identity or service principals for pull/push access instead.
"""
from src.rules.base import Category, Finding, Severity, rule
from src.rules.inventory_index import InventoryIndex


@rule(
    id="UNIV-SEC-009",
    name="Container Registry Admin User Enabled",
    category=Category.SECURITY,
    severity=Severity.MEDIUM,
    applies_to=["microsoft.containerregistry/registries"],
)
def evaluate(resource: dict, idx: InventoryIndex) -> Finding | None:
    props = resource.get("properties") or {}
    if not props.get("adminUserEnabled"):
        return None
    name = resource.get("name", "")
    return Finding(
        rule_id="UNIV-SEC-009",
        rule_name="Container Registry Admin User Enabled",
        category=Category.SECURITY,
        severity=Severity.MEDIUM,
        resource_id=resource["id"],
        resource_type=resource["type"],
        resource_name=name,
        reason=(
            f"Container registry '{name}' has the admin user account enabled. "
            "This is a shared credential that cannot be audited individually and has "
            "full read/write access. Disable adminUserEnabled; use managed identity or RBAC."
        ),
        recommended_action="update_config",
        evidence={"adminUserEnabled": True},
    )
