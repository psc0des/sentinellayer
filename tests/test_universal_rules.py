"""Phase 40B — Universal rules library tests.

One test class per rule. Each class has at least a match and a no-match case.
Plus an integration test against the 311-resource fixture inventory.
"""

import json
import pathlib
import pytest

from src.rules import evaluate_inventory
from src.rules.base import Category, Finding, Severity
from src.rules.inventory_index import InventoryIndex

_FIXTURE = pathlib.Path(__file__).parent / "fixtures" / "inventory_311.json"

_BASE_RID = "/subscriptions/00000000/resourceGroups/rg-test/providers"


def _res(rtype: str, name: str, props: dict = None, **extra) -> dict:
    rid = f"{_BASE_RID}/{rtype}/{name}"
    return {"id": rid, "type": rtype, "name": name, "properties": props or {}, **extra}


def _run_rule(rule_id: str, resources: list) -> list[Finding]:
    findings = evaluate_inventory(resources)
    return [f for f in findings if f.rule_id == rule_id]


# ---------------------------------------------------------------------------
# UNIV-SEC-001 — Public Network Access
# ---------------------------------------------------------------------------

class TestPublicNetworkAccess:
    def test_matches_storage_with_public_access_enabled(self):
        res = _res(
            "Microsoft.Storage/storageAccounts", "stpublic",
            {"publicNetworkAccess": "Enabled", "provisioningState": "Succeeded"},
        )
        assert _run_rule("UNIV-SEC-001", [res])

    def test_no_match_when_disabled(self):
        res = _res(
            "Microsoft.Storage/storageAccounts", "stprivate",
            {"publicNetworkAccess": "Disabled"},
        )
        assert not _run_rule("UNIV-SEC-001", [res])

    def test_no_match_when_property_absent(self):
        res = _res("Microsoft.Storage/storageAccounts", "stnofield", {})
        assert not _run_rule("UNIV-SEC-001", [res])

    def test_no_match_non_data_type(self):
        res = _res(
            "Microsoft.Compute/virtualMachines", "vmfoo",
            {"publicNetworkAccess": "Enabled"},
        )
        assert not _run_rule("UNIV-SEC-001", [res])


# ---------------------------------------------------------------------------
# UNIV-SEC-002 — Minimum TLS Version
# ---------------------------------------------------------------------------

class TestMinimumTlsVersion:
    def test_matches_storage_tls10(self):
        res = _res(
            "Microsoft.Storage/storageAccounts", "sttls10",
            {"minimumTlsVersion": "TLS1_0"},
        )
        assert _run_rule("UNIV-SEC-002", [res])

    def test_matches_storage_tls11(self):
        res = _res(
            "Microsoft.Storage/storageAccounts", "sttls11",
            {"minimumTlsVersion": "TLS1_1"},
        )
        assert _run_rule("UNIV-SEC-002", [res])

    def test_no_match_tls12(self):
        res = _res(
            "Microsoft.Storage/storageAccounts", "sttls12",
            {"minimumTlsVersion": "TLS1_2"},
        )
        assert not _run_rule("UNIV-SEC-002", [res])

    def test_no_match_no_field(self):
        res = _res("Microsoft.Storage/storageAccounts", "stnofield", {})
        assert not _run_rule("UNIV-SEC-002", [res])


# ---------------------------------------------------------------------------
# UNIV-SEC-003 — Storage HTTPS Only
# ---------------------------------------------------------------------------

class TestStorageHttpsOnly:
    def test_matches_when_false(self):
        res = _res(
            "Microsoft.Storage/storageAccounts", "sthttp",
            {"supportsHttpsTrafficOnly": False},
        )
        assert _run_rule("UNIV-SEC-003", [res])

    def test_no_match_when_true(self):
        res = _res(
            "Microsoft.Storage/storageAccounts", "sthttps",
            {"supportsHttpsTrafficOnly": True},
        )
        assert not _run_rule("UNIV-SEC-003", [res])

    def test_no_match_when_none(self):
        res = _res("Microsoft.Storage/storageAccounts", "stnofield", {})
        assert not _run_rule("UNIV-SEC-003", [res])


# ---------------------------------------------------------------------------
# UNIV-SEC-004 — Blob Public Access
# ---------------------------------------------------------------------------

