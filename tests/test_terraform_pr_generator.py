"""Tests for TerraformPRGenerator._apply_nsg_fix_to_content and _patch_block."""

import pytest
from src.core.terraform_pr_generator import TerraformPRGenerator


@pytest.fixture
def gen():
    return TerraformPRGenerator.__new__(TerraformPRGenerator)


STANDALONE_TF = """
resource "azurerm_network_security_group" "nsg_prod" {
  name                = "nsg-prod"
  resource_group_name = "my-rg"
}

resource "azurerm_network_security_rule" "allow_ssh_anywhere" {
  name                        = "allow-ssh-anywhere"
  priority                    = 100
  direction                   = "Inbound"
  access                      = "Allow"
  protocol                    = "Tcp"
  source_port_range           = "*"
  destination_port_range      = "22"
  source_address_prefix       = "*"
  destination_address_prefix  = "*"
  resource_group_name         = "my-rg"
  network_security_group_name = "nsg-prod"
}
"""

INLINE_TF = """
resource "azurerm_network_security_group" "nsg_prod" {
  name                = "nsg-prod"
  resource_group_name = "my-rg"

  tags = {
    environment = "prod"
  }

  security_rule {
    name                       = "allow-http"
    priority                   = 200
    direction                  = "Inbound"
    access                     = "Allow"
    protocol                   = "Tcp"
    source_port_range          = "*"
    destination_port_range     = "80"
    source_address_prefix      = "*"
    destination_address_prefix = "*"
  }

  security_rule {
    name                       = "allow-ssh-anywhere"
    priority                   = 100
    direction                  = "Inbound"
    access                     = "Allow"
    protocol                   = "Tcp"
    source_port_range          = "*"
    destination_port_range     = "22"
    source_address_prefix      = "*"
    destination_address_prefix = "*"
  }
}
"""

ALREADY_DENY_TF = """
resource "azurerm_network_security_group" "nsg_prod" {
  name = "nsg-prod"

  security_rule {
    name   = "allow-ssh-anywhere"
    access = "Deny"
  }
}
"""

UNRELATED_TF = """
resource "azurerm_virtual_network" "vnet" {
  name = "vnet-prod"
}
"""


class TestApplyNsgFixToContent:

    def test_patches_standalone_resource(self, gen):
        result = gen._apply_nsg_fix_to_content(STANDALONE_TF, "allow-ssh-anywhere")
        assert result is not None
        assert 'access                      = "Deny"' in result
        assert 'access                      = "Allow"' not in result

    def test_patches_inline_security_rule(self, gen):
        result = gen._apply_nsg_fix_to_content(INLINE_TF, "allow-ssh-anywhere")
        assert result is not None
        # The target rule should be patched
        assert '"allow-ssh-anywhere"' in result
        # access=Allow for allow-http should be unchanged (not in patched range)
        # but allow-ssh-anywhere's access must become Deny
        lines = result.split("\n")
        ssh_block_start = next(
            i for i, l in enumerate(lines) if "allow-ssh-anywhere" in l
        )
        # Find access line within ~5 lines of rule name
        for l in lines[ssh_block_start : ssh_block_start + 10]:
            if "access" in l:
                assert '"Deny"' in l
                break
        else:
            pytest.fail("access line not found near allow-ssh-anywhere rule")

    def test_only_patches_matching_rule_not_other_rules(self, gen):
        result = gen._apply_nsg_fix_to_content(INLINE_TF, "allow-ssh-anywhere")
        assert result is not None
        lines = result.split("\n")
        # Find allow-http block and verify its access is still Allow
        http_idx = next(i for i, l in enumerate(lines) if "allow-http" in l)
        for l in lines[http_idx : http_idx + 10]:
            if "access" in l:
                assert '"Allow"' in l
                break

    def test_returns_none_when_rule_not_found(self, gen):
        result = gen._apply_nsg_fix_to_content(INLINE_TF, "nonexistent-rule")
        assert result is None

    def test_returns_none_when_no_tf_resources(self, gen):
        result = gen._apply_nsg_fix_to_content(UNRELATED_TF, "allow-ssh-anywhere")
        assert result is None

    def test_returns_none_when_already_deny(self, gen):
        result = gen._apply_nsg_fix_to_content(ALREADY_DENY_TF, "allow-ssh-anywhere")
        assert result is None

    def test_rule_name_with_underscores(self, gen):
        tf = """
resource "azurerm_network_security_group" "nsg" {
  security_rule {
    name   = "allow_rdp_inbound"
    access = "Allow"
  }
}
"""
        result = gen._apply_nsg_fix_to_content(tf, "allow_rdp_inbound")
        assert result is not None
        assert '"Deny"' in result

    def test_empty_content_returns_none(self, gen):
        result = gen._apply_nsg_fix_to_content("", "allow-ssh-anywhere")
        assert result is None

    def test_handles_brace_on_next_line(self, gen):
        """security_rule with { on the next line (non-standard but possible)."""
        tf = """
resource "azurerm_network_security_group" "nsg" {
  security_rule
  {
    name   = "allow-ssh-anywhere"
    access = "Allow"
  }
}
"""
        result = gen._apply_nsg_fix_to_content(tf, "allow-ssh-anywhere")
        assert result is not None
        assert '"Deny"' in result
