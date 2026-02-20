# =============================================================================
# SentinelLayer — Azure Infrastructure
# =============================================================================
# Deploys all Azure services needed for SentinelLayer in a single plan.
#
# Resources deployed (in dependency order):
#   1. Resource Group          — container for all resources
#   2. Log Analytics Workspace — monitoring, created early so others can link
#   3. Key Vault               — secret store for API keys
#   4. Azure OpenAI            — GPT-4o for governance reasoning
#   5. Azure AI Search         — incident history vector search
#   6. Cosmos DB (SQL API)     — governance decision audit trail
#
# Usage:
#   cd infrastructure/terraform
#   cp terraform.tfvars.example terraform.tfvars   # fill in your values
#   terraform init
#   terraform plan
#   terraform apply
#   cd ../..
#   bash scripts/setup_env.sh                       # writes .env from outputs
# =============================================================================

terraform {
  required_version = ">= 1.5.0"

  required_providers {
    azurerm = {
      source  = "hashicorp/azurerm"
      version = "~> 3.90"
    }
  }
}

provider "azurerm" {
  features {
    key_vault {
      # Soft-delete and purge protection — safe defaults for dev
      purge_soft_delete_on_destroy    = true
      recover_soft_deleted_key_vaults = true
    }
  }
  subscription_id = var.subscription_id
}

# Read the current caller identity (needed for Key Vault access policy)
data "azurerm_client_config" "current" {}

# =============================================================================
# Local values — shared tags and computed names
# =============================================================================

locals {
  # All resource names share this suffix so they are globally unique
  # (Cosmos DB, Key Vault, OpenAI, and Search names must be unique across Azure)
  name_suffix = var.suffix

  common_tags = {
    project     = "sentinellayer"
    environment = var.env
    managed_by  = "terraform"
  }
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
# Created first so other resources can reference its ID for diagnostics.
# =============================================================================

resource "azurerm_log_analytics_workspace" "sentinel" {
  name                = "sentinel-log-${local.name_suffix}"
  resource_group_name = azurerm_resource_group.sentinel.name
  location            = azurerm_resource_group.sentinel.location
  sku                 = "PerGB2018"   # pay-per-GB, cheapest option
  retention_in_days   = 30
  tags                = local.common_tags
}

# =============================================================================
# 3. Azure Key Vault
# Stores all API keys / connection secrets so they don't live in .env long-term.
# =============================================================================

resource "azurerm_key_vault" "sentinel" {
  # Name must be 3–24 chars, globally unique, alphanumeric + hyphens
  name                = "sentinel-kv-${local.name_suffix}"
  resource_group_name = azurerm_resource_group.sentinel.name
  location            = azurerm_resource_group.sentinel.location
  tenant_id           = data.azurerm_client_config.current.tenant_id
  sku_name            = "standard"

  # Give the deploying identity full secret access (needed for CI/CD later)
  access_policy {
    tenant_id = data.azurerm_client_config.current.tenant_id
    object_id = data.azurerm_client_config.current.object_id

    secret_permissions = [
      "Backup", "Delete", "Get", "List", "Purge", "Recover", "Restore", "Set"
    ]
  }

  tags = local.common_tags
}

# =============================================================================
# 4. Azure OpenAI
# NOTE: Azure OpenAI requires manual access approval per subscription.
# Request access at: https://aka.ms/oai/access
#
# GPT-4o is available in: East US, East US 2, West US, Sweden Central,
# France Central, UK South, Australia East, Japan East.
# =============================================================================

resource "azurerm_cognitive_account" "openai" {
  name                = "sentinel-openai-${local.name_suffix}"
  resource_group_name = azurerm_resource_group.sentinel.name
  location            = azurerm_resource_group.sentinel.location
  kind                = "OpenAI"
  sku_name            = "S0"   # Only SKU available for Azure OpenAI

  tags = local.common_tags
}

# Model deployment inside the OpenAI account.
# Default model is gpt-4o-mini — it shares the same API as gpt-4o but has
# much higher quota availability on new subscriptions.
# To use gpt-4o, first request quota at: https://aka.ms/oai/quotaincrease
# then set openai_model = "gpt-4o" and openai_model_version = "2024-11-20"
# in terraform.tfvars.
resource "azurerm_cognitive_deployment" "gpt4o" {
  name                 = var.openai_model
  cognitive_account_id = azurerm_cognitive_account.openai.id

  model {
    format  = "OpenAI"
    name    = var.openai_model
    version = var.openai_model_version
  }

  scale {
    type     = "Standard"
    capacity = var.openai_capacity   # tokens-per-minute in thousands
  }
}

# =============================================================================
# 5. Azure AI Search
# NOTE: Only ONE free-tier Search service is allowed per Azure subscription.
# If you already have one, change sku to "basic" (~$73/month).
# =============================================================================

resource "azurerm_search_service" "sentinel" {
  name                = "sentinel-search-${local.name_suffix}"
  resource_group_name = azurerm_resource_group.sentinel.name
  location            = azurerm_resource_group.sentinel.location
  sku                 = var.search_sku   # "free" (default) or "basic"

  # Free tier restriction: only 1 replica, 1 partition
  replica_count   = var.search_sku == "free" ? null : 1
  partition_count = var.search_sku == "free" ? null : 1

  tags = local.common_tags
}

# =============================================================================
# 6. Cosmos DB (SQL API) — governance decision audit trail
# =============================================================================

resource "azurerm_cosmosdb_account" "sentinel" {
  name                = "sentinel-cosmos-${local.name_suffix}"
  resource_group_name = azurerm_resource_group.sentinel.name
  location            = azurerm_resource_group.sentinel.location
  offer_type          = "Standard"
  kind                = "GlobalDocumentDB"   # SQL API

  # Session consistency: best balance of performance and consistency for audit logs
  consistency_policy {
    consistency_level = "Session"
  }

  # Single-region for dev — add more geo_locations for production HA
  # zone_redundant = false: explicitly opt out of Availability Zones.
  # AZ accounts require reserved capacity that East US is currently out of.
  geo_location {
    location          = azurerm_resource_group.sentinel.location
    failover_priority = 0
    zone_redundant    = false
  }

  # Enable free tier (400 RU/s + 25 GB free) — only 1 per subscription
  free_tier_enabled = var.cosmos_free_tier

  tags = local.common_tags
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

  # Partition key — resource_id spreads decisions across partitions evenly
  # partition_key_paths replaces the deprecated partition_key_path (azurerm v4+)
  partition_key_paths   = ["/resource_id"]
  partition_key_version = 2

  # Automatic indexing of all fields (good for development)
  indexing_policy {
    indexing_mode = "consistent"

    included_path {
      path = "/*"
    }
  }
}
