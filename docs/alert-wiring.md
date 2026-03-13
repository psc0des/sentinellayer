# Alert Wiring Guide

This document explains how Azure Monitor alerts connect to RuriSkry, what is
wired automatically, what requires manual steps, and how to add monitoring for
new resources.

---

## How the pipeline works

```
Azure resource changes state (VM stops, CPU spikes, disk fills, etc.)
    ↓
Azure Monitor alert rule evaluates the condition
    ↓
Alert fires → Action Group (ag-ruriskry-prod)
    ↓
Webhook POST → /api/alert-trigger
    ↓
MonitoringAgent investigates the resource
    ↓
ProposedAction → SRI scoring → Governance verdict
    ↓
Slack notification (if DENIED or ESCALATED)
```

The Action Group is the **single integration point**. Any alert rule that
references it automatically feeds into RuriSkry. You never need to touch
application code to add a new alert.

---

## What is automatic (no wiring needed)

| Capability | How |
|---|---|
| **Resource discovery** | Agents query Azure Resource Graph for all resources in scope on every scan. New resources appear automatically on the next scan. |
| **Governance evaluation** | Any resource that an agent flags gets scored and evaluated — no registration needed. |
| **Action Group webhook** | Already configured in `terraform-prod`. Any alert rule that references `ag-ruriskry-prod` POSTs to `/api/alert-trigger` automatically. |
| **Slack notifications** | Fired automatically after every DENIED or ESCALATED verdict. |

---

## What requires manual wiring

