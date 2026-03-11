# =============================================================================
# RuriSkry - Azure Infrastructure (Foundry Only)
# =============================================================================
# This configuration manages Foundry (AIServices) as the only LLM platform.
# =============================================================================

terraform {
  required_version = ">= 1.5.0"

  required_providers {
    azurerm = {
      source  = "hashicorp/azurerm"
      version = "~> 4.0"
    }

    # AzAPI wraps the Azure REST API directly, giving access to every Azure
    # feature including preview APIs not yet in the AzureRM provider.
    # Used here for Foundry project management (allowProjectManagement flag
    # and azapi_resource for the project itself).
    # Docs: https://registry.terraform.io/providers/azure/azapi/latest
    azapi = {
      source  = "azure/azapi"
      version = "~> 2.0"
    }

    # Used for time_sleep — waits for Azure role assignment propagation before
    # the Container App tries to pull from ACR using its Managed Identity.
    time = {
      source  = "hashicorp/time"
      version = "~> 0.12"
    }
  }

  # Remote state — Azure Blob Storage.
  # One-time setup (run once before GitHub Actions can work):
  #   az group create -n ruriskry-tfstate-rg -l eastus2
  #   az storage account create -n ruriskrytfstate<suffix> -g ruriskry-tfstate-rg -l eastus2 --sku Standard_LRS
  #   az storage container create -n tfstate --account-name ruriskrytfstate<suffix>
  # Then migrate local state:
  #   terraform init -migrate-state
  backend "azurerm" {
    resource_group_name  = "ruriskry-tfstate-rg"
    storage_account_name = "ruriskrytfstatepsc0des"
    container_name       = "tfstate"
    key                  = "terraform-core.tfstate"
  }
}

provider "azurerm" {
  features {}
  subscription_id = var.subscription_id
}

# AzAPI inherits credentials from the az CLI login (same as AzureRM).
# No extra authentication needed.
provider "azapi" {}

data "azurerm_client_config" "current" {}

locals {
  name_suffix = var.suffix

  # Consistent name prefix for all hyphenated resources.
  # "ruriskry-core" is the longest prefix that satisfies every Azure name
  # length constraint, including Key Vault (max 24 chars):
  #   ruriskry-core-kv-<suffix> → 24 chars with a 7-char suffix.
  # ACR names are alphanumeric-only (no hyphens) — use acr_prefix instead.
  name_prefix = "ruriskry-core"
  acr_prefix  = "ruriskrycore"

  common_tags = {
    project     = "ruriskry"
    environment = var.env
    managed_by  = "terraform"
  }

  default_foundry_name = "${local.name_prefix}-foundry-${local.name_suffix}"
  foundry_account_name = var.foundry_account_name != "" ? var.foundry_account_name : local.default_foundry_name
  foundry_subdomain    = var.foundry_custom_subdomain_name != "" ? var.foundry_custom_subdomain_name : local.foundry_account_name
}

# =============================================================================
# 1. Resource Group
# =============================================================================

resource "azurerm_resource_group" "ruriskry" {
  name     = var.resource_group_name
  location = var.location
  tags     = local.common_tags
}

# =============================================================================
# 2. Log Analytics Workspace
# =============================================================================

resource "azurerm_log_analytics_workspace" "ruriskry" {
  name                = "${local.name_prefix}-log-${local.name_suffix}"
  resource_group_name = azurerm_resource_group.ruriskry.name
  location            = azurerm_resource_group.ruriskry.location
  sku                 = "PerGB2018"
  retention_in_days   = 30
  tags                = local.common_tags
}

# =============================================================================
# 3. Azure Key Vault
# =============================================================================

resource "azurerm_key_vault" "ruriskry" {
  name                = "${local.name_prefix}-kv-${local.name_suffix}"
  resource_group_name = azurerm_resource_group.ruriskry.name
  location            = azurerm_resource_group.ruriskry.location
  tenant_id           = data.azurerm_client_config.current.tenant_id
  sku_name            = "standard"

  # Purge protection disabled — allows clean redeploys with the same KV name
  # without hitting the 90-day soft-delete retention window. Acceptable for
  # this project; enable in regulated production environments where accidental
  # permanent deletion must be prevented.
  purge_protection_enabled   = false
  soft_delete_retention_days = 7

  access_policy {
    tenant_id = data.azurerm_client_config.current.tenant_id
    object_id = data.azurerm_client_config.current.object_id

    secret_permissions = [
      "Backup", "Delete", "Get", "List", "Purge", "Recover", "Restore", "Set"
    ]
  }

  tags = local.common_tags

  # The Container App's Managed Identity is granted access via a separate
  # azurerm_key_vault_access_policy resource (backend_identity). Mixing inline
  # access_policy blocks with standalone resources causes perpetual drift in
  # terraform plan. ignore_changes prevents Terraform from trying to "correct"
  # the KV's access_policy list on every apply.
  lifecycle {
    ignore_changes = [access_policy]
  }
}