class TestStorageBlobPublicAccess:
    def test_matches_when_true(self):
        res = _res(
            "Microsoft.Storage/storageAccounts", "stpub",
            {"allowBlobPublicAccess": True},
        )
        assert _run_rule("UNIV-SEC-004", [res])

    def test_no_match_when_false(self):
        res = _res(
            "Microsoft.Storage/storageAccounts", "stpriv",
            {"allowBlobPublicAccess": False},
        )
        assert not _run_rule("UNIV-SEC-004", [res])


# ---------------------------------------------------------------------------
# UNIV-SEC-005 — KeyVault Soft Delete
# ---------------------------------------------------------------------------

class TestKeyvaultSoftDelete:
    def test_matches_when_false(self):
        res = _res(
            "Microsoft.KeyVault/vaults", "kv-nosd",
            {"enableSoftDelete": False},
        )
        assert _run_rule("UNIV-SEC-005", [res])

    def test_matches_when_none(self):
        res = _res("Microsoft.KeyVault/vaults", "kv-nofield", {})
        assert _run_rule("UNIV-SEC-005", [res])

    def test_no_match_when_true(self):
        res = _res(
            "Microsoft.KeyVault/vaults", "kv-ok",
            {"enableSoftDelete": True},
        )
        assert not _run_rule("UNIV-SEC-005", [res])


# ---------------------------------------------------------------------------
# UNIV-SEC-006 — KeyVault Purge Protection
# ---------------------------------------------------------------------------

class TestKeyvaultPurgeProtection:
    def test_matches_when_false(self):
        res = _res(
            "Microsoft.KeyVault/vaults", "kv-nopp",
            {"enablePurgeProtection": False},
        )
        assert _run_rule("UNIV-SEC-006", [res])

    def test_matches_when_none(self):
        res = _res("Microsoft.KeyVault/vaults", "kv-nofield", {})
        assert _run_rule("UNIV-SEC-006", [res])

    def test_no_match_when_true(self):
        res = _res(
            "Microsoft.KeyVault/vaults", "kv-ok",
            {"enablePurgeProtection": True},
        )
        assert not _run_rule("UNIV-SEC-006", [res])


# ---------------------------------------------------------------------------
# UNIV-SEC-007 — Managed Identity Missing
# ---------------------------------------------------------------------------

class TestManagedIdentityMissing:
    def test_matches_vm_with_no_identity(self):
        res = _res(
            "Microsoft.Compute/virtualMachines", "vm-noid",
            {"provisioningState": "Succeeded"},
            identity={},
        )
        assert _run_rule("UNIV-SEC-007", [res])

    def test_no_match_system_assigned(self):
        res = _res(
            "Microsoft.Compute/virtualMachines", "vm-systemid",
            {},
            identity={"type": "SystemAssigned"},
        )
        assert not _run_rule("UNIV-SEC-007", [res])

    def test_no_match_user_assigned(self):
        res = _res(
            "Microsoft.Compute/virtualMachines", "vm-userid",
            {},
            identity={"type": "UserAssigned"},
        )
        assert not _run_rule("UNIV-SEC-007", [res])


# ---------------------------------------------------------------------------
# UNIV-SEC-008 — ACR Anonymous Pull
# ---------------------------------------------------------------------------

class TestAcrAnonymousPull:
    def test_matches_when_enabled(self):
        res = _res(
            "Microsoft.ContainerRegistry/registries", "acranon",
            {"anonymousPullEnabled": True},
        )
        assert _run_rule("UNIV-SEC-008", [res])

    def test_no_match_when_false(self):
        res = _res(
            "Microsoft.ContainerRegistry/registries", "acrpriv",
            {"anonymousPullEnabled": False},
        )
        assert not _run_rule("UNIV-SEC-008", [res])


# ---------------------------------------------------------------------------
# UNIV-SEC-009 — ACR Admin User
# ---------------------------------------------------------------------------

class TestAcrAdminUser:
    def test_matches_when_enabled(self):
        res = _res(
            "Microsoft.ContainerRegistry/registries", "acradmin",
            {"adminUserEnabled": True},
        )
        assert _run_rule("UNIV-SEC-009", [res])

    def test_no_match_when_disabled(self):
        res = _res(
            "Microsoft.ContainerRegistry/registries", "acrnoadmin",
            {"adminUserEnabled": False},
        )
        assert not _run_rule("UNIV-SEC-009", [res])


