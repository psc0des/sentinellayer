# RuriSkry — Architecture

## System Overview

RuriSkry implements a **governance pipeline** pattern that intercepts AI agent infrastructure
actions before they execute, scores them using the Skry Risk Index (SRI™), and returns a
structured verdict.

```
Operational Agent (proposes action)
    │
    ├─── A2A HTTP (src/a2a/ruriskry_a2a_server.py)
    ├─── MCP stdio (src/mcp_server/server.py)
    └─── Direct Python (src/core/interception.py)
    │
    ▼ (all three paths converge here)
RuriSkryPipeline.evaluate(action)
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

## Three Ways to Call RuriSkry

All three paths converge at `RuriSkryPipeline.evaluate()` — same SRI™ scoring,
same verdict, same Cosmos DB audit trail.

### 1. A2A (HTTP) — Enterprise / Multi-Service Pattern
External AI agents running as separate services (microservices, Kubernetes pods).
They discover RuriSkry via the Agent Card, send `ProposedAction` tasks over HTTP,
and receive streaming `GovernanceVerdict` results via SSE.

- **Entry point:** `src/a2a/ruriskry_a2a_server.py`
- **Start:** `uvicorn src.a2a.ruriskry_a2a_server:app --host 0.0.0.0 --port 8000`
- **Demo:** `python demo_a2a.py`

### 2. MCP (stdio) — Developer / IDE Pattern
AI tools on the same machine (Claude Desktop, GitHub Copilot, any MCP host) call
`skry_evaluate_action` as a structured MCP tool. Communication is via stdin/stdout
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

1. **Async end-to-end** — all agent `evaluate()` / `scan()` methods, all `@af.tool` callbacks,
   and all Azure SDK calls are `async def`. The pipeline uses `asyncio.gather()` so all 4
   governance agents run truly in parallel — no thread pool, no event-loop blocking. Topology
   enrichment fans out 4 concurrent KQL queries + 1 HTTP cost lookup via `asyncio.gather()`.
   Safe under FastAPI, MCP server (FastMCP), and async test runners.
   The Azure AI Search client (used by `HistoricalPatternAgent`) has no async variant, so its
   `search_incidents()` call is wrapped in `asyncio.to_thread()` — the standard Python pattern
   for bridging sync I/O into an async context without rewriting the underlying client library.

2. **A2A as the network protocol layer** — `src/a2a/ruriskry_a2a_server.py` exposes
   RuriSkry as an A2A-compliant HTTP server. Any A2A-capable agent discovers it via
   `/.well-known/agent-card.json`, sends `ProposedAction` tasks, and receives streaming
   `GovernanceVerdict` results via SSE. Existing MCP and direct Python paths are unchanged.

3. **MCP as interception layer** — `skry_evaluate_action` is a standard MCP tool; any
   MCP-capable agent (Claude Desktop, Copilot, custom agents) can call RuriSkry without
   SDK changes.

4. **Microsoft Agent Framework** — in live mode, each agent is backed by GPT-4.1 (via
   `agent-framework-core==1.0.0rc2`). The LLM calls a deterministic `@af.tool`, then synthesises
   a human-readable reasoning narrative. Mock mode bypasses the framework entirely (no Azure needed).

5. **DefaultAzureCredential (sync vs async, lifecycle)** — sync clients use
   `azure.identity.DefaultAzureCredential`; async clients (`.aio.*` packages) use
   `azure.identity.aio.DefaultAzureCredential`. Both resolve credentials the same way (`az login`
   locally, Managed Identity in Azure) — no code changes between environments. Using the wrong
   variant causes `TypeError` when the async SDK client tries to `await credential.get_token()`
   on a sync credential. The async credential must also be closed — it holds its own internal HTTP
   connections for token acquisition. Pattern: nest `async with DefaultAzureCredential() as
   credential:` around `async with SomeClient(credential) as client:` so both are closed
   deterministically when the block exits.

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
| `BlastRadiusAgent` | Infrastructure (0.30) | **Live:** `ResourceGraphClient` — KQL topology (tag + NSG join) · **Mock:** `seed_resources.json` |
| `PolicyComplianceAgent` | Policy (0.25) | `policies.json` — 6 governance rules |
| `HistoricalPatternAgent` | Historical (0.25) | Azure AI Search / `seed_incidents.json` |
| `FinancialImpactAgent` | Cost (0.20) | **Live:** `ResourceGraphClient` + Azure Retail Prices API · **Mock:** `seed_resources.json` |

### Operational Agents (the governed — propose actions)

| Agent | What it proposes | Current state |
|---|---|---|
| `CostOptimizationAgent` | VM downsizing, idle resource deletion | GPT-4.1 — all 5 azure_tools; `scan()` framework-only (returns `[]` when no endpoint); `_scan_rules()` for CI tests |
| `MonitoringAgent` | SRE anomaly remediation (circular deps, SPOFs, CPU spikes) | GPT-4.1 — all 5 azure_tools; alert-driven + proactive scan modes |
| `DeployAgent` | NSG deny-all rules, lifecycle tag additions | GPT-4.1 — all 5 azure_tools; generic lifecycle tag logic (no org-specific key names) |

**Phase 12 + Phase 15 (complete):** All three agents query real Azure data sources via all
5 tools in `azure_tools.py` and use GPT-4.1 via `agent-framework-core` to reason about
context before proposing. `scan()` is framework-only — `_scan_rules()` exists for direct
test access only.  Environment-agnostic: no hardcoded resource names, tag keys, or
org-specific assumptions. See the Two-Layer Intelligence Model section below.

---

## Azure Services (live mode)

### Governance Infrastructure (`infrastructure/terraform/`)

| Service | Used by | Config var |
|---|---|---|
| Azure OpenAI / GPT-4.1 | All 7 agents (Agent Framework) | `AZURE_OPENAI_ENDPOINT` |
| Azure AI Search | `HistoricalPatternAgent` | `AZURE_SEARCH_ENDPOINT` |
| Azure Cosmos DB — `governance-decisions` | `DecisionTracker` | `COSMOS_ENDPOINT` |
| Azure Cosmos DB — `governance-agents` | `AgentRegistry` | `COSMOS_ENDPOINT` |
| Azure Cosmos DB — `governance-scan-runs` | `ScanRunTracker` | `COSMOS_CONTAINER_SCAN_RUNS` |
| Azure Key Vault | All secrets at runtime | `AZURE_KEYVAULT_URL` |

In mock mode (`USE_LOCAL_MOCKS=true`), all four Azure services are replaced by local JSON files
and in-memory logic — no cloud connection needed.

To activate live Azure topology queries for governance agents (Phase 19), also set
`USE_LIVE_TOPOLOGY=true`. This third flag is required alongside `USE_LOCAL_MOCKS=false` and
`AZURE_SUBSCRIPTION_ID` — defaulting to `false` keeps tests safe even in live-mode environments.

### Governed Resources (`infrastructure/terraform-prod/`)

The resources that RuriSkry **governs** in live demos. These are the targets of operational
agent actions — not the governance system itself.

| Resource | Type | Governance Scenario |
|---|---|---|
| `vm-dr-01` | Linux VM (`var.vm_size`, default B2ls_v2) | DENIED — `disaster-recovery=true` policy |
| `vm-web-01` | Linux VM (`var.vm_size`, default B2ls_v2) | APPROVED — safe CPU-triggered scale-up (cloud-init runs stress-ng cron) |
| `payment-api-prod` | App Service F1 (free) | Critical dependency (raises blast radius) |
| `nsg-east-prod` | Network Security Group | ESCALATED — port 8080 open affects all governed VMs |
| `ruriskryprod{suffix}` | Storage Account LRS | Shared dependency; deletion = high blast radius |

---

## A2A Protocol Flow (Phase 10)

```
Operational Agent (A2A Client)          RuriSkry (A2A Server)
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
`RuriSkryPipeline.evaluate()`. No governance logic was duplicated.