resource "azurerm_key_vault_access_policy" "managed_identity_readers" {
  for_each = toset(var.managed_identity_principal_ids)

  key_vault_id = azurerm_key_vault.ruriskry.id
  tenant_id    = data.azurerm_client_config.current.tenant_id
  object_id    = each.value

  secret_permissions = ["Get", "List"]
}

# =============================================================================
# 4. Foundry (AIServices)
# =============================================================================

resource "azurerm_ai_services" "foundry" {
  name                  = local.foundry_account_name
  custom_subdomain_name = local.foundry_subdomain
  resource_group_name   = azurerm_resource_group.ruriskry.name
  location              = var.foundry_location
  sku_name              = "S0"
  # SEC-05: Disable local key authentication — all access must go through
  # Managed Identity (DefaultAzureCredential). Keys are still stored in Key
  # Vault as a fallback for local dev, but the API rejects key-based calls
  # in Azure, preventing credential theft from being immediately exploitable.
  local_authentication_enabled       = false
  public_network_access              = "Enabled"
  outbound_network_access_restricted = false
  tags                               = local.common_tags

  identity {
    type = "SystemAssigned"
  }

  # Azure requires all nested Foundry projects to be deleted before the account
  # can be destroyed. Projects created via the portal or outside Terraform won't
  # be in state, so Terraform can't delete them automatically. This provisioner
  # runs on destroy and removes any projects that exist under the account first.
  provisioner "local-exec" {
    when       = destroy
    command    = <<-EOT
      for project in $(az rest --method GET \
        --url "https://management.azure.com${self.id}/projects?api-version=2025-04-01-preview" \
        --query "value[].name" -o tsv 2>/dev/null); do
        az rest --method DELETE \
          --url "https://management.azure.com${self.id}/projects/$project?api-version=2025-04-01-preview" \
          2>/dev/null || true
      done
    EOT
    on_failure = continue
  }
}

# =============================================================================
# 5b. Foundry Project (AzAPI)
# =============================================================================
# Why AzAPI instead of AzureRM?
#   The AzureRM provider's azurerm_cognitive_account_project resource requires
#   allowProjectManagement=true on the parent account, but AzureRM has no
#   argument to set that flag. AzAPI talks directly to the Azure REST API and
#   can set both the flag and create the project in full.
#
# azapi_update_resource — patches the existing AzureRM-managed AIServices
#   account to enable project management. It only touches the fields listed
#   in body{} — everything else AzureRM manages is left untouched.
#
# azapi_resource — creates the Foundry project under the account. Uses the
#   2025-04-01-preview API which is the current Foundry project API version.

# Azure AIServices accounts report Succeeded before all internal provisioning
# is complete. Patching allowProjectManagement immediately after creation
# causes a 409 RequestConflict. A 90-second wait is sufficient for the account
# to reach a fully terminal state before the AzAPI patch runs.
resource "time_sleep" "foundry_ready" {
  count           = var.create_foundry_project ? 1 : 0
  create_duration = "90s"
  depends_on      = [azurerm_ai_services.foundry]
}

resource "azapi_update_resource" "foundry_allow_projects" {
  count       = var.create_foundry_project ? 1 : 0
  type        = "Microsoft.CognitiveServices/accounts@2025-04-01-preview"
  resource_id = azurerm_ai_services.foundry.id

  body = {
    properties = {
      allowProjectManagement = true
    }
  }

  depends_on = [time_sleep.foundry_ready]
}

