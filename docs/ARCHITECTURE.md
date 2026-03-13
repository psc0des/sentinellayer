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
    ├─ Risk Triage (Phase 26) — compute_fingerprint() + classify_tier()  ← <1 ms, 0 LLM calls
    │   Tier 1: non-production + isolated blast radius → deterministic only (0 LLM) ← ACTIVE (Phase 27A)
    │   Tier 2: production + service blast + no network → single LLM call (Phase 27B, future)
    │   Tier 3: compliance scope / network / destructive+critical → full pipeline
    │   force_deterministic = (triage_tier == 1) passed to all 4 agents
    │
    ├─ asyncio.gather() ──────────────────────────────────┐
    │   ├── BlastRadiusAgent.evaluate(force_deterministic)   → SRI:Infrastructure (weight 0.30)
    │   ├── PolicyComplianceAgent.evaluate(force_deterministic) → SRI:Policy (weight 0.25)
    │   ├── HistoricalPatternAgent.evaluate(force_deterministic) → SRI:Historical (weight 0.25)
    │   └── FinancialImpactAgent.evaluate(force_deterministic)   → SRI:Cost (weight 0.20)
    │   Each agent: if not use_framework OR force_deterministic → skip LLM, use rules
    │                                                      │
    │   All 4 run concurrently (async-first)  ◄────────────┘
    │
    ▼
GovernanceDecisionEngine.evaluate()
    │  SRI Composite = weighted sum of 4 dimensions
    │  Decision rules (priority order):
    │  1. DENIED    if CRITICAL policy violation (not llm_override)
    │  2. DENIED    if composite > 60
    │  3. ESCALATED if composite > 25
    │  4. ESCALATED if any HIGH violation (not llm_override) — Rule 3.5 verdict floor
    │  5. APPROVED  otherwise
    │
    │  verdict.triage_tier = 1 | 2 | 3  ← stamped after engine returns
    │  verdict.triage_mode = "deterministic" | "full"  ← Phase 27A
    │
    ▼
DecisionTracker.record(verdict)     ← writes to Cosmos DB (live) / JSON (mock)
    │                                   triage_tier + triage_mode stored in every record
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

4. **LLM-as-Decision-Maker (Phase 22)** — in live mode, each governance agent uses gpt-5-mini as
   an **active decision maker**, not a narrator. The flow: (1) deterministic rules run and produce
   a baseline score; (2) the LLM receives the baseline + full policy definitions + ops agent's
   reasoning; (3) the LLM calls `submit_governance_decision` with an adjusted score and per-adjustment
   justification; (4) a guardrail (`_llm_governance.py`) clamps the adjustment to +/-30 points from
   baseline so hallucination cannot dominate. This enables **remediation intent detection**: when an
   ops agent describes a security issue it is fixing, the LLM can reduce the policy score rather than
   blocking the remediation. Mock mode bypasses the framework entirely — deterministic baseline only
   (all tests pass unchanged).

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

9. **Risk Triage (Phase 26)** — before any governance agent runs, `compute_fingerprint()`
   derives an `ActionFingerprint` from the action and resource metadata in <1 ms (no I/O,
   no LLM). `classify_tier()` then routes the action to Tier 1 (0 LLM calls), Tier 2 (1
   consolidated call — Phase 27), or Tier 3 (full 4-agent pipeline). Four deterministic
   rules drive routing; ambiguous cases default to Tier 3 (conservative). The `OrgContext`
   (compliance frameworks, risk tolerance, business-critical RGs) loaded from env vars at
   startup informs compliance-scope detection — a production resource in a regulated org
   with `ORG_COMPLIANCE_FRAMEWORKS` set is always Tier 3 regardless of other signals.
   **Phase 27A (active)**: Tier 1 short-circuit is live. `force_deterministic = (triage_tier == 1)`
   is computed in the pipeline and passed to all four agents. Each agent's `evaluate()` signature
   accepts `force_deterministic: bool = False`; when True, the `if not use_framework or force_deterministic`
   branch skips the LLM entirely. The verdict's `triage_mode` field (`"deterministic"` | `"full"`)
   is stored in every record. `/api/metrics` reports `deterministic_evaluations` and `full_evaluations`.

---

## Agent Roles

### Governance Agents (the governors — evaluate proposed actions)