---

## Two-Layer Intelligence Model (Phase 12 Design)

RuriSkry is a **second opinion**, not the only intelligence in the system. For the
architecture to work well end-to-end, both layers need to be smart.

```
┌─────────────────────────────────────────────────────────────┐
│  Layer 1 — Ops Agent (pre-flight reasoning)                 │
│                                                             │
│  ● Query real data sources                                  │
│    - Azure Monitor: actual metric values + duration         │
│    - Resource Graph: real tags, dependencies, environment   │
│                                                             │
│  ● Reason before proposing                                  │
│    - "This VM has disaster-recovery=true — not safe to delete"
│    - "CPU has been > 80% for 20 min, not a transient spike" │
│                                                             │
│  ● Self-filter obviously dangerous proposals                │
│  ● Submit evidence-backed ProposedAction                    │
└────────────────────────┬────────────────────────────────────┘
                         │  ProposedAction (with rich context)
                         ▼
┌─────────────────────────────────────────────────────────────┐
│  Layer 2 — RuriSkry (independent second opinion)       │
│                                                             │
│  ● Catches what the ops agent missed                        │
│  ● Enforces org-wide policy the agent may not know          │
│  ● Applies SRI™ scoring across all 4 dimensions             │
│  ● Escalates or denies based on composite risk              │
└─────────────────────────────────────────────────────────────┘
```

