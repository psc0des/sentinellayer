# =============================================================================
# SentinelLayer - Azure Infrastructure (Foundry Only)
# =============================================================================
# This configuration manages Foundry (AIServices) as the only LLM platform.
# =============================================================================

terraform {
  required_version = ">= 1.5.0"

  required_providers {
    azurerm = {
      source  = "hashicorp/azurerm"
      version = "~> 4.58"
    }
  }
}

provider "azurerm" {
  features {}
  subscription_id = var.subscription_id
}

data "azurerm_client_config" "current" {}

locals {
  name_suffix = var.suffix

  common_tags = {
    project     = "sentinellayer"
    environment = var.env
    managed_by  = "terraform"
  }

  default_foundry_name = "sentinel-foundry-${local.name_suffix}"
  foundry_account_name = var.foundry_account_name != "" ? var.foundry_account_name : local.default_foundry_name
  foundry_subdomain    = var.foundry_custom_subdomain_name != "" ? var.foundry_custom_subdomain_name : local.foundry_account_name
}

# =============================================================================
# 1. Resource Group
# =============================================================================

resource "azurerm_resource_group" "sentinel" {
  name     = var.resource_group_name
  location = var.location
  tags     = local.common_tags
}

# =============================================================================
# 2. Log Analytics Workspace
# =============================================================================

resource "azurerm_log_analytics_workspace" "sentinel" {
  name                = "sentinel-log-${local.name_suffix}"
  resource_group_name = azurerm_resource_group.sentinel.name
  location            = azurerm_resource_group.sentinel.location
  sku                 = "PerGB2018"
  retention_in_days   = 30
  tags                = local.common_tags
}

# =============================================================================
# 3. Azure Key Vault
# =============================================================================

resource "azurerm_key_vault" "sentinel" {
  name                = "sentinel-kv-${local.name_suffix}"
  resource_group_name = azurerm_resource_group.sentinel.name
  location            = azurerm_resource_group.sentinel.location
  tenant_id           = data.azurerm_client_config.current.tenant_id
  sku_name            = "standard"

  access_policy {
    tenant_id = data.azurerm_client_config.current.tenant_id
    object_id = data.azurerm_client_config.current.object_id

    secret_permissions = [
      "Backup", "Delete", "Get", "List", "Purge", "Recover", "Restore", "Set"
    ]
  }

  tags = local.common_tags
}

resource "azurerm_key_vault_access_policy" "managed_identity_readers" {
  for_each = toset(var.managed_identity_principal_ids)

  key_vault_id = azurerm_key_vault.sentinel.id
  tenant_id    = data.azurerm_client_config.current.tenant_id
  object_id    = each.value

  secret_permissions = ["Get", "List"]
}

# =============================================================================
# 4. Foundry (AIServices)
# =============================================================================

resource "azurerm_ai_services" "foundry" {
  name                               = local.foundry_account_name
  custom_subdomain_name              = local.foundry_subdomain
  resource_group_name                = azurerm_resource_group.sentinel.name
  location                           = var.foundry_location
  sku_name                           = "S0"
  local_authentication_enabled       = true
  public_network_access              = "Enabled"
  outbound_network_access_restricted = false
  tags                               = local.common_tags

  identity {
    type = "SystemAssigned"
  }
}

resource "azurerm_cognitive_account_project" "foundry" {
  count                = var.create_foundry_project ? 1 : 0
  name                 = var.foundry_project_name
  location             = var.foundry_location
  cognitive_account_id = azurerm_ai_services.foundry.id
  tags                 = local.common_tags

  identity {
    type = "SystemAssigned"
  }
}

resource "azurerm_cognitive_deployment" "foundry_primary" {
  count                = var.create_foundry_deployment ? 1 : 0
  name                 = var.foundry_deployment_name
  cognitive_account_id = azurerm_ai_services.foundry.id

  model {
    format  = "OpenAI"
    name    = var.foundry_model
    version = var.foundry_model_version != "" ? var.foundry_model_version : null
  }

  sku {
    name     = var.foundry_scale_type
    capacity = var.foundry_capacity
  }
}

# =============================================================================
# 5. Azure AI Search
# =============================================================================

resource "azurerm_search_service" "sentinel" {
  name                = "sentinel-search-${local.name_suffix}"
  resource_group_name = azurerm_resource_group.sentinel.name
  location            = azurerm_resource_group.sentinel.location
  sku                 = var.search_sku

  replica_count   = var.search_sku == "free" ? null : 1
  partition_count = var.search_sku == "free" ? null : 1

  tags = local.common_tags
}

# =============================================================================
# 6. Cosmos DB (SQL API)
# =============================================================================

resource "azurerm_cosmosdb_account" "sentinel" {
  name                = "sentinel-cosmos-${local.name_suffix}"
  resource_group_name = azurerm_resource_group.sentinel.name
  location            = var.cosmos_location
  offer_type          = "Standard"
  kind                = "GlobalDocumentDB"

  consistency_policy {
    consistency_level = "Session"
  }

  geo_location {
    location          = var.cosmos_location
    failover_priority = 0
    zone_redundant    = false
  }

  free_tier_enabled = var.cosmos_free_tier
  tags              = local.common_tags
}

resource "azurerm_cosmosdb_sql_database" "sentinellayer" {
  name                = "sentinellayer"
  resource_group_name = azurerm_resource_group.sentinel.name
  account_name        = azurerm_cosmosdb_account.sentinel.name
}

resource "azurerm_cosmosdb_sql_container" "governance_decisions" {
  name                = "governance-decisions"
  resource_group_name = azurerm_resource_group.sentinel.name
  account_name        = azurerm_cosmosdb_account.sentinel.name
  database_name       = azurerm_cosmosdb_sql_database.sentinellayer.name

  partition_key_paths   = ["/resource_id"]
  partition_key_version = 2

  indexing_policy {
    indexing_mode = "consistent"

    included_path {
      path = "/*"
    }
  }
}

# =============================================================================
# 7. Key Vault secrets (service credentials)
# =============================================================================

resource "azurerm_key_vault_secret" "foundry_primary_key" {
  name         = var.keyvault_secret_name_foundry_key
  value        = azurerm_ai_services.foundry.primary_access_key
  key_vault_id = azurerm_key_vault.sentinel.id

  depends_on = [azurerm_key_vault_access_policy.managed_identity_readers]
}

resource "azurerm_key_vault_secret" "search_primary_key" {
  name         = var.keyvault_secret_name_search_key
  value        = azurerm_search_service.sentinel.primary_key
  key_vault_id = azurerm_key_vault.sentinel.id

  depends_on = [azurerm_key_vault_access_policy.managed_identity_readers]
}

resource "azurerm_key_vault_secret" "cosmos_primary_key" {
  name         = var.keyvault_secret_name_cosmos_key
  value        = azurerm_cosmosdb_account.sentinel.primary_key
  key_vault_id = azurerm_key_vault.sentinel.id

  depends_on = [azurerm_key_vault_access_policy.managed_identity_readers]
}
