# RuriSkry — Core Services Reference

Single source of truth for every Azure service used by RuriSkry.
Cross-references Terraform resource names, config variables, and the Python classes that use each service.

> **Rule:** Every row in this file must have a corresponding `resource` block in the Terraform file
> listed in its "Terraform resource" column. If a service exists in `.env` / `config.py` but has no
> Terraform resource, that is a gap that needs fixing.

---

## Governance Infrastructure (`infrastructure/terraform-core/`)

These are the services that RuriSkry **runs on**.

### Compute & Hosting

| Service | Terraform resource | Config var | Used by |
|---|---|---|---|
| Azure Container App (backend) | `azurerm_container_app.backend` | `API_HOST`, `API_PORT` | FastAPI governance API + all agents |
| Container Apps Environment | `azurerm_container_app_environment.ruriskry` | — | Hosts the Container App |
| Azure Container Registry | `azurerm_container_registry.ruriskry` | — | Stores backend Docker images (`admin_enabled=false`) |
| Azure Static Web App | `azurerm_static_web_app.dashboard` | `DASHBOARD_URL` | React dashboard frontend (Free tier, global CDN) |

### AI & Intelligence

| Service | Terraform resource | Config var | Used by | Notes |
|---|---|---|---|---|
| Azure AI Foundry (OpenAI) | `azurerm_ai_services.foundry` | `AZURE_OPENAI_ENDPOINT` / `AZURE_OPENAI_API_KEY` | All 7 agents via agent-framework-core | Model: `gpt-5-mini`; 200K TPM; `local_authentication_enabled=false` — MI auth only |
| Model deployment | `azurerm_cognitive_deployment.foundry_primary` | `AZURE_OPENAI_DEPLOYMENT` (`gpt-5-mini`) | All agents | GlobalStandard tier |
| Foundry project | `azapi_resource.foundry_project` | — | Agent Framework project isolation | Created when `create_foundry_project=true` |
| Azure AI Search | `azurerm_search_service.ruriskry` | `AZURE_SEARCH_ENDPOINT` / `AZURE_SEARCH_API_KEY` | `HistoricalPatternAgent` | BM25 full-text search; index: `incident-history` |

### Persistence (Cosmos DB)

Account: `azurerm_cosmosdb_account.ruriskry` → `COSMOS_ENDPOINT` / `COSMOS_KEY`
Database: `azurerm_cosmosdb_sql_database.ruriskry` → `COSMOS_DATABASE` (default: `ruriskry`)

| Container | Terraform resource | Partition key | Config var | Python class |
|---|---|---|---|---|
| `governance-decisions` | `azurerm_cosmosdb_sql_container.governance_decisions` | `/resource_id` | `COSMOS_CONTAINER_DECISIONS` | `CosmosDecisionClient` → `DecisionTracker` |
| `governance-agents` | `azurerm_cosmosdb_sql_container.governance_agents` | `/name` | — | `AgentRegistry` |
| `governance-alerts` | `azurerm_cosmosdb_sql_container.governance_alerts` | `/severity` | `COSMOS_CONTAINER_ALERTS` | `AlertTracker` |
| `governance-scan-runs` | `azurerm_cosmosdb_sql_container.governance_scan_runs` | `/agent_type` | `COSMOS_CONTAINER_SCAN_RUNS` | `ScanRunTracker` |
| `governance-executions` | `azurerm_cosmosdb_sql_container.governance_executions` | `/resource_id` | `COSMOS_CONTAINER_EXECUTIONS` | `CosmosExecutionClient` → `ExecutionGateway` |

### Security & Secrets

| Service | Terraform resource | Config var | Notes |
|---|---|---|---|
| Azure Key Vault | `azurerm_key_vault.ruriskry` | `AZURE_KEYVAULT_URL` | Runtime secret resolution; `purge_protection_enabled=false`, `soft_delete_retention_days=7` |
| KV secret — Foundry key | `azurerm_key_vault_secret.foundry_primary_key` | — | Secret name: `foundry-primary-key` |
| KV secret — Search key | `azurerm_key_vault_secret.search_primary_key` | — | Secret name: `search-primary-key` |
| KV secret — Cosmos key | `azurerm_key_vault_secret.cosmos_primary_key` | — | Secret name: `cosmos-primary-key` |
| User-Assigned MI (ACR) | `azurerm_user_assigned_identity.acr_pull` | — | Dedicated identity for ACR image pull — decoupled from backend MI |