| Agent | SRI Dimension | Data Source |
|---|---|---|
| `BlastRadiusAgent` | Infrastructure (0.30) | **Live:** `ResourceGraphClient` (KQL topology) + gpt-5-mini decision maker · **Mock:** `seed_resources.json` |
| `PolicyComplianceAgent` | Policy (0.25) | `policies.json` — 11 production policies · gpt-5-mini decision maker with remediation intent detection · structured `nsg_change_direction` field distinguishes opening from restricting ports |
| `HistoricalPatternAgent` | Historical (0.25) | Azure AI Search / `seed_incidents.json` · gpt-5-mini decision maker |
| `FinancialImpactAgent` | Cost (0.20) | **Live:** `ResourceGraphClient` + Azure Retail Prices API · gpt-5-mini decision maker · **Mock:** `seed_resources.json` |

All 4 agents follow the same Phase 22 pattern: deterministic baseline → LLM contextual adjustment
(+/-30 pts guardrail) → adjusted score used in SRI™ composite.
`src/governance_agents/_llm_governance.py` provides shared `clamp_score()`, `parse_llm_decision()`,
and `format_adjustment_text()` utilities used by all 4 agents.

### Operational Agents (the governed — propose actions)

| Agent | What it proposes | Scan coverage |
|---|---|---|
| `CostOptimizationAgent` | VM downsizing, idle resource deletion, unattached disk deletion, orphaned public IP release | gpt-5-mini — 8 tools (azure_tools). Discovers VMs, AKS, databases, Redis, storage accounts. Flags: deallocated VMs (disk cost with no value, MEDIUM), unattached disks (`diskState=Unattached`, MEDIUM), orphaned public IPs (LOW), over-provisioned SKUs (LOW). Calls `get_resource_health` before proposing deletion — won't delete platform-degraded resources. Calls `list_advisor_recommendations(category=Cost)` for pre-computed Microsoft intelligence. |
| `MonitoringAgent` | SRE anomaly remediation — VM restarts, scale-ups, AMA extension installs, tag additions | gpt-5-mini — 8 tools (azure_tools). **6-step enterprise proactive scan**: (1) resource discovery via Resource Graph, (2) VM power state check via `get_resource_details` (Compute instance view → `powerState` field) — stopped/deallocated = HIGH `restart_service`, (3) database health, (4) Container Apps & App Services, (5) observability gaps, (6) orphaned resources. Calls `get_resource_health` for platform availability confirmation. Calls `list_advisor_recommendations(category=HighAvailability)`. **Alert-driven mode** handles 5 alert types: A) availability/heartbeat, B) CPU/memory, C) disk/storage, D) database, E) network. |
| `DeployAgent` | NSG rule hardening, storage security fixes, DB/KV security configs, VM security posture fixes, lifecycle tag additions | gpt-5-mini — 8 tools (azure_tools). **7-domain security audit**: (1) resource discovery, (2) NSG audit, (3) storage security, (4) database & Key Vault, (5) VM security posture, (6) activity log audit, (7) zero-tag governance. Calls `list_advisor_recommendations(category=Security)` for Microsoft Security Center findings. |

**Phase 12 + Phase 15 + Agent Intelligence Overhaul (2026-03-13, complete):** All three agents query real Azure data sources via 8 tools in `azure_tools.py` and use gpt-5-mini via `agent-framework-core` to reason about context before proposing. Tools include: static config (Resource Graph), runtime metrics (Monitor), VM power state (Compute instance view), network rules (NSG), activity log, Azure Resource Health API, and Azure Advisor API. `scan()` is framework-only — `_scan_rules()` exists for direct test access only. Environment-agnostic: no hardcoded resource names, tag keys, or org-specific assumptions.

---

## Azure Services (live mode)

### Governance Infrastructure (`infrastructure/terraform-core/`)

Two Terraform providers are used: `hashicorp/azurerm` (~> 4.0) for standard resources and `azure/azapi` (~> 2.0) for Foundry project management (`azapi_update_resource` to set `allowProjectManagement=true`, `azapi_resource` to create the Foundry project). Set `create_foundry_project = true` in `terraform.tfvars` to provision the project automatically.