# ---------------------------------------------------------------------------
# UNIV-SEC-010 — Web HTTPS Only
# ---------------------------------------------------------------------------

class TestWebHttpsOnly:
    def test_matches_when_false(self):
        res = _res("Microsoft.Web/sites", "app-nossl", {"httpsOnly": False})
        assert _run_rule("UNIV-SEC-010", [res])

    def test_no_match_when_true(self):
        res = _res("Microsoft.Web/sites", "app-ssl", {"httpsOnly": True})
        assert not _run_rule("UNIV-SEC-010", [res])

    def test_no_match_when_none(self):
        res = _res("Microsoft.Web/sites", "app-nofield", {})
        assert not _run_rule("UNIV-SEC-010", [res])


# ---------------------------------------------------------------------------
# UNIV-SEC-011 — Web FTP State
# ---------------------------------------------------------------------------

class TestWebFtpsState:
    def test_matches_allallowed(self):
        res = _res(
            "Microsoft.Web/sites", "app-ftp",
            {"siteConfig": {"ftpsState": "AllAllowed"}},
        )
        assert _run_rule("UNIV-SEC-011", [res])

    def test_no_match_ftpsonly(self):
        res = _res(
            "Microsoft.Web/sites", "app-ftps",
            {"siteConfig": {"ftpsState": "FtpsOnly"}},
        )
        assert not _run_rule("UNIV-SEC-011", [res])

    def test_no_match_disabled(self):
        res = _res(
            "Microsoft.Web/sites", "app-noftp",
            {"siteConfig": {"ftpsState": "Disabled"}},
        )
        assert not _run_rule("UNIV-SEC-011", [res])


# ---------------------------------------------------------------------------
# UNIV-COST-001 — Deallocated VM
# ---------------------------------------------------------------------------

class TestDeallocatedVm:
    def test_matches_deallocated(self):
        res = _res(
            "Microsoft.Compute/virtualMachines", "vm-dead",
            {"provisioningState": "Succeeded"},
            powerState="VM deallocated",
        )
        assert _run_rule("UNIV-COST-001", [res])

    def test_no_match_running(self):
        res = _res(
            "Microsoft.Compute/virtualMachines", "vm-run",
            {"provisioningState": "Succeeded"},
            powerState="VM running",
        )
        assert not _run_rule("UNIV-COST-001", [res])

    def test_no_match_no_power_state(self):
        res = _res(
            "Microsoft.Compute/virtualMachines", "vm-nops",
            {"provisioningState": "Succeeded"},
        )
        assert not _run_rule("UNIV-COST-001", [res])


# ---------------------------------------------------------------------------
# UNIV-COST-002 — Unattached Disk
# ---------------------------------------------------------------------------

class TestUnattachedDisk:
    def test_matches_unattached_unreferenced(self):
        res = {
            "id": "/subscriptions/00000000/resourceGroups/rg-test/providers/Microsoft.Compute/disks/orphan-disk",
            "type": "Microsoft.Compute/disks",
            "name": "orphan-disk",
            "sku": {"name": "Premium_LRS"},
            "properties": {"diskState": "Unattached", "diskSizeGB": 64, "provisioningState": "Succeeded"},
        }
        assert _run_rule("UNIV-COST-002", [res])

    def test_no_match_attached(self):
        res = {
            "id": "/subscriptions/00000000/resourceGroups/rg-test/providers/Microsoft.Compute/disks/attached-disk",
            "type": "Microsoft.Compute/disks",
            "name": "attached-disk",
            "sku": {"name": "Premium_LRS"},
            "properties": {"diskState": "Attached", "diskSizeGB": 64},
        }
        assert not _run_rule("UNIV-COST-002", [res])

    def test_no_match_referenced_disk(self):
        disk_id = "/subscriptions/00000000/resourceGroups/rg-test/providers/Microsoft.Compute/disks/managed-disk"
        disk = {
            "id": disk_id,
            "type": "Microsoft.Compute/disks",
            "name": "managed-disk",
            "properties": {"diskState": "Unattached", "diskSizeGB": 32},
        }
        vm = {
            "id": "/subscriptions/00000000/resourceGroups/rg-test/providers/Microsoft.Compute/virtualMachines/vm1",
            "type": "Microsoft.Compute/virtualMachines",
            "name": "vm1",
            "properties": {"storageProfile": {"dataDisks": [{"managedDisk": {"id": disk_id}}]}},
        }
        assert not _run_rule("UNIV-COST-002", [disk, vm])


