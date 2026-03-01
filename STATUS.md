# SentinelLayer — Implementation Status

> **Read this first** if you are an AI assistant (Claude, Codex, Gemini, etc.)
> picking up this project. It tells you exactly what is done, what is live,
> and what comes next. Architecture and coding standards are in `CONTEXT.md`.

**Last updated:** 2026-03-01 (Phase 12 complete)
**Active branch:** `main`
**Demo verdict:** All 3 scenarios pass with real prod resource IDs (DENIED / APPROVED / ESCALATED)

---

## Quick State Summary

| Layer | Status | Backend |
|-------|--------|---------|
| Core models (`models.py`) | ✅ Complete | — |
| Governance engine (SRI scoring) | ✅ Complete | — |
| Policy agent | ✅ Complete + tested | `data/policies.json` |
| Blast radius agent | ✅ Complete + LLM reasoning | `data/seed_resources.json` + GPT-4.1 |
| Historical agent | ✅ Complete + live search | Azure AI Search (BM25) + GPT-4.1 |
| Financial agent | ✅ Complete + LLM reasoning | `data/seed_resources.json` + GPT-4.1 |
| Operational agent: deploy-agent | ✅ Complete | `data/seed_resources.json` + Resource Graph + NSG rules |
| Generic Azure tools (`azure_tools.py`) | ✅ Complete | `src/infrastructure/azure_tools.py` |
| Two-layer intelligence (Phase 12) | ✅ Complete | ops agents + GPT-4.1 investigation |
| Alert-trigger endpoint | ✅ Complete | `POST /api/alert-trigger` |
| Pipeline (parallel execution) | ✅ Complete | `asyncio.gather()` (async-first) |
| Microsoft Agent Framework | ✅ Complete | `agent-framework-core` + GPT-4.1 |
| Decision tracker | ✅ Complete | Azure Cosmos DB (live) / JSON (mock) |
| MCP server | ✅ Complete | FastMCP stdio (`server.py`) |
| Dashboard API | ✅ Complete | FastAPI REST (+ A2A agent endpoints) |
| Azure infrastructure (Terraform) | ✅ Deployed | Foundry · Search · Cosmos · KV |
| Mini prod environment (Terraform) | ✅ Complete | `infrastructure/terraform-prod/` |
| Secret management | ✅ Complete | Key Vault + `DefaultAzureCredential` |
| Live Azure wiring | ✅ Complete | All 3 services connected |
| React dashboard | ✅ Complete | `dashboard/` (Vite + React, same repo) |
| A2A Protocol server | ✅ Complete | `agent-framework-a2a` + `a2a-sdk` |
| A2A operational clients | ✅ Complete | `A2ACardResolver` + `A2AClient` + `httpx` |
| A2A agent registry | ✅ Complete | JSON (mock) / Cosmos DB (live) |

---

## Completed Phases (Chronological)

### Phase 1 — Core Domain Models
- [x] `src/core/models.py` — all Pydantic models: `ProposedAction`, `GovernanceVerdict`,
  `SentinelRiskIndex`, `BlastRadiusResult`, `PolicyResult`, `HistoricalResult`,
  `FinancialResult`, `SimilarIncident`
- [x] `src/config.py` — SRI thresholds + dimension weights via `pydantic-settings`
- [x] Learning: `learning/01-policy-agent.md`, `learning/02-governance-engine.md`

### Phase 2 — Governance Agents
- [x] `src/governance_agents/policy_agent.py` — 6 policies, critical-violation override
- [x] `src/governance_agents/blast_radius_agent.py` — resource dependency graph traversal
- [x] `src/governance_agents/historical_agent.py` — incident similarity scoring
- [x] `src/governance_agents/financial_agent.py` — cost delta + over-optimisation detection
- [x] Full unit test suite in `tests/`
- [x] Learning: `learning/03-blast-radius.md` through `learning/05-financial-agent.md`

### Phase 3 — Pipeline + Operational Agents
- [x] `src/core/pipeline.py` — parallel evaluation (later refactored to `asyncio.gather()`)
- [x] `src/core/decision_tracker.py` — audit trail
- [x] `src/operational_agents/monitoring_agent.py` — anomaly detection + action proposals
- [x] `src/operational_agents/cost_agent.py` — idle resource detection + savings proposals
- [x] `demo.py` — 3-scenario end-to-end demo
- [x] Learning: `learning/07-operational-agents.md`, `learning/08-pipeline.md`

### Phase 4 — MCP Server + Dashboard API
- [x] `src/mcp_server/server.py` — MCP tools: `evaluate_action`, `get_recent_decisions`,
  `get_resource_risk_profile`
- [x] `src/api/dashboard_api.py` — FastAPI REST: `/evaluate`, `/decisions`, `/health`
- [x] `src/core/interception.py` — MCP action interception layer
- [x] Learning: `learning/09-mcp-server.md`, `learning/10-dashboard-api.md`,
  `learning/12-interception.md`

