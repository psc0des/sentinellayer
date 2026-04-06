# CONTEXT.md — RuriSkry Project Context
> This file is the single source of truth for any AI coding agent working on this project.

## What Is This Project?
RuriSkry is a production-grade AI Action Governance & Simulation Engine. It intercepts AI agent infrastructure actions, simulates their impact, and scores them using the Skry Risk Index (SRI™) before allowing execution.

Originally built for the Microsoft AI Dev Days Hackathon 2026, RuriSkry has evolved into a fully async, enterprise-ready governance engine with live Azure topology analysis, durable Cosmos DB audit trails, Slack alerting, explainable AI verdicts with counterfactual analysis, and 792+ automated tests.

## Project Structure
```
src/
├── operational_agents/     # Agents that PROPOSE actions (the governed)
│   ├── monitoring_agent.py # SRE monitoring + anomaly detection (6-step enterprise scan + 5-type alert handling)
│   ├── cost_agent.py       # Cost optimization proposals (VM waste, unattached disks, orphaned public IPs)
│   └── deploy_agent.py     # Infrastructure security proposals — 7 domains: NSG, storage, DB/KV, VM posture, activity log, tagging (Phase 8+)
├── governance_agents/      # Agents that EVALUATE actions (the governors)
│   ├── blast_radius_agent.py    # SRI:Infrastructure (0-100) — LLM decision maker (Phase 22)
│   ├── policy_agent.py          # SRI:Policy (0-100) — LLM decision maker + remediation intent detection (Phase 22)
│   ├── historical_agent.py      # SRI:Historical (0-100) — LLM decision maker (Phase 22)
│   ├── financial_agent.py       # SRI:Cost (0-100) — LLM decision maker (Phase 22)
│   └── _llm_governance.py       # Shared guardrail utilities: clamp_score, parse_llm_decision, format_adjustment_text
├── core/
│   ├── models.py                # ALL Pydantic data models (READ THIS FIRST)
│   ├── pipeline.py              # asyncio.gather() orchestration — 4 governance agents concurrent
│   ├── governance_engine.py     # Calculates SRI™ Composite + verdict
│   ├── risk_triage.py           # Phase 26: compute_fingerprint, classify_tier, build_org_context
│   │                            #   Routes actions to Tier 1/2/3 before governance agents run
│   ├── decision_tracker.py      # Audit trail storage (verdicts → Cosmos / JSON)
│   ├── scan_run_tracker.py      # Scan-run lifecycle store (scan records → Cosmos / JSON)
│   ├── alert_tracker.py         # Alert investigation store (alerts → Cosmos / JSON)
│   ├── explanation_engine.py    # DecisionExplainer — factors, counterfactuals, LLM summary
│   └── interception.py          # ActionInterceptor façade (async)
├── mcp_server/
│   └── server.py                # Exposes governance tools via MCP
├── a2a/                         # A2A Protocol layer (Phase 10)
│   ├── ruriskry_a2a_server.py   # RuriSkry as A2A server (AgentExecutor + AgentCard)
│   ├── operational_a2a_clients.py  # Operational agent A2A client wrappers
│   └── agent_registry.py        # Tracks connected A2A agents with stats
├── infrastructure/              # Azure service clients (live + mock fallback)
│   ├── azure_tools.py           # 5 sync + 9 async (*_async) investigation tools
│   │                            #   query_resource_graph(_async), query_metrics(_async),
│   │                            #   get_resource_details(_async) [injects powerState for VMs via Compute instance view],
│   │                            #   query_activity_log(_async), list_nsg_rules(_async),
│   │                            #   get_resource_health_async (Resource Health API — Available/Unavailable/Degraded),
│   │                            #   list_advisor_recommendations_async (Azure Advisor — Cost/Security/HA/Perf),
│   │                            #   list_defender_assessments_async (Defender for Cloud — CIS/NIST/PCI-DSS),
│   │                            #   list_policy_violations_async (Azure Policy — compliance frameworks)
│   │                            #   — 9 async tools; DeployAgent uses all 9, Cost/Monitoring use 7
│   ├── llm_throttle.py          # asyncio.Semaphore + exponential backoff (Phase 12)
│   ├── resource_graph.py        # Azure Resource Graph — live: KQL + _azure_enrich_topology()
│   │                            #   (tags + NSG topology + cost_lookup); mock: seed_resources.json
│   ├── cost_lookup.py           # Azure Retail Prices REST API — sync + async; _extract_monthly_cost() shared helper
│   ├── cosmos_client.py         # Cosmos DB clients: CosmosDecisionClient (mock: data/decisions/*.json),
│   │                            #   CosmosExecutionClient (mock: data/executions/*.json)
│   ├── search_client.py         # Azure AI Search incidents (mock: seed_incidents.json)
│   ├── openai_client.py         # Azure OpenAI / gpt-5-mini (mock: canned string)
│   └── secrets.py               # Key Vault secret resolver (env → KV → empty)
├── notifications/               # Outbound alerting
│   └── slack_notifier.py        # send_verdict_notification/send_alert_notification/send_alert_resolved_notification — Block Kit; shared AsyncClient singleton, rate limiter, smart retry
├── api/
│   └── dashboard_api.py         # FastAPI REST endpoints — 38 total (Phase 10 agents,
│                                #   Phase 12 alert-trigger (creates pending record, no auto-investigation)
│                                #          + _normalize_azure_alert_payload(),
│                                #          + POST /api/alerts/{id}/investigate (manual trigger),
│                                #   Phase 13 scan triggers,
│                                #   Phase 16: SSE stream, cancel, last-run + durable store,
│                                #   Phase 17: notification-status + test-notification,
│                                #   Phase 18: evaluation explanation,
│                                #   Phase 21: execution gateway HITL + agent-fix + terraform stub,
│                                #   Alerts: GET /api/alerts*, active-count, stream — verdicts include
│                                #     execution_id + execution_status so dashboard shows action buttons)
└── config.py                    # Environment config with SRI thresholds
```

## How to Call RuriSkry

RuriSkry can be called in three ways. All three paths converge at the same
governance pipeline — same SRI™ scoring, same verdict, same audit trail.

### 1. A2A (HTTP) — Enterprise / Multi-Service Pattern

**Who uses it:** External AI agents running as separate services (microservices, Kubernetes pods, cloud-deployed agents).

**How it works:** The external agent downloads RuriSkry's Agent Card from
`/.well-known/agent-card.json`, then sends a `ProposedAction` as an A2A task over HTTP.
RuriSkry streams SSE progress updates and returns a `GovernanceVerdict` artifact.

**Entry point:** `src/a2a/ruriskry_a2a_server.py`
**Start:** `uvicorn src.a2a.ruriskry_a2a_server:app --host 0.0.0.0 --port 8000`
**Demo:** `python demo_a2a.py`

```python
# The external agent side (discovery + call)
resolver = A2ACardResolver(httpx_client=http_client, base_url="http://ruriskry:8000")
agent_card = await resolver.get_agent_card()
client = A2AClient(httpx_client=http_client, agent_card=agent_card)
async for event in client.send_message_streaming(request):
    ...  # receive progress + GovernanceVerdict
```

### 2. MCP (stdio) — Developer / IDE Pattern