| Service | Used by | Config var | Security posture |
|---|---|---|---|
| Azure OpenAI / gpt-5-mini (Foundry) | All 7 agents (Agent Framework) | `AZURE_OPENAI_ENDPOINT` | `local_authentication_enabled=false` — Managed Identity only; Container App MI has `Cognitive Services OpenAI User` role (`azurerm_role_assignment.foundry_openai_user`) |
| Azure AI Search | `HistoricalPatternAgent` | `AZURE_SEARCH_ENDPOINT` | — |
| Azure Cosmos DB — `governance-decisions` | `DecisionTracker` | `COSMOS_ENDPOINT` | Managed Identity auth; `network_acl_bypass_for_azure_services=true` |
| Azure Cosmos DB — `governance-agents` | `AgentRegistry` | `COSMOS_ENDPOINT` | Managed Identity auth |
| Azure Cosmos DB — `governance-alerts` | `AlertTracker` | `COSMOS_ENDPOINT` | Managed Identity auth; partition key `/severity` |
| Azure Cosmos DB — `governance-scan-runs` | `ScanRunTracker` | `COSMOS_CONTAINER_SCAN_RUNS` | Managed Identity auth |
| Azure Key Vault | All secrets at runtime | `AZURE_KEYVAULT_URL` | `purge_protection_enabled=false`; `soft_delete_retention_days=7` (set purge protection true in regulated production) |
| Azure Container Registry | Backend image pull | — | `admin_enabled=false`; User-Assigned MI has `AcrPull` role — no credentials in tfstate. Container App starts with MCR placeholder image; `deploy.sh` swaps to ACR image after role propagates. |

Additional security controls managed by Terraform:
- **Management lock** — `azurerm_management_lock` (CanNotDelete) on the resource group. The lock `depends_on` all major resources so `terraform destroy` removes it automatically before deleting anything else — no manual step required
- **Subscription-level Reader** — `azurerm_role_assignment.subscription_reader` grants the Container App's Managed Identity `Reader` at subscription scope for cross-RG Resource Graph scanning
- **Azure Monitor Action Group** — `azurerm_monitor_action_group.ruriskry` (`terraform-core`) points at `https://<backend-fqdn>/api/alert-trigger`. Attach alert rules to this group (portal or `az monitor metrics alert update --add-action`) so Azure Monitor alerts POST to the backend and trigger automatic governance evaluation. See [`docs/alert-wiring.md`](alert-wiring.md) for a step-by-step guide on wiring new resources.
- **CORS** — enforced at the application layer in `src/api/dashboard_api.py` via `CORSMiddleware` with exact origin matching against `DASHBOARD_URL`. The Container App references `azurerm_static_web_app.dashboard.default_host_name` directly in `main.tf` — Terraform creates the SWA first (implicit dependency), reads the URL in-memory, and passes the exact value into `DASHBOARD_URL` in the same apply — no tfvars patching, no re-apply, no stale CORS window, no wildcard patterns needed
- **Slack webhook** — stored as a Key Vault secret and injected via the Container App secret mechanism; not exposed as a plain env var

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
| `vm-dr-01` | Linux VM (`var.vm_size`, default B2ls_v2) | DENIED — `disaster-recovery=true` policy. `SystemAssigned` MI + `Monitoring Metrics Publisher` role for AMA telemetry. |
| `vm-web-01` | Linux VM (`var.vm_size`, default B2ls_v2) | APPROVED — safe CPU-triggered scale-up (cloud-init runs stress-ng cron). `SystemAssigned` MI + `Monitoring Metrics Publisher` role for AMA telemetry. |
| `payment-api-prod` | App Service F1 (free) | Critical dependency (raises blast radius) |
| `nsg-east-prod` | Network Security Group | ESCALATED — port 8080 open affects all governed VMs |
| `ruriskryprod{suffix}` | Storage Account LRS | Shared dependency; deletion = high blast radius |

---

## Deployment Architecture

### What Runs Where

RuriSkry is **not** a microservices mesh. All intelligence runs **in-process** inside a single FastAPI application. There are no separate agent services to deploy or orchestrate.