### Phase 5 — Azure Infrastructure (Terraform)
- [x] Terraform: `azurerm_ai_services` (Foundry) + `azurerm_cognitive_deployment` (gpt-41)
- [x] Terraform: Azure AI Search, Cosmos DB, Key Vault, Log Analytics
- [x] `scripts/setup_env.sh` — auto-populates `.env` from Terraform outputs
- [x] Learning: `learning/13-azure-infrastructure.md`, `learning/14-azure-ai-foundry.md`

### Phase 6 — Secret Management
- [x] `src/infrastructure/secrets.py` — `KeyVaultSecretResolver` (env → Key Vault → empty)
- [x] All infrastructure clients updated: env override → Key Vault → mock fallback
- [x] `.env` uses secret-name vars (`AZURE_OPENAI_API_KEY_SECRET_NAME=foundry-primary-key`)
  not plaintext keys
- [x] Learning: `learning/15-keyvault-managed-identity.md`

### Phase 7 — Live Azure Service Wiring
- [x] `src/infrastructure/openai_client.py` — added `analyze()` governance wrapper
- [x] `src/infrastructure/search_client.py` — added `index_incidents()` (idempotent seeding)
- [x] `src/core/decision_tracker.py` — delegates to `CosmosDecisionClient` (Cosmos DB live)
- [x] `blast_radius_agent.py` + `financial_agent.py` — GPT-4.1 enriches `reasoning` field
- [x] `historical_agent.py` — routes to Azure AI Search in live mode (BM25 full-text)
- [x] `scripts/seed_data.py` — 7/7 incidents indexed to `incident-history` Azure AI Search index
- [x] `demo.py` verified on live Azure: DENIED(77.0) / APPROVED(14.1) / ESCALATED(54.0)
- [x] Commit: `d9c467e` — `feat(azure): wire live Azure services with Key Vault secret resolution`
- [x] Learning: `learning/15-azure-integration.md`

### Phase 8 — Microsoft Agent Framework SDK  ← LATEST
- [x] `requirements.txt` — added `agent-framework-core>=1.0.0rc2`
- [x] All 4 governance agents refactored: rule-based logic extracted to `_evaluate_rules()`
  and registered as `@af.tool`; GPT-4.1 (via `agent.run()`) calls the tool and synthesises reasoning
  - `blast_radius_agent.py` → tool: `evaluate_blast_radius_rules(action_json)`
  - `policy_agent.py` → tool: `evaluate_policy_rules(action_json, metadata_json)`
  - `historical_agent.py` → tool: `evaluate_historical_rules(action_json)`
  - `financial_agent.py` → tool: `evaluate_financial_rules(action_json)`
- [x] All 3 operational agents use same framework pattern:
  - `cost_agent.py` → tool: `scan_cost_opportunities()`
  - `monitoring_agent.py` → tool: `scan_anomalies()`
  - `src/operational_agents/deploy_agent.py` — **NEW**: 3 detection rules (NSG deny-all,
    lifecycle tags, sparse topology); tool: `scan_deploy_opportunities()`
- [x] `src/core/pipeline.py` — added `DeployAgent` + new `scan_operational_agents()` method
- [x] Auth pattern: `DefaultAzureCredential` + `get_bearer_token_provider` → `AsyncAzureOpenAI`
  (Responses API requires `api_version="2025-03-01-preview"`)
- [x] Mock fallback preserved: `_use_framework = not use_local_mocks and bool(endpoint)`;
  `except Exception` fallback catches live failures
- [x] Commit: `6fac593` — `feat(framework): rebuild all agents on Microsoft Agent Framework SDK`
- [x] Learning: `learning/16-microsoft-agent-framework.md`

### Phase 12 — Intelligent Ops Agents  ← LATEST
- [x] `src/infrastructure/azure_tools.py` — **NEW**: 5 generic sync Azure investigation tools:
  - `query_resource_graph(kusto_query)` — KQL query via `ResourceGraphClient`; discovers resources
  - `query_metrics(resource_id, metric_names, timespan)` — real CPU/memory data via `MetricsQueryClient`
  - `get_resource_details(resource_id)` — full resource info via Resource Graph
  - `query_activity_log(resource_group, timespan)` — recent changes via `LogsQueryClient` (LA workspace)
  - `list_nsg_rules(nsg_resource_id)` — actual NSG security rules via Resource Graph
  - Each: `DefaultAzureCredential` live mode + realistic mock fallback from `seed_resources.json`
  - All sync (work directly inside `@af.tool` without `asyncio.run()` conflicts)
- [x] `src/operational_agents/cost_agent.py` — **rewritten**: Senior FinOps Engineer persona
  - Tools: `query_resource_graph`, `query_metrics`, `get_resource_details`, `propose_action`
  - GPT-4.1 discovers VMs, checks 7-day avg CPU, only proposes when evidence shows waste (< 20%)
  - `propose_action` tool validates ActionType/Urgency enums, parses ARM resource IDs
  - `_scan_rules()` preserved unchanged for mock/CI fallback
