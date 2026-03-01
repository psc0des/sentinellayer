# SentinelLayer API Reference

## MCP Tools

Exposed via `src/mcp_server/server.py` (FastMCP stdio transport).
Start with: `python -m src.mcp_server.server`

---

### `sentinel_evaluate_action`

Evaluate a proposed infrastructure action through the full SentinelLayer governance pipeline.
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

### `sentinel_query_history`

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

### `sentinel_get_risk_profile`

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
| GET | `/api/metrics` | Aggregate stats: decision counts, SRI avg/min/max, top violations |
| GET | `/api/resources/{resource_id}/risk` | Risk profile for one resource |
| GET | `/api/agents` | List operational agents connected via A2A |
| GET | `/api/agents/{agent_name}/history` | Recent decisions for one A2A agent |
| POST | `/api/alert-trigger` | Webhook — trigger monitoring agent from Azure Monitor alert |
| POST | `/api/scan/cost` | Start a background cost agent scan |
| POST | `/api/scan/monitoring` | Start a background monitoring agent scan |
| POST | `/api/scan/deploy` | Start a background deploy agent scan |
| POST | `/api/scan/all` | Start background scans for all three agents |
| GET | `/api/scan/{scan_id}/status` | Poll the status and results of a background scan |

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
  ]
}
```

---

## A2A Agent Endpoints (Phase 10)

Added to `src/api/dashboard_api.py`.

### `GET /api/agents`

List all operational agents connected to SentinelLayer via the A2A protocol,
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

### `POST /api/alert-trigger` (Phase 12)

Webhook endpoint for Azure Monitor alert rules. When an Azure Monitor alert fires,
a Logic App posts the alert payload here; SentinelLayer triggers the monitoring agent
and evaluates any proposed remediation.

**Request body:** Azure Monitor alert schema (passed through as `alert_data`).
**Response:**
```json
{
  "status": "processed",
  "proposals_evaluated": 1,
  "verdicts": [{ "decision": "approved", "sri_composite": 14.1 }]
}
```

---

### Scan Trigger Endpoints (Phase 13)

Start background agent scans without blocking the HTTP response. Returns immediately with
a `scan_id`; poll `GET /api/scan/{scan_id}/status` to track progress.

**Common request body (all POST scan endpoints):**
```json
{ "resource_group": "sentinel-prod-rg" }
```
`resource_group` is optional — omit or send `null` to scan the whole subscription.
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
  "resource_group": "sentinel-prod-rg",
  "started_at": "2026-03-01T10:00:00+00:00"
}
```

**Response (complete):**
```json
{
  "scan_id": "b3e7c1a2-...",
  "status": "complete",
  "agent_type": "cost",
  "resource_group": "sentinel-prod-rg",
  "started_at": "2026-03-01T10:00:00+00:00",
  "completed_at": "2026-03-01T10:00:12+00:00",
  "proposals_count": 2,
  "evaluations_count": 2,
  "proposals": [...],
  "evaluations": [...]
}
```

Returns **404** if the `scan_id` is not recognised (unknown or from a previous server restart — scan state is in-memory).

---

## A2A Protocol Endpoints (Phase 10)

Served by `src/a2a/sentinel_a2a_server.py`.
Start with: `uvicorn src.a2a.sentinel_a2a_server:app --host 0.0.0.0 --port 8000`
(or set `A2A_SERVER_URL` env var for a custom URL).

### `GET /.well-known/agent-card.json`

Returns the A2A Agent Card — machine-readable capabilities advertisement.

```json
{
  "name": "SentinelLayer Governance Engine",
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

For code that imports SentinelLayer directly (not via MCP):

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
print(verdict.sentinel_risk_index.sri_composite)   # 77.0
```
