# =============================================================================
# SentinelLayer — Terraform Input Variables
# =============================================================================
# Set values in terraform.tfvars (copy from terraform.tfvars.example).
# Never commit terraform.tfvars — it is in .gitignore.
# =============================================================================

variable "subscription_id" {
  description = "Azure Subscription ID — find it in: az account show --query id -o tsv"
  type        = string
}

variable "resource_group_name" {
  description = "Name of the Azure Resource Group to create"
  type        = string
  default     = "sentinel-layer-rg"
}

variable "location" {
  description = "Azure region for all resources (e.g. eastus, eastus2, westus)."
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
    Azure Cosmos DB, Key Vault, and Search names must be unique
    across ALL Azure customers worldwide.
    Use something like your initials + 4 random digits: "abc1234"
  EOT
  type        = string
  # No default — you MUST set this in terraform.tfvars
}

variable "search_sku" {
  description = <<-EOT
    Azure AI Search pricing tier.
    "free"  — 50 MB index, 3 indexes max, no SLA. Only 1 free per subscription.
    "basic" — 2 GB index, 15 indexes, SLA. ~$73/month.
    If you already have a free Search service, use "basic".
  EOT
  type        = string
  default     = "free"

  validation {
    condition     = contains(["free", "basic", "standard", "standard2"], var.search_sku)
    error_message = "search_sku must be one of: free, basic, standard, standard2."
  }
}

# NOTE: No OpenAI/Foundry variables here.
# GPT-4.1 is deployed manually via the Microsoft Foundry portal and its
# credentials are stored directly in .env — not managed by Terraform.
# See .env.example for the variable names to set.

variable "cosmos_free_tier" {
  description = <<-EOT
    Enable Cosmos DB free tier (400 RU/s + 25 GB free).
    Only 1 free-tier Cosmos account is allowed per Azure subscription.
    Set to false if your subscription already uses the free tier elsewhere.
  EOT
  type        = bool
  default     = true
}
