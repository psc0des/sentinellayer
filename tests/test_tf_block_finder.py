"""Tests for src/core/tf_block_finder.py — block finder and attribute helpers."""

import pytest
from src.core.tf_block_finder import (
    ARM_TO_TF_TYPE,
    TF_SKU_ATTRIBUTE,
    BlockMatch,
    TfFile,
    _extract_blocks,
    _name_matches_exact,
    _name_matches_interpolated,
    _parse_tfvars,
    _resolve_interpolation,
    find_dangling_references,
    find_tf_block,
    get_attribute_value,
)


# =============================================================================
# Fixtures — sample Terraform file content
# =============================================================================

SERVICE_PLAN_TF = '''
resource "azurerm_service_plan" "prod" {
  name                = "asp-ruriskry-prod-${var.suffix}"
  resource_group_name = azurerm_resource_group.prod.name
  location            = azurerm_resource_group.prod.location
  os_type             = "Linux"
  sku_name            = "F1"
  tags                = local.common_tags
}
'''

SERVICE_PLAN_EXACT_TF = '''
resource "azurerm_service_plan" "staging" {
  name     = "asp-ruriskry-staging"
  sku_name = "B1"
}
'''

VM_TF = '''
resource "azurerm_linux_virtual_machine" "web01" {
  name                = "vm-web01-${var.env}"
  resource_group_name = "rg-prod"
  size                = "Standard_B1s"
}
'''

MULTI_BLOCK_TF = '''
resource "azurerm_service_plan" "staging" {
  name     = "asp-staging"
  sku_name = "B1"
}

resource "azurerm_service_plan" "prod" {
  name     = "asp-prod-demo"
  sku_name = "F1"
}
'''

TFVARS_CONTENT = 'suffix = "demo"\nenv    = "prod"\n'

VARIABLES_TF = '''
variable "suffix" {
  default = "demo"
}
variable "env" {
  default = "dev"
}
'''


def _make_file(path: str, content: str, sha: str = "abc123") -> TfFile:
    return TfFile(file_path=path, file_sha=sha, content=content)


# =============================================================================
# ARM_TO_TF_TYPE coverage
# =============================================================================

class TestArmToTfType:
    def test_web_serverfarms_maps_to_service_plan(self):
        types = ARM_TO_TF_TYPE.get("microsoft.web/serverfarms", [])
        assert "azurerm_service_plan" in types

    def test_compute_vms_maps_to_linux_vm(self):
        types = ARM_TO_TF_TYPE.get("microsoft.compute/virtualmachines", [])
        assert "azurerm_linux_virtual_machine" in types

    def test_sql_databases_mapped(self):
        types = ARM_TO_TF_TYPE.get("microsoft.sql/servers/databases", [])
        assert "azurerm_mssql_database" in types

    def test_storage_accounts_mapped(self):
        types = ARM_TO_TF_TYPE.get("microsoft.storage/storageaccounts", [])
        assert "azurerm_storage_account" in types

    def test_key_vault_mapped(self):
        types = ARM_TO_TF_TYPE.get("microsoft.keyvault/vaults", [])
        assert "azurerm_key_vault" in types

    def test_aks_mapped(self):
        types = ARM_TO_TF_TYPE.get("microsoft.containerservice/managedclusters", [])
        assert "azurerm_kubernetes_cluster" in types

    def test_nsg_mapped(self):
        types = ARM_TO_TF_TYPE.get("microsoft.network/networksecuritygroups", [])
        assert "azurerm_network_security_group" in types

    def test_unknown_type_returns_empty(self):
        types = ARM_TO_TF_TYPE.get("microsoft.unknown/widget", [])
        assert types == []


# =============================================================================
# TF_SKU_ATTRIBUTE coverage
# =============================================================================

class TestTfSkuAttribute:
    def test_service_plan_sku_attribute(self):
        assert TF_SKU_ATTRIBUTE["azurerm_service_plan"] == "sku_name"

    def test_linux_vm_size_attribute(self):
        assert TF_SKU_ATTRIBUTE["azurerm_linux_virtual_machine"] == "size"

    def test_windows_vm_size_attribute(self):
        assert TF_SKU_ATTRIBUTE["azurerm_windows_virtual_machine"] == "size"


