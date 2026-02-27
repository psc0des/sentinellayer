# SentinelLayer — Architecture

## System Overview

SentinelLayer sits between AI operational agents and infrastructure execution.
Every proposed action is intercepted, scored, and either approved, escalated for
human review, or denied — before any change is made.

```
┌─────────────────────────────────────────────────────────────────────┐
│                     OPERATIONAL AGENTS (governed)                   │
│                                                                     │
│   CostOptimizationAgent  MonitoringAgent  DeployAgent               │
│   (cost-optimization-agent) (monitoring-agent) (deploy-agent)      │
└───────────────────┬──────────────────────────┬──────────────────────┘
                    │  ProposedAction           │  ProposedAction
                    │  (JSON via A2A)           │  (direct Python / MCP)
                    ▼                           ▼
┌─────────────────────────────────────────────────────────────────────┐
│                        SENTINELLAYER                                │
│                                                                     │
│  ┌─────────────────────────────────────────────────────────────┐   │
│  │  Protocol Layer                                             │   │
│  │                                                             │   │
│  │  A2A Server          MCP Server          Direct Python      │   │
│  │  (port 8000)         (stdio)             (pipeline.py)      │   │
│  │  /.well-known/       evaluate_action     pipeline.evaluate()│   │
│  │  agent-card.json     get_decisions       scan_operational() │   │
│  └──────────────┬───────────────────────────────────────────── ┘   │
│                 │  All paths converge here ↓                        │
│  ┌──────────────▼──────────────────────────────────────────────┐   │
│  │  Governance Pipeline — asyncio.gather() parallel execution  │   │
│  │                                                             │   │
│  │  BlastRadiusAgent   PolicyAgent   HistoricalAgent  FinancialAgent│
│  │  (SRI:Infra)        (SRI:Policy)  (SRI:Historical) (SRI:Cost)   │
│  │  weight 0.30        weight 0.25   weight 0.25      weight 0.20  │
│  └──────────────┬───────────────────────────────────────────── ┘   │
│                 │                                                   │
│  ┌──────────────▼──────────────────────────────────────────────┐   │
│  │  GovernanceDecisionEngine                                   │   │
│  │  SRI™ Composite = weighted average                          │   │
│  │  ≤25 → APPROVED | 26-60 → ESCALATED | >60 → DENIED         │   │
│  │  Critical policy violation → always DENIED                  │   │
│  └──────────────┬───────────────────────────────────────────── ┘   │
│                 │                                                   │
│  ┌──────────────▼──────────────────────────────────────────────┐   │
│  │  DecisionTracker → Cosmos DB (live) / data/decisions/ (mock)│   │
│  └─────────────────────────────────────────────────────────────┘   │
└─────────────────────────────────────────────────────────────────────┘
```

---

## A2A Protocol Flow (Phase 10)

```
Operational Agent                   SentinelLayer A2A Server
(A2A Client)                        (A2A Server — port 8000)
     │                                         │
     │  GET /.well-known/agent-card.json       │
     │ ──────────────────────────────────────► │
     │                                         │
     │  Agent Card JSON (name, skills, url)    │
     │ ◄────────────────────────────────────── │
     │                                         │
     │  POST /  tasks/sendSubscribe            │
     │  (ProposedAction JSON as TextPart)      │
     │ ──────────────────────────────────────► │
     │                                         │
     │  SSE: "Evaluating blast radius..."      │
     │ ◄────────────────────────────────────── │
     │  SSE: "Checking policy compliance..."   │
     │ ◄────────────────────────────────────── │
     │  SSE: "Querying historical incidents..."│
     │ ◄────────────────────────────────────── │
     │  SSE: "Calculating financial impact..." │
     │ ◄────────────────────────────────────── │
     │                                         │  await pipeline.evaluate()
     │                                         │  (4 agents via asyncio.gather)
     │                                         │
     │  SSE: "SRI Composite: 74.0 → DENIED"   │
     │ ◄────────────────────────────────────── │
     │  ARTIFACT: GovernanceVerdict JSON       │
     │ ◄────────────────────────────────────── │
     │  TASK COMPLETE                          │
     │ ◄────────────────────────────────────── │
     │                                         │
     ▼                                         ▼
 AgentRegistry.update_agent_stats()     DecisionTracker.record()
 (data/agents/ or Cosmos DB)            (data/decisions/ or Cosmos DB)
```