# ---------------------------------------------------------------------------
# UNIV-COST-003 — Unassociated Public IP
# ---------------------------------------------------------------------------

class TestUnassociatedPublicIp:
    def test_matches_static_unassociated(self):
        res = _res(
            "Microsoft.Network/publicIPAddresses", "pip-orphan",
            {"publicIPAllocationMethod": "Static", "ipAddress": "10.0.0.1"},
        )
        assert _run_rule("UNIV-COST-003", [res])

    def test_no_match_attached(self):
        res = _res(
            "Microsoft.Network/publicIPAddresses", "pip-attached",
            {
                "publicIPAllocationMethod": "Static",
                "ipAddress": "10.0.0.2",
                "ipConfiguration": {"id": "/some/nic/config"},
            },
        )
        assert not _run_rule("UNIV-COST-003", [res])

    def test_no_match_dynamic_no_ip(self):
        res = _res(
            "Microsoft.Network/publicIPAddresses", "pip-dyn",
            {"publicIPAllocationMethod": "Dynamic"},
        )
        assert not _run_rule("UNIV-COST-003", [res])


# ---------------------------------------------------------------------------
# UNIV-COST-004 — Unattached NIC
# ---------------------------------------------------------------------------

class TestUnattachedNic:
    def test_matches_nic_with_no_vm(self):
        res = _res("Microsoft.Network/networkInterfaces", "nic-orphan", {})
        assert _run_rule("UNIV-COST-004", [res])

    def test_no_match_nic_attached_to_vm(self):
        res = _res(
            "Microsoft.Network/networkInterfaces", "nic-attached",
            {"virtualMachine": {"id": "/subscriptions/00000000/resourceGroups/rg/providers/Microsoft.Compute/virtualMachines/vm1"}},
        )
        assert not _run_rule("UNIV-COST-004", [res])


# ---------------------------------------------------------------------------
# UNIV-COST-005 — Old Unattached Snapshot
# ---------------------------------------------------------------------------

class TestOldUnattachedSnapshot:
    def test_matches_old_snapshot(self):
        res = _res(
            "Microsoft.Compute/snapshots", "snap-old",
            {"diskSizeGB": 32, "timeCreated": "2023-01-01T00:00:00Z"},
        )
        assert _run_rule("UNIV-COST-005", [res])

    def test_no_match_recent_snapshot(self):
        from datetime import datetime, timezone, timedelta
        recent = (datetime.now(timezone.utc) - timedelta(days=10)).strftime("%Y-%m-%dT%H:%M:%SZ")
        res = _res(
            "Microsoft.Compute/snapshots", "snap-new",
            {"diskSizeGB": 32, "timeCreated": recent},
        )
        assert not _run_rule("UNIV-COST-005", [res])

    def test_no_match_no_date(self):
        res = _res("Microsoft.Compute/snapshots", "snap-nodate", {"diskSizeGB": 32})
        assert not _run_rule("UNIV-COST-005", [res])


# ---------------------------------------------------------------------------
# UNIV-COST-006 — Empty Recovery Vault
# ---------------------------------------------------------------------------

class TestEmptyRecoveryVault:
    def test_matches_zero_protected(self):
        res = _res(
            "Microsoft.RecoveryServices/vaults", "rv-empty",
            {"protectedItemCount": 0},
        )
        assert _run_rule("UNIV-COST-006", [res])

    def test_no_match_with_items(self):
        res = _res(
            "Microsoft.RecoveryServices/vaults", "rv-full",
            {"protectedItemCount": 5},
        )
        assert not _run_rule("UNIV-COST-006", [res])

    def test_no_match_count_none(self):
        res = _res("Microsoft.RecoveryServices/vaults", "rv-unknown", {})
        assert not _run_rule("UNIV-COST-006", [res])