```
┌──────────────────────────────────────────────────────────────────┐
│  Azure Container Apps  (single container)                        │
│                                                                  │
│  FastAPI (src/api/dashboard_api.py)                              │
│    │                                                             │
│    ├── CostOptimizationAgent    ┐                                │
│    ├── MonitoringAgent          ├── Operational agents           │
│    ├── DeployAgent              ┘  (in-process, async)           │
│    │                                                             │
│    ├── BlastRadiusAgent         ┐                                │
│    ├── PolicyComplianceAgent    ├── Governance agents            │
│    ├── HistoricalPatternAgent   ┘  (in-process, async)           │
│    └── FinancialImpactAgent                                      │
│                                                                  │
└──────────────────────────────────────────────────────────────────┘
         │  HTTPS calls                    │  HTTPS calls
         ▼                                 ▼
Azure OpenAI Foundry              Azure AI Search
Azure Cosmos DB                   Azure Resource Graph
Azure Key Vault
```

### What Is Already Deployed

| Service | Deployed via | Purpose |
|---------|-------------|---------|
| Azure OpenAI / gpt-5-mini (Foundry) | `infrastructure/terraform-core/` | LLM backbone for all 7 agents — project fully Terraform-managed via AzAPI provider |
| Azure AI Search | `infrastructure/terraform-core/` | Historical incident BM25 search |
| Azure Cosmos DB | `infrastructure/terraform-core/` | Audit trail + agent registry + scan runs — Managed Identity auth |
| Azure Key Vault | `infrastructure/terraform-core/` | Runtime secrets — purge protection enabled, 90-day soft-delete |
| Azure Container Registry | `infrastructure/terraform-core/` | Backend Docker image — admin disabled, pulled via Managed Identity |
| Azure Container Apps | `infrastructure/terraform-core/` | FastAPI backend (all agents in-process) |
| Azure Static Web Apps | `infrastructure/terraform-core/` | React dashboard (`dashboard/`) |
| Demo prod resources | `infrastructure/terraform-prod/` | Governed targets (VMs, NSG, storage) |

> All Azure resources are provisioned by Terraform. `scripts/deploy.sh` handles the full first-time deploy in one command: staged Terraform apply → Docker build + push (local Docker) → dashboard build + SWA deploy. For subsequent code changes see the Redeploy Workflows section in `infrastructure/terraform-core/deploy.md`.

### Request Flow (deployed)

```
Browser
  │  HTTPS
  ▼
Azure Static Web Apps          ← React dashboard (dashboard/)
  │  HTTPS API calls
  ▼
Azure Container Apps           ← FastAPI + all agents (src/)
  │  HTTPS calls via SDK
  ├──► Azure OpenAI Foundry    ← gpt-5-mini LLM calls (7 agents)
  ├──► Azure AI Search         ← historical incident lookup
  ├──► Azure Cosmos DB         ← audit trail reads/writes
  ├──► Azure Resource Graph    ← live topology queries
  └──► Azure Key Vault         ← secret resolution at startup
```

### In-Process Agent Model

This architecture is **intentional**:

- **No service discovery overhead** — agents call each other as Python function calls, not HTTP requests
- **`asyncio.gather()` works natively** — all 4 governance agents run in true parallel inside one event loop; no message broker needed
- **Single deployment unit** — one Container App image, one `az containerapp update`, done
- **Scales vertically** — increase the Container App's CPU/memory to handle more concurrent scans; add replicas for availability

The operational agents (`CostOptimizationAgent`, `MonitoringAgent`, `DeployAgent`) are instantiated lazily per request and hold no per-request state — all state lives in Cosmos DB or local JSON. The FastAPI `lifespan` hook runs on startup to mark any orphaned `"running"` scans as `"error"` (handles the case where the process was killed mid-scan during a redeployment).

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

**Intelligent monitoring-agent — actual end-to-end flow (live):**
```
Azure Monitor alert fires (e.g. vm-dr-01 heartbeat loss, vm-web-01 heartbeat loss, vm-web-01 CPU > 80%)
    ↓ ag-ruriskry-prod Action Group (terraform-prod, use_common_alert_schema=false)
POST /api/alert-trigger  ← Azure POSTs Common Alert Schema payload
    ↓
_normalize_azure_alert_payload():
  • Detects Log Alerts V2: alertTargetID = Log Analytics workspace (not the VM)
  • Workspace pivot: regex-extracts VM name from essentials.description / alertRule
  • Constructs correct VM ARM ID → resource_id = /subscriptions/.../vm-dr-01
    ↓
MonitoringAgent.scan(alert_payload) — always investigates the right VM
    ↓
gpt-5-mini: "VM deallocated, heartbeat absent. Restart required."
ProposedAction: restart_service on vm-dr-01
    ↓
Governance pipeline: SRI 1.5 → APPROVED ✅
    ↓
ExecutionGateway.process_verdict() → ExecutionRecord (status=manual_required)
    ↓ execution_id stored in verdict entry alongside SRI + decision
Alert record: status=resolved, 1 finding, shown in Alerts tab
    ↓ AlertPanel drilldown: AlertFindingActions renders action buttons
📝 Terraform PR  |  🤖 Fix by Agent  |  🌐 Azure Portal  |  ✕ Ignore
```

