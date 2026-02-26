# SentinelLayer — Implementation Status

> **Read this first** if you are an AI assistant (Claude, Codex, Gemini, etc.)
> picking up this project. It tells you exactly what is done, what is live,
> and what comes next. Architecture and coding standards are in `CONTEXT.md`.

**Last updated:** 2026-02-26
**Active branch:** `main`
**Demo verdict:** All 3 scenarios pass on live Azure (DENIED / APPROVED / ESCALATED)

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
| Pipeline (parallel execution) | ✅ Complete | `ThreadPoolExecutor` |
| Microsoft Agent Framework | ✅ Complete | `agent-framework-core` + GPT-4.1 |
| Decision tracker | ✅ Complete | Azure Cosmos DB (live) / JSON (mock) |
| MCP server | ✅ Complete | FastMCP stdio (`server.py`) |
| Dashboard API | ✅ Complete | FastAPI REST |
| Azure infrastructure (Terraform) | ✅ Deployed | Foundry · Search · Cosmos · KV |
| Secret management | ✅ Complete | Key Vault + `DefaultAzureCredential` |
| Live Azure wiring | ✅ Complete | All 3 services connected |
| React dashboard | ✅ Complete | `dashboard/` (Vite + React, same repo) |

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
- [x] `src/core/pipeline.py` — `ThreadPoolExecutor` parallel evaluation
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
- [x] Auth pattern: `AzureCliCredential` + `get_bearer_token_provider` → `AsyncAzureOpenAI`
  (Responses API requires `api_version="2025-03-01-preview"`)
- [x] Mock fallback preserved: `_use_framework = not use_local_mocks and bool(endpoint)`;
  `except Exception` fallback catches live failures; 361/388 tests pass (27 pre-existing failures unrelated)
- [x] Commit: `6fac593` — `feat(framework): rebuild all agents on Microsoft Agent Framework SDK`
- [x] Learning: `learning/16-microsoft-agent-framework.md`

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
```

API keys are **not** in `.env` — fetched at runtime from Key Vault.
Run `az login` locally before starting any live-mode service.

---

## How to Run

```bash
# One-time: seed incidents into Azure AI Search
python scripts/seed_data.py

# End-to-end governance demo (3 scenarios)
python demo.py

# FastAPI dashboard
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