# ---------------------------------------------------------------------------
# UNIV-COST-007 — Deprecated OMS Solution
# ---------------------------------------------------------------------------

class TestDeprecatedOmsSolution:
    def test_matches_security_solution(self):
        res = {
            "id": f"{_BASE_RID}/Microsoft.OperationsManagement/solutions/Security(la-workspace)",
            "type": "Microsoft.OperationsManagement/solutions",
            "name": "Security(la-workspace)",
            "properties": {"provisioningState": "Succeeded"},
        }
        assert _run_rule("UNIV-COST-007", [res])

    def test_matches_wiredata_solution(self):
        res = {
            "id": f"{_BASE_RID}/Microsoft.OperationsManagement/solutions/WireData(ws)",
            "type": "Microsoft.OperationsManagement/solutions",
            "name": "WireData(ws)",
            "properties": {},
        }
        assert _run_rule("UNIV-COST-007", [res])

    def test_no_match_non_deprecated(self):
        res = {
            "id": f"{_BASE_RID}/Microsoft.OperationsManagement/solutions/Updates(ws)",
            "type": "Microsoft.OperationsManagement/solutions",
            "name": "Updates(ws)",
            "properties": {},
        }
        assert not _run_rule("UNIV-COST-007", [res])


# ---------------------------------------------------------------------------
# UNIV-REL-001 — Provisioning State Failed
# ---------------------------------------------------------------------------

class TestProvisioningStateFailed:
    def test_matches_failed_state(self):
        res = _res("Microsoft.Compute/virtualMachines", "vm-fail", {"provisioningState": "Failed"})
        assert _run_rule("UNIV-REL-001", [res])

    def test_matches_canceled_state(self):
        res = _res("Microsoft.Storage/storageAccounts", "st-canceled", {"provisioningState": "Canceled"})
        assert _run_rule("UNIV-REL-001", [res])

    def test_no_match_succeeded(self):
        res = _res("Microsoft.Compute/virtualMachines", "vm-ok", {"provisioningState": "Succeeded"})
        assert not _run_rule("UNIV-REL-001", [res])

    def test_no_match_no_field(self):
        res = _res("Microsoft.Storage/storageAccounts", "st-nofield", {})
        assert not _run_rule("UNIV-REL-001", [res])


# ---------------------------------------------------------------------------
# UNIV-REL-002 — Single Replica Container App
# ---------------------------------------------------------------------------

class TestSingleReplicaContainerApp:
    def test_matches_max_replicas_1(self):
        res = _res(
            "Microsoft.App/containerApps", "app-single",
            {"template": {"scale": {"minReplicas": 1, "maxReplicas": 1}}},
        )
        assert _run_rule("UNIV-REL-002", [res])

    def test_no_match_max_replicas_3(self):
        res = _res(
            "Microsoft.App/containerApps", "app-ha",
            {"template": {"scale": {"minReplicas": 1, "maxReplicas": 3}}},
        )
        assert not _run_rule("UNIV-REL-002", [res])

    def test_no_match_default_scale(self):
        # Default maxReplicas (not set) is treated as 10 — no finding
        res = _res(
            "Microsoft.App/containerApps", "app-default",
            {"template": {}},
        )
        assert not _run_rule("UNIV-REL-002", [res])


# ---------------------------------------------------------------------------
# UNIV-REL-003 — Free/Basic SKU in Prod
# ---------------------------------------------------------------------------

class TestFreeBasicSkuInProd:
    def test_matches_basic_acr_in_prod_rg(self):
        res = {
            "id": "/subscriptions/00000000/resourceGroups/rg-prod/providers/Microsoft.ContainerRegistry/registries/acr",
            "type": "Microsoft.ContainerRegistry/registries",
            "name": "acr",
            "sku": {"name": "Basic"},
            "properties": {},
        }
        assert _run_rule("UNIV-REL-003", [res])

    def test_no_match_standard_acr_in_prod_rg(self):
        res = {
            "id": "/subscriptions/00000000/resourceGroups/rg-prod/providers/Microsoft.ContainerRegistry/registries/acr",
            "type": "Microsoft.ContainerRegistry/registries",
            "name": "acr",
            "sku": {"name": "Standard"},
            "properties": {},
        }
        assert not _run_rule("UNIV-REL-003", [res])

    def test_no_match_basic_in_dev_rg(self):
        res = {
            "id": "/subscriptions/00000000/resourceGroups/rg-dev/providers/Microsoft.ContainerRegistry/registries/acr",
            "type": "Microsoft.ContainerRegistry/registries",
            "name": "acr",
            "sku": {"name": "Basic"},
            "properties": {},
        }
        assert not _run_rule("UNIV-REL-003", [res])


