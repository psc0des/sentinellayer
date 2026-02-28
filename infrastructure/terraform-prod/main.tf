# =============================================================================
# SentinelLayer — Mini Production Environment
# =============================================================================
# Creates REAL Azure resources that SentinelLayer governs in live demos.
#
# Governance scenarios:
#   vm-dr-01         → Cost agent proposes DELETE  → SentinelLayer DENIES
#                      (disaster-recovery tag = policy violation)
#   vm-web-01        → SRE agent proposes SCALE UP → SentinelLayer APPROVES
#                      (legitimate CPU spike, no policy violations)
#   payment-api-prod → Critical dependency of vm-web-01 (critical=true tag)
#   nsg-east-prod    → Deploy agent proposes open port 8080 → ESCALATED
#                      (NSG changes affect all governed workloads — medium blast radius)
#   sentinelprod*    → Shared storage dependency of all three resources above
# =============================================================================

terraform {
  required_version = ">= 1.5.0"

  required_providers {
    azurerm = {
      source  = "hashicorp/azurerm"
      version = "~> 4.58"
    }
    http = {
      source  = "hashicorp/http"
      version = "~> 3.4"
    }
  }
}

provider "azurerm" {
  features {}
  subscription_id = var.subscription_id
}

data "azurerm_client_config" "current" {}
data "http" "current_public_ip" {
  url = "https://api.ipify.org"
}

locals {
  raw_public_ip = trimspace(data.http.current_public_ip.response_body)

  # For NSG source_address_prefix — /32 CIDR is valid in NSG rules.
  allowed_source_cidr = (
    var.allowed_source_cidr_override != "" ?
    var.allowed_source_cidr_override :
    "${local.raw_public_ip}/32"
  )

  # For Storage ip_rules — Azure Storage rejects /31 and /32 CIDRs.
  # Use plain IP for auto-detected case; strip /32 from override if present.
  storage_allowed_ip = (
    var.allowed_source_cidr_override != "" ?
    (endswith(var.allowed_source_cidr_override, "/32") ?
      cidrhost(var.allowed_source_cidr_override, 0) :
      var.allowed_source_cidr_override) :
    local.raw_public_ip
  )

  common_tags = {
    project    = "sentinellayer"
    managed_by = "terraform"
    purpose    = "governance-demo"
  }
}

# =============================================================================
# 1. Resource Group: sentinel-prod-rg
# =============================================================================

resource "azurerm_resource_group" "prod" {
  name     = "sentinel-prod-rg"
  location = var.location
  tags     = local.common_tags
}

# =============================================================================
# 2. Virtual Network + Subnet
# =============================================================================

resource "azurerm_virtual_network" "prod" {
  name                = "vnet-sentinel-prod"
  address_space       = ["10.1.0.0/16"]
  location            = azurerm_resource_group.prod.location
  resource_group_name = azurerm_resource_group.prod.name
  tags                = local.common_tags
}

resource "azurerm_subnet" "prod" {
  name                 = "subnet-sentinel-prod"
  resource_group_name  = azurerm_resource_group.prod.name
  virtual_network_name = azurerm_virtual_network.prod.name
  address_prefixes     = ["10.1.1.0/24"]
  service_endpoints    = ["Microsoft.Storage"]
}

# =============================================================================
# 3. NSG: nsg-east-prod
# =============================================================================
# Default: allow HTTP (80) + HTTPS (443) from:
#   1) your current public IP (auto-detected, or override)
#   2) inside the VNet (VirtualNetwork service tag)
# Demo scenario: deploy agent proposes opening port 8080 → SentinelLayer
# ESCALATES because NSG changes affect all workloads behind the subnet gateway.