**Who uses it:** AI tools like Claude Desktop, GitHub Copilot, or any MCP-compatible
agent running on the same machine as RuriSkry.

**How it works:** The MCP host (e.g. Claude Desktop) launches RuriSkry as a child
process over stdio and calls `skry_evaluate_action` as a structured tool. No network
required — communication is via stdin/stdout pipes.

**Entry point:** `src/mcp_server/server.py`
**Start:** `python -m src.mcp_server.server`

```json
// The MCP tool call (Claude Desktop sends this automatically)
{
  "tool": "skry_evaluate_action",
  "arguments": {
    "resource_id": "vm-23",
    "action_type": "delete_resource",
    "agent_id": "cost-optimization-agent",
    "reason": "VM idle for 30 days"
  }
}
```

### 3. Direct Python — Local / Test Pattern

**Who uses it:** Code inside the same codebase — `demo.py`, all unit tests,
and any Python script that imports RuriSkry directly.

**How it works:** Instantiate the pipeline or interceptor and `await` it directly.
No network, no process boundary, minimal overhead.

**Entry point:** `src/core/pipeline.py` or `src/core/interception.py`
**Demo:** `python demo.py`

```python
from src.core.pipeline import RuriSkryPipeline

pipeline = RuriSkryPipeline()
verdict = await pipeline.evaluate(action)  # same verdict as A2A or MCP
print(verdict.decision.value)              # "approved" | "escalated" | "denied"
```

### Comparison

| | A2A (HTTP) | MCP (stdio) | Direct Python |
|---|---|---|---|
| **Transport** | HTTP + SSE | stdin/stdout pipes | In-process function call |
| **Discovery** | Agent Card at `/.well-known/agent-card.json` | MCP host config | Python import |
| **Used by** | External agents (separate services) | Claude Desktop, Copilot, MCP hosts | demo.py, tests, same-codebase callers |
| **Pattern** | Enterprise / microservices | Developer / IDE | Local development / testing |
| **Streaming** | Yes — SSE progress updates | No | No |
| **Example** | `demo_a2a.py` | Claude Desktop | `demo.py` + `tests/` |

---

## Key Concepts

### Skry Risk Index (SRI™)
Every proposed action gets scored across 4 dimensions:
- **SRI:Infrastructure** (weight 0.30) — blast radius on dependent resources
- **SRI:Policy** (weight 0.25) — governance policy violations
- **SRI:Historical** (weight 0.25) — similarity to past incidents
- **SRI:Cost** (weight 0.20) — financial impact and volatility

### SRI™ Decision Thresholds
Decision rules applied in priority order:
1. Any non-overridden **CRITICAL** policy violation → DENIED regardless of composite score
2. Composite > 60 → DENIED
3. Composite > 25 → ESCALATED
4. Any non-overridden **HIGH** policy violation → ESCALATED floor (Rule 3.5 — prevents score dilution where low blast radius / cost / historical dims push composite below 25 despite a HIGH policy flag)
5. Otherwise → APPROVED (auto-execute)

### Data Flow

**Target architecture — three-layer intelligence:**
```
Operational Agent (Layer 1 — pre-flight reasoning)
    ├── Queries real data: Azure Monitor metrics, Resource Graph tags
    ├── Reasons about context before proposing (detects DR tags, blast radius)
    └── Submits ProposedAction with evidence-backed reason
            ↓
RuriSkry (Layer 2 — independent second opinion)
    → 4 governance agents evaluate in parallel
    → GovernanceDecisionEngine calculates SRI™ Composite
    → GovernanceVerdict returned (approved/escalated/denied)
    → Decision logged to audit trail
            ↓
Execution Gateway (Layer 3 — IaC-safe execution) [Phase 21 — COMPLETE]
    → DENIED    → blocked (no action)
    → ESCALATED → awaiting_review → same 4-button panel as APPROVED (choice = approval)
    → APPROVED  → manual_required → human picks: Create Terraform PR / Fix using Agent /
                                    Fix in Azure Portal / Decline
    → Human merges PR → CI/CD runs terraform apply → IaC state stays in sync
```

**Current state (Phase 20 complete + two audit rounds):** All `@af.tool` callbacks in every agent
are `async def` — including `historical_agent` (fixed in audit round 2; live mode wraps Azure AI
Search call in `asyncio.to_thread()`) and `policy_agent` (pure computation; fixed for architecture
contract compliance). Ops agents use async azure_tools (`*_async`) — DeployAgent uses 9, Cost/Monitoring use 7. Governance agents use
`_evaluate_rules_async()` and `_find_resource_async()`. The topology enrichment method uses
`asyncio.gather()` to run 4 KQL queries + 1 HTTP call concurrently. `asyncio.gather(4 governance
agents)` is now truly parallel — no blocking. `BlastRadiusAgent` and `FinancialImpactAgent` expose
`async def aclose()` to release the underlying `ResourceGraphClient` connection pool. See
STATUS.md for full phase breakdown.

## Important Files to Read First
1. `src/core/models.py` — ALL Pydantic models. Every agent uses these.
2. `src/config.py` — SRI thresholds and weights are configurable here.
3. `data/policies.json` — 11 production governance policies for PolicyComplianceAgent (DR protection,
   NSG change control, internet-exposed dangerous ports, unclassified NSG direction, tag enforcement,
   change windows, cost thresholds, critical/shared resource protection, prod deletion/downgrade).
   Engine supports 10 condition types including `nsg_change_direction` (structured intent field —
   "open" triggers POL-SEC-002 CRITICAL, "restrict"/None does not), `nsg_direction_unset` (fires when
   modify_nsg omits the direction field — POL-SEC-003 HIGH), `reason_pattern`, and `tags_absent`
   (see `policy_agent.py` docstring for full list).
4. `data/seed_incidents.json` — 7 past incidents for HistoricalPatternAgent.
5. `data/seed_resources.json` — Azure resource topology. Contains two sections:
   - **Mini prod resources** (`ruriskry-prod-rg`): `vm-dr-01`, `vm-web-01`, `payment-api-prod`,
     `nsg-east-prod`, `ruriskryproddata` — matches `infrastructure/terraform-prod/` deployment.
   - **Legacy mock resources**: `vm-23`, `api-server-03`, `nsg-east`, `aks-prod`, `storageshared01`
     — kept for test compatibility (all unit tests reference these names).
6. `src/a2a/ruriskry_a2a_server.py` — A2A entry point: `RuriSkryAgentExecutor` + `AgentCard`.
7. `src/a2a/agent_registry.py` — Agent registry: tracks connected agents and their stats.
8. `infrastructure/terraform-prod/` — Mini production environment for live demos. Deploy these
   resources so RuriSkry governs real Azure VMs instead of purely mock data.

## Current Development Phase
> For detailed progress tracking see **STATUS.md** at the project root.

**Agent Intelligence Overhaul (2026-03-13, COMPLETE)**

All three operational agent system instructions completely rewritten for enterprise-grade coverage:

