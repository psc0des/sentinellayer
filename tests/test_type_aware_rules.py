"""Phase 40C — Type-aware rules tests.

One test class per rule. Each has a match and a no-match case.
NSG rules use realistic securityRules array structures.
"""

import pytest

from src.rules import evaluate_inventory
from src.rules.base import Finding

_BASE_RID = "/subscriptions/00000000-0000-0000-0000-000000000001/resourceGroups/rg-test/providers"


def _run_rule(rule_id: str, resources: list) -> list[Finding]:
    findings = evaluate_inventory(resources)
    return [f for f in findings if f.rule_id == rule_id]


def _nsg(name: str, rules: list) -> dict:
    return {
        "id": f"{_BASE_RID}/Microsoft.Network/networkSecurityGroups/{name}",
        "type": "Microsoft.Network/networkSecurityGroups",
        "name": name,
        "properties": {"securityRules": rules, "provisioningState": "Succeeded"},
    }


def _inbound_allow(rule_name: str, src: str, port: str, proto: str = "TCP") -> dict:
    return {
        "name": rule_name,
        "properties": {
            "direction": "Inbound",
            "access": "Allow",
            "priority": 100,
            "protocol": proto,
            "sourceAddressPrefix": src,
            "destinationPortRange": port,
        },
    }


# ---------------------------------------------------------------------------
# TYPE-NSG-001 — Internet Exposed Management Ports
# ---------------------------------------------------------------------------

class TestNsgInternetExposedMgmtPorts:
    def test_matches_ssh_from_any(self):
        nsg = _nsg("nsg-ssh-open", [_inbound_allow("AllowSSH", "*", "22")])
        findings = _run_rule("TYPE-NSG-001", [nsg])
        assert findings
        assert findings[0].severity.value == "critical"

    def test_matches_rdp_from_internet(self):
        nsg = _nsg("nsg-rdp-open", [_inbound_allow("AllowRDP", "Internet", "3389")])
        assert _run_rule("TYPE-NSG-001", [nsg])

    def test_matches_port_range_covering_ssh(self):
        rule = _inbound_allow("AllowRange", "*", "20-25")
        nsg = _nsg("nsg-range", [rule])
        assert _run_rule("TYPE-NSG-001", [nsg])

    def test_no_match_specific_source(self):
        rule = _inbound_allow("AllowOffice", "10.1.2.3/32", "22")
        nsg = _nsg("nsg-office", [rule])
        assert not _run_rule("TYPE-NSG-001", [nsg])

    def test_no_match_non_mgmt_port(self):
        rule = _inbound_allow("AllowHTTPS", "*", "443")
        nsg = _nsg("nsg-https", [rule])
        assert not _run_rule("TYPE-NSG-001", [nsg])

    def test_no_match_outbound(self):
        rule = {
            "name": "OutSSH",
            "properties": {
                "direction": "Outbound",
                "access": "Allow",
                "priority": 100,
                "protocol": "TCP",
                "sourceAddressPrefix": "*",
                "destinationPortRange": "22",
            },
        }
        nsg = _nsg("nsg-out", [rule])
        assert not _run_rule("TYPE-NSG-001", [nsg])


# ---------------------------------------------------------------------------
# TYPE-NSG-002 — Wildcard Internet Exposure
# ---------------------------------------------------------------------------

class TestNsgWildcardInternetExposure:
    def test_matches_any_to_any(self):
        rule = _inbound_allow("AllowAll", "*", "*", proto="*")
        nsg = _nsg("nsg-any-any", [rule])
        assert _run_rule("TYPE-NSG-002", [nsg])

    def test_no_match_specific_port(self):
        rule = _inbound_allow("AllowHTTP", "*", "80")
        nsg = _nsg("nsg-http", [rule])
        assert not _run_rule("TYPE-NSG-002", [nsg])

    def test_no_match_deny_wildcard(self):
        rule = {
            "name": "DenyAll",
            "properties": {
                "direction": "Inbound",
                "access": "Deny",
                "priority": 4000,
                "protocol": "*",
                "sourceAddressPrefix": "*",
                "destinationPortRange": "*",
            },
        }
        nsg = _nsg("nsg-deny", [rule])
        assert not _run_rule("TYPE-NSG-002", [nsg])

    def test_no_match_empty_rules(self):
        nsg = _nsg("nsg-empty", [])
        assert not _run_rule("TYPE-NSG-002", [nsg])