resource "azurerm_network_security_group" "prod" {
  name                = "nsg-east-prod"
  location            = azurerm_resource_group.prod.location
  resource_group_name = azurerm_resource_group.prod.name

  tags = merge(local.common_tags, {
    environment = "production"
    managed-by  = "platform-team"
  })

  security_rule {
    name                       = "allow-http-my-ip"
    priority                   = 100
    direction                  = "Inbound"
    access                     = "Allow"
    protocol                   = "Tcp"
    source_port_range          = "*"
    destination_port_range     = "80"
    source_address_prefix      = local.allowed_source_cidr
    destination_address_prefix = "*"
  }

  security_rule {
    name                       = "allow-https-my-ip"
    priority                   = 110
    direction                  = "Inbound"
    access                     = "Allow"
    protocol                   = "Tcp"
    source_port_range          = "*"
    destination_port_range     = "443"
    source_address_prefix      = local.allowed_source_cidr
    destination_address_prefix = "*"
  }

  security_rule {
    name                       = "allow-http-vnet"
    priority                   = 120
    direction                  = "Inbound"
    access                     = "Allow"
    protocol                   = "Tcp"
    source_port_range          = "*"
    destination_port_range     = "80"
    source_address_prefix      = "VirtualNetwork"
    destination_address_prefix = "*"
  }

  security_rule {
    name                       = "allow-https-vnet"
    priority                   = 130
    direction                  = "Inbound"
    access                     = "Allow"
    protocol                   = "Tcp"
    source_port_range          = "*"
    destination_port_range     = "443"
    source_address_prefix      = "VirtualNetwork"
    destination_address_prefix = "*"
  }
}

resource "azurerm_subnet_network_security_group_association" "prod" {
  subnet_id                 = azurerm_subnet.prod.id
  network_security_group_id = azurerm_network_security_group.prod.id
}

# =============================================================================
# 4. Storage Account: sentinelprod{suffix}
# =============================================================================
# Shared dependency for vm-dr-01, vm-web-01, and payment-api-prod.
# Any action that removes or disrupts this storage has a HIGH blast radius
# because three production resources depend on it simultaneously.

resource "azurerm_storage_account" "prod" {
  name                            = "sentinelprod${var.suffix}"
  resource_group_name             = azurerm_resource_group.prod.name
  location                        = azurerm_resource_group.prod.location
  account_tier                    = "Standard"
  account_replication_type        = "LRS"
  min_tls_version                 = "TLS1_2"
  allow_nested_items_to_be_public = false

  network_rules {
    default_action             = "Deny"
    bypass                     = ["AzureServices"]
    ip_rules                   = [local.storage_allowed_ip] # plain IP — /32 CIDRs are rejected by Azure Storage
    virtual_network_subnet_ids = [azurerm_subnet.prod.id]
  }

  tags = merge(local.common_tags, {
    environment = "production"
    shared      = "true"
  })
}

# =============================================================================
# 5. Public IPs for the two VMs
# =============================================================================

resource "azurerm_public_ip" "dr01" {
  name                = "pip-vm-dr-01"
  resource_group_name = azurerm_resource_group.prod.name
  location            = azurerm_resource_group.prod.location
  allocation_method   = "Static"
  sku                 = "Standard"
  tags                = local.common_tags
}

resource "azurerm_public_ip" "web01" {
  name                = "pip-vm-web-01"
  resource_group_name = azurerm_resource_group.prod.name
  location            = azurerm_resource_group.prod.location
  allocation_method   = "Static"
  sku                 = "Standard"
  tags                = local.common_tags
}

# =============================================================================
# 6. Network Interface Cards
# =============================================================================

resource "azurerm_network_interface" "dr01" {
  name                = "nic-vm-dr-01"
  location            = azurerm_resource_group.prod.location
  resource_group_name = azurerm_resource_group.prod.name
  tags                = local.common_tags

  ip_configuration {
    name                          = "internal"
    subnet_id                     = azurerm_subnet.prod.id
    private_ip_address_allocation = "Dynamic"
    public_ip_address_id          = azurerm_public_ip.dr01.id
  }
}

resource "azurerm_network_interface" "web01" {
  name                = "nic-vm-web-01"
  location            = azurerm_resource_group.prod.location
  resource_group_name = azurerm_resource_group.prod.name
  tags                = local.common_tags

  ip_configuration {
    name                          = "internal"
    subnet_id                     = azurerm_subnet.prod.id
    private_ip_address_allocation = "Dynamic"
    public_ip_address_id          = azurerm_public_ip.web01.id
  }
}