- [x] `src/operational_agents/monitoring_agent.py` — **rewritten**: Senior SRE persona
  - New `alert_payload` parameter: alert-driven mode receives Azure Monitor webhook data
  - Alert mode: confirms metric with real data before proposing remediation
  - Scan mode: proactive reliability scan across a resource group
  - Tools: `query_metrics`, `get_resource_details`, `query_resource_graph`, `propose_action`
- [x] `src/operational_agents/deploy_agent.py` — **rewritten**: Senior Platform Engineer persona
  - GPT-4.1 discovers NSGs, inspects actual rules via `list_nsg_rules`, checks activity logs
  - Tools: `query_resource_graph`, `list_nsg_rules`, `get_resource_details`, `query_activity_log`, `propose_action`
- [x] `src/api/dashboard_api.py` — `POST /api/alert-trigger` endpoint added
  - Receives Azure Monitor alert webhook body (resource_id, metric, value, threshold)
  - Calls `MonitoringAgent.scan(alert_payload=...)` → `pipeline.evaluate()` for each proposal
  - Returns proposals + governance verdicts in one response
  - CORS updated to allow POST methods
- [x] `demo_live.py` — **NEW**: Phase 12 two-layer intelligence demo
  - Scenario 1: CPU alert → MonitoringAgent investigates vm-web-01 → SCALE_UP proposal
  - Scenario 2: FinOps scan → CostAgent discovers idle vm-dr-01 → SCALE_DOWN proposal
  - Scenario 3: Security review → DeployAgent audits nsg-east-prod → MODIFY_NSG proposal
  - Each: shows GPT-4.1 reasoning (Layer 1) + SentinelLayer SRI verdict (Layer 2)
- [x] **Test result: 398 passed, 10 xfailed, 0 failed** ✅
- [x] Commit: `af1bf28` — `feat(agents): environment-agnostic intelligent ops agents`
- [x] Learning: `learning/23-intelligent-agents.md`

### Phase 11 — Mini Production Environment
- [x] `infrastructure/terraform-prod/main.tf` — 14 Azure resources in `sentinel-prod-rg`:
  - `vm-dr-01` (Standard_B1ms, Ubuntu) — idle DR VM; cost agent → `DELETE` → **DENIED**
    (tags: `disaster-recovery=true`, `environment=production`, `owner=platform-team`, `cost-center=infrastructure`)
  - `vm-web-01` (Standard_B1ms, Ubuntu) — active web server; SRE agent → `SCALE_UP` → **APPROVED**
    (tags: `tier=web`, `environment=production`, `owner=web-team`, `cost-center=frontend`)
  - `payment-api-prod-{suffix}` (App Service Basic B1) — payment microservice; `critical=true`
    dependency of vm-web-01 that raises blast radius for any web-tier action
  - `nsg-east-prod` (NSG, HTTP/HTTPS allow) — deploy agent → open port 8080 → **ESCALATED**
    (affects all workloads behind subnet gateway; tags: `managed-by=platform-team`)
  - `sentinelprod{suffix}` (Storage LRS) — shared dependency for all three resources
  - Auto-shutdown at 22:00 UTC on both VMs (saves ~$1/day between demo runs)
  - CPU metric alert on `vm-web-01` (>80%, 15-min window) — triggers monitoring agent
  - Heartbeat scheduled-query alert on `vm-dr-01` (no heartbeat in 15 min) — triggers cost agent
  - Log Analytics workspace + Monitor action group backing both alerts
- [x] `infrastructure/terraform-prod/variables.tf` — 6 variables: `subscription_id`, `location`,
  `suffix` (regex-validated, drives globally-unique names), `vm_admin_username`,
  `vm_admin_password` (sensitive, 12-char min), `alert_email`
- [x] `infrastructure/terraform-prod/outputs.tf` — all resource IDs, names, tags, IPs,
  App Service URL, `seed_resources_ids` helper output for updating `data/seed_resources.json`
- [x] `infrastructure/terraform-prod/terraform.tfvars.example` — template with all placeholders
- [x] `infrastructure/terraform-prod/README.md` — governance scenario SRI score breakdowns,
  deploy/destroy commands, cost table (~$0.35/day with auto-shutdown), agent install note
- [x] `data/seed_resources.json` — new `sentinel-prod-rg` resources added with real Azure ID paths
  (placeholder subscription ID until `terraform apply`). Legacy mock resources (`vm-23`,
  `api-server-03`, `nsg-east`, etc.) **kept** for test compatibility.
- [x] `.gitignore` — `infrastructure/terraform-prod/` tfstate and tfvars entries added
- [x] `learning/21-mini-prod-environment.md` — IaC concepts, tagging strategy, auto-shutdown
  cost math, full governance scenario walkthrough for a non-programmer audience (gitignored)
- [x] **Test result: 398 passed, 10 xfailed, 0 failed** ✅ (seed_resources still has all legacy names)