**MonitoringAgent (`_SCAN_INSTRUCTIONS` — 6-step enterprise scan):**
- Step 1: Discover VMs, databases, containerApps, storageAccounts, disks, publicIPs via Resource Graph
- Step 2: Check EVERY VM power state via `get_resource_details` — stopped/deallocated = HIGH urgency `restart_service`. **Critical fix**: no metrics from a VM is confirmation it is DOWN, not confirmation it is clean. The old instructions never checked power state; they only ran `query_metrics`, which returns empty data for deallocated VMs. An empty metric result was silently interpreted as "no problem" — causing `vm-web-01` (deallocated) to be reported as "Clean" when it was actually down.
- Step 3: Database health — single-region Cosmos DB, no failover, publicNetworkAccess, no backup policy, P99 latency
- Step 4: Container Apps & App Services — replica count, HTTPS enforcement, Http5xx rate
- Step 5: Observability gaps — VMs missing Azure Monitor Agent extension, resources missing required tags
- Step 6: Orphaned resources — unattached disks, unassociated public IPs

**MonitoringAgent (`_ALERT_INSTRUCTIONS` — 5 alert types):**
Expanded from 2 types (availability + CPU) to 5: A) availability/heartbeat, B) CPU/memory, C) disk/storage, D) database/data service, E) network/connectivity. Each type specifies exact tools to call, evidence to collect, and urgency criteria.

**DeployAgent (`_AGENT_INSTRUCTIONS` — 7 security domains):**
Expanded from NSG-only to full infrastructure security posture: (1) resource discovery (NSGs, VMs, storage, databases, Key Vaults, public IPs), (2) NSG audit (CRITICAL/HIGH/MEDIUM severity tiers), (3) storage account security (`allowBlobPublicAccess`, HTTP-only, TLS < 1.2, open network ACL), (4) database & Key Vault security (publicNetworkAccess, no private endpoint, SQL open firewall, soft-delete disabled, purge protection), (5) VM security posture (OS disk encryption, password vs SSH keys, public IP with no NSG), (6) recent config changes via activity log audit, (7) zero-tag resource governance.

**CostAgent (`_AGENT_INSTRUCTIONS` — new waste categories):**
- Deallocated VMs: still paying for disk storage even when stopped — flag for delete/review (MEDIUM)
- Unattached disks (`diskState = 'Unattached'`): ongoing cost with zero value → `delete_resource` MEDIUM
- Orphaned public IPs (not attached to any NIC/LB): wasted reservation → `delete_resource` LOW
- Added Redis, storage accounts to discovery query
- Urgency scale added (MEDIUM for disks/deallocated VMs, LOW for rightsizing opportunities)

**Bug Fixes + Test Hardening (post-Phase 24, COMPLETE)**

- `src/api/dashboard_api.py`: `_run_agent_scan()` now persists `"status": "error"` (+ `"scan_error"` message) when the agent's LLM call fails or times out. Previously always wrote `"status": "complete"`, making timed-out scans indistinguishable from clean scans.
- `dashboard/src/pages/Scans.jsx`: Added `AGENT_TYPE_LABELS` map keyed by `scan.agent_type` (`"deploy"/"monitoring"/"cost"`). Label lookup now uses `agent_type` first — fixes `scan_tracker` showing in the Agent column. Added `AlertTriangle` red "Error" badge for `status === "error"` with tooltip showing the error message.
- `infrastructure/terraform-core/main.tf`: `azurerm_monitor_action_group.ruriskry` created as a named resource pointing at `https://<backend-fqdn>/api/alert-trigger`. No variable needed — webhook URL derived from Container App FQDN directly.
- `infrastructure/terraform-prod/main.tf`: `azurerm_monitor_action_group.prod` (`ag-ruriskry-prod`) has `use_common_alert_schema = false` and a `dynamic webhook_receiver` activated by `var.alert_webhook_url`. Both `alert-vm-dr-01-heartbeat` (scheduled query) and `alert-vm-web-01-cpu-high` (metric) rules reference this action group — setting `alert_webhook_url` in tfvars wires both rules to RuriSkry in one apply.
- `src/api/dashboard_api.py`: `_normalize_azure_alert_payload()` added — normalises Azure Monitor Common Alert Schema and non-common schema to flat internal format. **Workspace pivot**: Log Alerts V2 always reports the Log Analytics workspace as `alertTargetID` (never the monitored VM) and `configurationItems` is empty when query returns 0 rows. The normalizer detects `operationalinsights/workspaces` in resource_id and regex-extracts the actual VM name from `essentials.description` or `alertRule` name, constructing the correct VM ARM ID. Without this, MonitoringAgent investigated the workspace and found nothing ~50% of the time (LLM non-determinism).
- `dashboard/tests/regression.spec.js` + `e2e.spec.js`: Fixed 5 brittle test assertions (metrics contract `approval_rate` → `decisions`/`decision_percentages`; verdict badge locator scoped to `tbody`; Agents heading strict-mode `.first()`; table header waits via `locator('table').filter`; deploy scan e2e accepts framework error outcome).

**Phase 24 — Magic UI Visual Redesign + Ops Nerve Center Aesthetic (COMPLETE)**

Dashboard visual overhaul — production-grade "aerospace ops center" aesthetic implemented as bespoke components (no runtime library dependency):

**Phase 24a — Magic UI Components:**
- `dashboard/src/components/magicui/NumberTicker.jsx` — count-up animation via `requestAnimationFrame` + easeOutQuart. Animates metric values when data refreshes.
- `dashboard/src/components/magicui/GlowCard.jsx` — card wrapper with color-coded box-shadow glow (`blue/green/amber/red/slate`) + optional border beam (gradient light scanning across top edge via `@keyframes scanBeam`) + `urgent` prop → slow amber `animate-urgent-pulse` + `backdrop-filter: blur(12px)` glass depth.
- `dashboard/src/components/magicui/VerdictBadge.jsx` — unified verdict labels sitewide: dot indicator + uppercase pill + glow `text-shadow`. Emerald = approved, amber = escalated, rose = denied.
- `dashboard/src/components/magicui/TableSkeleton.jsx` — shimmer placeholder rows for tables during data load (avoids blank flash). Uses `.shimmer` CSS animation.
- `dashboard/src/pages/Overview.jsx` — NumberTicker on all 4 metric cards; `AreaChart` + `linearGradient` SVG for SRI trend (filled gradient area); GlowCard on every panel; staggered `.card-stagger` entrance animation; `urgent` prop on Pending Reviews card when HITL reviews are pending. **Triage Intelligence card (Phase 26/27A)**: teal GlowCard showing LLM calls saved (NumberTicker), Tier 1/2/3 counts with percentages, stacked progress bar (emerald=Tier1 / amber=Tier2 / rose=Tier3). Reads from `metrics.triage` API field; only renders when metrics loaded.
- `dashboard/src/components/ConnectedAgents.jsx` — GlowCard on every agent card: green glow when online, amber + fast beam when scanning, slate when offline.
- `AuditLog.jsx`, `DecisionTable.jsx`, `Decisions.jsx` — all replaced inline verdict spans with shared `VerdictBadge`; SRI colors updated to emerald/amber/rose throughout.
- `dashboard/src/components/AgentControls.jsx` — replaced emoji icons (💰🤖🔒) with Lucide SVG (`DollarSign`, `Activity`, `Shield`, `Zap`, `ClipboardList`).

