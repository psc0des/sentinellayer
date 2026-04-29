"""UNIV-SEC-004 — Blob public access must be disabled on storage accounts.

allowBlobPublicAccess=true allows anonymous reads to any container/blob marked public,
risking accidental data exposure.
"""
from src.rules.base import Category, Finding, Severity, rule
from src.rules.inventory_index import InventoryIndex


@rule(
    id="UNIV-SEC-004",
    name="Blob Public Access Allowed",
    category=Category.SECURITY,
    severity=Severity.HIGH,
    applies_to=["microsoft.storage/storageaccounts"],
)
def evaluate(resource: dict, idx: InventoryIndex) -> Finding | None:
    props = resource.get("properties") or {}
    allow_public = props.get("allowBlobPublicAccess")
    if allow_public is not True:
        return None
    name = resource.get("name", "")
    return Finding(
        rule_id="UNIV-SEC-004",
        rule_name="Blob Public Access Allowed",
        category=Category.SECURITY,
        severity=Severity.HIGH,
        resource_id=resource["id"],
        resource_type=resource["type"],
        resource_name=name,
        reason=(
            f"Storage account '{name}' has allowBlobPublicAccess=true. This allows "
            "anonymous internet access to any container marked public. Disable this setting "
            "and use SAS tokens or managed identity for controlled access."
        ),
        recommended_action="update_config",
        evidence={"allowBlobPublicAccess": True},
    )
