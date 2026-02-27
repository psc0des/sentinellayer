"""Dashboard REST API — serves governance data to the frontend.

Endpoints
---------
GET /api/evaluations              Recent governance decisions (newest-first).
GET /api/evaluations/{id}         Full detail for one evaluation.
GET /api/metrics                  Aggregate stats across all evaluations.
GET /api/resources/{id}/risk      Risk profile for one resource.
GET /api/agents                   List all connected A2A agents with stats.
GET /api/agents/{name}/history    Recent action history for one A2A agent.

Run
---
    python -m src.api.dashboard_api
"""

import logging

import uvicorn
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware

from src.a2a.agent_registry import AgentRegistry
from src.config import settings
from src.core.decision_tracker import DecisionTracker

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------

app = FastAPI(
    title="SentinelLayer Dashboard API",
    description=(
        "Governance decision history and risk metrics for the SentinelLayer dashboard."
    ),
    version="1.0.0",
)

# Allow any frontend origin during development.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET"],
    allow_headers=["*"],
)

# ---------------------------------------------------------------------------
# Tracker singleton — created once, reused on every request.
# ---------------------------------------------------------------------------

_tracker: DecisionTracker | None = None
_registry: AgentRegistry | None = None


def _get_tracker() -> DecisionTracker:
    """Return the module-level tracker singleton, creating it if needed."""
    global _tracker
    if _tracker is None:
        _tracker = DecisionTracker()
    return _tracker


def _get_registry() -> AgentRegistry:
    """Return the module-level agent registry singleton, creating it if needed."""
    global _registry
    if _registry is None:
        _registry = AgentRegistry()
    return _registry


# ---------------------------------------------------------------------------
# Endpoint 1 — list evaluations
# ---------------------------------------------------------------------------


@app.get("/api/evaluations")
async def list_evaluations(
    limit: int = Query(default=20, ge=1, le=100, description="Max records to return"),
    resource_id: str | None = Query(
        default=None, description="Filter by resource ID substring"
    ),
) -> dict:
    """Return recent governance decisions, newest-first.

    Query parameters:
    - **limit**: 1–100, default 20
    - **resource_id**: optional substring filter on the resource ID field
    """
    tracker = _get_tracker()
    if resource_id:
        records = tracker.get_by_resource(resource_id, limit=limit)
    else:
        records = tracker.get_recent(limit=limit)
    return {"count": len(records), "evaluations": records}


# ---------------------------------------------------------------------------
# Endpoint 2 — single evaluation detail
# ---------------------------------------------------------------------------


@app.get("/api/evaluations/{evaluation_id}")
async def get_evaluation(evaluation_id: str) -> dict:
    """Return the full stored record for one evaluation.

    Path parameter:
    - **evaluation_id**: the ``action_id`` UUID assigned when the action was evaluated.

    Returns 404 if the ID is not found in the local audit trail.
    """
    for record in _get_tracker()._load_all():
        if record.get("action_id") == evaluation_id:
            return record
    raise HTTPException(
        status_code=404,
        detail=f"Evaluation '{evaluation_id}' not found.",
    )


# ---------------------------------------------------------------------------
# Endpoint 3 — aggregate metrics
# ---------------------------------------------------------------------------