#### Phase 11 Bugfix — Azure capacity/quota constraints + region switch
- [x] `infrastructure/terraform-prod/main.tf` — VM size `Standard_B1s` → `Standard_B2ls_v2`
  (B1s/B1ms capacity unavailable in eastus/eastus2 on trial subscriptions; B2ls_v2 available in canadacentral)
- [x] `infrastructure/terraform-prod/main.tf` — App Service plan `B1` → `F1`
  (F1 free tier sufficient for governance demo; saves ~$0.43/day)
- [x] `infrastructure/terraform-prod/variables.tf` — default `location` changed to `canadacentral`
  (eastus/eastus2 had consistent quota failures; canadacentral has reliable B2ls_v2 + F1 availability)
- [x] `infrastructure/terraform-prod/variables.tf` — location description updated (removed eastus2 reference)
- [x] `infrastructure/terraform-prod/terraform.tfvars.example` — location updated to `canadacentral`, `vm_size` added explicitly
- Demo intent unchanged: governance verdicts (DENIED/APPROVED/ESCALATED) are tag-driven,
  not SKU-driven — swapping VM size has zero effect on SRI scoring

#### Phase 11 Enhancement — CPU stress automation + AMA/DCR + Bastion removal
- [x] `infrastructure/terraform-prod/main.tf` — `custom_data` (cloud-init) added to `vm-web-01`:
  installs `stress-ng` + adds cron job (`*/30 * * * *`, 20-min CPU spike) on first boot.
  CPU alert fires naturally every 30 min without manual intervention or SSH access.
  Cron persists across deallocation (OS disk preserved); only lost on `terraform destroy`.
- [x] `infrastructure/terraform-prod/main.tf` — Azure Monitor Agent (AMA) VM extension added
  to both VMs (`azurerm_virtual_machine_extension`); Data Collection Rule (DCR) +
  associations added — heartbeat alert now uses real telemetry, not "no data" state
- [x] `infrastructure/terraform-prod/main.tf` — Azure Bastion removed (subnet, public IP, host,
  SSH NSG rule). SSH not needed — VMs are governance targets, not interactive boxes.
  Saves ~$4.56/day. Use `az vm run-command invoke` for any one-off commands.
- [x] `infrastructure/terraform-prod/main.tf` — dynamic cost lookup map (`vm_hourly_rate_usd_by_sku`)
  added to `locals`; `outputs.tf` now prints actual hourly rate for the configured SKU
- [x] `infrastructure/terraform-prod/README.md` — updated: SKU, region, cost table,
  cloud-init note, AMA/DCR note, Bastion removal note

#### Phase 11 Bugfix — Storage ip_rules `/32` rejection (commit 31b40ba)
- [x] `infrastructure/terraform-prod/main.tf` — split `locals` into two:
  - `local.allowed_source_cidr` → `<ip>/32` — used for NSG `source_address_prefix` (NSG accepts `/32`)
  - `local.storage_allowed_ip` → plain IP — used for storage `ip_rules` (Azure Storage rejects `/31` and `/32` CIDRs)
  - `local.raw_public_ip` — intermediate: `trimspace(api.ipify.org response)`, consumed by both
  - For override CIDRs ending in `/32`: `cidrhost()` strips to plain IP for storage; NSG keeps the `/32`
- [x] `infrastructure/terraform-prod/outputs.tf` — added `storage_allowed_ip` output alongside
  the existing `nsg_allowed_source_cidr` so both effective values are visible after apply

### Phase 10 — A2A Protocol
- [x] `src/a2a/sentinel_a2a_server.py` — `SentinelAgentExecutor(AgentExecutor)` routes
  tasks through the governance pipeline; streams progress via `TaskUpdater.new_agent_message()`;
  returns `GovernanceVerdict` as A2A artifact. Agent Card at `/.well-known/agent-card.json`
  with 3 skills: `evaluate_action`, `query_decision_history`, `get_resource_risk_profile`.
- [x] `src/a2a/operational_a2a_clients.py` — `CostAgentA2AClient`, `MonitoringAgentA2AClient`,
  `DeployAgentA2AClient` — each wraps the corresponding operational agent, uses
  `A2ACardResolver` for discovery, `A2AClient.send_message_streaming()` for SSE transport,
  `httpx.AsyncClient` for async HTTP.
- [x] `src/a2a/agent_registry.py` — `AgentRegistry` persists agent stats to
  `data/agents/` (mock) or Cosmos DB container `governance-agents` (live).
  Methods: `register_agent()`, `get_connected_agents()`, `get_agent_stats()`, `update_agent_stats()`.
- [x] `src/api/dashboard_api.py` — added `GET /api/agents` and `GET /api/agents/{name}/history`.
- [x] `demo_a2a.py` — A2A end-to-end demo: server in background thread, 3 scenarios
  (DENIED / APPROVED / ESCALATED), agent registry summary.
- [x] `requirements.txt` — pinned `agent-framework-a2a==1.0.0b260225`, `a2a-sdk==0.3.24`,
  `httpx==0.28.1`.
