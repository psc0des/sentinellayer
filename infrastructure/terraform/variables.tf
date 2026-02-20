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
  description = <<-EOT
    Azure region for all resources.
    Azure OpenAI GPT-4o is available in: eastus, eastus2, westus,
    swedencentral, francecentral, uksouth, australiaeast, japaneast.
  EOT
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
    Azure Cosmos DB, Key Vault, OpenAI, and Search names must be unique
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

variable "openai_model" {
  description = <<-EOT
    Azure OpenAI model to deploy.
    "gpt-4o-mini" — cheaper, higher quota availability on new subscriptions.
    "gpt-4o"      — more capable; requires quota approval first:
                    https://aka.ms/oai/quotaincrease
  EOT
  type        = string
  default     = "gpt-4o-mini"
}

variable "openai_model_version" {
  description = <<-EOT
    Model version string.
    gpt-4o-mini → "2024-07-18"
    gpt-4o      → "2024-11-20"
  EOT
  type        = string
  default     = "2024-07-18"
}

variable "openai_capacity" {
  description = <<-EOT
    Tokens-per-minute quota to request, in thousands.
    Minimum is 1 (= 1 000 TPM). Start low and increase after quota is approved.
    Run `az cognitiveservices account list-skus` to see available capacity.
  EOT
  type        = number
  default     = 1
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