**Phase 24b — Ops Nerve Center Design System:**
- `dashboard/index.html` — Google Fonts preconnect + load: DM Sans (UI text) + JetBrains Mono (all data values).
- `dashboard/tailwind.config.js` — `fontFamily.sans = ["DM Sans", ...]`, `fontFamily.mono = ["JetBrains Mono", ...]`.
- `dashboard/src/index.css` — Full CSS design token system in `:root`: `--bg-base/#020817`, `--bg-surface`, `--border-subtle/default/accent`, `--accent-blue/teal/amber/emerald/rose`, `--font-ui`, `--font-data`. Keyframes: `breathe` (teal logo pulse 3.5s), `urgentPulse` (amber card glow 2.2s), `iconUrgent` (sidebar icon amber drop-shadow 2s), `scanBeam`, `fadeInUp`, `shimmerSlide`, `indicatorIn`. Utility classes: `.animate-breathe`, `.animate-urgent-pulse`, `.animate-icon-urgent`, `.animate-fade-in-up`, `.card-stagger`, `.shimmer`, `.metric-value`, `.metric-value-{blue/green/amber/red/slate}`, `.glass`, `.bg-dots`.
- `dashboard/src/components/Sidebar.jsx` — teal `animate-breathe` glow on SL logo; `animate-icon-urgent` amber pulse on Decisions icon when `pendingCount > 0`; amber badge with glow on both Overview and Decisions links; animated left-bar active indicator; pulsing "System online" status.
- `dashboard/src/App.jsx` — two-phase load: phase 1 calls `fetchMetrics()` + `fetchAgents()` only (fast REST, no Cosmos table scans) to clear `<LoadingScreen>` immediately; phase 2 fires `fetchAll()` non-blocking right after so evaluations/scans/alerts/pendingReviews populate within 1–2s; 5s background refresh continues as before. `bg-dots` dot-grid on main content area; `--font-ui` / `--bg-base` CSS variables applied to root layout.
- **Tests: 666 passed (unchanged — frontend-only change, no backend touched)**

**Phase 30 — Rollback for Agent-Applied Fixes (COMPLETE)**

`ExecutionAgent.rollback(action, plan)` inverts an applied fix. Mock path: `_rollback_mock()`
maps each `ActionType` to its inverse — `RESTART_SERVICE`→`deallocate_vm`,
`SCALE_UP`/`SCALE_DOWN`→`resize_vm` back to `current_sku`, `MODIFY_NSG`→`create_nsg_rule`
restored, `DELETE_RESOURCE`→cannot auto-rollback. Live path: `_rollback_with_framework()` LLM
reads `rollback_hint` from stored plan + current state, calls Azure SDK write tools in reverse.
On LLM/framework exception, `rollback()` returns `{"success": False, ...}` — it does **not**
fall back to `_rollback_mock()` (mock returns `success=True`, which would falsely mark the
record `rolled_back` while Azure never ran).
`ExecutionGateway.rollback_agent_fix()` validates `status == applied`, calls `agent.rollback()`,
sets `status → rolled_back` **only when rollback succeeds**; on failure keeps `status = applied`
(fix is still in place) and appends failure detail to `rollback_log` + `notes`. Stores `rollback_log`.
`POST /api/execution/{id}/rollback` endpoint.
`ExecutionRecord` gains `rollback_log: Optional[list]` field; `ExecutionStatus.rolled_back` added.
Dashboard: amber `↩ Rollback` button appears next to `Applied` badge in both
`EvaluationDrilldown.jsx` and `Alerts.jsx`. Confirm dialog shows `rollback_hint` from stored plan.
`rolled_back` status badge (amber) added to both `EXEC_STATUS_CONFIG` maps. `ExecutionLogView`
accepts `label` prop to show "Rollback Steps" separately from execution log.
On rollback failure, a rose-colored "Rollback attempted but failed" banner is shown — including
when `rollback_log` is empty (API error or LLM timeout); message includes the error string if
available. The Rollback button remains so the user can retry.
**AgentTerminal live progress**: `handleAgentFixExecute()` fires a `setInterval` while the API
call is pending (2s cadence), appending progress lines so the terminal appears active during the
LLM+SDK wait (30–60s). Interval cleared before real step-by-step animation starts.
**Tests: 779 passed.**

**Phase 31 — Manual Alert Investigation + Live Log + Rollback Fixes (COMPLETE)**

**Manual-only investigation:** `POST /api/alert-trigger` no longer auto-investigates. Alerts
land in `status="pending"`. New `POST /api/alerts/{alert_id}/investigate` endpoint (validates
`pending` status, returns 409 otherwise) launches `MonitoringAgent` via `BackgroundTasks`.
`active-count` endpoint counts `pending`+`investigating`. Frontend: filter option renamed
`"pending"` (was `"firing"`); `statusLabel/Icon/Color` includes `pending` case (Clock icon,
slate). "🔍 Investigate" button rendered per pending row in the table AND inside `AlertPanel`
drilldown when `localAlert.status === 'pending'`.

**Live investigation log in `AlertPanel`:** Alert records gain an `investigation_log: []` field.
`_emit_alert_event()` appends a lightweight snapshot of each SSE event (event type, timestamp,
message, plus optional decision/sri/count/execution_status) to this field — no SSE queue
competition since the panel polls REST instead of subscribing to SSE. `AlertPanel` holds
`localAlert` local state (synced from parent, updated by polling and button click). When
`localAlert.status === 'investigating'`, a recursive `setTimeout(poll, 1500)` fetches
`GET /api/alerts/{id}/status`, updates `localAlert`, and calls `fetchAll()` on completion.
`logLines` maps `investigation_log` entries through `eventToLogLine()` → `AgentTerminal`.
A "Live" pulse badge appears while polling. `fetchAlertStatus(alertId)` added to `api.js`.

**Rollback silent-failure bug (backend):** `ExecutionAgent.rollback()` was catching LLM/framework
exceptions and falling back to `_rollback_mock()`, which returns `success=True` for all action
types. This marked the record `rolled_back` in Cosmos while Azure never ran `vm deallocate`. Fixed
to return `{"success": False, "steps_completed": [], "summary": "Rollback failed — LLM agent
error: <exc>"}` on exception — matching the `execute()` pattern. Mock is now only used when
`_use_framework=False` (test/mock mode).

**Rollback empty-log UI gap (frontend):** Failure panel in `Alerts.jsx` and
`EvaluationDrilldown.jsx` was gated on `rollback_log.length > 0` — silently swallowed API
errors (catch block sets `rollback_log=[]`) and "LLM called no tools" outcomes. Fixed: panel
shows whenever `rollbackResult` is set and status is still `applied`, with contextual message
(steps if present, else error string from `.notes`, else generic retry prompt).

**Rollback credential bug:** All four rollback write tools (`start_vm`, `deallocate_vm`,
`resize_vm`, `create_nsg_rule`) in `_rollback_with_framework` were using
`async with DefaultAzureCredential() as cred:` — the sync `DefaultAzureCredential` (imported
at the top of the method for the token provider) does not implement `__aenter__`/`__aexit__`
so `async with` throws immediately. Execute-phase tools correctly import
`from azure.identity.aio import DefaultAzureCredential as AioCredential` locally in each tool
function and use `async with AioCredential()`. Rollback tools now match this pattern.

