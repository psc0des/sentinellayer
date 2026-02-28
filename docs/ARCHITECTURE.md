# SentinelLayer — Architecture

## System Overview

SentinelLayer implements a **governance pipeline** pattern that intercepts AI agent infrastructure
actions before they execute, scores them using the Sentinel Risk Index (SRI™), and returns a
structured verdict.

```
Operational Agent (proposes action)
    │
    ├─── A2A HTTP (src/a2a/sentinel_a2a_server.py)
    ├─── MCP stdio (src/mcp_server/server.py)
    └─── Direct Python (src/core/interception.py)
    │
    ▼ (all three paths converge here)
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

## Three Ways to Call SentinelLayer

All three paths converge at `SentinelLayerPipeline.evaluate()` — same SRI™ scoring,
same verdict, same Cosmos DB audit trail.

### 1. A2A (HTTP) — Enterprise / Multi-Service Pattern
External AI agents running as separate services (microservices, Kubernetes pods).
They discover SentinelLayer via the Agent Card, send `ProposedAction` tasks over HTTP,
and receive streaming `GovernanceVerdict` results via SSE.

- **Entry point:** `src/a2a/sentinel_a2a_server.py`
- **Start:** `uvicorn src.a2a.sentinel_a2a_server:app --host 0.0.0.0 --port 8000`
- **Demo:** `python demo_a2a.py`

### 2. MCP (stdio) — Developer / IDE Pattern
AI tools on the same machine (Claude Desktop, GitHub Copilot, any MCP host) call
`sentinel_evaluate_action` as a structured MCP tool. Communication is via stdin/stdout
pipes — no network, no port, no deployment required.

- **Entry point:** `src/mcp_server/server.py`
- **Start:** `python -m src.mcp_server.server`

### 3. Direct Python — Local / Test Pattern
Code in the same codebase calls the pipeline directly. No network, no process
boundary — minimal overhead. Used by `demo.py` and all unit tests.

- **Entry point:** `src/core/pipeline.py`
- **Demo:** `python demo.py`

| | A2A (HTTP) | MCP (stdio) | Direct Python |
|---|---|---|---|
| **Transport** | HTTP + SSE | stdin/stdout pipes | In-process call |
| **Discovery** | Agent Card `/.well-known/agent-card.json` | MCP host config | Python import |
| **Used by** | External agents (separate services) | Claude Desktop, Copilot | demo.py, tests |
| **Pattern** | Enterprise / microservices | Developer / IDE | Local / testing |
| **Streaming** | Yes — SSE progress updates | No | No |

---

## Key Design Decisions

1. **Async-first** — all agent `evaluate()` / `scan()` methods are `async def`. The pipeline uses
   `asyncio.gather()` so all 4 governance agents run concurrently without nested event loops.
   Safe under FastAPI, MCP server (FastMCP), and async test runners.

2. **A2A as the network protocol layer** — `src/a2a/sentinel_a2a_server.py` exposes
   SentinelLayer as an A2A-compliant HTTP server. Any A2A-capable agent discovers it via
   `/.well-known/agent-card.json`, sends `ProposedAction` tasks, and receives streaming
   `GovernanceVerdict` results via SSE. Existing MCP and direct Python paths are unchanged.

3. **MCP as interception layer** — `sentinel_evaluate_action` is a standard MCP tool; any
   MCP-capable agent (Claude Desktop, Copilot, custom agents) can call SentinelLayer without
   SDK changes.

4. **Microsoft Agent Framework** — in live mode, each agent is backed by GPT-4.1 (via
   `agent-framework-core==1.0.0rc2`). The LLM calls a deterministic `@af.tool`, then synthesises
   a human-readable reasoning narrative. Mock mode bypasses the framework entirely (no Azure needed).

5. **DefaultAzureCredential** — used in all 7 agents. Works with `az login` locally and Managed
   Identity in Azure — no code changes between environments.

6. **Branded scoring (SRI™)** — consistent 0–100 scale per dimension, weighted composite,
   configurable thresholds in `src/config.py`.

7. **Immutable audit trail** — every verdict is written to Cosmos DB (live) or a local JSON file
   (mock). Never overwritten; each decision gets a UUID `action_id`.

8. **Configurable thresholds** — `SRI_AUTO_APPROVE_THRESHOLD` (default 25) and
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

### Governance Infrastructure (`infrastructure/terraform/`)

| Service | Used by | Config var |
|---|---|---|
| Azure OpenAI / GPT-4.1 | All 7 agents (Agent Framework) | `AZURE_OPENAI_ENDPOINT` |
| Azure AI Search | `HistoricalPatternAgent` | `AZURE_SEARCH_ENDPOINT` |
| Azure Cosmos DB | `DecisionTracker` | `COSMOS_ENDPOINT` |
| Azure Key Vault | All secrets at runtime | `AZURE_KEYVAULT_URL` |

In mock mode (`USE_LOCAL_MOCKS=true`), all four Azure services are replaced by local JSON files
and in-memory logic — no cloud connection needed.

### Governed Resources (`infrastructure/terraform-prod/`)

The resources that SentinelLayer **governs** in live demos. These are the targets of operational
agent actions — not the governance system itself.

| Resource | Type | Governance Scenario |
|---|---|---|
| `vm-dr-01` | Linux VM (`var.vm_size`, default B2ls_v2) | DENIED — `disaster-recovery=true` policy |
| `vm-web-01` | Linux VM (`var.vm_size`, default B2ls_v2) | APPROVED — safe CPU-triggered scale-up (cloud-init runs stress-ng cron) |
| `payment-api-prod` | App Service B1 | Critical dependency (raises blast radius) |
| `nsg-east-prod` | Network Security Group | ESCALATED — port 8080 open affects all governed VMs |
| `sentinelprod{suffix}` | Storage Account LRS | Shared dependency; deletion = high blast radius |

---

## A2A Protocol Flow (Phase 10)

```
Operational Agent (A2A Client)          SentinelLayer (A2A Server)
       │                                        │
       │  GET /.well-known/agent-card.json      │
       │ ─────────────────────────────────────► │
       │  ← Agent Card (name, skills, url)      │
       │                                        │
       │  POST /  tasks/sendSubscribe            │
       │  (ProposedAction JSON as TextPart)      │
       │ ─────────────────────────────────────► │
       │  ← SSE: "Evaluating blast radius..."   │
       │  ← SSE: "Checking policy..."           │
       │  ← SSE: "SRI Composite: 74.0 → DENIED" │
       │  ← ARTIFACT: GovernanceVerdict JSON    │
       │  ← TASK COMPLETE                       │
       │                                        │
       ▼                                        ▼
