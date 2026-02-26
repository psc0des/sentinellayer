# SentinelLayer — Architecture

## System Overview

SentinelLayer implements a **governance pipeline** pattern that intercepts AI agent infrastructure
actions before they execute, scores them using the Sentinel Risk Index (SRI™), and returns a
structured verdict.

```
Operational Agent (proposes action)
    │
    ▼
ActionInterceptor  ←── MCP tool / direct Python call
    │
    ▼
SentinelLayerPipeline.evaluate(action)
    │
    ├─ asyncio.gather() ──────────────────────────────────┐
    │   ├── BlastRadiusAgent.evaluate()   → SRI:Infrastructure (weight 0.30)
    │   ├── PolicyComplianceAgent.evaluate() → SRI:Policy (weight 0.25)
    │   ├── HistoricalPatternAgent.evaluate() → SRI:Historical (weight 0.25)
    │   └── FinancialImpactAgent.evaluate()   → SRI:Cost (weight 0.20)
    │                                                      │
    │   All 4 run concurrently (async-first)  ◄────────────┘
    │
    ▼
GovernanceDecisionEngine.evaluate()
    │  SRI Composite = weighted sum of 4 dimensions
    │  APPROVED  if composite ≤ 25
    │  ESCALATED if 25 < composite ≤ 60
    │  DENIED    if composite > 60 OR any critical policy violation
    │
    ▼
DecisionTracker.record(verdict)     ← writes to Cosmos DB (live) / JSON (mock)
    │
    ▼
GovernanceVerdict returned to caller
```

---

## Key Design Decisions

1. **Async-first** — all agent `evaluate()` / `scan()` methods are `async def`. The pipeline uses
   `asyncio.gather()` so all 4 governance agents run concurrently without nested event loops.
   Safe under FastAPI, MCP server (FastMCP), and async test runners.

2. **MCP as interception layer** — `sentinel_evaluate_action` is a standard MCP tool; any
   MCP-capable agent (Claude Desktop, Copilot, custom agents) can call SentinelLayer without
   SDK changes.

3. **Microsoft Agent Framework** — in live mode, each agent is backed by GPT-4.1 (via
   `agent-framework-core==1.0.0rc2`). The LLM calls a deterministic `@af.tool`, then synthesises
   a human-readable reasoning narrative. Mock mode bypasses the framework entirely (no Azure needed).

4. **DefaultAzureCredential** — used in all 7 agents. Works with `az login` locally and Managed
   Identity in Azure — no code changes between environments.

5. **Branded scoring (SRI™)** — consistent 0–100 scale per dimension, weighted composite,
   configurable thresholds in `src/config.py`.

6. **Immutable audit trail** — every verdict is written to Cosmos DB (live) or a local JSON file
   (mock). Never overwritten; each decision gets a UUID `action_id`.

7. **Configurable thresholds** — `SRI_AUTO_APPROVE_THRESHOLD` (default 25) and
   `SRI_HUMAN_REVIEW_THRESHOLD` (default 60) are environment-variable driven.

---

## Agent Roles

### Governance Agents (the governors — evaluate proposed actions)

| Agent | SRI Dimension | Data Source |
|---|---|---|
| `BlastRadiusAgent` | Infrastructure (0.30) | `seed_resources.json` — dependency graph |
| `PolicyComplianceAgent` | Policy (0.25) | `policies.json` — 6 governance rules |
| `HistoricalPatternAgent` | Historical (0.25) | Azure AI Search / `seed_incidents.json` |
| `FinancialImpactAgent` | Cost (0.20) | `seed_resources.json` — monthly cost data |

### Operational Agents (the governed — propose actions)

| Agent | What it proposes |
|---|---|
| `CostOptimizationAgent` | VM downsizing, AKS node reduction |
| `MonitoringAgent` | SRE anomaly remediation (unowned critical resources, circular deps, SPOFs) |
| `DeployAgent` | NSG deny-all rules, lifecycle tag additions, observability resources |

---

## Azure Services (live mode)

| Service | Used by | Config var |
|---|---|---|
| Azure OpenAI / GPT-4.1 | All 7 agents (Agent Framework) | `AZURE_OPENAI_ENDPOINT` |
| Azure AI Search | `HistoricalPatternAgent` | `AZURE_SEARCH_ENDPOINT` |
| Azure Cosmos DB | `DecisionTracker` | `COSMOS_ENDPOINT` |
| Azure Key Vault | All secrets at runtime | `AZURE_KEYVAULT_URL` |

In mock mode (`USE_LOCAL_MOCKS=true`), all four Azure services are replaced by local JSON files
and in-memory logic — no cloud connection needed.

---

## File Map

```
src/
├── core/
│   ├── models.py              # All Pydantic models — shared contract
│   ├── pipeline.py            # asyncio.gather() orchestration
│   ├── governance_engine.py   # SRI composite + verdict logic
│   ├── decision_tracker.py    # Audit trail → Cosmos DB / JSON
│   └── interception.py        # ActionInterceptor façade (async)
├── governance_agents/         # 4 governors — all async def evaluate()
├── operational_agents/        # 3 governed agents — all async def scan()
├── mcp_server/server.py       # FastMCP stdio — sentinel_evaluate_action (async)
├── api/dashboard_api.py       # FastAPI REST — 4 async endpoints
├── infrastructure/            # Azure clients with mock fallback
└── config.py                  # SRI thresholds + env vars
data/
├── policies.json              # 6 governance policies
├── seed_incidents.json        # 7 historical incidents
└── seed_resources.json        # Azure resource topology mock
dashboard/                     # Vite + React frontend
```