resource "azapi_resource" "foundry_project" {
  count     = var.create_foundry_project ? 1 : 0
  type      = "Microsoft.CognitiveServices/accounts/projects@2025-04-01-preview"
  name      = var.foundry_project_name
  parent_id = azurerm_ai_services.foundry.id
  location  = var.foundry_location

  body = {
    properties = {}
    identity = {
      type = "SystemAssigned"
    }
  }

  depends_on = [azapi_update_resource.foundry_allow_projects]
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

resource "azurerm_search_service" "ruriskry" {
  name                = "${local.name_prefix}-search-${local.name_suffix}"
  resource_group_name = azurerm_resource_group.ruriskry.name
  location            = azurerm_resource_group.ruriskry.location
  sku                 = var.search_sku

  replica_count   = var.search_sku == "free" ? null : 1
  partition_count = var.search_sku == "free" ? null : 1

  tags = local.common_tags
}

# =============================================================================
# 6. Cosmos DB (SQL API)
# =============================================================================

resource "azurerm_cosmosdb_account" "ruriskry" {
  name                = "${local.name_prefix}-cosmos-${local.name_suffix}"
  resource_group_name = azurerm_resource_group.ruriskry.name
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

  # SEC-04: Access is controlled via Managed Identity (RBAC) rather than
  # network IP filtering. Container Apps on the Consumption plan have dynamic
  # outbound IPs so IP allowlisting would break connectivity. The Container App
  # authenticates to Cosmos DB using DefaultAzureCredential — no connection
  # string or key is used at runtime.
  network_acl_bypass_for_azure_services = true

  free_tier_enabled = var.cosmos_free_tier
  tags              = local.common_tags
}

resource "azurerm_cosmosdb_sql_database" "ruriskry" {
  name                = "ruriskry"
  resource_group_name = azurerm_resource_group.ruriskry.name
  account_name        = azurerm_cosmosdb_account.ruriskry.name
}

resource "azurerm_cosmosdb_sql_container" "governance_decisions" {
  name                = "governance-decisions"
  resource_group_name = azurerm_resource_group.ruriskry.name
  account_name        = azurerm_cosmosdb_account.ruriskry.name
  database_name       = azurerm_cosmosdb_sql_database.ruriskry.name

  partition_key_paths   = ["/resource_id"]
  partition_key_version = 2

  indexing_policy {
    indexing_mode = "consistent"

    included_path {
      path = "/*"
    }
  }
}

resource "azurerm_cosmosdb_sql_container" "governance_agents" {
  name                = "governance-agents"
  resource_group_name = azurerm_resource_group.ruriskry.name
  account_name        = azurerm_cosmosdb_account.ruriskry.name
  database_name       = azurerm_cosmosdb_sql_database.ruriskry.name

  partition_key_paths   = ["/name"]
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
# (existing Key Vault secrets below — see section 8+ for new resources)
# =============================================================================

resource "azurerm_key_vault_secret" "foundry_primary_key" {
  name         = var.keyvault_secret_name_foundry_key
  value        = azurerm_ai_services.foundry.primary_access_key
  key_vault_id = azurerm_key_vault.ruriskry.id

  depends_on = [azurerm_key_vault_access_policy.managed_identity_readers]
}

resource "azurerm_key_vault_secret" "search_primary_key" {
  name         = var.keyvault_secret_name_search_key
  value        = azurerm_search_service.ruriskry.primary_key
  key_vault_id = azurerm_key_vault.ruriskry.id

  depends_on = [azurerm_key_vault_access_policy.managed_identity_readers]
}

resource "azurerm_key_vault_secret" "cosmos_primary_key" {
  name         = var.keyvault_secret_name_cosmos_key
  value        = azurerm_cosmosdb_account.ruriskry.primary_key
  key_vault_id = azurerm_key_vault.ruriskry.id

  depends_on = [azurerm_key_vault_access_policy.managed_identity_readers]
}

# SEC-07: Teams webhook stored in Key Vault — not as a plain env var.
# The webhook URL grants anyone who has it the ability to post to your Teams
# channel. Storing it in Key Vault and injecting via Managed Identity means
# it never appears in tfstate, az containerapp show output, or env var dumps.
resource "azurerm_key_vault_secret" "teams_webhook" {
  count        = var.teams_webhook_url != "" ? 1 : 0
  name         = "teams-webhook-url"
  value        = var.teams_webhook_url
  key_vault_id = azurerm_key_vault.ruriskry.id

  depends_on = [azurerm_key_vault_access_policy.managed_identity_readers]
}

# GitHub PAT is stored manually in Key Vault — Terraform never touches the value.
# Run this once before setting use_github_pat = true in tfvars:
#   az keyvault secret set \
#     --vault-name ruriskry-kv-<suffix> \
#     --name github-pat \
#     --value "github_pat_xxx..."
# The Container App picks it up via its Managed Identity — no Terraform resource needed.

# =============================================================================
# 8. User-Assigned Managed Identity (for ACR pull)
# =============================================================================
# Why User-Assigned instead of System-Assigned for ACR pull?
#   System-Assigned identity only exists after the Container App is created,
#   creating a chicken-and-egg problem: the Container App needs AcrPull at
#   creation time, but we can't grant AcrPull until after it's created.
#   A User-Assigned identity is created independently, gets AcrPull before
#   the Container App exists, and is attached at creation time — no race.
#
#   The Container App still has System-Assigned identity for Key Vault access
#   (secrets, API keys). The User-Assigned identity is only for ACR pull.

resource "azurerm_user_assigned_identity" "acr_pull" {
  name                = "${local.name_prefix}-acr-pull-${local.name_suffix}"
  resource_group_name = azurerm_resource_group.ruriskry.name
  location            = azurerm_resource_group.ruriskry.location
}

# =============================================================================
# 8b. Azure Container Registry (ACR)
# =============================================================================
# Stores the Docker image for the FastAPI backend.
# Basic SKU is sufficient for a single-team workload.
# admin_enabled = false — image pull uses the User-Assigned Managed Identity.

resource "azurerm_container_registry" "ruriskry" {
  name                = "${local.acr_prefix}${local.name_suffix}"
  resource_group_name = azurerm_resource_group.ruriskry.name
  location            = azurerm_resource_group.ruriskry.location
  sku                 = "Basic"
  # SEC-02: Admin auth disabled — shared password would appear in tfstate in
  # plaintext. The Container App pulls images using the User-Assigned Managed
  # Identity (azurerm_user_assigned_identity.acr_pull) via the AcrPull role.
  admin_enabled = false
  tags          = local.common_tags
}

# Grant the User-Assigned Managed Identity permission to pull images from ACR.
# This role assignment is created BEFORE the Container App, solving the
# chicken-and-egg problem: the Container App needs AcrPull at creation time,
# but System-Assigned identity doesn't exist until after it's created.
# By using the User-Assigned identity here, AcrPull is already propagated
# before the Container App resource block even starts.
resource "azurerm_role_assignment" "acr_pull" {
  scope                = azurerm_container_registry.ruriskry.id
  role_definition_name = "AcrPull"
  principal_id         = azurerm_user_assigned_identity.acr_pull.principal_id
}

# Azure role assignments take 1-3 minutes to propagate globally.
# Without this delay the Container App's first revision starts before AcrPull
# is active, causing the image pull to fail with Operation expired.
# The sleep runs right after the role assignment so by the time the full
# terraform apply reaches the Container App, the permission is live.
# Docker build/push is handled by scripts/deploy.sh (not in Terraform) —
# this sleep is enough because the script runs docker push before the full apply.
# NOTE: time_sleep for ACR role propagation is no longer needed.
# The Container App starts with a public MCR placeholder image (no ACR auth),
# and deploy.sh updates to the real ACR image after Stage 2 completes —
# by which time 15+ minutes have passed since the role assignment.

# =============================================================================
# 9. Container Apps Environment
# =============================================================================
# Shared managed environment for all Container Apps.
# Wired to the Log Analytics workspace so container logs appear in Azure Monitor.

resource "azurerm_container_app_environment" "ruriskry" {
  name                       = "${local.name_prefix}-env-${local.name_suffix}"
  resource_group_name        = azurerm_resource_group.ruriskry.name
  location                   = azurerm_resource_group.ruriskry.location
  log_analytics_workspace_id = azurerm_log_analytics_workspace.ruriskry.id
  tags                       = local.common_tags
}

# =============================================================================
# 10. Container App — FastAPI Backend
# =============================================================================
# Hosts the FastAPI app (src/api/dashboard_api.py) with ALL governance agents
# and operational agents running in-process.  No separate agent services.
#
# Key design:
#   - SystemAssigned managed identity → Key Vault Get/List access granted below
#   - Non-secret env vars set inline (endpoints, flags, timeouts)
#   - ACR pull uses User-Assigned Managed Identity (no password — admin_enabled = false)
#   - AZURE_KEYVAULT_URL env var lets secrets.py resolve API keys at runtime
#     using DefaultAzureCredential (Managed Identity in Azure, az login locally)
#
resource "azurerm_container_app" "backend" {
  name                         = "${local.name_prefix}-backend-${local.name_suffix}"
  resource_group_name          = azurerm_resource_group.ruriskry.name
  container_app_environment_id = azurerm_container_app_environment.ruriskry.id
  revision_mode                = "Single"
  tags                         = local.common_tags

  # Lifecycle: image is managed by deploy.sh (az containerapp update), not Terraform.
  # Terraform creates the app with a public MCR placeholder; deploy.sh swaps in
  # the real ACR image after the full apply, when AcrPull role is guaranteed
  # propagated.  This eliminates the race condition that caused "unable to pull
  # image using Managed identity" on first deploy.
  lifecycle {
    ignore_changes = [template[0].container[0].image]
  }

  # SystemAssigned — used for Key Vault access (secrets, API keys at runtime).
  # UserAssigned (acr_pull) — used exclusively for ACR image pull.
  # Separating the two means AcrPull can be granted BEFORE the Container App
  # exists, eliminating the chicken-and-egg race that causes Operation expired.
  identity {
    type         = "SystemAssigned, UserAssigned"
    identity_ids = [azurerm_user_assigned_identity.acr_pull.id]
  }

  # SEC-02: No admin credentials — pulls via User-Assigned Managed Identity.
  # The identity must be listed in the identity block above and already have
  # the AcrPull role on the registry before the Container App is created.
  registry {
    server   = azurerm_container_registry.ruriskry.login_server
    identity = azurerm_user_assigned_identity.acr_pull.id
  }

  # SEC-07: Teams webhook — read from Key Vault via Managed Identity.
  dynamic "secret" {
    for_each = var.teams_webhook_url != "" ? [1] : []
    content {
      name                = "teams-webhook-url"
      key_vault_secret_id = "${azurerm_key_vault.ruriskry.vault_uri}secrets/teams-webhook-url"
      identity            = "System"
    }
  }

  # GitHub PAT — read from Key Vault by URI via the Container App's Managed Identity.
  # The secret must be stored manually before setting use_github_pat = true.
  # Key Vault URI format: <vault_uri>secrets/<secret-name> (no trailing slash on vault_uri).
  dynamic "secret" {
    for_each = var.use_github_pat ? [1] : []
    content {
      name                = "github-pat"
      key_vault_secret_id = "${azurerm_key_vault.ruriskry.vault_uri}secrets/github-pat"
      identity            = "System"
    }
  }

  template {
    min_replicas = var.backend_min_replicas
    max_replicas = var.backend_max_replicas

    container {
      name = "backend"
      # Placeholder image — deploy.sh replaces this with the real ACR image
      # after Stage 2 (az containerapp update --image).  Using a public MCR
      # image avoids the need for ACR auth at Container App creation time.
      image  = "mcr.microsoft.com/k8se/quickstart:latest"
      cpu    = var.backend_cpu
      memory = var.backend_memory

      # ── Core mode flags ──────────────────────────────────────────────────────
      env {
        name  = "USE_LOCAL_MOCKS"
        value = "false"
      }
      env {
        name  = "USE_LIVE_TOPOLOGY"
        value = "true"
      }

      # ── Azure service endpoints ───────────────────────────────────────────
      env {
        name  = "AZURE_KEYVAULT_URL"
        value = azurerm_key_vault.ruriskry.vault_uri
      }
      env {
        name  = "AZURE_OPENAI_ENDPOINT"
        value = azurerm_ai_services.foundry.endpoint
      }
      env {
        name  = "AZURE_OPENAI_DEPLOYMENT"
        value = var.foundry_deployment_name
      }
      env {
        name  = "COSMOS_ENDPOINT"
        value = azurerm_cosmosdb_account.ruriskry.endpoint
      }
      env {
        name  = "AZURE_SEARCH_ENDPOINT"
        value = "https://${azurerm_search_service.ruriskry.name}.search.windows.net"
      }
      env {
        name  = "AZURE_SUBSCRIPTION_ID"
        value = var.subscription_id
      }

      # ── LLM tuning ────────────────────────────────────────────────────────
      env {
        name  = "LLM_TIMEOUT"
        value = tostring(var.llm_timeout)
      }
      env {
        name  = "LLM_CONCURRENCY_LIMIT"
        value = tostring(var.llm_concurrency_limit)
      }

      # ── Execution gateway ─────────────────────────────────────────────────
      env {
        name  = "EXECUTION_GATEWAY_ENABLED"
        value = tostring(var.execution_gateway_enabled)
      }

      # ── Teams notifications ───────────────────────────────────────────────
      # SEC-07: Injected from Key Vault secret via Managed Identity — not a plain value.
      dynamic "env" {
        for_each = var.teams_webhook_url != "" ? [1] : []
        content {
          name        = "TEAMS_WEBHOOK_URL"
          secret_name = "teams-webhook-url"
        }
      }
      env {
        name  = "DASHBOARD_URL"
        value = "https://${azurerm_static_web_app.dashboard.default_host_name}"
      }

      # ── Org context (risk triage) ─────────────────────────────────────────
      env {
        name  = "ORG_NAME"
        value = var.org_name
      }
      env {
        name  = "ORG_COMPLIANCE_FRAMEWORKS"
        value = var.org_compliance_frameworks
      }
      env {
        name  = "ORG_RISK_TOLERANCE"
        value = var.org_risk_tolerance
      }

      # ── Execution Gateway — IaC repo ──────────────────────────────────────
      env {
        name  = "IAC_GITHUB_REPO"
        value = var.iac_github_repo
      }
      env {
        name  = "IAC_TERRAFORM_PATH"
        value = var.iac_terraform_path
      }

      # ── GitHub PAT — injected from Key Vault secret (not a plain value) ────
      dynamic "env" {
        for_each = var.use_github_pat ? [1] : []
        content {
          name        = "GITHUB_TOKEN"
          secret_name = "github-pat"
        }
      }
    }
  }

  ingress {
    external_enabled = true
    target_port      = 8000
    transport        = "auto"

    # SEC-06: CORS is enforced at the application layer in dashboard_api.py
    # (FastAPI CORSMiddleware). The AzureRM Container App resource does not
    # expose a cors_policy block — network-level CORS is not available on
    # Container Apps; it must be handled by the app itself.

    traffic_weight {
      latest_revision = true
      percentage      = 100
    }
  }
}

# Give the Container App's managed identity read access to Key Vault secrets.
# This allows DefaultAzureCredential (via Managed Identity) to resolve
# API keys at runtime — no secrets in env vars, no .env file in the container.

resource "azurerm_key_vault_access_policy" "backend_identity" {
  key_vault_id = azurerm_key_vault.ruriskry.id
  tenant_id    = data.azurerm_client_config.current.tenant_id
  object_id    = azurerm_container_app.backend.identity[0].principal_id

  secret_permissions = ["Get", "List"]

  depends_on = [azurerm_container_app.backend]
}

# =============================================================================
# 10b. Azure AI Foundry — Cognitive Services OpenAI User role
# =============================================================================
# local_authentication_enabled = false on the Foundry account means API keys
# are rejected. All callers must authenticate via Managed Identity (RBAC).
# The Container App's SystemAssigned MI needs the "Cognitive Services OpenAI
# User" role on the Foundry account to call POST /openai/responses.
# Without this, every agent scan fails with:
#   401 PermissionDenied: lacks Microsoft.CognitiveServices/accounts/OpenAI/responses/write

resource "azurerm_role_assignment" "foundry_openai_user" {
  scope                = azurerm_ai_services.foundry.id
  role_definition_name = "Cognitive Services OpenAI User"
  principal_id         = azurerm_container_app.backend.identity[0].principal_id

  depends_on = [azurerm_container_app.backend]
}

# =============================================================================
# 10c. Subscription-level Reader — cross-RG scanning
# =============================================================================
# The governance agents scan Azure resources across all resource groups in the
# subscription using Azure Resource Graph and the Azure SDK. The Container App's
# Managed Identity needs Reader at the subscription scope to query resources
# outside ruriskry-core-engine-rg.
#
# For cross-SUBSCRIPTION scanning (e.g. a hub-spoke or multi-sub org), grant
# the same Reader role on each additional subscription manually:
#
#   az role assignment create \
#     --assignee <container_app_principal_id> \
#     --role Reader \
#     --scope /subscriptions/<other-subscription-id>
#
# Get the principal ID after apply with:
#   terraform output -raw backend_container_app_principal_id
resource "azurerm_role_assignment" "subscription_reader" {
  scope                = "/subscriptions/${var.subscription_id}"
  role_definition_name = "Reader"
  principal_id         = azurerm_container_app.backend.identity[0].principal_id

  depends_on = [azurerm_container_app.backend]
}

# =============================================================================
# 10d. Network Contributor — execute NSG rule remediation via Azure SDK
# =============================================================================
# When a governance verdict is APPROVED or manually actioned, the Execution
# Gateway calls the Azure SDK to delete or modify NSG security rules
# (_execute_fix_via_sdk in execution_gateway.py → NetworkManagementClient).
# The MI needs Network Contributor on the subscription to delete securityRules
# across all resource groups it manages.
#
# Scope: subscription-level so it covers all managed resource groups.
# If you want tighter scope, change to the specific resource group:
#   scope = azurerm_resource_group.rg.id
resource "azurerm_role_assignment" "network_contributor" {
  scope                = "/subscriptions/${var.subscription_id}"
  role_definition_name = "Network Contributor"
  principal_id         = azurerm_container_app.backend.identity[0].principal_id

  depends_on = [azurerm_container_app.backend]
}

# =============================================================================
# 11. Static Web App — React Dashboard
# =============================================================================
# Hosts the compiled React dashboard (dashboard/dist/).
# Free tier: 100 GB bandwidth/month, custom domain, global CDN — sufficient
# for demos and light production use.
#
# Deployment: push to GitHub and use the deployment_token in a GitHub Actions
# workflow, OR deploy manually:
#   npm run build   (inside dashboard/)
#   npx @azure/static-web-apps-cli deploy ./dist \
#     --deployment-token $(terraform output -raw dashboard_deployment_token)

resource "azurerm_static_web_app" "dashboard" {
  name                = "${local.name_prefix}-dashboard-${local.name_suffix}"
  resource_group_name = azurerm_resource_group.ruriskry.name
  location            = var.static_web_app_location
  sku_tier            = "Free"
  sku_size            = "Free"
  tags                = local.common_tags
}

# =============================================================================
# 12. Security hardening — tfstate storage lock (SEC-08)
# =============================================================================
# The tfstate storage account holds Terraform state which contains sensitive
# values (Foundry keys, Cosmos keys). A CanNotDelete lock prevents accidental
# or malicious deletion of the storage account and its contents.
# The lock is applied to the tfstate resource group, not managed by this config
# directly — add it once via CLI after creating the storage account:
#
#   az lock create \
#     --name ruriskry-tfstate-lock \
#     --resource-group ruriskry-tfstate-rg \
#     --lock-type CanNotDelete \
#     --notes "Protects Terraform state storage from accidental deletion"
#
# Additionally, enable versioning on the storage account so every state write
# is preserved and recoverable:
#
#   az storage account blob-service-properties update \
#     --account-name ruriskrytfstate<suffix> \
#     --enable-versioning true

# SEC-08: Lock the main resource group to prevent accidental deletion of all
# production resources in a single az group delete command.
#
# enable_rg_lock = true → adds a CanNotDelete lock on the RG.
# Recommended for production; disable during active development to avoid
# ScopeLocked 409 errors on terraform destroy / reapply cycles.
resource "azurerm_management_lock" "ruriskry_rg" {
  count = var.enable_rg_lock ? 1 : 0

  name       = "ruriskry-core-engine-rg-lock"
  scope      = azurerm_resource_group.ruriskry.id
  lock_level = "CanNotDelete"
  notes      = "Prevents accidental deletion of production infrastructure. Managed by Terraform — removed automatically during terraform destroy."

  depends_on = [
    azurerm_container_app.backend,
    azurerm_container_app_environment.ruriskry,
    azurerm_static_web_app.dashboard,
    azurerm_container_registry.ruriskry,
    azurerm_cosmosdb_account.ruriskry,
    azurerm_cosmosdb_sql_database.ruriskry,
    azurerm_cosmosdb_sql_container.governance_decisions,
    azurerm_cosmosdb_sql_container.governance_agents,
    azurerm_ai_services.foundry,
    azurerm_search_service.ruriskry,
    azurerm_key_vault.ruriskry,
    azurerm_log_analytics_workspace.ruriskry,
    azurerm_user_assigned_identity.acr_pull,
    azurerm_role_assignment.acr_pull,
    azurerm_role_assignment.subscription_reader,
  ]
}
