# =============================================================================
# SentinelLayer — Azure Infrastructure
# =============================================================================
# Deploys all Azure services needed for SentinelLayer in a single plan.
#
# Resources deployed (in dependency order):
#   1. Resource Group          — container for all resources
#   2. Log Analytics Workspace — monitoring, created early so others can link
#   3. Key Vault               — secret store for API keys
#   4. Azure AI Foundry        — multi-model AI (GPT-4o, Llama, Mistral, xAI, Phi)
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
# 4. Azure AI Foundry (AI Services)
# "AIServices" kind replaces the old "OpenAI" kind and gives access to
# OpenAI models PLUS third-party models (Meta Llama, Mistral, xAI Grok, Phi).
# This is what the Azure portal calls "upgrading to Foundry".
#
# No manual access approval needed — AIServices is available immediately
# on any subscription without waiting for Microsoft quota approval.
#
# Supported regions (broader than old Azure OpenAI):
# eastus, eastus2, westus, westus3, swedencentral, francecentral,
# uksouth, australiaeast, japaneast, canadaeast, and more.
# =============================================================================

resource "azurerm_cognitive_account" "foundry" {
  name                = "sentinel-foundry-${local.name_suffix}"
  resource_group_name = azurerm_resource_group.sentinel.name
  location            = azurerm_resource_group.sentinel.location
  kind                = "AIServices"   # Foundry — unlocks all model families
  sku_name            = "S0"

  tags = local.common_tags
}

# Model deployment inside the Foundry account.
#
# With AIServices / Foundry you can deploy:
#   OpenAI  : gpt-4o, gpt-4o-mini, o1, o3-mini
#   Meta    : Meta-Llama-3.1-8B-Instruct, Meta-Llama-3.3-70B-Instruct
#   Mistral : Mistral-Small, Mistral-Large
#   xAI     : grok-3, grok-3-mini
#   Microsoft: Phi-4, Phi-4-mini
#
# Quota for Foundry models is available immediately (no special approval).
# Set create_openai_deployment = true in terraform.tfvars, then re-run apply.
#
# count = 0 means "don't create this resource yet".
# count = 1 means "create it".  Controlled by var.create_openai_deployment.
resource "azurerm_cognitive_deployment" "gpt4o" {
  count                = var.create_openai_deployment ? 1 : 0
  name                 = var.openai_model
  cognitive_account_id = azurerm_cognitive_account.foundry.id

  model {
    format  = "OpenAI"   # "OpenAI" is the format name even for non-OpenAI models
    name    = var.openai_model
    version = var.openai_model_version
  }

  scale {
    # GlobalStandard routes across Azure regions for better availability.
    # Use "Standard" if GlobalStandard is unavailable in your region.
    type     = "GlobalStandard"
    capacity = var.openai_capacity
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
