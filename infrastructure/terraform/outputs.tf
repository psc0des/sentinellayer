# =============================================================================
# SentinelLayer - Terraform Outputs (Foundry Only)
# =============================================================================

# --- Resource Group ---

output "resource_group_name" {
  description = "Name of the deployed resource group"
  value       = azurerm_resource_group.sentinel.name
}

output "location" {
  description = "Azure region where core resources were deployed"
  value       = azurerm_resource_group.sentinel.location
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
  value       = "https://${azurerm_search_service.sentinel.name}.search.windows.net"
}

output "search_primary_key" {
  description = "Azure AI Search primary admin key (sensitive)"
  value       = azurerm_search_service.sentinel.primary_key
  sensitive   = true
}

output "search_index_name" {
  description = "Name of the search index to use"
  value       = "incident-history"
}

# --- Azure Cosmos DB ---

output "cosmos_endpoint" {
  description = "Cosmos DB account endpoint URL"
  value       = azurerm_cosmosdb_account.sentinel.endpoint
}

output "cosmos_primary_key" {
  description = "Cosmos DB primary key (sensitive)"
  value       = azurerm_cosmosdb_account.sentinel.primary_key
  sensitive   = true
}

output "cosmos_database" {
  description = "Cosmos DB database name"
  value       = azurerm_cosmosdb_sql_database.sentinellayer.name
}

output "cosmos_container_decisions" {
  description = "Cosmos DB container for governance decisions"
  value       = azurerm_cosmosdb_sql_container.governance_decisions.name
}

# --- Log Analytics ---

output "log_analytics_workspace_id" {
  description = "Log Analytics workspace ID"
  value       = azurerm_log_analytics_workspace.sentinel.workspace_id
}

output "log_analytics_workspace_resource_id" {
  description = "Log Analytics workspace resource ID"
  value       = azurerm_log_analytics_workspace.sentinel.id
}

# --- Key Vault ---

output "keyvault_url" {
  description = "Azure Key Vault vault URI"
  value       = azurerm_key_vault.sentinel.vault_uri
}

output "keyvault_name" {
  description = "Azure Key Vault resource name"
  value       = azurerm_key_vault.sentinel.name
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

# --- Helpful summary ---

output "next_steps" {
  description = "What to do after terraform apply"
  value       = <<-EOT

    Infrastructure deployed successfully (Foundry-only).

    Generate .env automatically (Managed Identity + Key Vault mode):
      bash scripts/setup_env.sh

    Runtime endpoint values:
      AZURE_OPENAI_ENDPOINT   <- foundry_endpoint
      AZURE_OPENAI_DEPLOYMENT <- foundry_deployment
      AZURE_OPENAI_API_VERSION=2025-01-01-preview

    Runtime Key Vault secret names:
      AZURE_OPENAI_API_KEY_SECRET_NAME <- keyvault_secret_name_foundry_key
      AZURE_SEARCH_API_KEY_SECRET_NAME <- keyvault_secret_name_search_key
      COSMOS_KEY_SECRET_NAME           <- keyvault_secret_name_cosmos_key

    Optional local override (not recommended for real env):
      AZURE_OPENAI_API_KEY / AZURE_SEARCH_API_KEY / COSMOS_KEY

    Also set:
      AZURE_SUBSCRIPTION_ID=<your-id>
      AZURE_TENANT_ID=<your-id>
      USE_LOCAL_MOCKS=false

  EOT
}