**Why it matters — the tag example:**
`POL-DR-001` uses exact string matching (`disaster-recovery: true`). An intelligent
ops agent reading resource tags with semantic understanding would recognise a DR
resource before proposing its deletion — and either skip the proposal or explicitly
flag the risk in its reason. The exact-match policy is the safety net, not the
first line of defence. A purely rule-based ops agent is a weak Layer 1.

**Intelligent monitoring-agent — target end-to-end flow:**
```
Azure Monitor alert fires (vm-web-01 CPU > 80%)
    ↓ Logic App webhook
POST /api/evaluate  (or /api/alert-trigger)
    ↓
monitoring-agent queries Azure Monitor for real metric value + duration
    ↓
GPT-4.1 reasons: "CPU 89% sustained 20 min — not a spike.
                  B4ms covers headroom without over-provisioning."
    ↓
ProposedAction submitted with metric evidence
    ↓
RuriSkry: SRI 11.0 → APPROVED  ✅
```

---

## Live Azure Topology (Phase 19)

In live mode (`USE_LIVE_TOPOLOGY=true`), governance agents query Azure Resource Graph in
real-time instead of loading static `seed_resources.json` snapshots.

```
BlastRadiusAgent / FinancialImpactAgent
    │
    └── _find_resource_async(resource_id)
            │
            └── ResourceGraphClient._azure_enrich_topology_async(resource)
                    │
                    ├── asyncio.gather(                        ← 4 queries + 1 HTTP in parallel
                    │     KQL: VM → NIC → NSG join,
                    │     KQL: NSG → NIC → VM join,
                    │     KQL: reverse depends-on scan,
                    │     HTTP: Azure Retail Prices API (SKU → monthly cost)
                    │   )
                    │
                    ├── Tag parsing: depends-on → dependencies[], governs → governs[]
                    │
                    └── Returns enriched resource dict with:
                          dependencies, dependents, governs, monthly_cost, os_type
```

**Key design decisions:**

- **Tag-based dependency inference** — Azure resource tags (`depends-on`, `governs`) drive the
  dependency graph. KQL network topology (VM→NIC→NSG joins) supplements tags automatically.
- **OS-aware pricing** — `cost_lookup.py` queries the Azure Retail Prices REST API (public,
  no auth) with Windows/Linux filtering. Cache key includes `sku::location::os_type`.
- **KQL injection protection** — `_kql_escape()` escapes all user-supplied string literals
  in KQL queries to prevent quote injection.
- **Subscription-wide reverse lookup** — reverse depends-on scans are not scoped to a single
  resource group, so cross-RG dependents are always detected.
- **`async def aclose()`** — `ResourceGraphClient` exposes this to close the async SDK client's
  connection pool. `BlastRadiusAgent` and `FinancialImpactAgent` expose their own `aclose()` that
  delegates to `self._rg_client.aclose()`, so callers only need to close the agent.

Three flags must be set for live topology: `USE_LOCAL_MOCKS=false`, `AZURE_SUBSCRIPTION_ID`,
and `USE_LIVE_TOPOLOGY=true`. Defaulting the third flag to `false` prevents tests from making
real Azure calls even when a subscription is configured.

---

## Scan Durability & Real-Time Streaming (Phase 16)

Agent scans are **durable** — they persist to Cosmos DB (live) or local JSON (mock) and
survive server restarts. Real-time progress is streamed via SSE.