- [x] `tests/test_a2a.py` — 20 tests: Agent Card, registry CRUD, executor (mock pipeline),
  dashboard API endpoints.
- [x] **Test result: 398 passed, 10 xfailed, 0 failed** ✅

### Phase 10 Bug Fixes (commit 1fee7d1)
- [x] `src/a2a/sentinel_a2a_server.py` — `DecisionTracker().record(verdict)` added after
  `pipeline.evaluate()`; A2A verdicts now written to Cosmos DB audit trail (were silently dropped).
- [x] `infrastructure/terraform/main.tf` — `azurerm_cosmosdb_sql_container "governance_agents"`
  added with `partition_key_paths = ["/name"]`; container now exists in Terraform.
- [x] `src/a2a/operational_a2a_clients.py` — `agent_card_url=self._server_url` in all 3 clients
  (was `""` — empty string stored in registry).
- [x] `src/api/dashboard_api.py` — `get_recent(limit=1000)` raised from 200; prevents
  silent record truncation before agent-name filtering.
- [x] **Test result: 398 passed, 10 xfailed, 0 failed** ✅
- [x] Learning: `learning/20-a2a-bugfixes.md`

### Partition Key Mismatch Fix (commit a09dc96→ earlier)
- [x] `infrastructure/terraform/main.tf` — `governance-agents` container partition key
  corrected from `/agent_name` (field that never existed in documents) to `/name`
  (matches the `"name"` field in every registry document and the `partition_key=name`
  value passed by `_load_entry`). Option (b) chosen — zero Python changes required.
- [x] `CONTEXT.md`, `STATUS.md`, `docs/SETUP.md` — docs updated to `/name`
- [x] **Test result: 398 passed, 10 xfailed, 0 failed** ✅

### Runtime Fixes (commits ac6ca2c, 50fac30, 7b62822)
- [x] `src/a2a/operational_a2a_clients.py` — `A2ACardResolver` constructor renamed
  from `http_client=` to `httpx_client=` (a2a-sdk==0.3.24 API). Was causing
  `TypeError` at demo startup — no verdicts reached, `data/agents/` stayed empty.