> **Note on Log Alerts V2:** `azurerm_monitor_scheduled_query_rules_alert_v2` always sends the Log Analytics workspace ARM ID as `alertTargetID` (not the monitored VM), and `configurationItems` is always empty when the query returns 0 rows (the "no heartbeat" case). The workspace pivot in `_normalize_azure_alert_payload()` is essential for correct behaviour — without it the agent investigates the workspace and finds nothing actionable ~50% of the time.

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

## Execution Gateway & IaC-Safe Execution (Phase 21)

RuriSkry evaluates. Terraform executes. Humans approve. The Execution Gateway sits
between the governance verdict and any real-world change, ensuring IaC state never drifts.

```
GovernanceVerdict
       │
       ▼
ExecutionGateway.route_verdict()
  ├── DENIED    → status=blocked (log + Slack alert, no action)
  ├── ESCALATED → status=awaiting_review
  └── APPROVED  → status=manual_required
        │         (IaC metadata stored on record for on-demand PR creation)
        └── ExecutionRecord stored (JSON-durable: data/executions/)
```

All `manual_required` and `awaiting_review` records surface a **4-button HITL panel** in
the dashboard drilldown. The human chooses how to act — nothing executes automatically.

> This same HITL panel applies to **both scan verdicts** (Decisions tab → EvaluationDrilldown)
> and **alert findings** (Alerts tab → AlertPanel → AlertFindingActions). The `execution_id`
> is stored in every verdict entry so the dashboard always has a direct link to the
> ExecutionRecord and can render the correct action buttons.

**Execution model:**

| Verdict | Automatic Step | Dashboard Status | HITL Panel |
|---------|----------------|-----------------|------------|
| DENIED | Block. Log + Slack alert. | Blocked (red) | None |
| ESCALATED | Create review request. | Awaiting Review (yellow) | 4-button panel (action auto-approves) |
| APPROVED | Store record. | Manual Required (grey) | 4-button panel |

**4-button HITL panel options** (same panel for both APPROVED and ESCALATED):
1. **Create Terraform PR** — generates branch + HCL patch + GitHub PR on demand
2. **Open in Azure Portal** — direct link to the affected resource
3. **Fix using Agent** — LLM-driven two-phase execution: Plan (LLM reads resource state → structured steps table) → human reviews → Execute (LLM calls Azure SDK write tools step-by-step, fail-stop on error)
4. **Decline / Ignore** — marks record as `dismissed`; stops re-proposing

**IaC tag metadata** is stored on the `ExecutionRecord` at routing time (from `managed_by`,
`iac_repo`, `iac_path` tags) so "Create Terraform PR" works even if the resource's tags change later.

**Sub-resource tag lookup** — `_get_resource_tags()` strips sub-resource path segments
(`/securityRules/`, `/subnets/`, etc.) from ARM IDs before querying Azure, because individual
security rules and subnets carry no tags of their own — tags live on the parent resource.

Tag lookup in `dashboard_api._get_resource_tags()` is **environment-aware**:
- **Live mode** (`USE_LOCAL_MOCKS=false` + `AZURE_SUBSCRIPTION_ID` set): queries
  `ResourceGraphClient.get_resource_async(resource_id)` — reads real Azure tags immediately.
- **Mock / fallback**: reads `data/seed_resources.json`; also used if the live query fails.

