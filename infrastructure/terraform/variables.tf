# =============================================================================
# SentinelLayer - Terraform Input Variables
# =============================================================================
# Set values in terraform.tfvars (copy from terraform.tfvars.example).
# Never commit terraform.tfvars - it is in .gitignore.
# =============================================================================

variable "subscription_id" {
  description = "Azure Subscription ID - find it in: az account show --query id -o tsv"
  type        = string
}

variable "resource_group_name" {
  description = "Name of the Azure Resource Group to create"
  type        = string
  default     = "sentinel-layer-rg"
}

variable "location" {
  description = "Azure region for core resources (Search, Cosmos, Key Vault, Log Analytics)."
  type        = string
  default     = "eastus"
}

variable "env" {
  description = "Environment label attached to all resource tags (e.g. dev, staging, prod)"
  type        = string
  default     = "dev"
}

variable "suffix" {
  description = <<-EOT
    Short unique suffix appended to globally-unique resource names.
    Azure Cosmos DB, Key Vault, Foundry, and Search names must be unique
    across ALL Azure customers worldwide.
    Use something like your initials + 4 random digits: "abc1234"
  EOT
  type        = string
}

variable "search_sku" {
  description = <<-EOT
    Azure AI Search pricing tier.
    "free"  - 50 MB index, 3 indexes max, no SLA. Only 1 free per subscription.
    "basic" - 2 GB index, 15 indexes, SLA. ~$73/month.
    If you already have a free Search service, use "basic".
  EOT
  type        = string
  default     = "free"

  validation {
    condition     = contains(["free", "basic", "standard", "standard2"], var.search_sku)
    error_message = "search_sku must be one of: free, basic, standard, standard2."
  }
}

variable "cosmos_location" {
  description = "Region for Cosmos DB account (use an alternate region if East US has capacity issues)."
  type        = string
  default     = "eastus2"
}

# ---------------------------------------------------------------------------
# Foundry-only settings
# ---------------------------------------------------------------------------

variable "foundry_location" {
  description = "Region for Foundry account (for example eastus2)."
  type        = string
  default     = "eastus2"
}

variable "foundry_account_name" {
  description = "Optional explicit Foundry account name. Leave empty to auto-generate."
  type        = string
  default     = ""
}

variable "foundry_custom_subdomain_name" {
  description = "Optional explicit Foundry custom subdomain. Leave empty to use account name."
  type        = string
  default     = ""
}

variable "create_foundry_project" {
  description = "Create Foundry project resource under the AIServices account."
  type        = bool
  default     = false
}

variable "foundry_project_name" {
  description = "Foundry project name when create_foundry_project=true."
  type        = string
  default     = "sentinel-layer"
}

variable "create_foundry_deployment" {
  description = "Create model deployment in Foundry from Terraform."
  type        = bool
  default     = false
}

variable "foundry_model" {
  description = "Foundry model name (example: gpt-4.1)."
  type        = string
  default     = "gpt-4.1"
}

variable "foundry_model_version" {
  description = "Foundry model version string. Use empty string to let Azure choose latest."
  type        = string
  default     = ""
}

variable "foundry_deployment_name" {
  description = "Foundry deployment name used by application runtime."
  type        = string
  default     = "gpt-41"
}

variable "foundry_capacity" {
  description = "Foundry deployment capacity (thousands TPM)."
  type        = number
  default     = 1
}

variable "foundry_scale_type" {
  description = "Foundry deployment scale type."
  type        = string
  default     = "GlobalStandard"

  validation {
    condition     = contains(["GlobalStandard", "Standard"], var.foundry_scale_type)
    error_message = "foundry_scale_type must be either GlobalStandard or Standard."
  }
}

variable "cosmos_free_tier" {
  description = <<-EOT
    Enable Cosmos DB free tier (400 RU/s + 25 GB free).
    Only 1 free-tier Cosmos account is allowed per Azure subscription.
    Set to false if your subscription already uses the free tier elsewhere.
  EOT
  type        = bool
  default     = true
}

# ---------------------------------------------------------------------------
# Key Vault secret naming and identity access
# ---------------------------------------------------------------------------

variable "keyvault_secret_name_foundry_key" {
  description = "Key Vault secret name that stores Foundry primary access key."
  type        = string
  default     = "foundry-primary-key"
}

variable "keyvault_secret_name_search_key" {
  description = "Key Vault secret name that stores Azure AI Search primary key."
  type        = string
  default     = "search-primary-key"
}

variable "keyvault_secret_name_cosmos_key" {
  description = "Key Vault secret name that stores Cosmos DB primary key."
  type        = string
  default     = "cosmos-primary-key"
}

variable "managed_identity_principal_ids" {
  description = <<-EOT
    Optional list of Microsoft Entra object IDs for managed identities that
    should read secrets from Key Vault (Get/List).
    Example: ["00000000-0000-0000-0000-000000000000"]
  EOT
  type        = list(string)
  default     = []
}