# ---------------------------------------------------------------------------
# UNIV-REL-004 — Diagnostic Settings Missing
# ---------------------------------------------------------------------------

class TestDiagnosticSettingsMissing:
    def test_matches_keyvault_without_diag(self):
        res = {
            "id": "/subscriptions/00000000/resourceGroups/rg/providers/Microsoft.KeyVault/vaults/kv1",
            "type": "Microsoft.KeyVault/vaults",
            "name": "kv1",
            "properties": {},
        }
        assert _run_rule("UNIV-REL-004", [res])

    def test_no_match_when_diag_child_present(self):
        kv = {
            "id": "/subscriptions/00000000/resourceGroups/rg/providers/Microsoft.KeyVault/vaults/kv1",
            "type": "Microsoft.KeyVault/vaults",
            "name": "kv1",
            "properties": {},
        }
        diag = {
            "id": "/subscriptions/00000000/resourceGroups/rg/providers/Microsoft.KeyVault/vaults/kv1/providers/Microsoft.Insights/diagnosticSettings/diag1",
            "type": "Microsoft.Insights/diagnosticSettings",
            "name": "diag1",
            "properties": {},
        }
        assert not _run_rule("UNIV-REL-004", [kv, diag])


# ---------------------------------------------------------------------------
# UNIV-REL-005 — VM Missing AMA Extension
# ---------------------------------------------------------------------------

class TestVmMissingAmaExtension:
    def test_matches_vm_without_ama(self):
        res = _res(
            "Microsoft.Compute/virtualMachines", "vm-noama",
            {"provisioningState": "Succeeded"},
        )
        assert _run_rule("UNIV-REL-005", [res])

    def test_no_match_vm_with_ama_child(self):
        vm_id = "/subscriptions/00000000/resourceGroups/rg/providers/Microsoft.Compute/virtualMachines/vm-ama"
        vm = {
            "id": vm_id,
            "type": "Microsoft.Compute/virtualMachines",
            "name": "vm-ama",
            "properties": {},
        }
        ext = {
            "id": f"{vm_id}/extensions/AzureMonitorWindowsAgent",
            "type": "Microsoft.Compute/virtualMachines/extensions",
            "name": "AzureMonitorWindowsAgent",
            "properties": {},
        }
        assert not _run_rule("UNIV-REL-005", [vm, ext])


# ---------------------------------------------------------------------------
# UNIV-REL-006 — Cosmos DB Single Region
# ---------------------------------------------------------------------------

class TestCosmosDbSingleRegion:
    def test_matches_single_region(self):
        res = _res(
            "Microsoft.DocumentDB/databaseAccounts", "cosmos-single",
            {"locations": [{"locationName": "East US", "failoverPriority": 0}]},
        )
        assert _run_rule("UNIV-REL-006", [res])

    def test_no_match_multi_region(self):
        res = _res(
            "Microsoft.DocumentDB/databaseAccounts", "cosmos-multi",
            {"locations": [{"locationName": "East US"}, {"locationName": "West US"}]},
        )
        assert not _run_rule("UNIV-REL-006", [res])

    def test_no_match_multi_write(self):
        res = _res(
            "Microsoft.DocumentDB/databaseAccounts", "cosmos-mw",
            {"locations": [{"locationName": "East US"}], "enableMultipleWriteLocations": True},
        )
        assert not _run_rule("UNIV-REL-006", [res])


# ---------------------------------------------------------------------------
# UNIV-HYG-001 — Zero Tags
# ---------------------------------------------------------------------------