# ---------------------------------------------------------------------------
# TYPE-NSG-003 — Missing Deny-All Inbound
# ---------------------------------------------------------------------------

class TestNsgMissingDenyAllInbound:
    def test_matches_custom_allow_no_deny(self):
        rule = _inbound_allow("AllowApp", "10.0.0.0/8", "8080")
        nsg = _nsg("nsg-nodenyall", [rule])
        assert _run_rule("TYPE-NSG-003", [nsg])

    def test_no_match_has_deny_all(self):
        allow_rule = _inbound_allow("AllowApp", "10.0.0.0/8", "8080")
        deny_rule = {
            "name": "DenyAll",
            "properties": {
                "direction": "Inbound",
                "access": "Deny",
                "priority": 4096,
                "protocol": "*",
                "sourceAddressPrefix": "*",
                "destinationPortRange": "*",
            },
        }
        nsg = _nsg("nsg-withdeny", [allow_rule, deny_rule])
        assert not _run_rule("TYPE-NSG-003", [nsg])

    def test_no_match_no_custom_rules(self):
        nsg = _nsg("nsg-empty", [])
        assert not _run_rule("TYPE-NSG-003", [nsg])


# ---------------------------------------------------------------------------
# TYPE-AKS-001 — Autoscaler Disabled
# ---------------------------------------------------------------------------

class TestAksAutoscalerDisabled:
    def test_matches_autoscaler_off(self):
        res = {
            "id": f"{_BASE_RID}/Microsoft.ContainerService/managedClusters/aks-noscale",
            "type": "Microsoft.ContainerService/managedClusters",
            "name": "aks-noscale",
            "properties": {
                "agentPoolProfiles": [
                    {"name": "nodepool1", "enableAutoScaling": False, "count": 3}
                ]
            },
        }
        assert _run_rule("TYPE-AKS-001", [res])

    def test_no_match_autoscaler_on(self):
        res = {
            "id": f"{_BASE_RID}/Microsoft.ContainerService/managedClusters/aks-scaled",
            "type": "Microsoft.ContainerService/managedClusters",
            "name": "aks-scaled",
            "properties": {
                "agentPoolProfiles": [
                    {"name": "nodepool1", "enableAutoScaling": True, "minCount": 2, "maxCount": 10}
                ]
            },
        }
        assert not _run_rule("TYPE-AKS-001", [res])


# ---------------------------------------------------------------------------
# TYPE-AKS-002 — Outdated Kubernetes Version
# ---------------------------------------------------------------------------

class TestAksOutdatedVersion:
    def test_matches_old_version(self):
        res = {
            "id": f"{_BASE_RID}/Microsoft.ContainerService/managedClusters/aks-old",
            "type": "Microsoft.ContainerService/managedClusters",
            "name": "aks-old",
            "properties": {"kubernetesVersion": "1.26.6", "agentPoolProfiles": []},
        }
        assert _run_rule("TYPE-AKS-002", [res])

    def test_no_match_current_version(self):
        res = {
            "id": f"{_BASE_RID}/Microsoft.ContainerService/managedClusters/aks-new",
            "type": "Microsoft.ContainerService/managedClusters",
            "name": "aks-new",
            "properties": {"kubernetesVersion": "1.30.0", "agentPoolProfiles": []},
        }
        assert not _run_rule("TYPE-AKS-002", [res])

    def test_no_match_no_version(self):
        res = {
            "id": f"{_BASE_RID}/Microsoft.ContainerService/managedClusters/aks-noversion",
            "type": "Microsoft.ContainerService/managedClusters",
            "name": "aks-noversion",
            "properties": {"agentPoolProfiles": []},
        }
        assert not _run_rule("TYPE-AKS-002", [res])


# ---------------------------------------------------------------------------
# TYPE-WEB-001 — Client Cert Not Required
# ---------------------------------------------------------------------------