**Rollback missing plan fallback:** `rollback_agent_fix` raised `ValueError` → HTTP 400 when
`execution_plan` was `None` on the record (caused by Cosmos save failure between execute and
rollback, or a fresh replica loading a stale Cosmos record). Now generates the plan on-the-fly
via `generate_agent_fix_plan()` if missing, matching the `execute_agent_fix` pattern.

**AlertFindingActions status sync:** Component initialized `execStatus` from the alert
record's `execution_status` field, which is set at investigation time (`manual_required`) and
never updated as execution progresses. After a redeployment, stale React state showed `applied`
while the Cosmos record was `manual_required` — Rollback button appeared and fired a 400.
Fix: on mount, `AlertFindingActions` calls `fetchExecutionRecord(execId)` via new
`GET /api/execution/{id}/record` endpoint and syncs `execStatus`, `agentFixResult`, and
`prUrl` with the real Cosmos-backed state. Failure is silent (keeps prop value).

**api.js error clarity:** `rollbackAgentFix` always threw `"API error 400: rollback failed"`
regardless of backend detail. Now reads `body.detail` so the real reason is surfaced in the UI.
**Tests: 793 passed, 0 failed.**

**Phase 30 post-release fixes (COMPLETE)**

Dashboard and backend refinements shipped after the rollback feature:

- Alert labels: `"Resolved"` display text → `"Investigated"` in both the Alerts tab filter
  and the Overview AlertsCard. The stored status value in `AlertRecord` remains `"resolved"` for
  backward compatibility with existing data. `"Resolution Rate"` label → `"Investigation Rate"`.
- Decisions table: new **Agent** column with colored badge pills (Monitoring=blue, Cost=amber,
  Deploy=purple). `initialAgent` prop on `DecisionTable` allows pre-selecting the agent filter
  from the URL (`?agent=<agent_id>`). `key={agentParam}` on the component forces a remount when
  the agent filter changes via navigation (React Router v6 does not remount on query change).
- Scans → Decisions navigation: clicking a verdict count in the Scans page now navigates to
  `/decisions?agent=<agent_id>` so the Decisions table opens pre-filtered to that agent.
- "SRE" → "Monitoring" rename (frontend only): all `"SRE"` labels in `AgentControls`,
  `DecisionTable`, `LiveLogPanel`, `AuditLog`, `Overview`, and `Scans` replaced with
  `"Monitoring"`. Backend agent type value `"monitoring"` was already correct; only display
  labels changed.
- Execution gateway dedup `action_id` update: when a resource is re-scanned and an existing
  `manual_required` record is found for the same `(resource_id, action_type)`, the record's
  `action_id` is now updated to the latest verdict's `action_id` before returning. Previously
  the stale `action_id` caused the drilldown to show "No execution record" because the new
  verdict's `action_id` didn't match any execution record.
- `terraform-prod` AMA identity fix: both VMs (`vm-dr-01`, `vm-web-01`) given
  `identity { type = "SystemAssigned" }` blocks + `azurerm_role_assignment` resources for
  "Monitoring Metrics Publisher" role. Azure Monitor Agent silently drops telemetry without a
  valid managed identity — this was causing intermittent metric gaps in alert investigations.
- **Tests: 777 passed.** (1 new test: `TestDedupActionIdUpdate` in `test_execution_gateway.py`)

**Phase 29 — Post-Execution Verification, Admin Panel, Execution Metrics, Alerts Card (COMPLETE)**

`ExecutionAgent.verify()` re-checks resource state after execution using read-only tools
(`get_resource_details`, `list_nsg_rules`, `query_metrics`) and a `submit_verification_result`
capture tool. Mock path: `_verify_mock()` returns deterministic per-ActionType messages. Live
path: `_verify_with_framework()` LLM-driven with fallback to mock. Result stored as
`ExecutionRecord.verification: {confirmed, message, checked_at}`.

`ExecutionGateway.list_all()` returns all records newest-first. `GET /api/metrics` gains
`executions` block (total/applied/failed/pr_created/dismissed/pending/agent_fix_rate/success_rate).
`GET /api/config` returns safe system config. `Admin.jsx` new page — System Configuration card
(mode/timeout/concurrency/flags/version) + Danger Zone with Reset button (moved from header).
Settings gear icon + Admin NavLink in Sidebar bottom. `AlertsCard` + `ExecutionMetricsCard`
components added to Overview between metric cards and SRI trend. `ExecutionLogView` in both
`EvaluationDrilldown.jsx` and `Alerts.jsx` shows per-step execution log + verification badge.
**Tests: 763 passed.**

**Phase 28 — LLM-Driven Execution Agent (COMPLETE)**

The execution layer is now fully LLM-driven. `src/core/execution_agent.py` — `ExecutionAgent`
class with two public methods: `plan(action, verdict_snapshot) -> dict` (LLM reads resource
state and outputs structured steps) and `execute(plan, action) -> dict` (LLM calls Azure SDK
write tools step-by-step with fail-stop semantics). Plan output: `{steps, summary,
estimated_impact, rollback_hint, commands}`. Execute output: `{success, steps_completed,
summary}`. All 7 `ActionType` values covered in mock mode. The dashboard now shows a rich plan
view (steps table, impact, rollback hint, expandable CLI equivalent) instead of raw az commands.
**Tests: 748 passed.**

**Phase 21 — Execution Gateway & Human-in-the-Loop (COMPLETE)**

APPROVED verdicts route to IaC-safe Terraform PRs (via GitHub API) instead of direct Azure
SDK calls, preventing IaC state drift. ESCALATED verdicts get dashboard HITL buttons (Approve
/ Dismiss). IaC detection reads `managed_by=terraform` tag: **in live mode** via
`ResourceGraphClient.get_resource_async()`, with `seed_resources.json` fallback in mock mode
or on network failure. **Non-IaC action guard**: `create_pr_from_manual` checks
`_NON_IAC_ACTION_TYPES` before touching GitHub — `restart_service` (and any future operational
power actions) raise `ValueError` with a clear message directing the user to "Fix by Agent" or
`az vm start`, since Terraform manages desired state, not operational commands. `ExecutionRecord` is Cosmos-durable via `CosmosExecutionClient` (`governance-executions` container, partition `/resource_id`; mock: `data/executions/*.json`) — survives Container App revision deployments.

Key files: `src/core/execution_gateway.py`, `src/core/terraform_pr_generator.py`,
`tests/test_execution_gateway.py`. Endpoints: `GET /api/execution/pending-reviews`,
`GET /api/execution/by-action/{action_id}`, `POST /api/execution/{id}/approve`,
`POST /api/execution/{id}/dismiss`, `POST /api/execution/{id}/create-pr`,
`GET /api/execution/{id}/agent-fix-preview`, `POST /api/execution/{id}/agent-fix-execute`.
Env vars: `GITHUB_TOKEN`, `IAC_GITHUB_REPO`, `IAC_TERRAFORM_PATH`,
`EXECUTION_GATEWAY_ENABLED`. **Tests: 582 passed.**