---

## MCP Protocol Flow (Phase 4)

```
Claude Desktop / MCP Host
     │
     │  stdio transport
     │
     ▼
src/mcp_server/server.py  (FastMCP)
     │
     │  tool: sentinel_evaluate_action(action_json)
     │  tool: sentinel_get_recent_decisions(limit)
     │  tool: sentinel_get_resource_risk_profile(resource_id)
     │
     ▼
pipeline.evaluate(action)   → GovernanceVerdict
```

---

## Authentication & Credentials

```
DefaultAzureCredential
├── Local: az login  (AzureCliCredential)
└── Azure: Managed Identity (WorkloadIdentityCredential)

Key Vault (sentinel-kv-psc0des.vault.azure.net)
├── foundry-primary-key   → AzureOpenAIClient
├── search-primary-key    → AzureSearchClient
└── cosmos-primary-key    → CosmosDecisionClient

All secrets resolved via:
  secrets.py KeyVaultSecretResolver
  env var override → Key Vault → empty (mock fallback)
```

---

## Data Flow (Mock vs Live)

| Component | Mock (USE_LOCAL_MOCKS=true) | Live (USE_LOCAL_MOCKS=false) |
|-----------|---------------------------|------------------------------|
| Resource topology | `data/seed_resources.json` | Azure Resource Graph |
| Incident history | `data/seed_incidents.json` | Azure AI Search (BM25) |
| Governance decisions | `data/decisions/*.json` | Azure Cosmos DB (SQL API) |
| A2A agent registry | `data/agents/*.json` | Azure Cosmos DB (governance-agents) |
| LLM reasoning | Skipped (rule-based only) | Azure OpenAI GPT-4.1 via Foundry |
| Secrets | Empty strings / defaults | Azure Key Vault |

---

## Agent Framework Integration (Phase 8)

Each governance agent is backed by a Microsoft Agent Framework `Agent`:

```
AgentExecutor.evaluate(action)
     │
     ├─ _use_framework = True (live mode)
     │     │
     │     └─ OpenAIResponsesClient.as_agent()
     │           │
     │           ├─ @af.tool evaluate_*_rules(action_json) → structured result
     │           └─ GPT-4.1 calls tool → synthesises reasoning narrative
     │
     └─ _use_framework = False (mock mode)
           │
           └─ _evaluate_rules(action) → deterministic result (no LLM)
```

---

## Directory Map

```
sentinellayer/
├── src/
│   ├── a2a/               ← A2A Protocol (Phase 10)
│   ├── api/               ← FastAPI Dashboard REST
│   ├── core/              ← Pipeline, Engine, Models, Tracker
│   ├── governance_agents/ ← 4 SRI™ evaluation agents
│   ├── infrastructure/    ← Azure service clients
│   ├── mcp_server/        ← MCP stdio server
│   └── operational_agents/← 3 proposal-generating agents
├── data/
│   ├── agents/            ← A2A registry (mock)
│   ├── decisions/         ← Audit trail (mock)
│   ├── policies.json
│   ├── seed_incidents.json
│   └── seed_resources.json
├── tests/                 ← pytest mock-first tests
├── dashboard/             ← React frontend (Vite)
├── terraform/             ← Azure infra as code
├── scripts/               ← setup_env.sh, seed_data.py
├── demo.py                ← Direct Python pipeline demo
├── demo_a2a.py            ← A2A protocol demo (Phase 10)
└── learning/              ← Markdown tutorials (gitignored)
```
