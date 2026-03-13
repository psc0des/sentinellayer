# RuriSkry API Reference

## MCP Tools

Exposed via `src/mcp_server/server.py` (FastMCP stdio transport).
Start with: `python -m src.mcp_server.server`

---

### `skry_evaluate_action`

Evaluate a proposed infrastructure action through the full RuriSkry governance pipeline.
Runs all 4 SRI™ agents concurrently (`asyncio.gather`), records the verdict, and returns it.

**Input parameters (flat JSON — not nested):**

| Parameter | Type | Required | Description |
|---|---|---|---|
| `resource_id` | string | ✅ | Azure resource ID or short name (e.g. `"vm-23"`) |
| `resource_type` | string | ✅ | Azure resource type (e.g. `"Microsoft.Compute/virtualMachines"`) |
| `action_type` | string | ✅ | One of: `scale_up`, `scale_down`, `delete_resource`, `restart_service`, `modify_nsg`, `create_resource`, `update_config` |
| `agent_id` | string | ✅ | ID of the proposing agent (e.g. `"cost-optimization-agent"`) |
| `reason` | string | ✅ | Human-readable justification for the action |
| `urgency` | string | — | `low` \| `medium` \| `high` \| `critical` (default: `medium`) |
| `current_monthly_cost` | float | — | Current monthly cost in USD |
| `current_sku` | string | — | Current VM/resource SKU |
| `proposed_sku` | string | — | New SKU after the action |
| `nsg_change_direction` | string | — | `"open"` if the change broadens inbound access (triggers CRITICAL POL-SEC-002 check). `"restrict"` or omit if remediating/restricting access. Only relevant for `modify_nsg` actions. |

**Example input:**
```json
{
  "resource_id": "/subscriptions/demo/resourceGroups/prod/providers/Microsoft.Compute/virtualMachines/vm-23",
  "resource_type": "Microsoft.Compute/virtualMachines",
  "action_type": "delete_resource",
  "agent_id": "cost-optimization-agent",
  "reason": "VM idle for 30 days — estimated savings $847/month",
  "urgency": "high",
  "current_monthly_cost": 847.0
}
```

**Output:**
```json
{
  "action_id": "3f8a1c2d-...",
  "timestamp": "2026-02-26T12:00:00+00:00",
  "decision": "denied",
  "reason": "DENIED — Critical policy violation: POL-DR-001 (disaster-recovery protected resource). SRI Composite 77.0 exceeds threshold 60.",
  "sri_composite": 77.0,
  "sri_breakdown": {
    "infrastructure": 65.0,
    "policy": 100.0,
    "historical": 62.0,
    "cost": 45.0
  },
  "thresholds": {
    "auto_approve": 25,
    "human_review": 60
  },
  "resource_id": "vm-23",
  "agent_id": "cost-optimization-agent"
}
```

**Decision values:** `"approved"` | `"escalated"` | `"denied"`

---

### `skry_query_history`

Return recent governance decisions from the audit trail (Cosmos DB in live mode, local JSON in mock mode).

**Input parameters:**

| Parameter | Type | Required | Description |
|---|---|---|---|
| `limit` | int | — | Max records to return (1–100, default 10) |
| `resource_id` | string | — | Filter by resource ID substring |

**Example output:**
```json
{
  "count": 2,
  "decisions": [
    {
      "action_id": "3f8a1c2d-...",
      "timestamp": "2026-02-26T12:00:00+00:00",
      "decision": "denied",
      "sri_composite": 77.0,
      "resource_id": "vm-23",
      "action_type": "delete_resource",
      "agent_id": "cost-optimization-agent",
      "violations": ["POL-DR-001"]
    }
  ]
}
```

---

### `skry_get_risk_profile`

Return an aggregated risk summary for a specific resource across all historical evaluations.

**Input parameters:**

| Parameter | Type | Required | Description |
|---|---|---|---|
| `resource_id` | string | ✅ | Short name or partial Azure resource ID |

**Example output:**
```json
{
  "resource_id": "vm-23",
  "total_evaluations": 3,
  "decisions": {"approved": 0, "escalated": 1, "denied": 2},
  "avg_sri_composite": 74.3,
  "max_sri_composite": 77.0,
  "top_violations": ["POL-DR-001", "POL-CHANGE-001"],
  "last_evaluated": "2026-02-26T12:00:00+00:00"
}
```

---

## Dashboard REST API

