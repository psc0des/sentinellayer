"""UNIV-SEC-011 — App Service FTP state should be FTPS-only or disabled.

Plain FTP transmits credentials and file content in clear text. ftpsState should be
'FtpsOnly' (TLS-encrypted FTP) or 'Disabled'. 'AllAllowed' permits unencrypted FTP.
"""
from src.rules.base import Category, Finding, Severity, rule
from src.rules.inventory_index import InventoryIndex


@rule(
    id="UNIV-SEC-011",
    name="App Service Allows Unencrypted FTP",
    category=Category.SECURITY,
    severity=Severity.MEDIUM,
    applies_to=["microsoft.web/sites"],
)
def evaluate(resource: dict, idx: InventoryIndex) -> Finding | None:
    props = resource.get("properties") or {}
    site_config = props.get("siteConfig") or {}
    ftps_state = (site_config.get("ftpsState") or "").lower()
    if ftps_state in ("ftpsonly", "disabled", ""):
        return None
    name = resource.get("name", "")
    return Finding(
        rule_id="UNIV-SEC-011",
        rule_name="App Service Allows Unencrypted FTP",
        category=Category.SECURITY,
        severity=Severity.MEDIUM,
        resource_id=resource["id"],
        resource_type=resource["type"],
        resource_name=name,
        reason=(
            f"App Service '{name}' has ftpsState='{ftps_state}', allowing unencrypted FTP "
            "connections. Change to 'FtpsOnly' or 'Disabled' to prevent credential exposure."
        ),
        recommended_action="update_config",
        evidence={"ftpsState": ftps_state},
    )