# =============================================================================
# 7. VM 1: vm-dr-01 — Disaster Recovery (should be DENIED for deletion)
# =============================================================================
# This VM is intentionally idle — that's the point of a standby DR server.
# The cost agent will flag it as "unused for 30+ days" and propose deletion.
# SentinelLayer DENIES because:
#   - Policy: tag disaster-recovery=true → protected resource
#   - Blast radius: dr-failover-service and backup-coordinator depend on it
#   - Historical: similar DR deletions caused 2h outages in past incidents

resource "azurerm_linux_virtual_machine" "dr01" {
  name                            = "vm-dr-01"
  resource_group_name             = azurerm_resource_group.prod.name
  location                        = azurerm_resource_group.prod.location
  size                            = "Standard_B1s"
  admin_username                  = var.vm_admin_username
  admin_password                  = var.vm_admin_password
  disable_password_authentication = false

  network_interface_ids = [azurerm_network_interface.dr01.id]

  os_disk {
    caching              = "ReadWrite"
    storage_account_type = "Standard_LRS"
  }

  source_image_reference {
    publisher = "Canonical"
    offer     = "0001-com-ubuntu-server-jammy"
    sku       = "22_04-lts"
    version   = "latest"
  }

  tags = merge(local.common_tags, {
    disaster-recovery = "true"
    environment       = "production"
    owner             = "platform-team"
    cost-center       = "infrastructure"
  })
}

# =============================================================================
# 8. VM 2: vm-web-01 — Active Web Server (should be APPROVED for scale-up)
# =============================================================================
# This is the live web tier. When the CPU alert fires at >80%, the monitoring
# agent proposes a scale-up from Standard_B1s → Standard_B2s.
# SentinelLayer APPROVES because:
#   - No policy violations (no protected tags, no deny-listed action types)
#   - Low blast radius (no critical downstream services depend on it)
#   - Historical: scaling web VMs has zero incident history

resource "azurerm_linux_virtual_machine" "web01" {
  name                            = "vm-web-01"
  resource_group_name             = azurerm_resource_group.prod.name
  location                        = azurerm_resource_group.prod.location
  size                            = "Standard_B1s"
  admin_username                  = var.vm_admin_username
  admin_password                  = var.vm_admin_password
  disable_password_authentication = false

  network_interface_ids = [azurerm_network_interface.web01.id]

  os_disk {
    caching              = "ReadWrite"
    storage_account_type = "Standard_LRS"
  }

  source_image_reference {
    publisher = "Canonical"
    offer     = "0001-com-ubuntu-server-jammy"
    sku       = "22_04-lts"
    version   = "latest"
  }

  tags = merge(local.common_tags, {
    tier        = "web"
    environment = "production"
    owner       = "web-team"
    cost-center = "frontend"
  })
}

# =============================================================================
# 9. Auto-Shutdown Schedules (cost management)
# =============================================================================
# Both VMs auto-shutdown at 22:00 UTC every day.
# This prevents overnight charges while the VMs sit idle between demo runs.
# The B1s VM costs ~$0.02/hour — auto-shutdown saves ~$10/month per VM.

resource "azurerm_dev_test_global_vm_shutdown_schedule" "dr01" {
  virtual_machine_id    = azurerm_linux_virtual_machine.dr01.id
  location              = azurerm_resource_group.prod.location
  enabled               = true
  daily_recurrence_time = "2200"
  timezone              = "UTC"

  notification_settings {
    enabled = false
  }
}

resource "azurerm_dev_test_global_vm_shutdown_schedule" "web01" {
  virtual_machine_id    = azurerm_linux_virtual_machine.web01.id
  location              = azurerm_resource_group.prod.location
  enabled               = true
  daily_recurrence_time = "2200"
  timezone              = "UTC"

  notification_settings {
    enabled = false
  }
}

# =============================================================================
# 10. App Service: payment-api-prod (Free F1 tier)
# =============================================================================
# The payment microservice that vm-web-01 depends on.
# Tagged critical=true — so SentinelLayer's blast radius agent will score any
# action touching vm-web-01 higher because it could cascade to the payment API.

resource "azurerm_service_plan" "prod" {
  name                = "asp-sentinel-prod-${var.suffix}"
  resource_group_name = azurerm_resource_group.prod.name
  location            = azurerm_resource_group.prod.location
  os_type             = "Linux"
  sku_name            = "F1"
  tags                = local.common_tags
}