Served by `src/api/dashboard_api.py` (FastAPI).
Start with: `uvicorn src.api.dashboard_api:app --reload`

All endpoints are `async def` (FastAPI manages the event loop).

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/api/evaluations` | List recent governance decisions (newest-first) |
| GET | `/api/evaluations/{evaluation_id}` | Full record for one evaluation by UUID |
| GET | `/api/metrics` | Aggregate stats: decision counts, SRI avg/min/max, top violations, triage tier distribution |
| GET | `/api/resources/{resource_id}/risk` | Risk profile for one resource |
| GET | `/api/agents` | List operational agents connected via A2A |
| GET | `/api/agents/{agent_name}/history` | Recent decisions for one A2A agent |
| GET | `/api/agents/{agent_name}/last-run` | Most recent completed scan for one agent |
| GET | `/api/notification-status` | Teams webhook configuration status |
| POST | `/api/test-notification` | Send a sample DENIED Adaptive Card to the configured Teams webhook |
| POST | `/api/alert-trigger` | Webhook — trigger async alert investigation from Azure Monitor |
| GET | `/api/alerts` | List all alert records newest-first |
| GET | `/api/alerts/active-count` | Count of currently firing/investigating alerts |
| GET | `/api/alerts/{alert_id}/status` | Full detail for one alert |
| GET | `/api/alerts/{alert_id}/stream` | SSE stream of real-time investigation progress |
| POST | `/api/scan/cost` | Start a background cost agent scan |
| POST | `/api/scan/monitoring` | Start a background monitoring agent scan |
| POST | `/api/scan/deploy` | Start a background deploy agent scan |
| POST | `/api/scan/all` | Start background scans for all three agents |
| GET | `/api/scan-history` | List all scan runs newest-first — operational audit log (one record per scan execution) |
| GET | `/api/scan/{scan_id}/status` | Poll the status and results of a background scan |
| GET | `/api/scan/{scan_id}/stream` | SSE stream of real-time scan progress events |
| PATCH | `/api/scan/{scan_id}/cancel` | Request cancellation of a running scan |
| GET | `/api/evaluations/{evaluation_id}/explanation` | Full decision explanation with counterfactual analysis |
| GET | `/api/execution/pending-reviews` | List ESCALATED verdicts awaiting human review |
| GET | `/api/execution/by-action/{action_id}` | Execution status for a verdict |
| POST | `/api/execution/{execution_id}/approve` | Human approves an escalated verdict |
| POST | `/api/execution/{execution_id}/dismiss` | Human dismisses a verdict |
| POST | `/api/execution/{execution_id}/create-pr` | Create Terraform PR from a `manual_required` record |
| GET | `/api/execution/{execution_id}/agent-fix-preview` | Generate LLM-driven execution plan (steps, summary, impact, rollback, backward-compat `commands`) |
| POST | `/api/execution/{execution_id}/agent-fix-execute` | Execute fix via Azure Python SDK (`azure.mgmt.network/compute/resource`) using `DefaultAzureCredential` |
| POST | `/api/execution/{execution_id}/rollback` | Roll back an agent-applied fix (status must be `applied`); sets status → `rolled_back`, stores `rollback_log` |
| GET | `/api/execution/{execution_id}/terraform` | Generate Terraform HCL fix for a `manual_required` or `pr_created` execution record |
| GET | `/api/config` | Safe system configuration — mode, timeouts, feature flags (no secrets) |
| POST | `/api/admin/reset` | ⚠ Dev/test only — wipe all local JSON data and reset in-memory state |

### Query parameters for `GET /api/evaluations`

| Parameter | Type | Default | Description |
|---|---|---|---|
| `limit` | int | 20 | Max records (1–100) |
| `resource_id` | string | — | Substring filter on resource ID |

### `GET /api/metrics` response shape

```json
{
  "total_evaluations": 4,
  "decisions": {"approved": 2, "escalated": 1, "denied": 1},
  "decision_percentages": {"approved": 50.0, "escalated": 25.0, "denied": 25.0},
  "sri_composite": {"avg": 42.1, "min": 14.1, "max": 77.0},
  "sri_dimensions": {
    "avg_infrastructure": 38.5,
    "avg_policy": 51.2,
    "avg_historical": 40.0,
    "avg_cost": 29.3
  },
  "top_violations": [
    {"policy_id": "POL-DR-001", "count": 1}
  ],
  "most_evaluated_resources": [
    {"resource_id": "web-tier-01", "count": 3}
  ],
  "triage": {
    "tier_counts": {"tier_1": 2, "tier_2": 1, "tier_3": 1, "unknown": 0},
    "tier_percentages": {"tier_1": 50.0, "tier_2": 25.0, "tier_3": 25.0, "unknown": 0.0},
    "llm_calls_saved": 8,
    "deterministic_evaluations": 2,
    "full_evaluations": 2
  },
  "executions": {
    "total": 5,
    "applied": 2,
    "failed": 0,
    "pr_created": 1,
    "dismissed": 1,
    "pending": 1,
    "agent_fix_rate": 100.0,
    "success_rate": 100.0
  }
}
```

`triage.llm_calls_saved` = `tier_1 count × 4` — each Tier 1 action skips all four governance
agent LLM calls. Short-circuiting is active as of Phase 27A.

`triage.deterministic_evaluations` = count of verdicts where `triage_mode == "deterministic"` (Tier 1 short-circuit, no LLM).

`triage.full_evaluations` = count of verdicts where `triage_mode == "full"` (Tier 2/3, LLM engaged).

Records that pre-date Phase 26 have `triage_tier: null` and are counted under `unknown`. Records
that pre-date Phase 27A have `triage_mode: null` and are not counted in either mode bucket.
```

