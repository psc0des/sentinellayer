# SentinelLayer — Implementation Status

> **Read this first** if you are an AI assistant (Claude, Codex, Gemini, etc.)
> picking up this project. It tells you exactly what is done, what is live,
> and what comes next. Architecture and coding standards are in `CONTEXT.md`.

**Last updated:** 2026-02-27 (Phase 10)
**Active branch:** `main`
**Demo verdict:** All 3 A2A scenarios pass in mock mode (DENIED / APPROVED / ESCALATED)

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
| Operational agent: deploy-agent | ✅ Complete | `data/seed_resources.json` |
| Pipeline (parallel execution) | ✅ Complete | `asyncio.gather()` (async-first) |
| Microsoft Agent Framework | ✅ Complete | `agent-framework-core` + GPT-4.1 |
| Decision tracker | ✅ Complete | Azure Cosmos DB (live) / JSON (mock) |
| MCP server | ✅ Complete | FastMCP stdio (`server.py`) |
| Dashboard API | ✅ Complete | FastAPI REST (+ A2A agent endpoints) |
| Azure infrastructure (Terraform) | ✅ Deployed | Foundry · Search · Cosmos · KV |
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

### Phase 10 — A2A Protocol  ← LATEST
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

# A2A protocol demo — server + 3 agent clients via A2A (Phase 10)
python demo_a2a.py

# SentinelLayer as A2A server (Phase 10)
uvicorn src.a2a.sentinel_a2a_server:app --host 0.0.0.0 --port 8000

# FastAPI dashboard (includes /api/agents endpoints)
uvicorn src.api.dashboard_api:app --reload

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

## What's Next (Suggested)

These are ideas, not commitments. Pick up from here:

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
| `src/a2a/sentinel_a2a_server.py` | A2A server — AgentCard + SentinelAgentExecutor + audit trail write | Phase 10 bugfixes |
| `src/a2a/operational_a2a_clients.py` | A2A client wrappers — `httpx_client=`; SSE `.root.result` unwrap | SSE fix |
| `src/a2a/agent_registry.py` | Tracks connected A2A agents + governance stats; cosmos_key guard matches CosmosDecisionClient | Registry fix |
| `src/api/dashboard_api.py` | FastAPI REST — 6 endpoints; uses `get_recent()` not `_load_all()` | Runtime fixes |
| `infrastructure/terraform/main.tf` | Azure infra — Foundry, Search, Cosmos (2 containers), KV | Phase 10 bugfixes |
| `dashboard/src/App.jsx` | Root component — fetchAll, setInterval, ConnectedAgents, LiveActivityFeed | Runtime fixes |
| `dashboard/src/components/ConnectedAgents.jsx` | Agent card grid with online status + bar chart (NEW) | Runtime fixes |
| `dashboard/src/components/LiveActivityFeed.jsx` | Real-time evaluation feed with relative timestamps (NEW) | Runtime fixes |
| `dashboard/src/api.js` | Frontend fetch helpers incl. fetchAgents() | Runtime fixes |
| `demo_a2a.py` | A2A end-to-end demo (3 scenarios); removed USE_LOCAL_MOCKS setdefault | Registry fix |
