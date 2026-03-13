# =============================================================================
# RuriSkry - Terraform Input Variables
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
  default     = "ruriskry-core-engine-rg"
}

variable "enable_rg_lock" {
  description = "Enable CanNotDelete management lock on the resource group. Set false during development to allow clean terraform destroy without ScopeLocked errors."
  type        = bool
  default     = false
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
  default     = "ruriskry"
}

variable "create_foundry_deployment" {
  description = "Create model deployment in Foundry from Terraform."
  type        = bool
  default     = false
}

variable "foundry_model" {
  description = "Foundry model name (example: gpt-5-mini)."
  type        = string
  default     = "gpt-5-mini"
}

variable "foundry_model_version" {
  description = "Foundry model version string. Use empty string to let Azure choose latest."
  type        = string
  default     = ""
}

variable "foundry_deployment_name" {
  description = "Foundry deployment name used by application runtime (passed as AZURE_OPENAI_DEPLOYMENT env var)."
  type        = string
  default     = "gpt-5-mini"
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

# ---------------------------------------------------------------------------
# Container App — backend
# ---------------------------------------------------------------------------

variable "backend_image" {
  description = <<-EOT
    Docker image name (and tag) to run in the Container App.
    Must exist in the ACR created by this Terraform config.
    Example: "ruriskry-backend:latest"
    Build and push first:
      docker build -t <acr_login_server>/ruriskry-backend:latest .
      az acr login --name ruriskry<suffix>
      docker push <acr_login_server>/ruriskry-backend:latest
  EOT
  type        = string
  default     = "ruriskry-backend:latest"
}

variable "backend_cpu" {
  description = "vCPU allocated to the backend container (e.g. 0.5, 1.0, 2.0)."
  type        = number
  default     = 1.0
}

variable "backend_memory" {
  description = "Memory allocated to the backend container (e.g. '2Gi'). Must match a valid cpu/memory pair."
  type        = string
  default     = "2Gi"
}

variable "backend_min_replicas" {
  description = "Minimum number of Container App replicas. 0 = scale to zero when idle (cold start ~10 s)."
  type        = number
  default     = 1
}

variable "backend_max_replicas" {
  description = "Maximum number of Container App replicas."
  type        = number
  default     = 3
}

# ---------------------------------------------------------------------------
# Runtime feature flags (passed as env vars into the Container App)
# ---------------------------------------------------------------------------

variable "execution_gateway_enabled" {
  description = "Enable Execution Gateway — routes APPROVED verdicts to IaC PRs. Requires GITHUB_TOKEN set separately."
  type        = bool
  default     = false
}

variable "llm_timeout" {
  description = "Hard timeout in seconds for each LLM call (asyncio.wait_for + HTTP client timeout). Must be >300s for multi-step agent loops with gpt-5-mini."
  type        = number
  default     = 600
}

variable "llm_concurrency_limit" {
  description = "Max simultaneous LLM calls across all agents (shared semaphore). 3 operational + 4 governance + 1 execution = up to 8 callers; 6 is safe at 200K TPM. Lower only if hitting 429s."
  type        = number
  default     = 6
}

variable "slack_webhook_url" {
  description = "Slack Incoming Webhook URL for DENIED/ESCALATED governance alerts and Azure Monitor alert notifications. Leave empty to disable."
  type        = string
  default     = ""
  sensitive   = true
}

variable "slack_notifications_enabled" {
  description = "Master on/off switch for all Slack notifications. Set false to pause without removing the webhook URL."
  type        = bool
  default     = true
}

# ---------------------------------------------------------------------------
# Org context (risk triage env vars)
# ---------------------------------------------------------------------------

variable "org_name" {
  description = "Display name for your organisation (used in triage context)."
  type        = string
  default     = "Contoso"
}

variable "org_compliance_frameworks" {
  description = "Comma-separated compliance frameworks in scope, e.g. 'HIPAA,PCI-DSS'. Empty = no compliance scope."
  type        = string
  default     = ""
}

variable "org_risk_tolerance" {
  description = "Organisation risk posture: conservative, moderate, or aggressive."
  type        = string
  default     = "moderate"

  validation {
    condition     = contains(["conservative", "moderate", "aggressive"], var.org_risk_tolerance)
    error_message = "org_risk_tolerance must be conservative, moderate, or aggressive."
  }
}

# ---------------------------------------------------------------------------
# Execution Gateway — GitHub
# ---------------------------------------------------------------------------

variable "use_github_pat" {
  description = <<-EOT
    Set to true after manually storing your GitHub PAT in Key Vault:
      az keyvault secret set \
        --vault-name <keyvault_name> \
        --name github-pat \
        --value "github_pat_xxx..."
    When true, the Container App wires GITHUB_TOKEN from that Key Vault secret
    via its Managed Identity. The PAT value never enters Terraform or tfstate.
  EOT
  type        = bool
  default     = false
}

variable "iac_github_repo" {
  description = "GitHub repo that owns the IaC, e.g. 'psc0des/ruriskry-iac-test'."
  type        = string
  default     = ""
}

variable "iac_terraform_path" {
  description = "Path within iac_github_repo to the Terraform config directory."
  type        = string
  default     = "infrastructure/terraform-prod"
}

# ---------------------------------------------------------------------------
# Static Web App
# ---------------------------------------------------------------------------

variable "static_web_app_location" {
  description = <<-EOT
    Azure region for Static Web Apps. Limited availability — use one of:
    eastus2, centralus, westus2, westeurope, eastasia, southeastasia.
  EOT
  type        = string
  default     = "eastus2"
}