resource "azurerm_linux_web_app" "payment_api" {
  name                = "payment-api-prod-${var.suffix}"
  resource_group_name = azurerm_resource_group.prod.name
  location            = azurerm_resource_group.prod.location
  service_plan_id     = azurerm_service_plan.prod.id

  site_config {
    always_on = false # F1 free tier does not support always_on
  }

  tags = merge(local.common_tags, {
    tier        = "api"
    environment = "production"
    critical    = "true"
  })
}

# =============================================================================
# 11. Log Analytics Workspace (backing store for monitor alerts)
# =============================================================================

resource "azurerm_log_analytics_workspace" "prod" {
  name                = "law-sentinel-prod-${var.suffix}"
  resource_group_name = azurerm_resource_group.prod.name
  location            = azurerm_resource_group.prod.location
  sku                 = "PerGB2018"
  retention_in_days   = 30
  tags                = local.common_tags
}

# =============================================================================
# 12. Monitor Action Group (alert destination)
# =============================================================================

resource "azurerm_monitor_action_group" "prod" {
  name                = "ag-sentinel-prod"
  resource_group_name = azurerm_resource_group.prod.name
  short_name          = "sentinel"
  tags                = local.common_tags

  email_receiver {
    name          = "ops-team"
    email_address = var.alert_email
  }
}

# =============================================================================
# 13. CPU Alert on vm-web-01 (threshold 80%)
# =============================================================================
# Fires when vm-web-01 average CPU exceeds 80% over a 15-minute window.
# In the demo flow: alert fires → monitoring agent proposes scale-up →
# SentinelLayer evaluates → APPROVES (safe action, legitimate load).

resource "azurerm_monitor_metric_alert" "web01_cpu" {
  name                = "alert-vm-web-01-cpu-high"
  resource_group_name = azurerm_resource_group.prod.name
  scopes              = [azurerm_linux_virtual_machine.web01.id]
  description         = "Triggers SentinelLayer when vm-web-01 CPU > 80% — scale-up candidate"
  severity            = 2
  frequency           = "PT5M"
  window_size         = "PT15M"

  criteria {
    metric_namespace = "Microsoft.Compute/virtualMachines"
    metric_name      = "Percentage CPU"
    aggregation      = "Average"
    operator         = "GreaterThan"
    threshold        = 80
  }

  action {
    action_group_id = azurerm_monitor_action_group.prod.id
  }

  tags = local.common_tags
}

# =============================================================================
# 14. Heartbeat Alert on vm-dr-01 (idle / stopped VM detection)
# =============================================================================
# If vm-dr-01 sends no heartbeat for 15 minutes it is considered idle/stopped.
# In the demo flow: no heartbeat → cost agent flags as idle → proposes deletion
# → SentinelLayer DENIES (disaster-recovery policy + high blast radius).
# Requires Azure Monitor Agent installed on the VM to emit heartbeats.

resource "azurerm_monitor_scheduled_query_rules_alert_v2" "dr01_heartbeat" {
  name                = "alert-vm-dr-01-heartbeat"
  resource_group_name = azurerm_resource_group.prod.name
  location            = azurerm_resource_group.prod.location
  description         = "Detects when vm-dr-01 stops sending heartbeats — idle/stopped VM"
  severity            = 2
  enabled             = true

  evaluation_frequency = "PT5M"
  window_duration      = "PT15M"
  scopes               = [azurerm_log_analytics_workspace.prod.id]

  criteria {
    # Returns one row per heartbeat received from vm-dr-01 in the last 15 minutes.
    # Count = 0 means the VM is not communicating → alert fires.
    query = <<-QUERY
      Heartbeat
      | where Computer contains "vm-dr-01"
      | where TimeGenerated > ago(15m)
    QUERY

    time_aggregation_method = "Count"
    threshold               = 0
    operator                = "LessThanOrEqual"

    failing_periods {
      minimum_failing_periods_to_trigger_alert = 1
      number_of_evaluation_periods             = 1
    }
  }

  action {
    action_groups = [azurerm_monitor_action_group.prod.id]
  }

  tags = local.common_tags
}