---

## A2A Agent Endpoints (Phase 10)

Added to `src/api/dashboard_api.py`.

### `GET /api/agents`

List all operational agents connected to RuriSkry via the A2A protocol,
sorted by most-recently-seen first.

**Response:**
```json
{
  "count": 3,
  "agents": [
    {
      "name": "cost-optimization-agent",
      "agent_card_url": "http://localhost:8000",
      "registered_at": "2026-02-27T10:00:00Z",
      "last_seen": "2026-02-27T10:05:00Z",
      "total_actions_proposed": 5,
      "approval_count": 2,
      "denial_count": 2,
      "escalation_count": 1
    }
  ]
}
```

---

### `GET /api/agents/{agent_name}/history`

Return recent governance decisions for one A2A agent.

**Path parameter:** `agent_name` — e.g. `cost-optimization-agent`.

**Query parameters:**

| Parameter | Type | Default | Description |
|---|---|---|---|
| `limit` | int | 10 | Max records (1–100) |

**Response:**
```json
{
  "agent": { "name": "cost-optimization-agent", "total_actions_proposed": 5, ... },
  "history_count": 3,
  "history": [ { "action_id": "...", "decision": "denied", ... } ]
}
```

Returns **404** if the agent is not registered.

---

### `POST /api/alert-trigger`

Webhook endpoint for Azure Monitor Action Groups. When an alert fires, Azure POSTs
the alert payload here. RuriSkry creates an alert record (status `firing`), launches
the investigation in the background, and returns immediately so the webhook does not
time out. Duplicate alerts for the same `resource_id + metric` while one is still
`firing` or `investigating` return the existing `alert_id` with `"duplicate": true`.

**Request body — three accepted formats:**

1. **Azure Monitor Common/Non-common Alert Schema** (sent by Azure Monitor Action Groups):
```json
{
  "schemaId": "azureMonitorCommonAlertSchema",
  "data": {
    "essentials": { "alertRule": "alert-vm-dr-01-heartbeat", "severity": "Sev2",
                    "firedDateTime": "...", "alertTargetIDs": ["...workspace..."],
                    "configurationItems": [], "description": "vm-dr-01 heartbeat lost" },
    "alertContext": { "conditionType": "LogQueryCriteria", "condition": {} }
  }
}
```

2. **Flat format** (direct API calls / testing):
```json
{
  "resource_id": "/subscriptions/.../virtualMachines/vm-web-01",
  "metric": "Percentage CPU",
  "value": 95.0,
  "threshold": 80.0,
  "severity": "3",
  "resource_group": "ruriskry-prod-rg"
}
```

All fields are optional. The endpoint automatically normalises both formats via `_normalize_azure_alert_payload()`.

> **Log Alerts V2 workspace pivot:** Azure Monitor Log Alerts V2 always reports the Log Analytics *workspace* as `alertTargetID`, not the monitored VM — even when the query monitors a specific VM. The normalizer detects this (resource type `operationalinsights/workspaces`) and regex-extracts the actual affected resource name from `essentials.description` or `alertRule`, then constructs the correct VM ARM ID. This ensures the MonitoringAgent always investigates the right resource deterministically.