Post-deploy fixes: `_run_agent_scan()` updates `AgentRegistry` per verdict (Connected Agents
panel stays current); "Run All Agents" opens merged SSE log for all 3 agents;
`ExecutionGateway.get_unresolved_proposals()` re-flags `manual_required` issues on every scan
until human dismisses them ("flag until fixed" governance pattern).

**Scan visibility + HITL action panel (post-Phase 21 improvements):**

- `deploy_agent.py` — `_scan_with_framework()` maintains a `scan_notes: list[str]` closure
  that each `@af.tool` callback appends to. After the LLM run, `self.scan_notes` is set so
  `_run_agent_scan()` can emit each note as a `reasoning` SSE event.
- `terraform_pr_generator.py` — `_apply_nsg_fix_to_content()` two-pass brace-counting walk:
  (1) standalone `azurerm_network_security_rule` blocks, (2) inline `security_rule {}` blocks
  inside `azurerm_network_security_group`. `_find_and_patch_tf_file()` calls `repo.update_file()`
  on the existing file — real one-line diff, not a stub.
- `execution_gateway.py` — `route_verdict()`: APPROVED verdicts always go to `manual_required`
  (no auto-PR); IaC metadata stored so "Create Terraform PR" button works on demand.
  `get_unresolved_proposals()` deduplicates by (resource_id, action_type), keeping oldest record.
  `execute_agent_fix()` and `create_pr_from_manual()` accept `awaiting_review` and `pr_created`
  in addition to `manual_required` — auto-approving ESCALATED records when user picks an action.
  `generate_agent_fix_plan()` (async) instantiates `ExecutionAgent` and returns the structured
  plan. `execute_agent_fix()` reads the stored `execution_plan` from the record and delegates
  to `ExecutionAgent.execute()`. Hardcoded `_build_az_commands()` and `_execute_fix_via_sdk()`
  removed in Phase 28.
- `dashboard_api.py` — `_get_resource_tags()` walks up to parent resource for sub-resource ARM
  IDs (e.g. `.../securityRules/rule-name` → looks up parent NSG tags). Re-flag logic: strips
  duplicate `[Unresolved since ...]` prefix; extracts NSG parent name for `/securityRules/`
  ARM IDs so auto-dismiss matches correctly. **Agent-id filter (Phase 31 bugfix):**
  `get_unresolved_proposals()` returns ALL agents' records, so `_run_agent_scan()` immediately
  filters the result to `agent_id == _AGENT_REGISTRY_NAMES[agent_type]` before re-appending —
  prevents cross-agent contamination where a cost scan would re-evaluate monitoring-agent
  proposals and pollute its own record.
- `EvaluationDrilldown.jsx` — `awaiting_review` (ESCALATED) and `manual_required` (APPROVED)
  share the same 4-button panel: Create Terraform PR / Fix using Agent / Fix in Azure Portal /
  Decline. Choosing any action on an ESCALATED record auto-approves it. `pr_created` panel:
  Fix using Agent / Fix in Azure Portal / Close PR — "Show Terraform Fix" stub removed.
- `historical_agent.py` — `_governance_history_boost()`: deterministic supplement that reads
  `DecisionTracker.get_recent()` and adds +25 per ESCALATED / +5 per APPROVED decision for the
  same `action_type` (capped at +60). Prevents Azure AI Search query variance from resetting
  the historical score to 0 between runs on the same recurring issue.

**Phase 20 — Async End-to-End Migration (complete)**

- `src/infrastructure/cost_lookup.py` — `_extract_monthly_cost(items, os_type)` shared helper
  (DRY: used by both sync + async paths). `get_sku_monthly_cost_async()` via `httpx.AsyncClient`;
  shares the same `_cache` dict with the sync version.
- `src/infrastructure/resource_graph.py` — `_async_rg_client` (`azure.mgmt.resourcegraph.aio`,
  credential: `azure.identity.aio.DefaultAzureCredential`); `get_resource_async()`,
  `list_all_async()`, `_azure_enrich_topology_async()` which uses `asyncio.gather()` for 4
  concurrent KQL/HTTP calls. `async def aclose()` — closes the connection pool at shutdown.
- `src/infrastructure/azure_tools.py` — 5 async variants: `query_resource_graph_async`,
  `query_metrics_async`, `get_resource_details_async`, `query_activity_log_async`,
  `list_nsg_rules_async`. Each uses `async with DefaultAzureCredential() as credential:`
  nested inside `async with SomeClient(credential) as client:` — both are closed
  deterministically on exit (async credential type required for `.aio` clients; credentials
  hold their own internal HTTP connections for token acquisition). Mock mode unchanged.
- `src/governance_agents/blast_radius_agent.py` + `financial_agent.py` — `_evaluate_rules_async()`,
  `_find_resource_async()`, and all helpers now `async def`; `@af.tool` callbacks `async def`;
  framework "tool not called" fallback → `await self._evaluate_rules_async(action)` (was sync).
  Both agents expose `async def aclose()` delegating to `self._rg_client.aclose()`.
- `src/governance_agents/historical_agent.py` — `@af.tool evaluate_historical_rules` changed to
  `async def`; added `_evaluate_rules_async()` using `asyncio.to_thread()` in live mode (Azure AI
  Search I/O is blocking; thread pool prevents event loop stall). Mock mode: sync call (no I/O).
- `src/governance_agents/policy_agent.py` — `@af.tool evaluate_policy_rules` changed to
  `async def` (pure computation; no I/O; fixes architecture contract compliance).
- `src/operational_agents/cost_agent.py`, `monitoring_agent.py`, `deploy_agent.py` — all
  `@af.tool` azure_tool callbacks `async def` + `await *_async()`. `propose_action` stays sync.
- `tests/test_async_migration.py` (NEW) — 39 tests: cache sharing, `asyncio.gather` call count,
  mock parity, `inspect.iscoroutinefunction` assertions, `aclose()` existence checks, historical
  + policy tool async assertions.
- **Test result: 505 passed, 0 failed** ✅

**Phase 19 — Live Azure Topology for Governance Agents (complete)**

- `src/infrastructure/cost_lookup.py` (NEW) — `get_sku_monthly_cost(sku, location)`: public Azure
  Retail Prices API, no auth. Returns monthly USD (min hourly price × 730). Module-level `_cache`.
- `src/infrastructure/resource_graph.py` — `_azure_enrich_topology(resource)` enriches live
  resources with tag-based deps (`depends-on`, `governs`), KQL VM→NSG network join, NSG→VM
  governs join, reverse depends-on scan, and `monthly_cost` from `cost_lookup`.
- `src/governance_agents/blast_radius_agent.py` — `__init__` branched on
  `_live = not use_local_mocks and bool(subscription_id) and use_live_topology`. Live mode
  skips JSON, uses `ResourceGraphClient`. `_find_resource()`, `_detect_spofs()`,
  `_get_affected_zones()` all route to `_rg_client` in live mode.
- `src/governance_agents/financial_agent.py` — same branch pattern. Live `monthly_cost` from
  enriched ResourceGraphClient dict replaces static JSON value.
- `infrastructure/terraform-prod/main.tf` — `depends-on` + `governs` tags on 4 resources.
- `src/config.py` — `use_live_topology: bool = False` (env var `USE_LIVE_TOPOLOGY=true`).
  Explicit opt-in required to activate live Azure topology; default `false` keeps tests safe
  even when `USE_LOCAL_MOCKS=false` + `AZURE_SUBSCRIPTION_ID` are set.
