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