**Key design decisions:**
- **Gateway never executes directly** — it only creates PRs or marks for manual review
- **No auto-PR** — PR creation is user-initiated (clicking "Create Terraform PR"); prevents surprise drift
- **Gateway failure never breaks the verdict** — wrapped in `try/except`; verdict is primary
- **Opt-in by default** — `EXECUTION_GATEWAY_ENABLED=false` until explicitly enabled
- **HITL always exists** — every action requires a human choice in the dashboard
- **ESCALATED auto-approved by action** — choosing any panel button on an `awaiting_review` record transitions it to `manual_required` then executes; no separate approval step
- **Dedup on route** — `route_verdict()` checks for an existing `manual_required` record for the same `(resource_id, action_type)` before creating a new one; prevents duplicate entries on re-scan
- **Lifecycle tracking** — `ExecutionRecord` tracks: pending → manual_required / pr_created / awaiting_review → applied / dismissed / rolled_back
- **Durable state** — `ExecutionRecord` persisted as JSON in `data/executions/`; survives restarts
- **Flag until fixed** — `manual_required` records are re-proposed on every subsequent scan via `get_unresolved_proposals()`; stops when human clicks **Decline / Ignore** or the agent stops flagging it. `pr_created`, `awaiting_review`, `blocked`, `dismissed`, `applied`, and `rolled_back` records are excluded.
- **Dedup `action_id` update** — when a resource is re-scanned and an existing `manual_required` record is found for the same `(resource_id, action_type)`, the record's `action_id` is updated to the latest verdict's `action_id` before returning. This ensures the drilldown's `by-action` lookup always resolves to the live execution record.
- **Rollback** — after a fix is applied (`status=applied`), `ExecutionGateway.rollback_agent_fix()` calls `ExecutionAgent.rollback()`. On success: sets `status → rolled_back`, stores `rollback_log`. On failure: keeps `status = applied` (fix is still in place), stores failed `rollback_log`, appends failure note. Mock path maps each `ActionType` to its deterministic inverse (RESTART→deallocate, SCALE_UP/DOWN→resize back, NSG→restore rule, DELETE→cannot auto-rollback). Live path is LLM-driven using `rollback_hint` from the stored plan. The dashboard shows an amber ↩ Rollback button next to the Applied badge; on failure a rose-colored "Rollback attempted but failed" banner shows and the button remains for retry.
- **NSG rule auto-dismiss** — when a scan finds resource `nsg-east-prod` clean, the system dismisses all `manual_required` records whose ARM ID contains `/securityRules/` with that NSG as parent. The parent name is extracted from the ARM ID segment before `/securityRules/`.
- **Deterministic historical boost** — `HistoricalPatternAgent._governance_history_boost()` reads `DecisionTracker.get_recent(50)` and adds +25 per prior ESCALATED / +5 per prior APPROVED for the same `action_type` (cap +60). Ensures consistent ESCALATED routing when Azure AI Search BM25 returns sparse results.

**Files:** `src/core/execution_agent.py` (Phase 28/29/30 — LLM plan + execute + verify + rollback), `src/core/execution_gateway.py`, `src/core/terraform_pr_generator.py`
**Endpoints:** `GET /api/execution/pending-reviews`, `GET /api/execution/by-action/{action_id}`,
`POST /api/execution/{id}/approve`, `POST /api/execution/{id}/dismiss`,
`POST /api/execution/{id}/create-pr`, `GET /api/execution/{id}/agent-fix-preview`,
`POST /api/execution/{id}/agent-fix-execute`, `POST /api/execution/{id}/rollback`, `GET /api/config`
**Env vars:** `GITHUB_TOKEN`, `IAC_GITHUB_REPO`, `IAC_TERRAFORM_PATH`, `EXECUTION_GATEWAY_ENABLED`
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

## Slack Notification Layer (Phase 17 + hardened 2026-03-14)

Every DENIED or ESCALATED verdict automatically triggers a Slack message via Incoming Webhook —
no one needs to watch the dashboard 24/7. Three notification types are implemented:

| Function | Trigger | Wired in |
|---|---|---|
| `send_verdict_notification` | DENIED / ESCALATED governance verdict | `pipeline.py` |
| `send_alert_notification` | Azure Monitor alert received (investigation started) | `dashboard_api.py` alert-trigger endpoint |
| `send_alert_resolved_notification` | Alert investigation complete | `dashboard_api.py` background task |