- `tests/test_live_topology.py` (NEW) — 16 tests covering all new live-mode paths.
- `tests/test_decision_tracker.py` — 10 `@pytest.mark.xfail` markers removed from `TestRecord`;
  `tracker._dir` → `tracker._cosmos._decisions_dir` (stale since Phase 7 Cosmos migration).
- **Test result: 466 passed, 0 failed** ✅ (505 after Phase 20 + audit fixes)

**Phase 18 — Decision Explanation Engine (complete)**

- `src/core/explanation_engine.py` (NEW) — `DecisionExplainer.explain(verdict, action)` returns a
  `DecisionExplanation` with ranked `Factor` list, `Counterfactual` scenarios, policy violations,
  risk highlights, and an LLM-generated summary (gpt-5-mini in live mode; template fallback in mock).
  Module-level `_explanation_cache` keyed by `action_id` prevents redundant recomputation.
- `src/core/models.py` — 3 new Pydantic models: `Factor`, `Counterfactual`, `DecisionExplanation`.
- `src/api/dashboard_api.py` — 1 new endpoint: `GET /api/evaluations/{id}/explanation` (18 total).
  Reconstructs `GovernanceVerdict` from the stored flat record, calls the explainer, returns JSON.
- `dashboard/src/components/EvaluationDrilldown.jsx` (NEW) — 6-section full-page drilldown:
  verdict header, SRI bars (with primary-factor ⭐), explanation, counterfactual cards, agent
  reasoning, collapsible JSON audit trail. Opened by clicking any row in the Live Activity Feed.
- `dashboard/src/App.jsx` — `drilldownEval` state drives navigation to/from the drilldown.
- Test result: **434 passed, 10 xfailed, 0 failed** ✅

**Phase 17 — Slack Notifications (complete)**

- `src/notifications/slack_notifier.py` (NEW) — async fire-and-forget Block Kit messages via Slack Incoming Webhook.
  Three notification functions: `send_verdict_notification` (DENIED/ESCALATED verdicts → `pipeline.py`),
  `send_alert_notification` (investigation started — fired from `_run_alert_investigation` when user manually triggers),
  `send_alert_resolved_notification` (investigation complete).
  Enterprise hardened: shared `httpx.AsyncClient` singleton (TLS reuse), rate limiter (≥1.1 s between sends),
  smart retry (4xx=no retry, 429=Retry-After header, 5xx=exponential backoff). Never raises.
- `src/config.py` — settings: `slack_webhook_url`, `slack_notifications_enabled`, `slack_timeout`, `dashboard_url`.
- `src/api/dashboard_api.py` — 2 new endpoints: `GET /api/notification-status`, `POST /api/test-notification` (17 total).
- Frontend: 🔔 Slack pill in header — green clickable button (sends test notification) when configured, grey static pill when not.
- Test result: **429 passed, 10 xfailed, 0 failed** ✅

**Phase 16 — Scan Durability, Live Log & Agent Action Menus (complete)**

- `src/core/scan_run_tracker.py` (NEW) — durable scan-run store, mirrors `DecisionTracker` pattern.
  Cosmos DB live mode / `data/scans/*.json` mock mode. Survives server restarts.
- `src/api/dashboard_api.py` — 3 new endpoints: `GET /api/scan/{id}/stream` (SSE),
  `PATCH /api/scan/{id}/cancel`, `GET /api/agents/{name}/last-run`. All scan reads now use
  `_get_scan_record()` (memory-first, durable fallback).
- Frontend: `LiveLogPanel.jsx` event styles, `ConnectedAgents.jsx` ⋮ action menus,
  `hasScanId` bug fix, enriched `LastRunPanel`.
- Test result: **424 passed, 10 xfailed, 0 failed** ✅

**Phase 14 — Verification & Fixes (complete)**

Comprehensive verification of Phase 12/13. Key fixes applied:

- ``src/infrastructure/azure_tools.py``: live-mode exceptions now raise ``RuntimeError``
  (descriptive message + "run az login" hint) instead of silently falling back to mock data.
  ``get_resource_details`` and ``list_nsg_rules`` seed-data fallbacks are now gated behind
  ``_use_mocks()`` — they never run in live mode.
- All 3 ops agents: ``agent.run(prompt)`` wrapped in ``run_with_throttle`` (asyncio.Semaphore
  + exponential back-off retry) — same throttling guarantee as the governance agents.
- ``demo_live.py``: hardcoded ``"ruriskry-prod-rg"`` replaced with ``--resource-group / -g``
  CLI argument (``None`` default = scan whole subscription).
- ``tests/test_agent_agnostic.py`` (NEW, 22 tests): verifies environment-agnosticism at SDK
  level — no hardcoded resource names, KQL passes through unchanged, RuntimeError on Azure
  failure, custom RG passed to framework, mock metrics returns structured dict.
- **Test result: 420 passed, 10 xfailed, 0 failed** ✅
- Commit: ``ee2c0fd``

**Phase 13 — Agent Scan Triggers + Environment-Agnosticism Fixes (complete)**

Five new scan endpoints (`POST /api/scan/cost|monitoring|deploy|all`, `GET /api/scan/{id}/status`)
let judges trigger ops agent scans directly from the dashboard — no terminal required.
`AgentControls.jsx` provides per-agent buttons with spinners and 2-second polling.
Environment-agnosticism fixes: cost agent KQL broadened beyond VM+AKS; deploy agent
no longer prescribes specific tag key names; all 3 agents return `[]` on live-mode failure
instead of seed-data proposals; mock RG filter and default metrics fixed.

**Phase 12 — Intelligent Ops Agents (complete)**

Ops agents now use 5 generic Azure tools (``src/infrastructure/azure_tools.py``) to
investigate real data before proposing. gpt-5-mini discovers resources, checks actual CPU
metrics, inspects NSG rules, and reviews activity logs — then calls ``propose_action``
with evidence-backed reasons. ``POST /api/alert-trigger`` enables Azure Monitor webhook
integration. Run ``python demo_live.py --resource-group <rg>`` (or without flag to scan
the whole subscription) to see two-layer intelligence end-to-end.
Note: live-mode failure now raises ``RuntimeError`` and returns ``[]`` — never falls back
to seed data. All ops agent framework calls are throttled via ``run_with_throttle``.

**Phase 11 — Mini Production Environment (complete)**

- `infrastructure/terraform-prod/` — Terraform config that creates 5 real Azure resources in
  `ruriskry-prod-rg` for live hackathon demos: `vm-dr-01` (DENIED scenario), `vm-web-01`
  (APPROVED scenario), `payment-api-prod` (critical dependency), `nsg-east-prod` (ESCALATED
  scenario), shared storage `ruriskryprod{suffix}`. Auto-shutdown at 22:00 UTC on both VMs.
  Azure Monitor alerts: CPU >80% on `vm-web-01`, heartbeat on `vm-dr-01`.
