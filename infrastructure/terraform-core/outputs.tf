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
  value       = var.create_foundry_project ? azurerm_cognitive_account_project.foundry[0].name : ""
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

output "acr_admin_username" {
  description = "ACR admin username (for docker login)"
  value       = azurerm_container_registry.ruriskry.admin_username
}

output "acr_admin_password" {
  description = "ACR admin password (for docker login)"
  value       = azurerm_container_registry.ruriskry.admin_password
  sensitive   = true
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

# --- Helpful summary ---

output "next_steps" {
  description = "What to do after terraform apply"
  value       = <<-EOT

    ✅ Infrastructure deployed.

    ── Deploy FastAPI backend ─────────────────────────────────────
    1. Build and push Docker image:
         az acr login --name ${azurerm_container_registry.ruriskry.name}
         docker build -t ${azurerm_container_registry.ruriskry.login_server}/ruriskry-backend:latest .
         docker push ${azurerm_container_registry.ruriskry.login_server}/ruriskry-backend:latest

    2. Update the Container App to pull the new image:
         az containerapp update \
           --name ${azurerm_container_app.backend.name} \
           --resource-group ${azurerm_resource_group.ruriskry.name} \
           --image ${azurerm_container_registry.ruriskry.login_server}/ruriskry-backend:latest

    3. Backend is live at:
         https://${azurerm_container_app.backend.ingress[0].fqdn}

    ── Deploy React dashboard ──────────────────────────────────────
    1. Set backend URL before building — create dashboard/.env.production:
         VITE_API_URL=https://${azurerm_container_app.backend.ingress[0].fqdn}

    2. Build the React app:
         cd dashboard && npm run build

    3. Deploy using SWA CLI:
         npx @azure/static-web-apps-cli deploy ./dist \
           --deployment-token <run: terraform output -raw dashboard_deployment_token> \
           --env production

    4. Dashboard is live at:
         https://${azurerm_static_web_app.dashboard.default_host_name}

    ── Local .env values ──────────────────────────────────────────
      AZURE_OPENAI_ENDPOINT = ${azurerm_ai_services.foundry.endpoint}
      COSMOS_ENDPOINT       = ${azurerm_cosmosdb_account.ruriskry.endpoint}
      AZURE_SEARCH_ENDPOINT = https://${azurerm_search_service.ruriskry.name}.search.windows.net
      AZURE_KEYVAULT_URL    = ${azurerm_key_vault.ruriskry.vault_uri}

  EOT
}