**Response:**
```json
{ "status": "firing", "alert_id": "a1b2c3d4-..." }
```

The background investigation runs: `MonitoringAgent.scan(alert_payload)` → governance
pipeline → `ExecutionGateway.process_verdict()` → verdicts recorded with `execution_id` +
`execution_status` per finding. Subscribe to `GET /api/alerts/{alert_id}/stream` for
real-time SSE updates, or poll `GET /api/alerts/{alert_id}/status`.

---

### `GET /api/alerts`

List all alert records newest-first. Powers the Alerts tab on the dashboard.

**Query parameters:**

| Parameter | Type | Default | Description |
|---|---|---|---|
| `limit` | int | 50 | Max records (1–500) |

**Response:**
```json
{
  "count": 2,
  "alerts": [
    {
      "alert_id": "a1b2c3d4-...",
      "status": "resolved",
      "resource_id": "/subscriptions/.../vm-web-01",
      "resource_name": "vm-web-01",
      "metric": "Percentage CPU",
      "value": 95.0,
      "threshold": 80.0,
      "severity": "3",
      "fired_at": "2026-03-11T12:00:00+00:00",
      "received_at": "2026-03-11T12:00:01+00:00",
      "investigating_at": "2026-03-11T12:00:01+00:00",
      "resolved_at": "2026-03-11T12:00:45+00:00",
      "proposals_count": 1,
      "totals": { "approved": 1, "escalated": 0, "denied": 0 },
      "proposals": [...],
      "verdicts": [
        {
          "decision": "approved",
          "skry_risk_index": { "sri_composite": 8.0, "sri_infrastructure": 5.0, ... },
          "execution_id": "e5f6g7h8-...",
          "execution_status": "manual_required"
        }
      ],
      "description": "Detects when vm-dr-01 stops sending heartbeats",
      "alert_rule_name": "alert-vm-dr-01-heartbeat"
    }
  ]
}
```

Each entry in `verdicts[]` includes `execution_id` and `execution_status` from the
ExecutionGateway — these power the action buttons (📝 Terraform PR, 🤖 Fix by Agent,
🌐 Azure Portal, ✕ Ignore) shown in the Alerts tab drilldown panel for each finding.

Merges in-memory (active) and durable (historical) records. Cosmos internal fields stripped.

---

### `GET /api/alerts/active-count`

Quick count of non-resolved alerts (status `firing` or `investigating`).
Used by the sidebar badge to show a live count without fetching all alert data.

**Response:**
```json
{ "active_count": 2 }
```

---

### `GET /api/alerts/{alert_id}/status`

Return the full alert record for one alert. Returns 404 if not found.

**Response:** Same shape as one element of the `alerts` array in `GET /api/alerts`.

---

### `GET /api/alerts/{alert_id}/stream`

SSE stream for real-time investigation progress. Identical pattern to
`GET /api/scan/{scan_id}/stream` — one JSON event per line, heartbeat every 30s,
terminates on `alert_resolved` or `alert_error`.

**Events:** `alert_investigating`, `reasoning`, `discovery`, `evaluation`, `verdict`,
`execution`, `alert_resolved`, `alert_error`.

Returns 404 if no active SSE stream exists for the given alert_id.

---

### Scan Trigger Endpoints (Phase 13)

Start background agent scans without blocking the HTTP response. Returns immediately with
a `scan_id`; poll `GET /api/scan/{scan_id}/status` to track progress.

**Common request body (all POST scan endpoints):**
```json
{ "resource_group": "ruriskry-prod-rg" }
```
`resource_group` is optional — omit or send `null` to use the `DEFAULT_RESOURCE_GROUP`
config value (itself defaulting to `null` = whole subscription).
Precedence: body `resource_group` → `DEFAULT_RESOURCE_GROUP` env var → whole subscription.
Empty body `{}` is also accepted.

**Common response:**
```json
{ "status": "started", "scan_id": "b3e7c1a2-...", "agent_type": "cost" }
```
`POST /api/scan/all` returns `scan_ids` (array) instead of `scan_id`.

| Endpoint | Agent triggered |
|---|---|
| `POST /api/scan/cost` | `CostOptimizationAgent` |
| `POST /api/scan/monitoring` | `MonitoringAgent` |
| `POST /api/scan/deploy` | `DeployAgent` |
| `POST /api/scan/all` | All three, as independent background tasks |

---

### `GET /api/scan/{scan_id}/status`