### Observability

| Service | Terraform resource | Config var | Used by |
|---|---|---|---|
| Log Analytics Workspace | `azurerm_log_analytics_workspace.ruriskry` | `LOG_ANALYTICS_WORKSPACE_ID` | VM metrics, heartbeat queries (`query_metrics` tool) |
| Monitor Action Group | `azurerm_monitor_action_group.ruriskry` | — | Receives Azure Monitor alerts → `POST /api/alert-trigger` |

### Role Assignments

All assigned to the Container App's System-Assigned Managed Identity unless noted.

| Role | Terraform resource | Principal | Scope | Why |
|---|---|---|---|---|
| AcrPull | `azurerm_role_assignment.acr_pull` | User-Assigned MI | ACR | Pull Docker images without stored credentials |
| Cognitive Services OpenAI User | `azurerm_role_assignment.foundry_openai_user` | Container App MI | Foundry account | Call Foundry/OpenAI APIs via MI (key auth disabled) |
| Reader | `azurerm_role_assignment.subscription_reader` | Container App MI | Subscription | Cross-RG Resource Graph scanning by governance agents |
| Network Contributor | `azurerm_role_assignment.network_contributor` | Container App MI | Subscription | Execution Gateway: delete/create NSG security rules |
| Virtual Machine Contributor | `azurerm_role_assignment.vm_contributor` | Container App MI | Subscription | Execution Gateway: start/restart VMs, read instance view |

---

## Governed Resources (`infrastructure/terraform-prod/`)

These are the resources RuriSkry **watches and proposes changes to** — not the governance system itself.

### Compute

| Resource | Type | Terraform resource | Governance scenario |
|---|---|---|---|
| `vm-web-01` | Linux VM (B2ls_v2) | `azurerm_linux_virtual_machine.web` | APPROVED — safe CPU scale-up; stress-ng cron triggers CPU alerts. `SystemAssigned` MI + `Monitoring Metrics Publisher` role for AMA telemetry |
| `vm-dr-01` | Linux VM (B2ls_v2) | `azurerm_linux_virtual_machine.dr` | DENIED — `disaster-recovery=true` tag; policy blocks all changes. `SystemAssigned` MI + `Monitoring Metrics Publisher` role |

### Networking

| Resource | Type | Terraform resource | Governance scenario |
|---|---|---|---|
| `nsg-east-prod` | Network Security Group | `azurerm_network_security_group.prod` | ESCALATED — port 8080 open to internet affects both governed VMs |

### PaaS

| Resource | Type | Terraform resource | Governance scenario |
|---|---|---|---|
| `payment-api-prod` | App Service F1 | `azurerm_service_plan` + `azurerm_linux_web_app` | Critical dependency — raises blast radius score for any co-located resource change |
| `ruriskryprod{suffix}` | Storage Account LRS | `azurerm_storage_account.prod` | Shared dependency — deletion = high blast radius |

### Observability (prod)

| Resource | Terraform resource | Purpose |
|---|---|---|
| Log Analytics Workspace (prod) | `azurerm_log_analytics_workspace.prod` | VM metrics, Heartbeat, AzureActivity table for governed VMs |
| Diagnostic Setting — Activity Logs | `azurerm_monitor_diagnostic_setting.activity_logs` | Streams 8 activity log categories to prod workspace → populates `AzureActivity` KQL table |
| Alert rule — vm-web-01 CPU | `azurerm_monitor_metric_alert.vm_web_cpu` | Fires when CPU > threshold → `POST /api/alert-trigger` → MonitoringAgent |
| Alert rule — vm-web-01 heartbeat | `azurerm_monitor_metric_alert.vm_web_heartbeat` | Fires on missing heartbeat → MonitoringAgent |
| Alert rule — vm-dr-01 CPU | `azurerm_monitor_metric_alert.vm_dr_cpu` | Same pattern for DR VM |
| Alert rule — vm-dr-01 heartbeat | `azurerm_monitor_metric_alert.vm_dr_heartbeat` | Same pattern for DR VM |
| Action Group | `azurerm_monitor_action_group.prod` (`ag-ruriskry-prod`) | Wires all alert rules to backend webhook; `use_common_alert_schema=false` |

---

## Environment Variable Reference