AgentRegistry.update_agent_stats()    DecisionTracker.record()
(data/agents/ or Cosmos DB)           (data/decisions/ or Cosmos DB)
```

All three paths — A2A, MCP, and direct Python — converge at
`SentinelLayerPipeline.evaluate()`. No governance logic was duplicated.

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
├── a2a/                       # A2A Protocol layer (Phase 10)
│   ├── sentinel_a2a_server.py # A2A server — AgentCard + SentinelAgentExecutor
│   ├── operational_a2a_clients.py # A2A client wrappers for 3 operational agents
│   └── agent_registry.py     # Tracks connected agents + stats
├── mcp_server/server.py       # FastMCP stdio — sentinel_evaluate_action (async)
├── api/dashboard_api.py       # FastAPI REST — 6 async endpoints (+ /api/agents)
├── infrastructure/            # Azure clients with mock fallback
└── config.py                  # SRI thresholds + env vars
data/
├── agents/                    # A2A agent registry (mock mode)
├── policies.json              # 6 governance policies
├── seed_incidents.json        # 7 historical incidents
└── seed_resources.json        # Azure resource topology (see note below)
infrastructure/
├── terraform/                 # Main infra — Foundry, Search, Cosmos, Key Vault
└── terraform-prod/            # Mini prod env — VMs, NSG, storage, App Service, alerts
dashboard/                     # Vite + React frontend
```

### seed_resources.json — Two Sections

`data/seed_resources.json` contains two groups of resources:

1. **Mini prod resources** (sentinel-prod-rg) — `vm-dr-01`, `vm-web-01`, `payment-api-prod`,
   `nsg-east-prod`, `sentinelproddata`. These match `infrastructure/terraform-prod/` exactly.
   After `terraform apply`, replace `YOUR-SUBSCRIPTION-ID` with your real subscription ID.
   Each has a specific governance scenario (DENIED / APPROVED / ESCALATED).

2. **Legacy mock resources** — `vm-23`, `api-server-03`, `web-tier-01`, `nsg-east`, `aks-prod`,
   `storageshared01`. These are referenced by all unit tests and must not be removed.
