"""UNIV-REL-003 — Free/Basic SKU in a resource group that appears to be production.

Free and Basic SKUs offer no SLA, no geo-redundancy, and limited capacity. Resources
in resource groups named *prod*, *production*, *prd* should use Standard or higher.
"""
from src.rules.base import Category, Finding, Severity, rule
from src.rules.inventory_index import InventoryIndex

_PROD_RG_PATTERNS = ("prod", "production", "prd", "-prd-", "-prod-")
_CHEAP_SKU_PREFIXES = ("free", "basic", "b1", "b2", "f1", "f0")

_APPLICABLE_TYPES = [
    "microsoft.web/sites",
    "microsoft.web/serverfarms",
    "microsoft.cache/redis",
    "microsoft.search/searchservices",
    "microsoft.containerregistry/registries",
    "microsoft.servicebus/namespaces",
    "microsoft.eventhub/namespaces",
]


def _is_prod_rg(resource_id: str) -> bool:
    parts = resource_id.lower().split("/")
    for i, part in enumerate(parts):
        if part == "resourcegroups" and i + 1 < len(parts):
            rg = parts[i + 1]
            return any(p in rg for p in _PROD_RG_PATTERNS)
    return False


def _is_cheap_sku(resource: dict) -> str | None:
    sku = resource.get("sku") or {}
    sku_name = (sku.get("name") or sku.get("tier") or "").lower()
    for prefix in _CHEAP_SKU_PREFIXES:
        if sku_name.startswith(prefix):
            return sku_name
    return None


@rule(
    id="UNIV-REL-003",
    name="Free/Basic SKU in Production Resource Group",
    category=Category.RELIABILITY,
    severity=Severity.MEDIUM,
    applies_to=_APPLICABLE_TYPES,
)
def evaluate(resource: dict, idx: InventoryIndex) -> Finding | None:
    rid = resource.get("id") or ""
    if not _is_prod_rg(rid):
        return None
    cheap_sku = _is_cheap_sku(resource)
    if not cheap_sku:
        return None
    name = resource.get("name", "")
    return Finding(
        rule_id="UNIV-REL-003",
        rule_name="Free/Basic SKU in Production Resource Group",
        category=Category.RELIABILITY,
        severity=Severity.MEDIUM,
        resource_id=resource["id"],
        resource_type=resource["type"],
        resource_name=name,
        reason=(
            f"'{name}' uses SKU '{cheap_sku}' in a resource group that appears to be "
            "production. Free/Basic SKUs carry no SLA and have limited capacity. "
            "Upgrade to Standard or higher for production workloads."
        ),
        recommended_action="update_config",
        evidence={"sku": cheap_sku},
    )