- [x] `src/api/dashboard_api.py` — replaced two `_get_tracker()._load_all()` calls
  with `_get_tracker().get_recent(limit=10_000)`. `_load_all()` does not exist on
  `DecisionTracker` (it's private to `CosmosDecisionClient`). Was causing HTTP 500
  on `GET /api/metrics` and `GET /api/evaluations/{id}`.
- [x] `tests/test_dashboard_api.py` — removed 17 `@pytest.mark.xfail` decorators from
  `TestGetEvaluation` and `TestGetMetrics`. These tests now pass because the
  `_load_all()` root cause is fixed. Remaining 10 xfails: `TestRecord` tests about
  `tracker._dir` (unrelated Phase 7 issue).
- [x] `dashboard/src/components/ConnectedAgents.jsx` — NEW: agent card grid with
  online/offline status (last_seen < 5 min), mini flex bar chart (approved/escalated/denied).
- [x] `dashboard/src/components/LiveActivityFeed.jsx` — NEW: real-time feed of
  recent evaluations, relative time display, VerdictBadge.
- [x] `dashboard/src/App.jsx` — `fetchAll()` extracted for silent background refresh;
  `setInterval(5000)` auto-refresh with `clearInterval` cleanup; SRI gauge shows
  triggering `agent_id`.
- [x] **Test result: 398 passed, 10 xfailed, 0 failed** ✅
- [x] Learning: `learning/19-dashboard-a2a.md`

### TaskUpdater Async Await Fix (commit 5094313)
- [x] `src/a2a/sentinel_a2a_server.py` — Added `await` to all five `TaskUpdater`
  calls in `SentinelAgentExecutor.execute()`. `submit()`, `start_work()`,
  `add_artifact()`, and `complete()` are all `async def` in the a2a-sdk. Calling
  them without `await` creates coroutine objects that Python silently discards
  (no error raised). The artifact was never enqueued → client stream received no
  `TaskArtifactUpdateEvent` → `verdict_json` stayed `None` → `send_action_to_sentinel`
  returned `None` → `_update_registry()` never called → dashboard showed
  "No A2A agents connected yet" even after all previous fixes.
- [x] `tests/test_a2a.py` — Updated 3 `TestSentinelAgentExecutor` tests from
  `updater_instance = MagicMock()` to `AsyncMock()`. `MagicMock` objects cannot be
  `await`ed; `AsyncMock` supports both sync calls and `await` automatically.
- [x] **Test result: 398 passed, 10 xfailed, 0 failed** ✅ (20/20 A2A tests pass)

### AgentRegistry Cosmos Key + Demo Mock Fix (commit 3534d0e)
- [x] `src/a2a/agent_registry.py` — `cosmos_key` now resolved before the `_is_mock`
  check, adding `or not self._cosmos_key` to the condition (mirrors `CosmosDecisionClient`
  exactly). Previously the key was resolved inside the live-mode `try` block but not
  guarded — registry entered live mode with an empty key, `_save_entry()` called
  `container.upsert_item()`, Cosmos rejected with auth error, exception propagated with
  no catch → all agent writes silently dropped → dashboard showed "No A2A agents connected".
  `CosmosDecisionClient` always had this guard and fell to mock correctly; now both clients
  behave identically.
- [x] `demo_a2a.py` — removed `os.environ.setdefault("USE_LOCAL_MOCKS", "true")`.
  `setdefault` only writes if the key is absent; because Python loads imports before
  dotenv files, the setdefault always fired first and forced mock mode regardless of what
  `.env` said. Demo now reads `USE_LOCAL_MOCKS` from `.env` like the dashboard API does.
- [x] **Test result: 398 passed, 10 xfailed, 0 failed** ✅

### SSE Event Unwrapping Fix (commit 72d5204)
- [x] `src/a2a/operational_a2a_clients.py` — `A2AClient.send_message_streaming()`
  yields `SendStreamingMessageResponse` objects, not raw events. The actual
  `TaskStatusUpdateEvent` / `TaskArtifactUpdateEvent` is at `.root.result`.
  Previous code checked `isinstance(event.root, TaskArtifactUpdateEvent)` which
  was always False → `verdict_json` never set → `send_action_to_sentinel` always
  returned `None` → `data/agents/` stayed empty → dashboard showed
  "No A2A agents connected yet". Fix: added `result = getattr(root, "result", root)`
  and switched isinstance checks to use `result`.
- [x] **Test result: 398 passed, 10 xfailed, 0 failed** ✅

### Phase 9 — Async-First Refactor
- [x] **Issue 1 — async-first**: all 7 agent `evaluate()`/`scan()` methods → `async def`;
  `asyncio.run()` removed everywhere; callers use `await`
- [x] `src/core/pipeline.py` — `ThreadPoolExecutor` replaced with `asyncio.gather()`
  (4 governance agents + 3 operational agents run concurrently in the same event loop)
- [x] `src/core/interception.py` — `intercept()` and `intercept_from_dict()` → `async def`
- [x] `src/mcp_server/server.py` — `sentinel_evaluate_action()` → `async def`
- [x] `demo.py` — `scenario_1/2/3()` and `main()` → `async def`, entry: `asyncio.run(main())`
- [x] `src/api/dashboard_api.py` — all 4 endpoints → `async def`
- [x] **Issue 2 — credentials**: `AzureCliCredential` → `DefaultAzureCredential` in all 7 agents
  (works for `az login` locally and Managed Identity in Azure)
- [x] **Issue 3 — pin dep**: `requirements.txt`: `agent-framework-core>=1.0.0rc2` → `==1.0.0rc2`
- [x] **Issue 4 — xfail**: 27 pre-existing failures marked `@pytest.mark.xfail`
  (10 × `TestRecord` — `tracker._dir` gone; 17 × dashboard — `_load_all()` gone; both Phase 7)
- [x] Installed `pytest-asyncio==1.3.0` (was missing from environment)
- [x] **Test result: 361 passed, 27 xfailed, 0 failed** ✅
- [x] Commit: `164b713` — `fix(async): refactor to async-first, pin deps, mark known xfails`
- [x] Learning: `learning/17-async-refactor.md`

---

## Current Configuration

```
USE_LOCAL_MOCKS=false                   ← live Azure is the default
AZURE_OPENAI_ENDPOINT=https://sentinel-foundry-psc0des.cognitiveservices.azure.com/
AZURE_OPENAI_DEPLOYMENT=gpt-41
AZURE_SEARCH_ENDPOINT=https://sentinel-search-psc0des.search.windows.net
AZURE_SEARCH_INDEX=incident-history     ← seeded with 7 incidents
COSMOS_ENDPOINT=https://sentinel-cosmos-psc0des.documents.azure.com:443/
COSMOS_DATABASE=sentinellayer
COSMOS_CONTAINER_DECISIONS=governance-decisions
AZURE_KEYVAULT_URL=https://sentinel-kv-psc0des.vault.azure.net/
A2A_SERVER_URL=http://localhost:8000    ← A2A server base URL (Phase 10)
```

API keys are **not** in `.env` — fetched at runtime from Key Vault.
Run `az login` locally before starting any live-mode service.

---

## How to Run

```bash
# One-time: seed incidents into Azure AI Search
python scripts/seed_data.py

# End-to-end governance demo — direct Python pipeline (3 scenarios)
python demo.py

# Phase 12 — two-layer intelligence demo (ops agents investigate + SentinelLayer evaluates)
python demo_live.py

# A2A protocol demo — server + 3 agent clients via A2A (Phase 10)
python demo_a2a.py

# SentinelLayer as A2A server (Phase 10)
uvicorn src.a2a.sentinel_a2a_server:app --host 0.0.0.0 --port 8000

# FastAPI dashboard (includes /api/agents + alert-trigger endpoints)
uvicorn src.api.dashboard_api:app --reload

# Trigger monitoring agent via API (simulates Azure Monitor alert webhook):
# POST /api/alert-trigger  body: {"resource_id":"vm-web-01","metric":"Percentage CPU","value":95}

# Unit tests (mock mode — no Azure needed)
pytest tests/ -v
```

---

## Known Issues / Tech Debt

- [ ] `learning/` numbering is inconsistent — files 03, 04, 15 have duplicates from
  mid-sprint renames. Does not affect functionality.
- [ ] Azure AI Search uses BM25 full-text; vector embeddings (semantic ranking) would
  require a separate `text-embedding-3-small` deployment in Foundry.
- [ ] `functions/function_app.py` exists but is not wired into the main pipeline.
  Azure Function deployment is not yet configured.
- [ ] React dashboard (`learning/11-react-dashboard.md`) is documented; frontend lives
  in `dashboard/` (not `ui/`).
- [ ] No CI/CD pipeline — tests run locally only.

---

## Known Limitations (Azure OpenAI Rate Limiting)

**HTTP 429 — Too Many Requests:** All 5 agents hit Azure OpenAI's rate limit during
`demo.py` and fall back to deterministic rules. This means the AI reasoning layer is
not exercised in practice today.

**Why it happens:** Azure OpenAI deployments have a **Tokens Per Minute (TPM)** and
**Requests Per Minute (RPM)** quota. Running 5 governance agents × 3 scenarios = up to
15 concurrent LLM calls exhausts even a generous quota in seconds. The `except Exception`
fallback in each agent catches the 429 and silently continues with rule-based scoring.

**Where to check:** Azure Portal → Azure OpenAI → your deployment → Quotas.
Request a quota increase or add exponential back-off + retry logic in each agent's
`_evaluate_with_framework()` before re-attempting the framework path.

**Impact:** Governance scoring still works correctly (deterministic rules are the
safety floor), but GPT-4.1's semantic reasoning — which should catch things like
equivalent tag formats or novel risk patterns — is never reached.

---

## What's Next (Suggested)

These are ideas, not commitments. Pick up from here:

### Phase 13 — Azure Monitor Alert Webhook Wiring (Priority)

Phase 12 built the `POST /api/alert-trigger` endpoint and the intelligent `MonitoringAgent`.
Phase 13 closes the loop by wiring real Azure Monitor alerts directly to this endpoint:

```
Real flow (Phase 13 target):
  vm-web-01 CPU > 80% (stress-ng fires every 30 min via cron)
        ↓
  Azure Monitor metric alert fires
        ↓
  Action Group webhook → POST /api/alert-trigger
        ↓
  MonitoringAgent.scan(alert_payload=...) → confirms via query_metrics
        ↓
  GPT-4.1: "7-day avg CPU 82.5%, peak 100% — sustained load. Propose scale_up."
        ↓
  SentinelLayer evaluates → APPROVED (SRI < 25, low blast radius)
        ↓
  Verdict written to Cosmos DB, visible on dashboard
```

**Steps:**
1. Expose `POST /api/alert-trigger` publicly (ngrok for demo, or Azure App Service)
2. In Azure Portal: Alerts → Action Groups → add Webhook pointing to the endpoint
3. Test: run `stress-ng` on vm-web-01 → alert fires → end-to-end

### Phase 14 — React Dashboard: Live Agent Intelligence Panel

Add a new dashboard panel showing the two-layer intelligence in real time:
- Layer 1 card: which ops agent fired, what tools it called, the evidence it gathered
- Layer 2 card: SentinelLayer's SRI breakdown and verdict

### Phase 15 — Multi-Agent Orchestrator

Build an orchestrator that runs all 3 ops agents on a schedule and pipes proposals
through SentinelLayer automatically — fully autonomous cloud governance loop.

- [ ] **Wire Logic App webhook** — Azure Monitor alert → HTTP POST to
  `POST /api/evaluate` or a new `/api/alert-trigger` endpoint
- [ ] **Intelligent monitoring-agent** — queries real Azure Monitor API for metric
  values; GPT-4.1 decides whether to propose scale-up and what SKU based on data
- [ ] **Intelligent cost-agent** — queries Azure Monitor 30-day CPU history for all
  VMs; GPT-4.1 reasons about idle vs standby vs DR before proposing deletion
- [ ] **Semantic policy matching** — policy evaluation should use GPT-4.1 to match
  resource tags semantically (not exact string) — a resource tagged `disaster-recovery:
  true` or `purpose: disaster-recovery` or `dr-role: standby` should all trigger
  `POL-DR-001`; exact string match is brittle against real-world tag drift
- [ ] **Fix 429 rate limiting** — add exponential back-off + retry in
  `_evaluate_with_framework()` across all agents; alternatively request TPM quota
  increase in Azure Portal

### Other Improvements
- [ ] **Vector search** — deploy `text-embedding-3-small` in Foundry, add vector field
  to `incident-history` index, generate embeddings on seed + query
- [ ] **Azure Function deployment** — wire `functions/function_app.py` for serverless
  governance endpoint
- [ ] **CI/CD** — GitHub Actions: run `pytest tests/ -v` on PR, deploy to Azure on merge
- [ ] **More policies** — add `data/policies.json` entries for cost caps, region
  restrictions, tag compliance
- [ ] **More seed incidents** — expand `data/seed_incidents.json` beyond 7 entries
- [ ] **Streaming LLM responses** — stream GPT-4.1 tokens to the dashboard in real time

---

## Secret Names in Key Vault

| Secret Name | Used By |
|-------------|---------|
| `foundry-primary-key` | `AzureOpenAIClient` |
| `search-primary-key` | `AzureSearchClient` |
| `cosmos-primary-key` | `CosmosDecisionClient` |

---

## File Ownership Map

| File | What it does | Last changed |
|------|-------------|-------------|
| `src/core/models.py` | All Pydantic data models — shared contract | Phase 1 |
| `src/core/pipeline.py` | Parallel agent orchestration + `scan_operational_agents()` | Phase 8 |
| `src/core/governance_engine.py` | SRI composite + verdict logic | Phase 2 |
| `src/core/decision_tracker.py` | Verdict → Cosmos DB / JSON | Phase 7 |
| `src/governance_agents/blast_radius_agent.py` | SRI:Infrastructure — Agent Framework + `@tool` | Phase 8 |
| `src/governance_agents/policy_agent.py` | SRI:Policy — Agent Framework + `@tool` | Phase 8 |
| `src/governance_agents/historical_agent.py` | SRI:Historical — Agent Framework + `@tool` | Phase 8 |
| `src/governance_agents/financial_agent.py` | SRI:Cost — Agent Framework + `@tool` | Phase 8 |
| `src/operational_agents/cost_agent.py` | Cost proposals — Agent Framework + `@tool` | Phase 8 |
| `src/operational_agents/monitoring_agent.py` | SRE anomaly detection — Agent Framework + `@tool` | Phase 8 |
| `src/operational_agents/deploy_agent.py` | Infrastructure deploy proposals (NEW) | Phase 8 |
| `src/infrastructure/openai_client.py` | GPT-4.1 via Foundry (direct completions) | Phase 7 |
| `src/infrastructure/cosmos_client.py` | Cosmos DB read/write | Phase 6 |
| `src/infrastructure/search_client.py` | Azure AI Search + index seeding | Phase 7 |
| `src/infrastructure/secrets.py` | Key Vault secret resolution | Phase 6 |
| `src/config.py` | All env vars + SRI thresholds | Phase 1 |
| `data/policies.json` | 6 governance policies | Phase 2 |
| `data/seed_incidents.json` | 7 past incidents (also in Azure Search) | Phase 3 |
| `data/seed_resources.json` | Azure resource topology mock | Phase 2 |
| `scripts/seed_data.py` | Index seed_incidents into Azure Search | Phase 5 |
| `src/a2a/sentinel_a2a_server.py` | A2A server — AgentCard + SentinelAgentExecutor + audit trail write; all TaskUpdater calls awaited | TaskUpdater fix |
| `src/a2a/operational_a2a_clients.py` | A2A client wrappers — `httpx_client=`; SSE `.root.result` unwrap | SSE fix |
| `src/a2a/agent_registry.py` | Tracks connected A2A agents + governance stats; cosmos_key guard matches CosmosDecisionClient | Registry fix |
| `src/api/dashboard_api.py` | FastAPI REST — 6 endpoints; uses `get_recent()` not `_load_all()` | Runtime fixes |
| `infrastructure/terraform/main.tf` | Azure infra — Foundry, Search, Cosmos (2 containers), KV | Phase 10 bugfixes |
| `infrastructure/terraform-prod/main.tf` | Mini prod env — 2 VMs, NSG, storage, App Service, monitor alerts | Phase 11 |
| `infrastructure/terraform-prod/outputs.tf` | Exports all resource IDs, names, tags, URLs | Phase 11 |
| `infrastructure/terraform-prod/variables.tf` | Input variables incl. sensitive vm_admin_password | Phase 11 |
| `data/seed_resources.json` | Azure resource topology — sentinel-prod-rg resources + legacy mocks | Phase 11 |
| `dashboard/src/App.jsx` | Root component — fetchAll, setInterval, ConnectedAgents, LiveActivityFeed | Runtime fixes |
| `dashboard/src/components/ConnectedAgents.jsx` | Agent card grid with online status + bar chart (NEW) | Runtime fixes |
| `dashboard/src/components/LiveActivityFeed.jsx` | Real-time evaluation feed with relative timestamps (NEW) | Runtime fixes |
| `dashboard/src/api.js` | Frontend fetch helpers incl. fetchAgents() | Runtime fixes |
| `demo_a2a.py` | A2A end-to-end demo (3 scenarios); removed USE_LOCAL_MOCKS setdefault | Registry fix |
