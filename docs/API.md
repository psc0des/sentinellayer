# SentinelLayer API Reference

## MCP Tools (exposed by SentinelLayer)

### `sentinel_evaluate_action`
Evaluate a proposed infrastructure action before execution.

**Input:**
```json
{
  "agent_id": "cost-optimization-agent",
  "action_type": "delete_resource",
  "target": {
    "resource_id": "/subscriptions/.../virtualMachines/vm-23",
    "resource_type": "Microsoft.Compute/virtualMachines",
    "current_monthly_cost": 847.00
  },
  "reason": "VM idle for 30 days",
  "urgency": "low"
}
```

**Output:**
```json
{
  "action_id": "uuid",
  "sentinel_risk_index": {
    "sri_infrastructure": 32,
    "sri_policy": 40,
    "sri_historical": 15,
    "sri_cost": 10,
    "sri_composite": 72
  },
  "decision": "denied",
  "reason": "Critical policy violation — automatic denial"
}
```

### `sentinel_query_history`
Query governance decision history for auditing.

### `sentinel_get_risk_profile`
Get aggregated risk profile for a specific Azure resource.

## Dashboard REST API

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/api/evaluations` | List recent SRI™ evaluations |
| GET | `/api/evaluations/:id` | Get single evaluation detail |
| GET | `/api/metrics` | Aggregate dashboard metrics |
| GET | `/api/resources/:id/risk` | Resource risk profile |