| Capability | Why it is manual | What to do |
|---|---|---|
| **Azure Monitor Agent (AMA)** | AMA must be installed on each VM for heartbeat and performance metrics to flow into Log Analytics. Without it, heartbeat alerts never fire. | See [Step 2](#step-2--install-azure-monitor-agent) below. |
| **Data Collection Rule (DCR) association** | AMA needs to know which workspace to send data to. Each VM must be associated with the DCR. | See [Step 2](#step-2--install-azure-monitor-agent) below. |
| **Alert rules** | Alert rules are scoped to specific resources or resource groups. A new VM has no alert rules by default. | See [Step 3](#step-3--create-alert-rules) below. |

---

## Adding a new VM

Follow these steps in `infrastructure/terraform-prod/main.tf`. Each step builds
on the previous one.

### Step 1 — Define the VM resource

Create the VM resource as normal. Tag it so RuriSkry agents can classify it:

```hcl
resource "azurerm_linux_virtual_machine" "my_vm" {
  name                = "vm-my-service"
  resource_group_name = azurerm_resource_group.prod.name
  location            = azurerm_resource_group.prod.location
  # ... standard VM config ...

  tags = merge(local.common_tags, {
    "criticality"  = "high"          # low | medium | high | critical
    "cost-center"  = "my-team"
    "environment"  = "production"
    "iac_path"     = "infrastructure/terraform-prod"
    "iac_repo"     = "yourorg/yourrepo"
  })
}
```

> **Why tags matter:** RuriSkry's Risk Triage engine reads `criticality` to
> classify actions into tiers. A `critical` VM routes actions through full LLM
> governance (Tier 3). A `low` VM gets fast-path deterministic evaluation
> (Tier 1).

### Step 2 — Install Azure Monitor Agent

AMA sends heartbeats and performance metrics to Log Analytics. Without it,
heartbeat alerts never fire and the MonitoringAgent has no telemetry to work
with.

```hcl
# Install AMA
resource "azurerm_virtual_machine_extension" "ama_my_vm" {
  name                       = "AzureMonitorLinuxAgent"
  virtual_machine_id         = azurerm_linux_virtual_machine.my_vm.id
  publisher                  = "Microsoft.Azure.Monitor"
  type                       = "AzureMonitorLinuxAgent"
  type_handler_version       = "1.0"
  auto_upgrade_minor_version = true
  tags                       = local.common_tags
}

# Associate VM with the shared Data Collection Rule
resource "azurerm_monitor_data_collection_rule_association" "my_vm" {
  name                    = "dcra-vm-my-service"
  target_resource_id      = azurerm_linux_virtual_machine.my_vm.id
  data_collection_rule_id = azurerm_monitor_data_collection_rule.vm_signals.id
}

# Grant AMA permission to publish metrics
resource "azurerm_role_assignment" "ama_my_vm" {
  scope                = azurerm_linux_virtual_machine.my_vm.id
  role_definition_name = "Monitoring Metrics Publisher"
  principal_id         = azurerm_linux_virtual_machine.my_vm.identity[0].principal_id
}
```

> **Prerequisite:** The VM must have a `SystemAssigned` managed identity. Add
> `identity { type = "SystemAssigned" }` to the VM resource block.

### Step 3 — Create alert rules

Create at least the heartbeat alert. Add the CPU alert if the VM is a
workload VM that can legitimately spike.

#### Heartbeat alert (recommended for all VMs)

Fires when the VM sends no heartbeat for 15 minutes — detects stopped,
deallocated, or crashed VMs. This is the **most important alert** for
operational health.

```hcl
resource "azurerm_monitor_scheduled_query_rules_alert_v2" "my_vm_heartbeat" {
  name                = "alert-vm-my-service-heartbeat"
  resource_group_name = azurerm_resource_group.prod.name
  location            = azurerm_resource_group.prod.location
  description         = "Detects when vm-my-service stops sending heartbeats"
  severity            = 1   # 0=Critical, 1=Error, 2=Warning, 3=Info
  enabled             = true

  evaluation_frequency = "PT5M"   # evaluate every 5 minutes
  window_duration      = "PT15M"  # look back 15 minutes

  scopes = [azurerm_log_analytics_workspace.prod.id]

  criteria {
    query = <<-QUERY
      Heartbeat
      | where _ResourceId =~ "${azurerm_linux_virtual_machine.my_vm.id}"
      | where TimeGenerated > ago(15m)
    QUERY

    time_aggregation_method = "Count"
    threshold               = 0
    operator                = "LessThanOrEqual"  # fires when count == 0

    failing_periods {
      minimum_failing_periods_to_trigger_alert = 1
      number_of_evaluation_periods             = 1
    }
  }

  action {
    action_groups = [azurerm_monitor_action_group.prod.id]  # wires to RuriSkry
  }

  tags = local.common_tags

  depends_on = [
    azurerm_virtual_machine_extension.ama_my_vm,
    azurerm_monitor_data_collection_rule_association.my_vm
  ]
}
```

#### CPU alert (optional — for workload VMs that can spike)

Fires when average CPU exceeds 80% over 15 minutes. Use this for VMs running
active workloads where high CPU is a signal to scale up.

```hcl
resource "azurerm_monitor_metric_alert" "my_vm_cpu" {
  name                = "alert-vm-my-service-cpu-high"
  resource_group_name = azurerm_resource_group.prod.name
  scopes              = [azurerm_linux_virtual_machine.my_vm.id]
  description         = "Triggers RuriSkry when vm-my-service CPU > 80%"
  severity            = 2
  frequency           = "PT5M"
  window_size         = "PT15M"

  criteria {
    metric_namespace = "Microsoft.Compute/virtualMachines"
    metric_name      = "Percentage CPU"
    aggregation      = "Average"
    operator         = "GreaterThan"
    threshold        = 80
  }

  action {
    action_group_id = azurerm_monitor_action_group.prod.id
  }

  tags = local.common_tags
}
```

> **Important:** A CPU alert cannot detect a deallocated VM. A deallocated VM
> emits zero metrics — Azure Monitor puts the alert in "No data" state, not
> "Fired". Always create the heartbeat alert in addition to any metric alerts.

### Step 4 — Apply

```bash
cd infrastructure/terraform-prod
terraform plan   # verify only your new resources appear
terraform apply
```

Heartbeat data takes ~5 minutes to appear in Log Analytics after AMA is
installed. The first alert evaluation runs within 5 minutes of that.

---

## Adding a non-VM resource (storage, database, Container App)

RuriSkry's agents already scan these resource types automatically via Resource
Graph. You only need alert rules if you want **event-driven** (real-time) alerts
in addition to periodic scans.

For non-VM resources, use metric alerts scoped to the resource:

```hcl
# Example: Storage account — alert on egress anomaly
resource "azurerm_monitor_metric_alert" "storage_egress" {
  name                = "alert-storage-my-account-egress"
  resource_group_name = azurerm_resource_group.prod.name
  scopes              = [azurerm_storage_account.my_account.id]
  severity            = 2
  frequency           = "PT5M"
  window_size         = "PT15M"

  criteria {
    metric_namespace = "Microsoft.Storage/storageAccounts"
    metric_name      = "Egress"
    aggregation      = "Total"
    operator         = "GreaterThan"
    threshold        = 10737418240  # 10 GB
  }

  action {
    action_group_id = azurerm_monitor_action_group.prod.id
  }
}
```

Non-VM resources do not need AMA or DCR — those are VM-only components.

---

## How RuriSkry identifies the resource from an alert

When an alert fires, Azure POSTs a JSON payload to `/api/alert-trigger`.
The `_normalize_azure_alert_payload()` function in `dashboard_api.py` extracts
the affected resource ID.

**Metric alerts** — straightforward. `alertTargetIDs[0]` is the resource ARM ID.

**Log Alerts V2 (heartbeat queries)** — Azure always sends the Log Analytics
workspace ARM ID as the target, never the VM. The normalizer extracts the VM
name from `essentials.description` or the alert rule name using a regex, then
reconstructs the correct VM ARM ID.

This is why the alert `description` field matters:

```hcl
description = "Detects when vm-my-service stops sending heartbeats"
#                                 ^^^^^^^^^^^^^^
#                                 Must contain the VM name — used by the normalizer
```

If the description does not contain the VM name, the normalizer falls back to
the alert rule name. Keep both consistent.

---

## Large environments — automatic wiring via Azure Policy

If your fleet has many VMs and you cannot maintain per-VM Terraform resources,
use Azure Policy to automate AMA installation and a single resource-group-scoped
heartbeat alert to cover all VMs:

**Azure Policy (auto-installs AMA on any new VM in the resource group):**

```hcl
resource "azurerm_resource_group_policy_assignment" "ama_auto" {
  name                 = "auto-ama-linux"
  resource_group_id    = azurerm_resource_group.prod.id
  policy_definition_id = "/providers/Microsoft.Authorization/policyDefinitions/ae8a10e6-19d6-44a3-a02d-a2bdfc707742"
  location             = azurerm_resource_group.prod.location
  identity { type = "SystemAssigned" }
}
```

**Single heartbeat alert covering all VMs in the resource group:**

```hcl
resource "azurerm_monitor_scheduled_query_rules_alert_v2" "all_vms_heartbeat" {
  name                = "alert-all-vms-heartbeat"
  resource_group_name = azurerm_resource_group.prod.name
  location            = azurerm_resource_group.prod.location
  description         = "Detects any VM in ruriskry-prod-rg that stops sending heartbeats"
  severity            = 1
  enabled             = true

  evaluation_frequency = "PT5M"
  window_duration      = "PT15M"
  scopes               = [azurerm_log_analytics_workspace.prod.id]

  criteria {
    query = <<-QUERY
      Heartbeat
      | where _ResourceId startswith tolower(
          "/subscriptions/${var.subscription_id}/resourcegroups/${azurerm_resource_group.prod.name}/")
      | summarize LastHeartbeat = max(TimeGenerated) by Computer, _ResourceId
      | where LastHeartbeat < ago(15m)
    QUERY

    time_aggregation_method = "Count"
    threshold               = 0
    operator                = "GreaterThan"  # fires when any VM is missing

    failing_periods {
      minimum_failing_periods_to_trigger_alert = 1
      number_of_evaluation_periods             = 1
    }
  }

  action {
    action_groups = [azurerm_monitor_action_group.prod.id]
  }
}
```

With this setup, a new VM added to the resource group — by Terraform, the portal,
or a script — is automatically monitored within ~5 minutes of creation.

---

## Troubleshooting

**Alert fired but no investigation appeared in the Alerts tab**

1. Check the backend logs: `az containerapp logs show --name <app> --resource-group <rg> --follow`
2. Verify the webhook URL in the action group matches your backend URL
3. The alert payload must contain the VM name in `essentials.description` or the alert rule name — check the raw payload in Azure Monitor → Alerts → Alert history → JSON

**Heartbeat alert fires immediately after VM creation**

Expected — AMA takes 3–5 minutes to install and start sending heartbeats. The
alert evaluates before the first heartbeat arrives. It resolves automatically
once heartbeats begin flowing.

**Alert fires but MonitoringAgent proposes nothing**

The agent found the resource but determined no action was needed. Check the
alert investigation in the Alerts tab for the agent's reasoning. Common causes:
VM restarted itself before the agent ran; resource is tagged `criticality=low`
and the issue is below threshold.

**VM is deallocated but no alert fires**

The VM likely has only a CPU metric alert (not a heartbeat alert). A deallocated
VM emits no metrics. Add the heartbeat alert from [Step 3](#step-3--create-alert-rules).