# =============================================================================
# _extract_blocks
# =============================================================================

class TestExtractBlocks:
    def test_extracts_service_plan_block(self):
        blocks = _extract_blocks(SERVICE_PLAN_TF, "azurerm_service_plan")
        assert len(blocks) == 1
        b = blocks[0]
        assert b["logical_name"] == "prod"
        assert "asp-ruriskry-prod-" in b["fields"]["name"]
        assert b["fields"]["sku_name"] == "F1"

    def test_extracts_multiple_blocks(self):
        blocks = _extract_blocks(MULTI_BLOCK_TF, "azurerm_service_plan")
        assert len(blocks) == 2
        names = [b["logical_name"] for b in blocks]
        assert "staging" in names
        assert "prod" in names

    def test_wrong_type_returns_empty(self):
        blocks = _extract_blocks(SERVICE_PLAN_TF, "azurerm_virtual_machine")
        assert blocks == []

    def test_extracts_vm_block(self):
        blocks = _extract_blocks(VM_TF, "azurerm_linux_virtual_machine")
        assert len(blocks) == 1
        assert blocks[0]["logical_name"] == "web01"
        assert blocks[0]["fields"]["size"] == "Standard_B1s"


# =============================================================================
# _name_matches_exact
# =============================================================================

class TestNameMatchesExact:
    def test_exact_match(self):
        assert _name_matches_exact("asp-ruriskry-staging", "asp-ruriskry-staging")

    def test_case_insensitive(self):
        assert _name_matches_exact("ASP-Ruriskry-Staging", "asp-ruriskry-staging")

    def test_no_match(self):
        assert not _name_matches_exact("asp-ruriskry-staging", "asp-ruriskry-prod")

    def test_empty_strings(self):
        assert not _name_matches_exact("", "something")


# =============================================================================
# _name_matches_interpolated
# =============================================================================

class TestNameMatchesInterpolated:
    def test_suffix_interpolation(self):
        # "asp-ruriskry-prod-${var.suffix}" vs "asp-ruriskry-prod-demo"
        assert _name_matches_interpolated(
            "asp-ruriskry-prod-${var.suffix}", "asp-ruriskry-prod-demo"
        )

    def test_prefix_interpolation(self):
        # "${var.prefix}-prod-demo" vs "myprefix-prod-demo"
        assert _name_matches_interpolated(
            "${var.prefix}-prod-demo", "myprefix-prod-demo"
        )

    def test_middle_interpolation(self):
        # "${var.env}-app-${var.suffix}" vs "prod-app-v2"
        assert _name_matches_interpolated(
            "${var.env}-app-${var.suffix}", "prod-app-v2"
        )

    def test_no_interpolation_returns_false(self):
        # No ${} → not an interpolated name → falls to exact match pass
        assert not _name_matches_interpolated("asp-staging", "asp-staging")

    def test_static_segment_not_in_azure_name(self):
        assert not _name_matches_interpolated(
            "asp-ruriskry-prod-${var.suffix}", "totally-different-name"
        )

    def test_empty_input(self):
        assert not _name_matches_interpolated("", "something")


# =============================================================================
# _parse_tfvars and _resolve_interpolation
# =============================================================================