All variables read by `src/config.py` from `.env` or Azure Container App environment.

| Variable | Default | Set by | Notes |
|---|---|---|---|
| `AZURE_OPENAI_ENDPOINT` | `""` | `deploy.sh` (from `terraform output`) | Required for live LLM calls |
| `AZURE_OPENAI_API_KEY` | `""` | KV secret `foundry-primary-key` | Resolved at runtime by `KeyVaultSecretResolver` |
| `AZURE_OPENAI_DEPLOYMENT` | `gpt-5-mini` | `terraform.tfvars` | Model deployment name |
| `AZURE_OPENAI_API_VERSION` | `2025-01-01-preview` | `config.py` | Responses API requires this version |
| `AZURE_SEARCH_ENDPOINT` | `""` | `deploy.sh` | Required for historical incident search |
| `AZURE_SEARCH_API_KEY` | `""` | KV secret `search-primary-key` | |
| `AZURE_SEARCH_INDEX` | `incident-history` | `config.py` | Search index name |
| `COSMOS_ENDPOINT` | `""` | `deploy.sh` | Required for all Cosmos persistence |
| `COSMOS_KEY` | `""` | KV secret `cosmos-primary-key` | |
| `COSMOS_DATABASE` | `ruriskry` | `config.py` | |
| `COSMOS_CONTAINER_DECISIONS` | `governance-decisions` | `config.py` | |
| `COSMOS_CONTAINER_SCAN_RUNS` | `governance-scan-runs` | `config.py` | |
| `COSMOS_CONTAINER_ALERTS` | `governance-alerts` | `config.py` | |
| `COSMOS_CONTAINER_EXECUTIONS` | `governance-executions` | `config.py` | |
| `AZURE_KEYVAULT_URL` | `""` | `deploy.sh` | Required for secret resolution |
| `AZURE_MANAGED_IDENTITY_CLIENT_ID` | `""` | Terraform (Container App env) | User-Assigned MI client ID for ACR pull |
| `AZURE_SUBSCRIPTION_ID` | `""` | `.env` / tfvars | Required for Resource Graph scanning |
| `AZURE_TENANT_ID` | `""` | `.env` | Required for DefaultAzureCredential |
| `LOG_ANALYTICS_WORKSPACE_ID` | `""` | `deploy.sh` | Required for metric/heartbeat queries |
| `SLACK_WEBHOOK_URL` | `""` | KV secret / `.env` | Required for Slack notifications |
| `SLACK_NOTIFICATIONS_ENABLED` | `true` | `config.py` | |
| `SLACK_TIMEOUT` | `10` | `config.py` | Per-request timeout (seconds) |
| `DASHBOARD_URL` | `http://localhost:5173` | Terraform (Container App env) | CORS origin + Slack notification links |
| `GITHUB_TOKEN` | `""` | KV secret `github-pat` | Required for Terraform PR generation |
| `IAC_GITHUB_REPO` | `""` | `.env` / tfvars | e.g. `psc0des/ruriskry-iac-test` |
| `IAC_TERRAFORM_PATH` | `infrastructure/terraform-prod` | `config.py` | Path within IaC repo |
| `EXECUTION_GATEWAY_ENABLED` | `false` | `.env` / tfvars | `true` to enable PR creation |
| `USE_LOCAL_MOCKS` | `true` | `.env` | `false` for live Azure mode |
| `USE_LIVE_TOPOLOGY` | `false` | `.env` | `true` for real Resource Graph topology queries |
| `DEMO_MODE` | `false` | `.env` | Returns hardcoded proposals without Azure OpenAI |
| `LLM_CONCURRENCY_LIMIT` | `6` | `.env` | Max simultaneous LLM calls across all agents |
| `LLM_TIMEOUT` | `600` | `.env` | Wall-clock timeout (seconds) per agentic LLM call |
| `SEQUENTIAL_LLM` | `false` | `.env` | `true` to serialise all LLM calls (very low quota) |
| `DEFAULT_RESOURCE_GROUP` | `""` | `.env` | Scan scope default; empty = whole subscription |
| `SRI_AUTO_APPROVE_THRESHOLD` | `25` | `.env` | SRI score at or below → APPROVED |
| `SRI_HUMAN_REVIEW_THRESHOLD` | `60` | `.env` | SRI score above → DENIED (25–60 = ESCALATED) |
