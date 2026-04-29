"""UNIV-SEC-001 — Flag resources with publicNetworkAccess explicitly enabled.

Data services (Storage, Cosmos, KeyVault, SQL, Postgres, etc.) that expose a public
network endpoint without a private endpoint widen the attack surface unnecessarily.
"""
from src.rules.base import Category, Finding, Severity, rule
from src.rules.inventory_index import InventoryIndex

_DATA_TYPES = {
    "microsoft.storage/storageaccounts",
    "microsoft.documentdb/databaseaccounts",
    "microsoft.keyvault/vaults",
    "microsoft.sql/servers",
    "microsoft.dbforpostgresql/servers",
    "microsoft.dbforpostgresql/flexibleservers",
    "microsoft.dbformysql/servers",
    "microsoft.dbformysql/flexibleservers",
    "microsoft.cache/redis",
    "microsoft.cognitiveservices/accounts",
    "microsoft.containerregistry/registries",
    "microsoft.search/searchservices",
}


@rule(
    id="UNIV-SEC-001",
    name="Public Network Access Enabled on Data Service",
    category=Category.SECURITY,
    severity=Severity.HIGH,
    applies_to=list(_DATA_TYPES),
)
def evaluate(resource: dict, idx: InventoryIndex) -> Finding | None:
    rtype = (resource.get("type") or "").lower()
    if rtype not in _DATA_TYPES:
        return None
    props = resource.get("properties") or {}
    pna = (props.get("publicNetworkAccess") or "").lower()
    if pna != "enabled":
        return None
    name = resource.get("name", "")
    return Finding(
        rule_id="UNIV-SEC-001",
        rule_name="Public Network Access Enabled on Data Service",
        category=Category.SECURITY,
        severity=Severity.HIGH,
        resource_id=resource["id"],
        resource_type=resource["type"],
        resource_name=name,
        reason=(
            f"'{name}' has publicNetworkAccess=Enabled. Data services should use "
            "private endpoints and disable public access to prevent exposure to the internet."
        ),
        recommended_action="update_config",
        evidence={"publicNetworkAccess": "Enabled"},
    )