- `data/seed_resources.json` updated: real `ruriskry-prod-rg` resource IDs added alongside
  legacy mock resources. Replace `YOUR-SUBSCRIPTION-ID` with your subscription ID after
  running `terraform apply` in `infrastructure/terraform-prod/`.
- **Test result: 420 passed, 10 xfailed, 0 failed** ✅

**Previous: Phase 10 — A2A Protocol + Bug Fixes**

- `src/a2a/ruriskry_a2a_server.py` — RuriSkry exposed as an A2A-compliant
  server using `agent-framework-a2a` + `a2a-sdk`. Agent Card at
  `/.well-known/agent-card.json`. `RuriSkryAgentExecutor` routes tasks through
  the existing governance pipeline, streaming SSE progress via `TaskUpdater`.
  `DecisionTracker().record(verdict)` called after every A2A evaluation so
  decisions appear in `/api/evaluations`, `/api/metrics`, and Cosmos DB.
  **`TaskUpdater` API (a2a-sdk 0.3.24):** `submit()`, `start_work()`,
  `add_artifact()`, and `complete()` are `async` — must be awaited.
  `new_agent_message()` is **sync** — it only *creates* a `Message` object and
  does not enqueue it. To stream a progress event use the two-step pattern:
  `msg = updater.new_agent_message([Part(...)])` then
  `await updater.update_status(TaskState.working, message=msg)`.
  Calling without `await` silently drops async calls (coroutine never executed), so the artifact
  is never enqueued and the client stream receives no verdict.
- `src/a2a/operational_a2a_clients.py` — Three A2A client wrappers
  (`CostAgentA2AClient`, `MonitoringAgentA2AClient`, `DeployAgentA2AClient`)
  using `A2ACardResolver(httpx_client=..., base_url=...)` + `A2AClient` with
  `httpx.AsyncClient`. `agent_card_url=self._server_url` (was `""` — now a
  real URL). `httpx_client=` keyword is required by a2a-sdk==0.3.24 (was
  incorrectly `http_client=`). SSE event unwrapping: `A2AClient` yields
  `SendStreamingMessageResponse`; the actual event is at `.root.result` —
  code now uses `result = getattr(root, "result", root)` before isinstance
  checks (was checking `.root` directly, so TaskArtifactUpdateEvent was
  never reached and verdict_json was always None).
- `src/a2a/agent_registry.py` — Tracks connected agents with governance stats
  (approval/denial/escalation counts). JSON mock in `data/agents/`, Cosmos DB
  container `governance-agents` (partition key `/name`) in live mode.
  `cosmos_key` is now resolved **before** the `_is_mock` decision (mirrors
  `CosmosDecisionClient`): `_is_mock = use_local_mocks or not endpoint or not key`.
  Previously the key was resolved inside the `try` block but not checked — so the
  registry entered live mode with an empty key, then `_save_entry()` failed with
  no try/except and agents were silently dropped.
- `src/api/dashboard_api.py` — New endpoints: `GET /api/agents`,
  `GET /api/agents/{name}/history`. Agent history pre-fetch raised to
  `limit=1000` (was 200). Internal: `get_recent(limit=10_000)` used for
  `/api/evaluations/{id}` and `/api/metrics` (was `_load_all()` which does
  not exist on `DecisionTracker`; that caused 500 errors on both endpoints).
- `infrastructure/terraform-core/main.tf` — `governance-agents` Cosmos container
  added alongside `governance-decisions`.
- `demo_a2a.py` — End-to-end A2A demo: server in background thread, 3
  scenarios (DENIED / APPROVED / ESCALATED), agent registry summary.
  `os.environ.setdefault("USE_LOCAL_MOCKS", "true")` removed — demo now
  reads `USE_LOCAL_MOCKS` from `.env` like every other process (setdefault
  was silently overriding `.env` because it always ran before dotenv loading).
- **Test result: 420 passed, 10 xfailed, 0 failed** ✅
  (17 previously-xfailed dashboard tests promoted to passing after `_load_all`
  fix; 10 remaining xfails were `TestRecord` tests about `tracker._dir` —
  fully fixed in Phase 19 post-work: `tracker._dir` → `tracker._cosmos._decisions_dir`.)
- MCP and direct Python pipeline continue to work unchanged.

**Previous: Phase 9 — Async-First Refactor**

- All 7 agent `evaluate()`/`scan()` methods are `async def` — safe to `await` from FastAPI, MCP, and async tests.
- `pipeline.py` uses `asyncio.gather()` (replaced `ThreadPoolExecutor`) — all 4 governance agents run
  concurrently in the same event loop; no nested `asyncio.run()` calls anywhere.
- Auth: `DefaultAzureCredential` + `get_bearer_token_provider` → `AsyncAzureOpenAI` (works for both
  `az login` locally and Managed Identity in Azure). Responses API: `api_version="2025-03-01-preview"`.
- `_use_framework = not use_local_mocks and bool(azure_openai_endpoint)` — mock mode skips the
  framework path; `except Exception` fallback handles live-mode failures gracefully.
- `requirements.txt`: `agent-framework-core==1.0.0rc2` (pinned exact version).
- 27 pre-existing test failures (Phase 7 Cosmos migration) marked `@pytest.mark.xfail`.
- **Test result: 361 passed, 27 xfailed, 0 failed.**
- `USE_LOCAL_MOCKS=false` is the default — live Azure services are active.
- `DecisionTracker` delegates to `CosmosDecisionClient` — decisions persist to Cosmos DB.
- `HistoricalPatternAgent` uses `AzureSearchClient` (BM25) in live mode; keyword matching in mock.

**Previous: Phase 8 — Microsoft Agent Framework SDK (via Phase 9)**

- All 4 governance agents + all 3 operational agents rebuilt on `agent-framework-core`.
- Each agent defines its rule-based logic as an `@af.tool`; gpt-5-mini calls the tool and synthesises reasoning.
- `deploy_agent.py` added: proposes NSG deny-all rules, lifecycle tag additions, observability resources.
- `pipeline.py` exposes `scan_operational_agents()` running all 3 operational agents concurrently.

## Coding Standards
- Python 3.11+
- Use type hints everywhere
- Use Pydantic models from src/core/models.py for all inputs/outputs
- Governance agents use **async** `evaluate()` — always `async def evaluate(self, action: ProposedAction)`
- Pipeline calls all agents via `asyncio.gather()` — never wrap in `asyncio.run()` from inside a coroutine
- Return the appropriate Pydantic result model (e.g., PolicyResult, BlastRadiusResult)
- Keep code clean with docstrings
- Follow existing patterns in models.py

## Testing
- Tests go in tests/ directory
- Use pytest with pytest-asyncio
- Run tests: `pytest tests/ -v`
- Test each agent independently with mock ProposedAction data

## Git Commits
Use conventional commits:
- `feat(policy): implement PolicyComplianceAgent`
- `feat(engine): implement SRI Composite scoring`
- `test(policy): add unit tests for policy evaluation`
- `fix(blast-radius): handle missing dependencies gracefully`

## What NOT To Do
- Do NOT make tests depend on live Azure APIs (keep tests mock-first and deterministic)
- Do NOT install new dependencies without checking requirements.txt first
- Do NOT modify src/core/models.py without asking (it's the shared contract)
- Do NOT use print() for logging — use Python's logging module