Poll a background scan started by one of the scan trigger endpoints.

**Response (in progress):**
```json
{
  "scan_id": "b3e7c1a2-...",
  "status": "running",
  "agent_type": "cost",
  "resource_group": "ruriskry-prod-rg",
  "started_at": "2026-03-01T10:00:00+00:00"
}
```

**Response (complete):**
```json
{
  "scan_id": "b3e7c1a2-...",
  "status": "complete",
  "agent_type": "cost",
  "resource_group": "ruriskry-prod-rg",
  "started_at": "2026-03-01T10:00:00+00:00",
  "completed_at": "2026-03-01T10:00:12+00:00",
  "proposals_count": 2,
  "evaluations_count": 2,
  "proposals": [...],
  "evaluations": [...]
}
```

Returns **404** only if the `scan_id` is completely unknown. Scan records are persisted by
`ScanRunTracker` (Cosmos DB / local JSON), so status survives server restarts.

---

### `GET /api/scan/{scan_id}/stream` (Phase 16)

Stream real-time scan progress as Server-Sent Events (SSE).

Connect with the browser's native `EventSource` API:
```javascript
const es = new EventSource(`http://localhost:8000/api/scan/${scanId}/stream`)
es.onmessage = (e) => console.log(JSON.parse(e.data))
```

Each event is a JSON object with at minimum `event` (type string) and `timestamp`. The stream
terminates when a `scan_complete` or `scan_error` event arrives. Events emitted before the
client connects are buffered in the queue and delivered immediately on connection.

**Event types:**

| Event | Icon | When emitted |
|---|---|---|
| `scan_started` | 🚀 | Scan begins |
| `discovery` | 🔍 | Agent returned proposals list |
| `analysis` | 🧠 | Starting evaluation for one proposal |
| `reasoning` | 🤔 | Agent's reason for the proposal |
| `proposal` | 📋 | Proposing the action |
| `evaluation` | ⚖️ | Pipeline evaluating the action |
| `verdict` | ✅/⚠️/🚫 | Verdict returned (with `decision` and `sri_composite`) |
| `persisted` | 💾 | Verdict written to audit trail |
| `scan_complete` | ✔️ | All proposals evaluated |
| `scan_error` | ❌ | Unhandled exception or user cancellation |

If the scan is already complete when the client connects, a synthetic terminal event is returned
immediately. Returns **404** if `scan_id` is unknown.

---

### `PATCH /api/scan/{scan_id}/cancel` (Phase 16)

Request cancellation of a running scan. The background task checks the cancellation flag before
each proposal evaluation and stops cleanly at the next checkpoint. The persisted status is
set to `"cancelled"`.

Returns **404** if the scan_id is not found.
Returns **400** if the scan is not currently running.

**Response:**
```json
{ "status": "cancellation_requested", "scan_id": "b3e7c1a2-..." }
```

---

### `GET /api/scan-history`

Return all scan-run records newest-first. This is the **operational audit log** — one record
per scan execution (not per governance verdict). Powers the Audit Log tab in the dashboard.
Use `GET /api/evaluations` for governance-verdict-level data.

**Query parameters:**

| Parameter | Type | Default | Description |
|---|---|---|---|
| `limit` | int | 50 | Max records (1–500) |

**Response:**
```json
{
  "count": 3,
  "scans": [
    {
      "scan_id": "b3e7c1a2-...",
      "agent_type": "deploy",
      "status": "complete",
      "started_at": "2026-03-11T12:00:00+00:00",
      "completed_at": "2026-03-11T12:00:45+00:00",
      "proposals_count": 2,
      "evaluations_count": 2,
      "scanned_resources_count": 34,
      "totals": { "approved": 1, "escalated": 0, "denied": 1 },
      "scanned_resources": [
        { "id": "/subscriptions/.../resourceGroups/rg/providers/Microsoft.Compute/virtualMachines/vm1", "name": "vm1", "type": "Microsoft.Compute/virtualMachines", "location": "eastus" }
      ],
      "proposed_actions": [...],
      "evaluations": [...],
      "scan_error": null
    }
  ]
}
```

Each scan record includes:
- `scanned_resources[]` — **full list of every Azure resource in scope at scan start time**, snapshotted via `ResourceGraphClient.list_all_async()`. This is what powers the Audit Log drilldown's "X resources examined" view. Each entry has `{id, name, type, location}`.
- `scanned_resources_count` — precomputed length of `scanned_resources[]` for fast table rendering.
- `proposed_actions[]` — only resources the agent flagged for governance review (subset of scanned_resources).
- `evaluations[]` — full governance verdict per proposal.

Cosmos internal fields (`_rid`, `_etag`, etc.) are stripped. Mock mode reads from
`data/scans/*.json`; live mode queries Cosmos DB with `ORDER BY c.started_at DESC`.

---

### `GET /api/notification-status` (Phase 17)

Return the current Teams notification configuration status. The dashboard header uses this
to render the 🔔 Teams indicator pill.

**Response:**
```json
{
  "teams_configured": true,
  "teams_enabled": true
}
```

`teams_configured` is `true` when `TEAMS_WEBHOOK_URL` is non-empty.
`teams_enabled` reflects `TEAMS_NOTIFICATIONS_ENABLED` (default `true`).

---

### `POST /api/test-notification` (Phase 17)

Send a sample DENIED Adaptive Card to the configured Teams webhook. Useful for judges to
verify the Teams integration works without running a full governance evaluation.

Returns immediately if no webhook is configured.

**Response (sent):**
```json
{ "status": "sent" }
```

**Response (skipped):**
```json
{ "status": "skipped", "reason": "TEAMS_WEBHOOK_URL not configured" }
```

**Response (failed):**
```json
{ "status": "failed" }
```

The sample card shows a realistic DENIED verdict for `vm-dr-01` with SRI 77.0 and POL-DR-001
violation — identical format to real governance notifications.

---

### `GET /api/evaluations/{evaluation_id}/explanation` (Phase 18)

Return a full `DecisionExplanation` for one governance evaluation. The dashboard drilldown
calls this endpoint when a row in the Live Activity Feed is clicked.

**Path parameter:**
- `evaluation_id` — the `action_id` UUID from the governance verdict.

**Response:**
```json
{
  "summary": "This action was DENIED due to a critical policy violation (POL-DR-001) combined with high infrastructure blast radius score of 65.0.",
  "primary_factor": "Policy Compliance — critical policy violation POL-DR-001",
  "contributing_factors": [
    {
      "dimension": "Policy Compliance",
      "score": 95.0,
      "weight": 0.25,
      "weighted_contribution": 23.75,
      "reasoning": "POL-DR-001 matched (critical); auto-deny triggered."
    },
    {
      "dimension": "Infrastructure (Blast Radius)",
      "score": 65.0,
      "weight": 0.30,
      "weighted_contribution": 19.5,
      "reasoning": "Resource has 3 dependents; restart impact: high."
    }
  ],
  "policy_violations": ["POL-DR-001: Disaster-recovery VMs must not be deleted"],
  "risk_highlights": [
    "Critical policy violation auto-denied this action.",
    "3 dependent resources would be impacted."
  ],
  "counterfactuals": [
    {
      "change_description": "If the top policy violation were resolved",
      "predicted_new_score": 53.1,
      "predicted_new_verdict": "ESCALATED",
      "explanation": "Removing the critical violation drops the policy score from 95 → 55. Composite falls below the 60-point deny threshold."
    }
  ]
}
```

Returns `404` if the `evaluation_id` is not found in the audit trail.

---

### `GET /api/agents/{agent_name}/last-run` (Phase 16)

Return the most recent completed scan results for one agent. Prefers the durable scan store
(`ScanRunTracker`) so results survive server restarts; falls back to the audit trail.

**Response:**
```json
{
  "source": "scan_tracker",
  "scan_id": "b3e7c1a2-...",
  "status": "complete",
  "scan_error": null,
  "agent_type": "cost",
  "started_at": "2026-03-02T10:00:00+00:00",
  "completed_at": "2026-03-02T10:00:15+00:00",
  "proposals_count": 2,
  "evaluations_count": 2,
  "proposed_actions": [...],
  "evaluations": [...],
  "totals": { "approved": 1, "escalated": 0, "denied": 1 }
}
```

`source` is `"scan_tracker"` if found in durable store, `"tracker"` if from audit trail only.
`status` is `"complete"` for successful scans, `"error"` when the LLM/agent framework timed out or threw (in which case `scan_error` contains the error message and `proposals_count`/`evaluations_count` will be 0).
Unknown agent names return an empty `no_data` response (not 404).

---

## Execution Gateway Endpoints

These endpoints manage the IaC-safe execution lifecycle for governance verdicts.

### `GET /api/execution/by-action/{action_id}`

Get the execution status for a governance verdict.

**Path parameter:** `action_id` — the UUID from the governance verdict.

> **Note:** This route uses `/by-action/` prefix (not `/{action_id}` directly)
> to prevent FastAPI from shadowing the static `/pending-reviews` route.

**Response:**
```json
{
  "action_id": "c68c25ca-...",
  "executions": [
    {
      "execution_id": "a1b2c3d4-...",
      "action_id": "c68c25ca-...",
      "verdict": "approved",
      "status": "pr_created",
      "iac_managed": true,
      "iac_tool": "terraform",
      "iac_repo": "psc0des/ruriskry",
      "iac_path": "infrastructure/terraform-prod",
      "pr_url": "https://github.com/psc0des/ruriskry/pull/42",
      "pr_number": 42,
      "reviewed_by": "",
      "created_at": "2026-03-05T12:00:00+00:00",
      "updated_at": "2026-03-05T12:00:05+00:00",
      "notes": ""
    }
  ]
}
```

Returns the following if the verdict has no execution record:
```json
{
  "status": "no_execution",
  "action_id": "c68c25ca-...",
  "gateway_enabled": true
}
```
`gateway_enabled` reflects the current `EXECUTION_GATEWAY_ENABLED` setting.
When `true` and `no_execution`, the verdict predates gateway enablement — run a new scan
to generate an execution record. When `false`, the gateway is disabled globally.

---

### `GET /api/execution/pending-reviews`

List all ESCALATED verdicts awaiting human review.

**Response:**
```json
{
  "count": 1,
  "reviews": [
    {
      "execution_id": "e5f6g7h8-...",
      "action_id": "d4e5f6g7-...",
      "verdict": "escalated",
      "status": "awaiting_review",
      "iac_managed": true,
      "iac_tool": "terraform",
      "created_at": "2026-03-05T12:00:00+00:00"
    }
  ]
}
```

---

### `POST /api/execution/{execution_id}/approve`

Human approves an ESCALATED verdict for execution. After approval, routes to the
IaC PR path (if managed) or manual path.

**Request body:**
```json
{ "reviewed_by": "admin@example.com" }
```

**Response:** Updated `ExecutionRecord` JSON.

Returns **400** if the execution is not in `awaiting_review` status.

---

### `POST /api/execution/{execution_id}/dismiss`

Human dismisses a verdict — no execution will happen.

**Request body:**
```json
{ "reviewed_by": "admin@example.com", "reason": "Not needed — planned maintenance covers this." }
```

**Response:** Updated `ExecutionRecord` JSON with `status: "dismissed"`.

---

### `POST /api/execution/{execution_id}/create-pr`

Create a Terraform PR from a `manual_required` execution record. Reuses the `TerraformPRGenerator` flow. If GitHub is not configured, the record stays `manual_required` with an explanatory note.

**Request body:**
```json
{ "reviewed_by": "admin@example.com" }
```

**Response:** Updated `ExecutionRecord` JSON. Status transitions to `pr_created` on success.

Returns `404` if `execution_id` is unknown, `400` if status is not `manual_required` or snapshot is missing.

---

### `GET /api/execution/{execution_id}/agent-fix-preview`

Generate the LLM-driven execution plan for a `manual_required` issue. Pure read — no side effects. The plan is stored on `ExecutionRecord.execution_plan` so the execute endpoint can run exactly what was reviewed.

**Response:**
```json
{
  "execution_id": "50823c45-...",
  "action_type": "modify_nsg",
  "resource_id": "/subscriptions/.../nsg-east",
  "steps": [
    {
      "operation": "delete_nsg_rule",
      "target": "/subscriptions/.../nsg-east",
      "params": { "resource_group": "rg-prod", "nsg_name": "nsg-east", "rule_name": "AllowAll_Inbound" },
      "reason": "Rule allows unrestricted inbound traffic"
    }
  ],
  "summary": "Delete insecure NSG rule AllowAll_Inbound from nsg-east",
  "estimated_impact": "Inbound traffic matching this rule will be blocked",
  "rollback_hint": "az network nsg rule create ... --name AllowAll_Inbound",
  "commands": ["az network nsg rule delete --resource-group rg-prod --nsg-name nsg-east --name AllowAll_Inbound"],
  "warning": "These steps will modify your Azure environment. Review carefully before executing."
}
```

Returns `404` if `execution_id` is unknown, `400` if no verdict snapshot is stored.

---

### `POST /api/execution/{execution_id}/agent-fix-execute`

Execute the `az` CLI fix commands for a `manual_required` record. In mock mode, simulates success. In live mode, runs each command and returns the result.

**Request body:**
```json
{ "reviewed_by": "admin@example.com" }
```

**Response:** Updated `ExecutionRecord` JSON. Status transitions to `applied` on success, `failed` on error.

Returns `404` if `execution_id` is unknown, `400` if status is not `manual_required` or snapshot is missing.

---

### `GET /api/execution/{execution_id}/terraform`

Generate a Terraform HCL fix for any execution record that has a verdict snapshot.
Used by the dashboard **Show Terraform Fix** button for `manual_required` and `pr_created` records.

For `modify_nsg` actions the response contains three concrete remediation options:
- Option A: Remove the insecure rule entirely
- Option B: Restrict `sourceAddressPrefix` to a specific IP
- Option C: Add a higher-priority deny rule

Rule name, port, and resource group are parsed automatically from the agent's reason string.

**Response:**
```json
{
  "execution_id": "50823c45-...",
  "hcl": "# RuriSkry Governance...\nresource \"azurerm_network_security_rule\" ..."
}
```

Returns `404` if `execution_id` is unknown, `400` if no verdict snapshot is stored.

---

### `GET /api/config`

Returns safe system configuration — no secrets exposed.

**Response:**
```json
{
  "mode": "mock",
  "llm_timeout": 120,
  "llm_concurrency_limit": 5,
  "execution_gateway_enabled": true,
  "use_live_topology": false,
  "version": "1.0.0"
}
```

`mode` is `"live"` when `USE_LOCAL_MOCKS=false` and `AZURE_OPENAI_ENDPOINT` is set; otherwise `"mock"`.

---

### `POST /api/admin/reset`

**Dev/test only.** Deletes all local JSON files in `data/decisions/`, `data/executions/`, and `data/scans/`. Clears in-memory scan state and resets the `ExecutionGateway` singleton. Never touches Cosmos DB.

**Response:**
```json
{ "status": "ok", "deleted": { "decisions": 3, "executions": 1, "scans": 5 }, "total": 9 }
```

---

## A2A Protocol Endpoints (Phase 10)

Served by `src/a2a/ruriskry_a2a_server.py`.
Start with: `uvicorn src.a2a.ruriskry_a2a_server:app --host 0.0.0.0 --port 8000`
(or set `A2A_SERVER_URL` env var for a custom URL).

### `GET /.well-known/agent-card.json`

Returns the A2A Agent Card — machine-readable capabilities advertisement.

```json
{
  "name": "RuriSkry Governance Engine",
  "description": "AI Action Governance — evaluates proposed infrastructure actions using SRI™ scoring.",
  "version": "1.0.0",
  "url": "http://localhost:8000",
  "capabilities": { "streaming": true },
  "skills": [
    { "id": "evaluate_action", ... },
    { "id": "query_decision_history", ... },
    { "id": "get_resource_risk_profile", ... }
  ]
}
```

`/.well-known/agent.json` is also served as a legacy alias.

### `POST /` — A2A task submission (streaming)

Send a `ProposedAction` JSON string as the message text using JSON-RPC
`tasks/sendSubscribe`. Receive SSE progress then a `GovernanceVerdict` artifact.

**SSE progress stream:**
```
"Evaluating blast radius..."
"Checking policy compliance..."
"Querying historical incidents..."
"Calculating financial impact..."
"SRI Composite: 74.0 → DENIED"
```

**Final artifact:** full `GovernanceVerdict` JSON (same shape as MCP output above).

---

## Direct Python API

For code that imports RuriSkry directly (not via MCP):

```python
from src.core.interception import ActionInterceptor
from src.core.models import ActionTarget, ActionType, ProposedAction, Urgency

interceptor = ActionInterceptor()

action = ProposedAction(
    agent_id="cost-optimization-agent",
    action_type=ActionType.DELETE_RESOURCE,
    target=ActionTarget(
        resource_id="vm-23",
        resource_type="Microsoft.Compute/virtualMachines",
        current_monthly_cost=847.0,
    ),
    reason="VM idle for 30 days",
    urgency=Urgency.HIGH,
)

# intercept() and intercept_from_dict() are both async — use await
verdict = await interceptor.intercept(action)
print(verdict.decision.value)          # "denied"
print(verdict.skry_risk_index.sri_composite)   # 77.0
```
