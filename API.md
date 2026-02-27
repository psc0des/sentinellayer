# SentinelLayer — API Reference

> **Dashboard API** — served by FastAPI at `http://localhost:8000` (default).
> Run with: `uvicorn src.api.dashboard_api:app --reload`

---

## Governance Endpoints

### `GET /api/evaluations`

Return recent governance decisions, newest-first.

**Query parameters:**
| Name | Type | Default | Description |
|------|------|---------|-------------|
| `limit` | int | 20 | Max records to return (1–100) |
| `resource_id` | string | — | Optional substring filter on resource ID |

**Response:**
```json
{
  "count": 3,
  "evaluations": [
    {
      "action_id": "abc-123",
      "decision": "denied",
      "sri_composite": 74.0,
      "resource_id": "vm-23",
      "timestamp": "2026-02-27T10:00:00Z"
    }
  ]
}
```

---

### `GET /api/evaluations/{evaluation_id}`

Return the full stored record for one evaluation.

**Path parameter:** `evaluation_id` — the UUID assigned when the action was evaluated.

**Returns:** Full evaluation dict, or **404** if not found.

---

### `GET /api/metrics`

Return aggregate statistics across all governance evaluations.

**Response:**
```json
{
  "total_evaluations": 100,
  "decisions": {"approved": 40, "escalated": 35, "denied": 25},
  "decision_percentages": {"approved": 40.0, "escalated": 35.0, "denied": 25.0},
  "sri_composite": {"avg": 42.3, "min": 5.0, "max": 95.0},
  "sri_dimensions": {
    "avg_infrastructure": 38.1,
    "avg_policy": 22.5,
    "avg_historical": 18.4,
    "avg_cost": 15.2
  },
  "top_violations": [{"policy_id": "POL-DR-001", "count": 12}],
  "most_evaluated_resources": [{"resource_id": "vm-23", "count": 8}]
}
```

---

### `GET /api/resources/{resource_id}/risk`

Return the aggregated SRI™ risk profile for a specific Azure resource.

**Path parameter:** `resource_id` — short name or substring of Azure resource ID.

**Returns:** Risk profile dict, or **404** if no evaluations exist for this resource.

---

## A2A Agent Endpoints (Phase 10)

### `GET /api/agents`

List all operational agents connected to SentinelLayer via the A2A protocol.

**Response:**
```json
{
  "count": 3,
  "agents": [
    {
      "name": "cost-optimization-agent",
      "agent_card_url": "",
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

Return the recent governance decision history for one A2A agent.

**Path parameter:** `agent_name` — the agent identifier (e.g. `cost-optimization-agent`).

**Query parameters:**
| Name | Type | Default | Description |
|------|------|---------|-------------|
| `limit` | int | 10 | Max records to return (1–100) |

**Returns:**
```json
{
  "agent": { "name": "cost-optimization-agent", "total_actions_proposed": 5, ... },
  "history_count": 3,
  "history": [
    { "action_id": "...", "decision": "denied", "resource_id": "vm-23", ... }
  ]
}
```

**Returns 404** if the agent is not registered.

---

## A2A Protocol Endpoints

SentinelLayer also runs as an A2A-compliant server at port 8000 (or `A2A_SERVER_URL`).

### `GET /.well-known/agent-card.json`

Returns the A2A Agent Card — a machine-readable description of SentinelLayer's capabilities.

```json
{
  "name": "SentinelLayer Governance Engine",
  "description": "AI Action Governance — evaluates proposed infrastructure actions using SRI™ scoring.",
  "version": "1.0.0",
  "url": "http://localhost:8000",
  "capabilities": {"streaming": true},
  "skills": [
    {"id": "evaluate_action", "name": "Evaluate Action", ...},
    {"id": "query_decision_history", "name": "Query Decision History", ...},
    {"id": "get_resource_risk_profile", "name": "Get Resource Risk Profile", ...}
  ]
}
```

### `GET /.well-known/agent.json`

Legacy alias for the Agent Card (same response as above).

### `POST /`

A2A JSON-RPC 2.0 endpoint — accepts `tasks/sendMessage` and `tasks/sendSubscribe` requests.

Send a `ProposedAction` JSON string as the message text.  Receive a `GovernanceVerdict`
JSON string as the artifact payload.

**Streaming:** Use `tasks/sendSubscribe` (SSE) to receive live progress messages:
- `"Evaluating blast radius..."`
- `"Checking policy compliance..."`
- `"Querying historical incidents..."`
- `"Calculating financial impact..."`
- `"SRI Composite: 74.0 → DENIED"`

---

## MCP Tools

When using SentinelLayer via the Model Context Protocol (Claude Desktop, etc.):

| Tool | Description |
|------|-------------|
| `sentinel_evaluate_action` | Evaluate a ProposedAction and return GovernanceVerdict |
| `sentinel_get_recent_decisions` | Return recent decisions from audit trail |
| `sentinel_get_resource_risk_profile` | Get risk profile for a resource |

Run MCP server: `python -m src.mcp_server.server`