class TestWebClientCertNotRequired:
    def test_matches_api_without_client_cert(self):
        res = {
            "id": f"{_BASE_RID}/Microsoft.Web/sites/api-nocert",
            "type": "Microsoft.Web/sites",
            "name": "api-nocert",
            "kind": "app,linux,api",
            "properties": {"clientCertEnabled": False},
        }
        assert _run_rule("TYPE-WEB-001", [res])

    def test_no_match_api_with_client_cert(self):
        res = {
            "id": f"{_BASE_RID}/Microsoft.Web/sites/api-cert",
            "type": "Microsoft.Web/sites",
            "name": "api-cert",
            "kind": "app,api",
            "properties": {"clientCertEnabled": True},
        }
        assert not _run_rule("TYPE-WEB-001", [res])

    def test_no_match_non_api_app(self):
        # Regular web app (not API) — rule doesn't apply
        res = {
            "id": f"{_BASE_RID}/Microsoft.Web/sites/regular-app",
            "type": "Microsoft.Web/sites",
            "name": "regular-app",
            "kind": "app",
            "properties": {"clientCertEnabled": False},
        }
        assert not _run_rule("TYPE-WEB-001", [res])


# ---------------------------------------------------------------------------
# TYPE-SQL-001 — No Failover Group
# ---------------------------------------------------------------------------

class TestSqlNoFailoverGroup:
    def test_matches_sql_server_without_failover(self):
        res = {
            "id": f"{_BASE_RID}/Microsoft.Sql/servers/sql-nofailover",
            "type": "Microsoft.Sql/servers",
            "name": "sql-nofailover",
            "properties": {"provisioningState": "Succeeded"},
        }
        assert _run_rule("TYPE-SQL-001", [res])

    def test_no_match_sql_server_with_failover_group(self):
        server_id = f"{_BASE_RID}/Microsoft.Sql/servers/sql-withfailover"
        server = {
            "id": server_id,
            "type": "Microsoft.Sql/servers",
            "name": "sql-withfailover",
            "properties": {},
        }
        failover = {
            "id": f"{server_id}/failoverGroups/fg1",
            "type": "Microsoft.Sql/servers/failoverGroups",
            "name": "fg1",
            "properties": {},
        }
        assert not _run_rule("TYPE-SQL-001", [server, failover])


# ---------------------------------------------------------------------------
# TYPE-COSMOS-001 — Cosmos Automatic Failover Disabled
# ---------------------------------------------------------------------------

class TestCosmosNoGeoRedundancy:
    def test_matches_multi_region_no_auto_failover(self):
        res = {
            "id": f"{_BASE_RID}/Microsoft.DocumentDB/databaseAccounts/cosmos-nofailover",
            "type": "Microsoft.DocumentDB/databaseAccounts",
            "name": "cosmos-nofailover",
            "properties": {
                "enableAutomaticFailover": False,
                "locations": [
                    {"locationName": "East US"},
                    {"locationName": "West US"},
                ],
            },
        }
        assert _run_rule("TYPE-COSMOS-001", [res])

    def test_no_match_auto_failover_enabled(self):
        res = {
            "id": f"{_BASE_RID}/Microsoft.DocumentDB/databaseAccounts/cosmos-auto",
            "type": "Microsoft.DocumentDB/databaseAccounts",
            "name": "cosmos-auto",
            "properties": {
                "enableAutomaticFailover": True,
                "locations": [
                    {"locationName": "East US"},
                    {"locationName": "West US"},
                ],
            },
        }
        assert not _run_rule("TYPE-COSMOS-001", [res])

    def test_no_match_single_region(self):
        # Single-region is flagged by UNIV-REL-006; this rule defers
        res = {
            "id": f"{_BASE_RID}/Microsoft.DocumentDB/databaseAccounts/cosmos-single",
            "type": "Microsoft.DocumentDB/databaseAccounts",
            "name": "cosmos-single",
            "properties": {
                "enableAutomaticFailover": False,
                "locations": [{"locationName": "East US"}],
            },
        }
        assert not _run_rule("TYPE-COSMOS-001", [res])