@app.get("/api/metrics")
async def get_metrics() -> dict:
    """Return aggregate statistics across all governance evaluations.

    Includes:
    - Total evaluation count
    - Decision breakdown (approved / escalated / denied) with percentages
    - SRI composite min / avg / max
    - Per-dimension SRI averages (infrastructure, policy, historical, cost)
    - Top 5 most-violated policies
    - Top 5 most-evaluated resources
    """
    records = _get_tracker()._load_all()

    if not records:
        return {
            "total_evaluations": 0,
            "decisions": {"approved": 0, "escalated": 0, "denied": 0},
            "decision_percentages": {"approved": 0.0, "escalated": 0.0, "denied": 0.0},
            "sri_composite": {"avg": None, "min": None, "max": None},
            "sri_dimensions": {
                "avg_infrastructure": None,
                "avg_policy": None,
                "avg_historical": None,
                "avg_cost": None,
            },
            "top_violations": [],
            "most_evaluated_resources": [],
        }

    total = len(records)

    # --- Decision counts ---
    counts: dict[str, int] = {"approved": 0, "escalated": 0, "denied": 0}
    for r in records:
        decision = r.get("decision", "").lower()
        if decision in counts:
            counts[decision] += 1
    percentages = {k: round(v / total * 100, 1) for k, v in counts.items()}

    # --- SRI composite stats ---
    composites = [r["sri_composite"] for r in records if "sri_composite" in r]
    sri_composite = {
        "avg": round(sum(composites) / len(composites), 2) if composites else None,
        "min": round(min(composites), 2) if composites else None,
        "max": round(max(composites), 2) if composites else None,
    }

    # --- Per-dimension averages ---
    def _avg_dim(dim: str) -> float | None:
        vals = [
            r["sri_breakdown"][dim]
            for r in records
            if "sri_breakdown" in r and dim in r["sri_breakdown"]
        ]
        return round(sum(vals) / len(vals), 2) if vals else None

    sri_dimensions = {
        "avg_infrastructure": _avg_dim("infrastructure"),
        "avg_policy": _avg_dim("policy"),
        "avg_historical": _avg_dim("historical"),
        "avg_cost": _avg_dim("cost"),
    }

    # --- Top violated policies ---
    violation_freq: dict[str, int] = {}
    for r in records:
        for pol_id in r.get("violations", []):
            violation_freq[pol_id] = violation_freq.get(pol_id, 0) + 1
    top_violations = [
        {"policy_id": k, "count": v}
        for k, v in sorted(
            violation_freq.items(), key=lambda x: x[1], reverse=True
        )[:5]
    ]

    # --- Most evaluated resources ---
    resource_freq: dict[str, int] = {}
    for r in records:
        rid = r.get("resource_id", "unknown")
        resource_freq[rid] = resource_freq.get(rid, 0) + 1
    most_evaluated = [
        {"resource_id": k, "count": v}
        for k, v in sorted(
            resource_freq.items(), key=lambda x: x[1], reverse=True
        )[:5]
    ]

    return {
        "total_evaluations": total,
        "decisions": counts,
        "decision_percentages": percentages,
        "sri_composite": sri_composite,
        "sri_dimensions": sri_dimensions,
        "top_violations": top_violations,
        "most_evaluated_resources": most_evaluated,
    }


# ---------------------------------------------------------------------------
# Endpoint 4 — resource risk profile
# ---------------------------------------------------------------------------


@app.get("/api/resources/{resource_id}/risk")
async def get_resource_risk(resource_id: str) -> dict:
    """Return the aggregated risk profile for a specific resource.

    Path parameter:
    - **resource_id**: short name or partial Azure resource ID
      (e.g. ``vm-23`` or ``nsg-east``). Matched as a substring.

    Returns 404 if no evaluations exist for this resource.
    """
    profile = _get_tracker().get_risk_profile(resource_id)
    if profile["total_evaluations"] == 0:
        raise HTTPException(
            status_code=404,
            detail=f"No evaluations found for resource '{resource_id}'.",
        )
    return profile


# ---------------------------------------------------------------------------
# Endpoint 5 — list connected A2A agents
# ---------------------------------------------------------------------------


@app.get("/api/agents")
async def list_agents() -> dict:
    """Return all A2A agents registered with SentinelLayer.

    Each entry includes counters for approved, denied, and escalated
    proposals so the dashboard can show per-agent governance stats.

    Returns a list of agent entries sorted by most-recently-seen first.
    """
    registry = _get_registry()
    agents = registry.get_connected_agents()
    return {"count": len(agents), "agents": agents}


# ---------------------------------------------------------------------------
# Endpoint 6 — per-agent action history
# ---------------------------------------------------------------------------


@app.get("/api/agents/{agent_name}/history")
async def get_agent_history(
    agent_name: str,
    limit: int = Query(default=10, ge=1, le=100, description="Max records to return"),
) -> dict:
    """Return the recent governance decision history for one A2A agent.

    Path parameter:
    - **agent_name**: the agent identifier (e.g. ``cost-optimization-agent``).

    Uses ``DecisionTracker.get_recent()`` filtered to decisions where
    ``agent_id`` matches the given name.

    Returns 404 if the agent is not registered.
    """
    registry = _get_registry()
    agent = registry.get_agent_stats(agent_name)
    if agent is None:
        raise HTTPException(
            status_code=404,
            detail=f"Agent '{agent_name}' is not registered.",
        )

    # Filter the audit trail for decisions from this agent
    tracker = _get_tracker()
    all_records = tracker.get_recent(limit=200)
    agent_records = [
        r
        for r in all_records
        if r.get("agent_id") == agent_name or r.get("proposed_action", {}).get("agent_id") == agent_name
    ]

    return {
        "agent": agent,
        "history_count": len(agent_records[:limit]),
        "history": agent_records[:limit],
    }


# ---------------------------------------------------------------------------
# Entry point — run with: python -m src.api.dashboard_api
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    uvicorn.run(
        "src.api.dashboard_api:app",
        host=settings.api_host,
        port=settings.api_port,
        reload=False,
    )