class TestZeroTags:
    def test_matches_resource_with_no_tags(self):
        res = _res("Microsoft.Compute/virtualMachines", "vm-notag", {})
        assert _run_rule("UNIV-HYG-001", [res])

    def test_matches_empty_tags_dict(self):
        res = {
            "id": f"{_BASE_RID}/Microsoft.Compute/virtualMachines/vm-emptytag",
            "type": "Microsoft.Compute/virtualMachines",
            "name": "vm-emptytag",
            "tags": {},
            "properties": {},
        }
        assert _run_rule("UNIV-HYG-001", [res])

    def test_no_match_resource_with_tags(self):
        res = {
            "id": f"{_BASE_RID}/Microsoft.Compute/virtualMachines/vm-tagged",
            "type": "Microsoft.Compute/virtualMachines",
            "name": "vm-tagged",
            "tags": {"owner": "alice", "env": "prod"},
            "properties": {},
        }
        assert not _run_rule("UNIV-HYG-001", [res])

    def test_skips_extension_type(self):
        res = {
            "id": f"{_BASE_RID}/Microsoft.Compute/virtualMachines/vm1/extensions/ext1",
            "type": "Microsoft.Compute/virtualMachines/extensions",
            "name": "ext1",
            "properties": {},
        }
        assert not _run_rule("UNIV-HYG-001", [res])


# ---------------------------------------------------------------------------
# UNIV-HYG-002 — Missing Required Tags
# ---------------------------------------------------------------------------

class TestMissingRequiredTags:
    def test_matches_missing_owner(self):
        res = {
            "id": f"{_BASE_RID}/Microsoft.Compute/virtualMachines/vm-partialTag",
            "type": "Microsoft.Compute/virtualMachines",
            "name": "vm-partialTag",
            "tags": {"environment": "prod", "costcenter": "eng"},
            "properties": {},
        }
        findings = _run_rule("UNIV-HYG-002", [res])
        assert findings
        assert "owner" in findings[0].evidence["missing_tags"]

    def test_no_match_all_required_present(self):
        res = {
            "id": f"{_BASE_RID}/Microsoft.Compute/virtualMachines/vm-fulltag",
            "type": "Microsoft.Compute/virtualMachines",
            "name": "vm-fulltag",
            "tags": {"owner": "alice", "environment": "prod", "costcenter": "eng"},
            "properties": {},
        }
        assert not _run_rule("UNIV-HYG-002", [res])

    def test_no_match_fully_untagged(self):
        # Fully untagged resources are caught by UNIV-HYG-001; this rule skips them
        res = {
            "id": f"{_BASE_RID}/Microsoft.Compute/virtualMachines/vm-notag",
            "type": "Microsoft.Compute/virtualMachines",
            "name": "vm-notag",
            "tags": {},
            "properties": {},
        }
        assert not _run_rule("UNIV-HYG-002", [res])


# ---------------------------------------------------------------------------
# Integration test — 311-resource fixture
# ---------------------------------------------------------------------------

def _group_by_category(findings: list) -> dict:
    result: dict = {}
    for f in findings:
        result.setdefault(f.category, []).append(f)
    return result


def test_full_coverage_against_real_inventory():
    data = json.loads(_FIXTURE.read_text())
    resources = data["resources"]
    findings = evaluate_inventory(resources)
    by_cat = _group_by_category(findings)

    assert len(by_cat.get(Category.COST, [])) >= 6, (
        f"Expected ≥6 cost findings, got {len(by_cat.get(Category.COST, []))}"
    )
    assert len(by_cat.get(Category.SECURITY, [])) >= 8, (
        f"Expected ≥8 security findings, got {len(by_cat.get(Category.SECURITY, []))}"
    )
    assert len(by_cat.get(Category.RELIABILITY, [])) >= 5, (
        f"Expected ≥5 reliability findings, got {len(by_cat.get(Category.RELIABILITY, []))}"
    )
    assert len(by_cat.get(Category.HYGIENE, [])) >= 3, (
        f"Expected ≥3 hygiene findings, got {len(by_cat.get(Category.HYGIENE, []))}"
    )

    # Specific must-finds
    assert any(
        f.rule_id == "UNIV-COST-001" and "Logic-OpenVPN" in f.resource_name
        for f in findings
    ), "Must find deallocated VM 'Logic-OpenVPN'"

    assert any(
        f.rule_id == "UNIV-COST-002" and "OrceDataBase_DataDisk_0" in f.resource_name
        for f in findings
    ), "Must find unattached disk 'OrceDataBase_DataDisk_0'"