```
POST /api/scan/cost
    ↓
_run_agent_scan(scan_id, "cost", resource_group)     ← background asyncio task
    │
    ├── _persist_scan_record(scan_id, {status: "running"})     ← write-through cache
    ├── agent.scan(resource_group)                             ← ops agent investigation
    │   ├── event → asyncio.Queue (producer)                   ← 9 event types
    │   └── pipeline.evaluate(proposal) for each proposal
    ├── _persist_scan_record(scan_id, {status: "complete"})
    │
GET /api/scan/{id}/stream                            ← SSE consumer
    └── reads from asyncio.Queue → yields Server-Sent Events
        (late connections receive buffered events)
```

**Key design decisions:**

- **Write-through cache** — `_persist_scan_record()` is called on every status change. The
  in-memory `_scans` dict is the fast path; `ScanRunTracker` is the durable fallback.
- **asyncio.Queue bridges producer↔consumer** — the background task pushes events; the SSE
  generator awaits them. Events are buffered for late-connecting clients.
- **Cancellation** — `PATCH /api/scan/{id}/cancel` sets a flag; the background task checks
  it before each proposal evaluation and stops cleanly.

---

## Execution Gateway & IaC-Safe Execution (Phase 21 — Planned)

RuriSkry evaluates. Terraform executes. Humans approve. The Execution Gateway sits
between the governance verdict and any real-world change, ensuring IaC state never drifts.

```
GovernanceVerdict
       │
       ▼
ExecutionGateway.process_verdict()
  ├── DENIED    → status=blocked (log + Teams alert, no action)
  ├── ESCALATED → status=awaiting_review (dashboard Approve/Dismiss buttons)
  └── APPROVED  →
        ├── IaC-managed? (reads managed_by tag from resource metadata)
        │     ├── YES → TerraformPRGenerator.create_pr()
        │     │           ├── Create branch: ruriskry/approved/{resource}-{id}
        │     │           ├── Generate Terraform HCL change
        │     │           ├── Open PR with SRI context + reviewer checklist
        │     │           └── status=pr_created (with pr_url + pr_number)
        │     └── NO  → status=manual_required
        └── ExecutionRecord stored (in-memory; Cosmos DB in future)
```

**Three-tier execution model:**

| Verdict | Execution Path | Dashboard Status |
|---------|----------------|-----------------|
| DENIED | Block. Log + Teams alert. | Blocked (red) |
| ESCALATED | Create review request. HITL buttons in drilldown. | Awaiting Review (yellow) |
| APPROVED + IaC | Auto-generate Terraform PR. Human merges. CI/CD applies. | PR Created → Merged → Applied (green) |
| APPROVED + no IaC | Mark for manual execution. | Manual Required (grey) |

**IaC detection** — Azure resource tags drive the routing:
```hcl
tags = {
  managed_by = "terraform"    # Detected by ExecutionGateway
  iac_repo   = "psc0des/ruriskry"
  iac_path   = "infrastructure/terraform-prod"
}
```

**Key design decisions:**
- **Gateway never executes directly** — it only creates PRs or marks for manual review
- **Gateway failure never breaks the verdict** — wrapped in `try/except`; verdict is primary
- **Opt-in by default** — `EXECUTION_GATEWAY_ENABLED=false` until explicitly enabled
- **HITL always exists** — even APPROVED actions require a human to merge the PR
- **Lifecycle tracking** — `ExecutionRecord` tracks: pending → pr_created → pr_merged → applied

**New files:** `src/core/execution_gateway.py`, `src/core/terraform_pr_generator.py`
**New endpoints:** `GET /api/execution/pending-reviews`, `GET /api/execution/by-action/{action_id}`,
`POST /api/execution/{id}/approve`, `POST /api/execution/{id}/dismiss`
**New env vars:** `GITHUB_TOKEN`, `IAC_GITHUB_REPO`, `IAC_TERRAFORM_PATH`, `EXECUTION_GATEWAY_ENABLED`
**Implementation guide:** `Adding-Terraform-Feature.md`

---

## Azure OpenAI Rate Limiting (HTTP 429)

In live mode all 7 agents (4 governance + 3 operational) call Azure OpenAI concurrently.
For 3 demo scenarios that is up to 21 LLM calls in a few seconds — which exhausts Azure
OpenAI's **Tokens Per Minute (TPM)** and **Requests Per Minute (RPM)** quotas immediately.

**Throttle wrapper — `src/infrastructure/llm_throttle.py`:** All 7 agents call `agent.run()`
via `run_with_throttle()`, which wraps the call in an `asyncio.Semaphore` (limits concurrent
calls) and adds exponential back-off retry on HTTP 429. Both governance agents and operational
agents use this wrapper.

