# =============================================================================
# SentinelLayer — Terraform Outputs
# =============================================================================
# After `terraform apply`, these values are printed to the terminal.
# The setup_env.sh script reads these with `terraform output -json`
# to auto-generate the project .env file.
#
# Values marked `sensitive = true` are hidden in terminal output but can
# still be read by scripts via `terraform output -raw <name>`.
# =============================================================================

# --- Resource Group ---

output "resource_group_name" {
  description = "Name of the deployed resource group"
  value       = azurerm_resource_group.sentinel.name
}

output "location" {
  description = "Azure region where resources were deployed"
  value       = azurerm_resource_group.sentinel.location
}

# --- Azure AI Foundry ---

output "openai_endpoint" {
  description = "Azure AI Foundry endpoint URL (used by the OpenAI SDK — same env var name)"
  value       = azurerm_cognitive_account.foundry.endpoint
}

output "openai_primary_key" {
  description = "Azure AI Foundry primary API key (sensitive)"
  value       = azurerm_cognitive_account.foundry.primary_access_key
  sensitive   = true
}

output "openai_deployment" {
  description = "Name of the deployed model. Empty if create_openai_deployment = false."
  value       = var.create_openai_deployment ? azurerm_cognitive_deployment.gpt4o[0].name : ""
}

output "foundry_account_name" {
  description = "Azure AI Foundry account name (useful for az cli commands)"
  value       = azurerm_cognitive_account.foundry.name
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
  description = "Log Analytics workspace ID (for diagnostic settings)"
  value       = azurerm_log_analytics_workspace.sentinel.workspace_id
}

output "log_analytics_workspace_resource_id" {
  description = "Full ARM resource ID of the Log Analytics workspace"
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

# --- Helpful summary ---

output "next_steps" {
  description = "What to do after terraform apply"
  value       = <<-EOT

    ✓ Infrastructure deployed successfully!

    Next steps:
    1. Run from project root:
         bash scripts/setup_env.sh
       This generates .env with all connection strings.

    2. Add your subscription and tenant IDs to .env:
         AZURE_SUBSCRIPTION_ID=<your-id>
         AZURE_TENANT_ID=<your-id>

    3. Switch from mock to Azure mode:
         USE_LOCAL_MOCKS=false   (already set by setup_env.sh)

    4. Upload seed data to Azure AI Search:
         python scripts/seed_data.py

  EOT
}
