# =============================================================================
# SentinelLayer — Mini Production Environment — Outputs
# =============================================================================
# After `terraform apply`, these values are printed to the console.
# Use them to update data/seed_resources.json with real Azure resource IDs.
# Run: terraform output -json > prod_outputs.json
# =============================================================================

# --- Resource Group ---

output "resource_group_name" {
  description = "Name of the prod resource group"
  value       = azurerm_resource_group.prod.name
}

output "resource_group_id" {
  description = "Full Azure resource ID of the prod resource group"
  value       = azurerm_resource_group.prod.id
}

output "location" {
  description = "Azure region where all prod resources were deployed"
  value       = azurerm_resource_group.prod.location
}

output "subscription_id" {
  description = "Azure Subscription ID (used in resource ID paths)"
  value       = var.subscription_id
}

# --- VM: vm-dr-01 ---

output "vm_dr01_id" {
  description = "Full Azure resource ID of vm-dr-01 (disaster recovery VM)"
  value       = azurerm_linux_virtual_machine.dr01.id
}

output "vm_dr01_name" {
  description = "Name of the disaster recovery VM"
  value       = azurerm_linux_virtual_machine.dr01.name
}

output "vm_dr01_tags" {
  description = "Tags applied to vm-dr-01 (disaster-recovery, environment, owner, cost-center)"
  value       = azurerm_linux_virtual_machine.dr01.tags
}

output "vm_dr01_private_ip" {
  description = "Private IP address of vm-dr-01"
  value       = azurerm_network_interface.dr01.private_ip_address
}

output "vm_dr01_public_ip" {
  description = "Public IP address of vm-dr-01"
  value       = azurerm_public_ip.dr01.ip_address
}

# --- VM: vm-web-01 ---

output "vm_web01_id" {
  description = "Full Azure resource ID of vm-web-01 (active web server)"
  value       = azurerm_linux_virtual_machine.web01.id
}

output "vm_web01_name" {
  description = "Name of the web server VM"
  value       = azurerm_linux_virtual_machine.web01.name
}

output "vm_web01_tags" {
  description = "Tags applied to vm-web-01 (tier, environment, owner, cost-center)"
  value       = azurerm_linux_virtual_machine.web01.tags
}

output "vm_web01_private_ip" {
  description = "Private IP address of vm-web-01"
  value       = azurerm_network_interface.web01.private_ip_address
}

output "vm_web01_public_ip" {
  description = "Public IP address of vm-web-01"
  value       = azurerm_public_ip.web01.ip_address
}

# --- App Service: payment-api-prod ---

output "payment_api_id" {
  description = "Full Azure resource ID of payment-api-prod App Service"
  value       = azurerm_linux_web_app.payment_api.id
}

output "payment_api_name" {
  description = "Name of the payment API App Service (includes suffix)"
  value       = azurerm_linux_web_app.payment_api.name
}

output "payment_api_url" {
  description = "Default HTTPS URL of the payment API (https://<name>.azurewebsites.net)"
  value       = "https://${azurerm_linux_web_app.payment_api.default_hostname}"
}

output "payment_api_tags" {
  description = "Tags applied to payment-api-prod (tier, environment, critical)"
  value       = azurerm_linux_web_app.payment_api.tags
}

# --- NSG: nsg-east-prod ---

output "nsg_id" {
  description = "Full Azure resource ID of nsg-east-prod"
  value       = azurerm_network_security_group.prod.id
}

output "nsg_name" {
  description = "Name of the network security group"
  value       = azurerm_network_security_group.prod.name
}

output "nsg_tags" {
  description = "Tags applied to nsg-east-prod (environment, managed-by)"
  value       = azurerm_network_security_group.prod.tags
}

# --- Storage Account ---

output "storage_account_id" {
  description = "Full Azure resource ID of the shared storage account"
  value       = azurerm_storage_account.prod.id
}

output "storage_account_name" {
  description = "Name of the shared storage account (sentinelprod + suffix)"
  value       = azurerm_storage_account.prod.name
}

output "storage_account_primary_endpoint" {
  description = "Primary blob endpoint of the shared storage account"
  value       = azurerm_storage_account.prod.primary_blob_endpoint
}

# --- Log Analytics ---

output "log_analytics_workspace_id" {
  description = "Log Analytics workspace resource ID"
  value       = azurerm_log_analytics_workspace.prod.id
}

output "log_analytics_workspace_guid" {
  description = "Log Analytics workspace GUID (used when configuring VM agents)"
  value       = azurerm_log_analytics_workspace.prod.workspace_id
}

# --- Monitor Alerts ---

output "cpu_alert_id" {
  description = "Resource ID of the vm-web-01 CPU metric alert"
  value       = azurerm_monitor_metric_alert.web01_cpu.id
}

output "heartbeat_alert_id" {
  description = "Resource ID of the vm-dr-01 heartbeat scheduled query alert"
  value       = azurerm_monitor_scheduled_query_rules_alert_v2.dr01_heartbeat.id
}

# --- Helpful seed_resources.json snippet ---

output "seed_resources_ids" {
  description = "Copy these IDs into data/seed_resources.json after deployment"
  value       = <<-EOT

    Paste these into data/seed_resources.json → resources[*].id:

    vm-dr-01:
      ${azurerm_linux_virtual_machine.dr01.id}

    vm-web-01:
      ${azurerm_linux_virtual_machine.web01.id}

    payment-api-prod:
      ${azurerm_linux_web_app.payment_api.id}

    nsg-east-prod:
      ${azurerm_network_security_group.prod.id}

    storage account (sentinelprod${var.suffix}):
      ${azurerm_storage_account.prod.id}

  EOT
}

output "next_steps" {
  description = "What to do after terraform apply"
  value       = <<-EOT

    Mini prod environment deployed successfully!

    1. Update data/seed_resources.json:
         Run: terraform output seed_resources_ids
         Copy the IDs into the "id" fields for each resource.

    2. Start the governance demo (mock mode):
         python demo.py

    3. Start the A2A demo (talks to real resources via mock graph):
         python demo_a2a.py

    4. Start the governance API + dashboard:
         uvicorn src.api.dashboard_api:app --reload
         cd dashboard && npm run dev

    5. To destroy when done (avoid charges):
         terraform destroy

    Cost estimate (while VMs are running, Standard_B1s):
      vm-dr-01:  ~$0.021/hour (~$0.50/day with auto-shutdown at 22:00 UTC)
      vm-web-01: ~$0.021/hour (~$0.50/day with auto-shutdown at 22:00 UTC)
      App Service F1: FREE
      Storage LRS: ~$0.01/GB/month
      Log Analytics: pay-per-GB, minimal for demo

  EOT
}
