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
    Azure AI Foundry (AIServices) is available in most regions including:
    eastus, eastus2, westus, westus3, swedencentral, francecentral,
    uksouth, australiaeast, japaneast, canadaeast.
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

variable "create_openai_deployment" {
  description = <<-EOT
    Whether to create the model deployment inside the Foundry account.
    Azure AI Foundry (AIServices) quota is available immediately — no
    Microsoft approval wait. Set to true and run terraform apply.
  EOT
  type        = bool
  default     = false
}

variable "openai_model" {
  description = <<-EOT
    Model to deploy in Azure AI Foundry. Examples:
    OpenAI  : "gpt-4o-mini" (default), "gpt-4o", "o3-mini"
    Meta    : "Meta-Llama-3.1-8B-Instruct", "Meta-Llama-3.3-70B-Instruct"
    Mistral : "Mistral-Small", "Mistral-Large"
    xAI     : "grok-3-mini"
    Microsoft: "Phi-4", "Phi-4-mini"
  EOT
  type        = string
  default     = "gpt-4o-mini"
}

variable "openai_model_version" {
  description = <<-EOT
    Model version string. Leave empty ("") to use the latest version.
    gpt-4o-mini → "2024-07-18"
    gpt-4o      → "2024-11-20"
    Third-party models (Llama, Mistral, etc.) typically use "" (latest).
  EOT
  type        = string
  default     = "2024-07-18"
}

variable "openai_capacity" {
  description = <<-EOT
    Tokens-per-minute quota in thousands (GlobalStandard scale).
    Minimum is 1 (= 1 000 TPM). GlobalStandard has much higher limits
    than Standard. Start at 1 and increase as needed for the hackathon.
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
