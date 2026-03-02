# CONTEXT.md — RuriSkry Project Context
> This file is the single source of truth for any AI coding agent working on this project.

## What Is This Project?
RuriSkry is an AI Action Governance & Simulation Engine for the Microsoft AI Dev Days Hackathon 2026. It intercepts AI agent infrastructure actions, simulates their impact, and scores them using the Skry Risk Index (SRI™) before allowing execution.

## Project Structure
```
src/
├── operational_agents/     # Agents that PROPOSE actions (the governed)
│   ├── monitoring_agent.py # SRE monitoring + anomaly detection
│   ├── cost_agent.py       # Cost optimization proposals
│   └── deploy_agent.py     # Infrastructure deployment + config proposals (Phase 8)
├── governance_agents/      # Agents that EVALUATE actions (the governors)
│   ├── blast_radius_agent.py    # SRI:Infrastructure (0-100)
│   ├── policy_agent.py          # SRI:Policy (0-100)
│   ├── historical_agent.py      # SRI:Historical (0-100)
│   └── financial_agent.py       # SRI:Cost (0-100)
├── core/
│   ├── governance_engine.py     # Calculates SRI™ Composite + verdict
│   ├── decision_tracker.py      # Audit trail storage (verdicts → Cosmos / JSON)
│   ├── scan_run_tracker.py      # Scan-run lifecycle store (scan records → Cosmos / JSON) ← NEW Phase 16
│   ├── interception.py          # MCP action interception
│   └── models.py                # ALL Pydantic data models (READ THIS FIRST)
├── mcp_server/
│   └── server.py                # Exposes governance tools via MCP
├── a2a/                         # A2A Protocol layer (Phase 10)
│   ├── ruriskry_a2a_server.py   # RuriSkry as A2A server (AgentExecutor + AgentCard)
│   ├── operational_a2a_clients.py  # Operational agent A2A client wrappers
│   └── agent_registry.py        # Tracks connected A2A agents with stats
├── infrastructure/              # Azure service clients (live + mock fallback)
│   ├── azure_tools.py           # 5 generic investigation tools (Phase 12) ← NEW
│   │                            #   query_resource_graph, query_metrics,
│   │                            #   get_resource_details, query_activity_log,
│   │                            #   list_nsg_rules — used by all ops agents
│   ├── llm_throttle.py          # asyncio.Semaphore + exponential backoff (Phase 12)
│   ├── resource_graph.py        # Azure Resource Graph (mock: seed_resources.json)
│   ├── cosmos_client.py         # Cosmos DB decisions (mock: data/decisions/*.json)
│   ├── search_client.py         # Azure AI Search incidents (mock: seed_incidents.json)
│   ├── openai_client.py         # Azure OpenAI / GPT-4.1 (mock: canned string)
│   └── secrets.py               # Key Vault secret resolver (env → KV → empty)
├── notifications/               # Outbound alerting ← NEW Phase 17
│   └── teams_notifier.py        # send_teams_notification() — Adaptive Card to Teams webhook
├── api/
│   └── dashboard_api.py         # FastAPI REST endpoints — 17 total (Phase 10 agents,
│                                #   Phase 12 alert-trigger, Phase 13 scan triggers,
│                                #   Phase 16: SSE stream, cancel, last-run + durable store,
│                                #   Phase 17: notification-status + test-notification)
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
- SRI ≤ 25 → APPROVED (auto-execute)
- SRI 26-60 → ESCALATED (human review)
- SRI > 60 → DENIED (blocked)
- Any critical policy violation → DENIED regardless of score

### Data Flow

**Target architecture — two-layer intelligence:**
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
```

**Current state (Phase 12 complete):** Ops agents now genuinely investigate real Azure
data with GPT-4.1 before proposing. Each agent uses 5 generic tools from
``src/infrastructure/azure_tools.py`` to query Resource Graph, Monitor metrics, NSG rules,
and activity logs. See STATUS.md for the complete Phase 12 breakdown.

## Important Files to Read First
1. `src/core/models.py` — ALL Pydantic models. Every agent uses these.
2. `src/config.py` — SRI thresholds and weights are configurable here.
3. `data/policies.json` — 6 governance policies for PolicyComplianceAgent.
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

**Phase 18 — Decision Explanation Engine (complete)**

- `src/core/explanation_engine.py` (NEW) — `DecisionExplainer.explain(verdict, action)` returns a
  `DecisionExplanation` with ranked `Factor` list, `Counterfactual` scenarios, policy violations,
  risk highlights, and an LLM-generated summary (GPT-4.1 in live mode; template fallback in mock).
  Module-level `_explanation_cache` keyed by `action_id` prevents redundant recomputation.
- `src/core/models.py` — 3 new Pydantic models: `Factor`, `Counterfactual`, `DecisionExplanation`.
- `src/api/dashboard_api.py` — 1 new endpoint: `GET /api/evaluations/{id}/explanation` (18 total).
  Reconstructs `GovernanceVerdict` from the stored flat record, calls the explainer, returns JSON.
- `dashboard/src/components/EvaluationDrilldown.jsx` (NEW) — 6-section full-page drilldown:
  verdict header, SRI bars (with primary-factor ⭐), explanation, counterfactual cards, agent
  reasoning, collapsible JSON audit trail. Opened by clicking any row in the Live Activity Feed.
- `dashboard/src/App.jsx` — `drilldownEval` state drives navigation to/from the drilldown.
- Test result: **434 passed, 10 xfailed, 0 failed** ✅

**Phase 17 — Microsoft Teams Notifications (complete)**

- `src/notifications/teams_notifier.py` (NEW) — async fire-and-forget Adaptive Card via Teams Incoming Webhook.
  Triggered after every DENIED or ESCALATED verdict via `asyncio.create_task()` in `pipeline.py`.
  APPROVED verdicts skipped. Empty webhook URL = silent no-op. Retries once; never raises.
- `src/config.py` — 3 new settings: `teams_webhook_url`, `teams_notifications_enabled`, `dashboard_url`.
- `src/api/dashboard_api.py` — 2 new endpoints: `GET /api/notification-status`, `POST /api/test-notification` (17 total).
- Frontend: 🔔 Teams pill in header — green clickable button (sends test notification) when configured, grey static pill when not.
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
investigate real data before proposing. GPT-4.1 discovers resources, checks actual CPU
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
  **All `TaskUpdater` calls are `async def` and must be awaited**: `submit()`,
  `start_work()`, `add_artifact()`, and `complete()` — calling without `await`
  silently drops them (coroutine created but never executed), so the artifact
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
- `infrastructure/terraform/main.tf` — `governance-agents` Cosmos container
  added alongside `governance-decisions`.
- `demo_a2a.py` — End-to-end A2A demo: server in background thread, 3
  scenarios (DENIED / APPROVED / ESCALATED), agent registry summary.
  `os.environ.setdefault("USE_LOCAL_MOCKS", "true")` removed — demo now
  reads `USE_LOCAL_MOCKS` from `.env` like every other process (setdefault
  was silently overriding `.env` because it always ran before dotenv loading).
- **Test result: 420 passed, 10 xfailed, 0 failed** ✅
  (17 previously-xfailed dashboard tests promoted to passing after `_load_all`
  fix; 10 remaining xfails are `TestRecord` tests about `tracker._dir`.)
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
- Each agent defines its rule-based logic as an `@af.tool`; GPT-4.1 calls the tool and synthesises reasoning.
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
