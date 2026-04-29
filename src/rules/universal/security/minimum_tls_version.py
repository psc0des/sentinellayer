"""UNIV-SEC-002 — Flag resources not enforcing TLS 1.2 or higher.

TLS 1.0 and 1.1 have known vulnerabilities. Azure enforces TLS 1.2 as the minimum
for most services, but older resources may still have lower values set explicitly.
"""
from src.rules.base import Category, Finding, Severity, rule
from src.rules.inventory_index import InventoryIndex

_WEAK_TLS = {"tls1_0", "tls1_1", "1.0", "1.1"}

_APPLICABLE_TYPES = [
    "microsoft.storage/storageaccounts",
    "microsoft.web/sites",
    "microsoft.cache/redis",
    "microsoft.sql/servers",
    "microsoft.servicebus/namespaces",
    "microsoft.eventhub/namespaces",
]


@rule(
    id="UNIV-SEC-002",
    name="Weak TLS Version Allowed",
    category=Category.SECURITY,
    severity=Severity.HIGH,
    applies_to=_APPLICABLE_TYPES,
)
def evaluate(resource: dict, idx: InventoryIndex) -> Finding | None:
    props = resource.get("properties") or {}
    tls = (
        props.get("minimumTlsVersion")
        or props.get("minimalTlsVersion")
        or props.get("minTlsVersion")
        or ""
    ).lower().replace(" ", "")
    if not tls or tls not in _WEAK_TLS:
        return None
    name = resource.get("name", "")
    return Finding(
        rule_id="UNIV-SEC-002",
        rule_name="Weak TLS Version Allowed",
        category=Category.SECURITY,
        severity=Severity.HIGH,
        resource_id=resource["id"],
        resource_type=resource["type"],
        resource_name=name,
        reason=(
            f"'{name}' allows TLS version '{tls}'. TLS 1.0 and 1.1 have known "
            "cryptographic weaknesses. Set minimumTlsVersion to TLS1_2 or higher."
        ),
        recommended_action="update_config",
        evidence={"minimumTlsVersion": tls},
    )
