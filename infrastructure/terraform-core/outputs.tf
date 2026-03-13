# =============================================================================
# RuriSkry - Terraform Outputs (Foundry Only)
# =============================================================================

# --- Resource Group ---

output "resource_group_name" {
  description = "Name of the deployed resource group"
  value       = azurerm_resource_group.ruriskry.name
}

output "location" {
  description = "Azure region where core resources were deployed"
  value       = azurerm_resource_group.ruriskry.location
}

# --- Foundry runtime outputs ---

output "ai_platform" {
  description = "Active AI platform"
  value       = "foundry"
}

output "foundry_endpoint" {
  description = "Foundry endpoint URL"
  value       = azurerm_ai_services.foundry.endpoint
}

output "foundry_primary_key" {
  description = "Foundry primary API key"
  value       = azurerm_ai_services.foundry.primary_access_key
  sensitive   = true
}

output "foundry_deployment" {
  description = "Foundry deployment name"
  value       = var.create_foundry_deployment ? azurerm_cognitive_deployment.foundry_primary[0].name : var.foundry_deployment_name
}

output "foundry_account_name" {
  description = "Foundry account name"
  value       = azurerm_ai_services.foundry.name
}

output "foundry_project_name" {
  description = "Foundry project name (empty when not created)"
  value       = var.create_foundry_project ? azapi_resource.foundry_project[0].name : ""
}

# --- Azure AI Search ---

output "search_endpoint" {
  description = "Azure AI Search service endpoint URL"
  value       = "https://${azurerm_search_service.ruriskry.name}.search.windows.net"
}

output "search_primary_key" {
  description = "Azure AI Search primary admin key (sensitive)"
  value       = azurerm_search_service.ruriskry.primary_key
  sensitive   = true
}

output "search_index_name" {
  description = "Name of the search index to use"
  value       = "incident-history"
}

# --- Azure Cosmos DB ---

output "cosmos_endpoint" {
  description = "Cosmos DB account endpoint URL"
  value       = azurerm_cosmosdb_account.ruriskry.endpoint
}

output "cosmos_primary_key" {
  description = "Cosmos DB primary key (sensitive)"
  value       = azurerm_cosmosdb_account.ruriskry.primary_key
  sensitive   = true
}

output "cosmos_database" {
  description = "Cosmos DB database name"
  value       = azurerm_cosmosdb_sql_database.ruriskry.name
}

output "cosmos_container_decisions" {
  description = "Cosmos DB container for governance decisions"
  value       = azurerm_cosmosdb_sql_container.governance_decisions.name
}

output "cosmos_container_alerts" {
  description = "Cosmos DB container for alert investigation records"
  value       = azurerm_cosmosdb_sql_container.governance_alerts.name
}

# --- Log Analytics ---

output "log_analytics_workspace_id" {
  description = "Log Analytics workspace ID"
  value       = azurerm_log_analytics_workspace.ruriskry.workspace_id
}

output "log_analytics_workspace_resource_id" {
  description = "Log Analytics workspace resource ID"
  value       = azurerm_log_analytics_workspace.ruriskry.id
}

# --- Key Vault ---

output "keyvault_url" {
  description = "Azure Key Vault vault URI"
  value       = azurerm_key_vault.ruriskry.vault_uri
}

output "keyvault_name" {
  description = "Azure Key Vault resource name"
  value       = azurerm_key_vault.ruriskry.name
}

output "keyvault_secret_name_foundry_key" {
  description = "Key Vault secret name for Foundry primary key"
  value       = azurerm_key_vault_secret.foundry_primary_key.name
}

output "keyvault_secret_name_search_key" {
  description = "Key Vault secret name for Azure AI Search primary key"
  value       = azurerm_key_vault_secret.search_primary_key.name
}

output "keyvault_secret_name_cosmos_key" {
  description = "Key Vault secret name for Cosmos DB primary key"
  value       = azurerm_key_vault_secret.cosmos_primary_key.name
}

# --- Azure Container Registry ---

output "acr_login_server" {
  description = "ACR login server URL — use as the image registry prefix"
  value       = azurerm_container_registry.ruriskry.login_server
}

output "acr_name" {
  description = "ACR resource name — used with 'az acr login --name'"
  value       = azurerm_container_registry.ruriskry.name
}


# --- Container App (backend) ---

output "backend_url" {
  description = "Public HTTPS URL of the FastAPI backend Container App"
  value       = "https://${azurerm_container_app.backend.ingress[0].fqdn}"
}

output "backend_container_app_name" {
  description = "Container App resource name — used with 'az containerapp update'"
  value       = azurerm_container_app.backend.name
}

output "backend_container_app_principal_id" {
  description = "Managed Identity principal ID of the Container App — use this to grant Reader on additional subscriptions for cross-subscription scanning"
  value       = azurerm_container_app.backend.identity[0].principal_id
}

# --- Static Web App (dashboard) ---

output "dashboard_url" {
  description = "Public HTTPS URL of the React dashboard (Static Web App)"
  value       = "https://${azurerm_static_web_app.dashboard.default_host_name}"
}

output "dashboard_deployment_token" {
  description = "Static Web App deployment token — use with SWA CLI or GitHub Actions"
  value       = azurerm_static_web_app.dashboard.api_key
  sensitive   = true
}

# --- Azure Monitor ---

output "alert_action_group_id" {
  description = "Resource ID of the Azure Monitor Action Group — attach this to alert rules"
  value       = azurerm_monitor_action_group.ruriskry.id
}

output "alert_webhook_url" {
  description = "Webhook URL the Action Group posts to — matches /api/alert-trigger on the backend"
  value       = "https://${azurerm_container_app.backend.ingress[0].fqdn}/api/alert-trigger"
}

# --- Helpful summary ---

output "next_steps" {
  description = "Live URLs and next steps after deployment"
  value       = <<-EOT

    ✅ Infrastructure deployed.

    ── Live endpoints ─────────────────────────────────────────────
      Dashboard  →  https://${azurerm_static_web_app.dashboard.default_host_name}
      Backend    →  https://${azurerm_container_app.backend.ingress[0].fqdn}

    ── If you used deploy.sh ──────────────────────────────────────
      Deployment is complete. See the script output for remaining manual steps.

    ── If you ran terraform apply manually ────────────────────────
      See infrastructure/terraform-core/deploy.md § Manual Deploy
      for the Docker build/push and dashboard deploy commands.

    ── Wiring alert rules to the Action Group ─────────────────────
      After apply, attach existing alert rules to the Action Group:
        az monitor metrics alert update \
          --name <your-alert-rule> \
          --resource-group <rg> \
          --add-action $(terraform output -raw alert_action_group_id)

      Or set the Action Group in the Azure Portal:
        Monitor → Alerts → Alert rules → Edit rule → Actions tab

  EOT
}