Governance agents additionally have an `except Exception` fallback that catches any remaining
429 and continues with deterministic rule-based scoring. Operational agents return `[]` on
live-mode failure (no seed-data fallback — that would produce false positives on real Azure).

**Symptoms:** `PolicyComplianceAgent: framework call failed (429 Too Many Requests) —
falling back to rules.` in logs for governance agents; ops agents silently return no proposals.

**Fixes:**
1. Request TPM quota increase: Azure Portal → Azure OpenAI → your deployment → Quotas
2. The `run_with_throttle` retry already applies exponential back-off — increase retry count/delay in `llm_throttle.py` if needed
3. Reduce parallelism: run governance agents sequentially when under quota pressure

---

## Teams Notification Layer (Phase 17)

Every DENIED or ESCALATED verdict automatically triggers a Microsoft Teams Adaptive Card —
no one needs to watch the dashboard 24/7.

```
RuriSkryPipeline.evaluate()
    ↓ verdict
asyncio.create_task(send_teams_notification(verdict, action))   ← fire-and-forget
    ↓ runs concurrently, never blocks governance
httpx.AsyncClient.post(TEAMS_WEBHOOK_URL, json=adaptive_card)
```

**Key design decisions:**

- **Fire-and-forget via `asyncio.create_task()`** — the pipeline returns the verdict
  immediately; the notification runs in the background. A slow Teams endpoint never delays
  governance.
- **Never raises** — `send_teams_notification` wraps everything in `except Exception`.
  Notification failure is logged and swallowed; the governance decision is unaffected.
- **APPROVED verdicts skipped** — only actionable alerts sent; no noise.
- **Retry-once** — one retry after 2 s on network failure, then gives up cleanly.
- **Zero-config default** — `TEAMS_WEBHOOK_URL=""` silently disables notifications.
  No env var = no error, no Teams connection needed to run RuriSkry.

**Adaptive Card payload** — contains: verdict badge (🚫/⚠️), resource + agent + action
facts, SRI composite + 4-dimension breakdown, governance reason (≤300 chars), top policy
violation if any, "View in Dashboard" button (configurable URL), timestamp.

**Dashboard integration** — `GET /api/notification-status` drives the 🔔 pill in the header.
`POST /api/test-notification` sends a realistic sample DENIED card for judges to verify the
integration without running a full scan.

---

## Decision Explanation Engine (Phase 18)

Every governance verdict now has a full explainability layer. Clicking any row in the Live
Activity Feed opens a 6-section full-page drilldown.

```
GET /api/evaluations/{id}/explanation
    ↓
DecisionExplainer.explain(verdict, action)
    ├── _build_factors()           → ranked Factor list (by weighted_contribution)
    ├── _extract_policy_violations() → from agent_results["policy"]
    ├── _build_risk_highlights()   → natural-language risk callouts
    ├── _build_counterfactuals()   → 3 "what would change this?" scenarios per verdict type
    └── _try_llm_summary()         → GPT-4.1 plain-English summary (template fallback in mock)
    ↓
DecisionExplanation (cached by action_id)
```

**Counterfactual analysis** — hypothetical score recalculation per verdict type:
- DENIED → "if policy violation resolved → score drops to X → ESCALATED"
- ESCALATED → "if cost reduced → score drops to Y → APPROVED"
- APPROVED → "if tagged critical → score rises to Z → ESCALATED"

**Frontend — EvaluationDrilldown.jsx (6 sections):**
1. Verdict header — large badge, SRI composite score, resource/agent/timestamp
2. SRI™ Dimensional Breakdown — 4 horizontal bars, ⭐ marks the primary factor
3. Decision Explanation — GPT-4.1 summary, primary factor callout, risk highlights, policy violations
4. Counterfactual Analysis — score-transition cards ("X.X → Y.Y → VERDICT pill")
5. Agent Reasoning — proposing agent's reason + per-governance-agent assessments
6. Audit Trail — UUID, timestamp, collapsible raw JSON

**SRI data format note** — the stored tracker record uses a flat format (`sri_composite` +
`sri_breakdown.{infrastructure,policy,historical,cost}`). The frontend maps this to the
expected `{sri_composite, sri_infrastructure, sri_policy, ...}` shape via a fallback
in `EvaluationDrilldown.jsx` line 71.