class TestTfvarsResolution:
    def test_parse_tfvars_file(self):
        files = [
            _make_file("terraform.tfvars", TFVARS_CONTENT),
        ]
        vars_dict = _parse_tfvars(files)
        assert vars_dict["suffix"] == "demo"
        assert vars_dict["env"] == "prod"

    def test_parse_variable_defaults(self):
        files = [_make_file("variables.tf", VARIABLES_TF)]
        vars_dict = _parse_tfvars(files)
        assert vars_dict["suffix"] == "demo"
        assert vars_dict["env"] == "dev"

    def test_tfvars_overrides_defaults(self):
        files = [
            _make_file("variables.tf", VARIABLES_TF),
            _make_file("terraform.tfvars", TFVARS_CONTENT),
        ]
        vars_dict = _parse_tfvars(files)
        # tfvars env = "prod" overrides variables.tf default "dev"
        assert vars_dict["env"] == "prod"

    def test_resolve_suffix(self):
        resolved = _resolve_interpolation(
            "asp-ruriskry-prod-${var.suffix}", {"suffix": "demo"}
        )
        assert resolved == "asp-ruriskry-prod-demo"

    def test_resolve_multiple_vars(self):
        resolved = _resolve_interpolation(
            "${var.prefix}-${var.env}", {"prefix": "asp", "env": "prod"}
        )
        assert resolved == "asp-prod"

    def test_unresolved_var_kept_as_is(self):
        resolved = _resolve_interpolation(
            "asp-${var.unknown}", {"suffix": "demo"}
        )
        assert resolved == "asp-${var.unknown}"


# =============================================================================
# find_tf_block — integration tests across all 3 passes
# =============================================================================

class TestFindTfBlock:
    def test_pass1_exact_match(self):
        """Pass 1: literal name matches directly."""
        files = [_make_file("main.tf", SERVICE_PLAN_EXACT_TF)]
        result = find_tf_block(files, "asp-ruriskry-staging", ["azurerm_service_plan"])
        assert result is not None
        assert result.logical_name == "staging"
        assert result.tf_type == "azurerm_service_plan"

    def test_pass2_interpolated_name(self):
        """Pass 2: static prefix of 'asp-ruriskry-prod-${var.suffix}' matches 'asp-ruriskry-prod-demo'."""
        files = [_make_file("main.tf", SERVICE_PLAN_TF)]
        result = find_tf_block(files, "asp-ruriskry-prod-demo", ["azurerm_service_plan"])
        assert result is not None
        assert result.logical_name == "prod"
        assert result.address == "azurerm_service_plan.prod"

    def test_pass3_tfvars_resolution(self):
        """Pass 3: after resolving var.suffix=demo, 'asp-ruriskry-prod-demo' exact-matches."""
        # Use content where the static prefix is too short to match on its own
        # but resolves exactly once we substitute the var.
        tf_content = '''
resource "azurerm_service_plan" "prod" {
  name     = "${var.suffix}"
  sku_name = "F1"
}
'''
        files = [
            _make_file("main.tf", tf_content),
            _make_file("terraform.tfvars", 'suffix = "asp-prod-demo"\n'),
        ]
        result = find_tf_block(files, "asp-prod-demo", ["azurerm_service_plan"])
        assert result is not None
        assert result.logical_name == "prod"

    def test_no_match_returns_none(self):
        files = [_make_file("main.tf", SERVICE_PLAN_TF)]
        result = find_tf_block(files, "completely-different-name", ["azurerm_service_plan"])
        assert result is None

    def test_wrong_tf_type_returns_none(self):
        files = [_make_file("main.tf", SERVICE_PLAN_TF)]
        result = find_tf_block(files, "asp-ruriskry-prod-demo", ["azurerm_linux_virtual_machine"])
        assert result is None

    def test_picks_correct_block_among_multiple(self):
        files = [_make_file("main.tf", MULTI_BLOCK_TF)]
        result = find_tf_block(files, "asp-prod-demo", ["azurerm_service_plan"])
        assert result is not None
        assert result.logical_name == "prod"

    def test_block_match_has_correct_metadata(self):
        files = [_make_file("infra/main.tf", SERVICE_PLAN_EXACT_TF, sha="deadbeef")]
        result = find_tf_block(files, "asp-ruriskry-staging", ["azurerm_service_plan"])
        assert result is not None
        assert result.file_path == "infra/main.tf"
        assert result.file_sha == "deadbeef"
        assert "azurerm_service_plan" in result.raw_block

    def test_searches_multiple_files(self):
        files = [
            _make_file("networking.tf", VM_TF),
            _make_file("services.tf", SERVICE_PLAN_EXACT_TF),
        ]
        result = find_tf_block(files, "asp-ruriskry-staging", ["azurerm_service_plan"])
        assert result is not None
        assert result.file_path == "services.tf"

    def test_vm_block_found(self):
        files = [_make_file("main.tf", VM_TF)]
        result = find_tf_block(files, "vm-web01-prod", ["azurerm_linux_virtual_machine"])
        assert result is not None
        assert result.logical_name == "web01"