```
RuriSkryPipeline.evaluate()
    ↓ verdict
asyncio.create_task(send_verdict_notification(verdict, action))   ← fire-and-forget
    ↓ runs concurrently, never blocks governance
_get_client()                                                      ← shared singleton (TLS reuse)
    ↓
_acquire_rate_slot()                                               ← rate limiter (≥1.1s between sends)
    ↓
httpx.AsyncClient.post(SLACK_WEBHOOK_URL, json=block_kit_payload)
```

**Key design decisions:**

- **Fire-and-forget via `asyncio.create_task()`** — the pipeline returns the verdict
  immediately; the notification runs in the background. A slow Slack endpoint never delays
  governance.
- **Shared `httpx.AsyncClient` singleton** — one client per process, initialized lazily with
  double-checked locking. Reuses the TLS connection across all notifications instead of
  opening a new TCP+TLS handshake per call.
- **Rate limiter** — `asyncio.Lock` + `time.monotonic()` enforces ≥1.1 s between sends,
  staying under Slack's ~1 msg/sec limit during burst alert loads.
- **Smart retry** — errors are categorized: 4xx (client error) = no retry; 429 = obeys
  `Retry-After` header (capped 30 s); 5xx / network timeout = exponential backoff
  (2 s → 4 s), 3 max attempts. Never retries when retrying would be pointless.
- **Never raises** — all public functions return `bool`. Notification failure is logged
  with structured `extra={}` fields and swallowed; the governance decision is unaffected.
- **APPROVED verdicts skipped** — only actionable alerts sent; no noise.
- **Zero-config default** — `SLACK_WEBHOOK_URL=""` silently disables notifications.
  No env var = no error, no Slack connection needed to run RuriSkry.

**Block Kit payload** — contains: verdict badge (🚫/⚠️), resource + agent + action
facts, SRI composite + 4-dimension breakdown, governance reason (≤300 chars), top policy
violation if any, "View in Dashboard" button (configurable URL), timestamp.