---

## File Map

```
src/
├── core/
│   ├── models.py              # All Pydantic models — shared contract
│   ├── pipeline.py            # asyncio.gather() orchestration
│   ├── governance_engine.py   # SRI composite + verdict logic
│   ├── decision_tracker.py    # Audit trail → Cosmos DB / JSON (verdicts)
│   ├── scan_run_tracker.py    # Scan-run lifecycle → Cosmos DB / JSON (scan records)
│   ├── explanation_engine.py  # DecisionExplainer — factors, counterfactuals, LLM summary
│   ├── execution_gateway.py   # (Phase 21 — planned) Verdict→IaC routing; HITL approval
│   ├── terraform_pr_generator.py # (Phase 21 — planned) GitHub PR creation via PyGithub
│   └── interception.py        # ActionInterceptor façade (async)
├── governance_agents/         # 4 governors — all async def evaluate()
├── operational_agents/        # 3 governed agents — all async def scan()
├── a2a/                       # A2A Protocol layer (Phase 10)
│   ├── ruriskry_a2a_server.py # A2A server — AgentCard + RuriSkryAgentExecutor
│   ├── operational_a2a_clients.py # A2A client wrappers for 3 operational agents
│   └── agent_registry.py     # Tracks connected agents + stats
├── mcp_server/server.py       # FastMCP stdio — skry_evaluate_action (async)
├── notifications/             # Outbound alerting (Phase 17)
│   └── teams_notifier.py      # Adaptive Card → Teams webhook on DENIED/ESCALATED; fire-and-forget
├── api/dashboard_api.py       # FastAPI REST — 18 async endpoints (evaluations, agents,
│                              #   scan triggers, SSE stream, cancel, last-run,
│                              #   notification-status, test-notification, explanation)
├── infrastructure/            # Azure clients with mock fallback
│   ├── azure_tools.py         # 5 sync tools + 5 async variants (*_async): Resource Graph, metrics, NSG, activity log; mock fallbacks
│   ├── resource_graph.py      # Live: _azure_enrich_topology() — tags + KQL topology + cost_lookup
│   ├── cost_lookup.py         # Azure Retail Prices API — SKU→monthly cost; no auth; module-level cache
│   ├── llm_throttle.py        # asyncio.Semaphore + exponential backoff for Azure OpenAI rate limits
│   ├── cosmos_client.py       # Cosmos DB decisions client (live: CosmosClient; mock: JSON files)
│   ├── search_client.py       # Azure AI Search client (live: BM25 full-text; mock: keyword matching)
│   ├── openai_client.py       # Azure OpenAI / GPT-4.1 client (live: Responses API; mock: canned string)
│   └── secrets.py             # Key Vault secret resolver (env → KV → empty string)
└── config.py                  # SRI thresholds + env vars + DEMO_MODE + Teams settings
dashboard/
└── src/components/
    ├── AgentControls.jsx      # Scan trigger panel: per-agent buttons, RG filter, 2 s polling, LiveLogPanel
    ├── LiveLogPanel.jsx        # SSE slide-out log panel: 9 event type styles, auto-scroll
    ├── ConnectedAgents.jsx    # Agent card grid: ⋮ action menu, scan/log/results/history/details panels
    ├── EvaluationDrilldown.jsx # Full-page drilldown: SRI bars, explanation, counterfactuals, reasoning, JSON audit
    └── LiveActivityFeed.jsx   # Real-time verdict feed; rows clickable → opens EvaluationDrilldown
data/
├── agents/                    # A2A agent registry (mock mode)
├── decisions/                 # Governance verdict audit trail (mock mode)
├── scans/                     # Scan-run records (mock mode — ScanRunTracker)
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

1. **Mini prod resources** (ruriskry-prod-rg) — `vm-dr-01`, `vm-web-01`, `payment-api-prod`,
   `nsg-east-prod`, `ruriskryprodprod`. These match `infrastructure/terraform-prod/` exactly.
   After `terraform apply`, replace `YOUR-SUBSCRIPTION-ID` with your real subscription ID.
   Each has a specific governance scenario (DENIED / APPROVED / ESCALATED).

2. **Legacy mock resources** — `vm-23`, `api-server-03`, `web-tier-01`, `nsg-east`, `aks-prod`,
   `storageshared01`. These are referenced by all unit tests and must not be removed.