# =============================================================================
# get_attribute_value
# =============================================================================

class TestGetAttributeValue:
    def _make_block(self, raw: str) -> BlockMatch:
        return BlockMatch(
            tf_type="azurerm_service_plan",
            logical_name="prod",
            file_path="main.tf",
            file_sha="abc",
            start_line=0,
            end_line=5,
            raw_block=raw,
            name_value="test",
        )

    def test_extracts_sku_name(self):
        block = self._make_block(SERVICE_PLAN_TF)
        assert get_attribute_value(block, "sku_name") == "F1"

    def test_extracts_size(self):
        block = self._make_block(VM_TF)
        assert get_attribute_value(block, "size") == "Standard_B1s"

    def test_missing_attribute_returns_none(self):
        block = self._make_block(SERVICE_PLAN_TF)
        assert get_attribute_value(block, "nonexistent_field") is None

    def test_ignores_variable_reference(self):
        # Variable references don't have "value" in quotes — should return None
        content = 'resource "x" "y" {\n  size = var.vm_size\n}'
        block = self._make_block(content)
        assert get_attribute_value(block, "size") is None


# =============================================================================
# find_dangling_references
# =============================================================================

class TestFindDanglingReferences:
    def _make_file(self, path: str, content: str) -> TfFile:
        return TfFile(file_path=path, file_sha="abc", content=content)

    def _make_block(self, tf_type: str, logical_name: str, file_path: str) -> BlockMatch:
        return BlockMatch(
            tf_type=tf_type,
            logical_name=logical_name,
            file_path=file_path,
            file_sha="abc",
            start_line=0,
            end_line=5,
            raw_block="",
            name_value="test",
        )

    def test_finds_reference_in_other_file(self):
        block = self._make_block("azurerm_service_plan", "prod", "main.tf")
        other = self._make_file(
            "outputs.tf",
            'output "plan_id" {\n  value = azurerm_service_plan.prod.id\n}\n',
        )
        refs = find_dangling_references(block, [other])
        assert len(refs) == 1
        assert refs[0]["file_path"] == "outputs.tf"
        assert refs[0]["line"] == 2
        assert "azurerm_service_plan.prod" in refs[0]["text"]

    def test_skips_source_file(self):
        block = self._make_block("azurerm_service_plan", "prod", "main.tf")
        source = self._make_file(
            "main.tf",
            'resource "azurerm_service_plan" "prod" {\n  name = "test"\n}\n',
        )
        refs = find_dangling_references(block, [source])
        assert refs == []

    def test_no_references_returns_empty(self):
        block = self._make_block("azurerm_service_plan", "prod", "main.tf")
        other = self._make_file("outputs.tf", 'output "x" { value = "static" }')
        refs = find_dangling_references(block, [other])
        assert refs == []

    def test_multiple_references_in_same_file(self):
        block = self._make_block("azurerm_service_plan", "prod", "main.tf")
        content = (
            'resource "azurerm_linux_web_app" "app" {\n'
            '  service_plan_id = azurerm_service_plan.prod.id\n'
            '}\n'
            'output "plan_id" { value = azurerm_service_plan.prod.id }\n'
        )
        other = self._make_file("app.tf", content)
        refs = find_dangling_references(block, [other])
        assert len(refs) == 2

    def test_multiple_files_scanned(self):
        block = self._make_block("azurerm_service_plan", "prod", "main.tf")
        f1 = self._make_file("app.tf", 'service_plan_id = azurerm_service_plan.prod.id')
        f2 = self._make_file("outputs.tf", 'value = azurerm_service_plan.prod.sku_name')
        refs = find_dangling_references(block, [f1, f2])
        assert len(refs) == 2
        paths = {r["file_path"] for r in refs}
        assert paths == {"app.tf", "outputs.tf"}