**Dashboard integration** — `GET /api/notification-status` drives the 🔔 pill in the header.
`POST /api/test-notification` sends a realistic sample DENIED message to verify the
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
    └── _try_llm_summary()         → gpt-5-mini plain-English summary (template fallback in mock)
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
3. Decision Explanation — gpt-5-mini summary, primary factor callout, risk highlights, policy violations
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
│   ├── alert_tracker.py       # Alert investigation lifecycle → Cosmos DB / JSON (alert records)
│   ├── explanation_engine.py  # DecisionExplainer — factors, counterfactuals, LLM summary
│   ├── execution_agent.py     # LLM-driven execution — plan() + execute() + verify() + rollback() four-phase agent
│   ├── execution_gateway.py   # Verdict→IaC routing; HITL approval; delegates to ExecutionAgent; list_all()
│   ├── terraform_pr_generator.py # GitHub PR creation via PyGithub (asyncio.to_thread)
│   └── interception.py        # ActionInterceptor façade (async)
├── governance_agents/         # 4 governors — all async def evaluate()
├── operational_agents/        # 3 governed agents — all async def scan()
├── a2a/                       # A2A Protocol layer (Phase 10)
│   ├── ruriskry_a2a_server.py # A2A server — AgentCard + RuriSkryAgentExecutor
│   ├── operational_a2a_clients.py # A2A client wrappers for 3 operational agents
│   └── agent_registry.py     # Tracks connected agents + stats
├── mcp_server/server.py       # FastMCP stdio — skry_evaluate_action (async)
├── notifications/             # Outbound alerting (Phase 17)
│   └── slack_notifier.py      # Block Kit attachments → Slack webhook on DENIED/ESCALATED; fire-and-forget
├── api/dashboard_api.py       # FastAPI REST — 33 async endpoints (evaluations, agents,
│                              #   scan triggers, alerts lifecycle, SSE streams, cancel,
│                              #   last-run, notification-status, test-notification, explanation, HITL)
├── infrastructure/            # Azure clients with mock fallback
│   ├── azure_tools.py         # 5 sync + 7 async tools: Resource Graph, metrics, NSG, activity log,
│   │                          #   get_resource_details (+ VM powerState via Compute instance view),
│   │                          #   get_resource_health_async (Resource Health API — Available/Unavailable),
│   │                          #   list_advisor_recommendations_async (Azure Advisor); mock fallbacks
│   ├── resource_graph.py      # Live: _azure_enrich_topology() — tags + KQL topology + cost_lookup
│   ├── cost_lookup.py         # Azure Retail Prices API — SKU→monthly cost; no auth; module-level cache
│   ├── llm_throttle.py        # asyncio.Semaphore + exponential backoff for Azure OpenAI rate limits
│   ├── cosmos_client.py       # Cosmos DB decisions client (live: CosmosClient; mock: JSON files)
│   ├── search_client.py       # Azure AI Search client (live: BM25 full-text; mock: keyword matching)
│   ├── openai_client.py       # Azure OpenAI / gpt-5-mini client (live: Responses API; mock: canned string)
│   └── secrets.py             # Key Vault secret resolver (env → KV → empty string)
└── config.py                  # SRI thresholds + env vars + DEMO_MODE + Slack settings
dashboard/
└── src/
    ├── pages/
    │   ├── Overview.jsx          # Landing: NumberTicker metrics, gradient SRI AreaChart, AlertsCard (rose glow if firing), ExecutionMetricsCard, pending reviews, scan history
    │   ├── Scans.jsx             # Scan trigger panel + scan history table
    │   ├── Agents.jsx            # ConnectedAgents wrapper page
    │   ├── Decisions.jsx         # DecisionTable + EvaluationDrilldown (breadcrumb nav)
    │   ├── Alerts.jsx             # Azure Monitor alert investigations — table + severity/status filters + drilldown panel;
    │   │                          #   AlertFindingActions: per-finding action buttons (📝 Terraform PR, 🤖 Fix by Agent,
    │   │                          #   🌐 Azure Portal, ✕ Ignore) — same HITL flow as scan verdicts, driven by execution_id
    │   ├── AuditLog.jsx          # Scan-level operational audit log with filters + CSV/JSON export
    │   └── Admin.jsx             # Admin panel — System Configuration card (mode/timeout/concurrency/flags) + Danger Zone (Reset)
    ├── components/
    │   ├── magicui/
    │   │   ├── NumberTicker.jsx  # Count-up animation (RAF + easeOutQuart); used on metric cards
    │   │   ├── GlowCard.jsx      # Card wrapper: color-coded glow + backdrop-blur glass + border beam + urgent pulse
    │   │   ├── VerdictBadge.jsx  # Dot + pill verdict labels (emerald/amber/rose) with glow; used sitewide
    │   │   └── TableSkeleton.jsx # Shimmer placeholder rows for tables while data loads
    │   ├── Sidebar.jsx           # Left nav: teal breathe logo, animated active indicator, amber urgency pulse on Decisions, red alert count badge, Settings gear + Admin link at bottom
    │   ├── DecisionTable.jsx     # Sortable/filterable/paginated verdict table + CSV/JSON export
    │   ├── ConnectedAgents.jsx   # Agent card grid (GlowCard): ⋮ menu, scan/log/results/history/details panels
    │   ├── EvaluationDrilldown.jsx # Full drilldown: SRI bars, explanation, counterfactuals, HITL action panel, ExecutionLogView (per-step log + verification badge)
    │   ├── AgentControls.jsx     # Scan trigger panel: per-agent buttons, RG filter, 2 s polling
    │   ├── LiveLogPanel.jsx      # SSE slide-out log: 9 event type styles, auto-scroll
    │   └── LiveActivityFeed.jsx  # Real-time verdict feed; rows open EvaluationDrilldown
    ├── index.css                 # Design token system (CSS :root vars) + all keyframes (breathe/urgentPulse/scanBeam/fadeInUp) + utility classes (.animate-breathe, .animate-urgent-pulse, .bg-dots, .metric-value, .shimmer)
    └── App.jsx                   # Router shell: bg-dots dot-grid on content area, --font-ui/--bg-base tokens applied; routes include /admin
data/
├── agents/                    # A2A agent registry (mock mode)
├── decisions/                 # Governance verdict audit trail (mock mode)
├── scans/                     # Scan-run records (mock mode — ScanRunTracker)
├── policies.json              # 6 governance policies
├── seed_incidents.json        # 7 historical incidents
└── seed_resources.json        # Azure resource topology (see note below)
infrastructure/
├── terraform-core/            # Main infra — Foundry, Search, Cosmos, Key Vault, ACR, Container Apps, Static Web App
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
