"""Dashboard REST API — serves governance data to the frontend.

Endpoints
---------
GET  /api/evaluations              Recent governance decisions (newest-first).
GET  /api/evaluations/{id}         Full detail for one evaluation.
GET  /api/metrics                  Aggregate stats across all evaluations.
GET  /api/resources/{id}/risk      Risk profile for one resource.
GET  /api/agents                   List all connected A2A agents with stats.
GET  /api/agents/{name}/history    Recent action history for one A2A agent.
POST /api/alert-trigger            Receive Azure Monitor alert → trigger MonitoringAgent
                                   → evaluate proposals → return verdicts.
POST /api/scan/cost                Trigger cost optimisation agent scan.
POST /api/scan/monitoring          Trigger SRE monitoring agent scan.
POST /api/scan/deploy              Trigger infrastructure deploy agent scan.
POST /api/scan/all                 Trigger all 3 agents simultaneously.
GET  /api/scan/{scan_id}/status    Check if a scan is complete + retrieve results.

Run
---
    python -m src.api.dashboard_api
"""

import asyncio
import logging
import uuid
from datetime import datetime, timezone
from typing import Any

import uvicorn
from fastapi import BackgroundTasks, Body, FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

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
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)

# ---------------------------------------------------------------------------
# Tracker singleton — created once, reused on every request.
# ---------------------------------------------------------------------------

_tracker: DecisionTracker | None = None
_registry: AgentRegistry | None = None

# ---------------------------------------------------------------------------
# Scan request model + in-memory scan store
# ---------------------------------------------------------------------------


class ScanRequest(BaseModel):
    """Optional body for POST /api/scan/* endpoints."""

    resource_group: str | None = None


# Keyed by scan_id (UUID str).  Values:
#   status          "running" | "complete" | "error"
#   agent_type      "cost" | "monitoring" | "deploy"
#   started_at      ISO-8601 string
#   completed_at    ISO-8601 string (set when done)
#   proposed_actions list[dict]   — proposals from the ops agent
#   evaluations     list[dict]   — governance verdicts from the pipeline
#   error           str | None   — set on exception
_scans: dict[str, dict] = {}


async def _run_agent_scan(
    scan_id: str,
    agent_type: str,
    resource_group: str | None,
) -> None:
    """Background coroutine: run one ops agent, evaluate all proposals, persist results.

    Called by FastAPI BackgroundTasks — runs after the HTTP response is sent,
    so the caller receives the scan_id immediately without waiting for the
    (potentially slow) LLM + Azure calls.

    Parameters
    ----------
    scan_id:        UUID string used as the key in ``_scans``.
    agent_type:     One of ``"cost"``, ``"monitoring"``, or ``"deploy"``.
    resource_group: Optional Azure resource group to scope the scan to.
    """
    from src.core.pipeline import SentinelLayerPipeline
    from src.operational_agents.cost_agent import CostOptimizationAgent
    from src.operational_agents.deploy_agent import DeployAgent
    from src.operational_agents.monitoring_agent import MonitoringAgent

    try:
        # --- Pick the right ops agent ---
        if agent_type == "cost":
            agent = CostOptimizationAgent()
            proposals = await agent.scan(target_resource_group=resource_group)
        elif agent_type == "monitoring":
            agent = MonitoringAgent()
            proposals = await agent.scan(target_resource_group=resource_group)
        else:  # "deploy"
            agent = DeployAgent()
            proposals = await agent.scan(target_resource_group=resource_group)

        # --- Evaluate every proposal through the full governance pipeline ---
        pipeline = SentinelLayerPipeline()
        tracker = _get_tracker()
        evaluations: list[dict] = []

        for action in proposals:
            verdict = await pipeline.evaluate(action)
            tracker.record(verdict)
            evaluations.append(verdict.model_dump(mode="json"))

        _scans[scan_id].update(
            {
                "status": "complete",
                "completed_at": datetime.now(timezone.utc).isoformat(),
                "proposed_actions": [p.model_dump(mode="json") for p in proposals],
                "evaluations": evaluations,
            }
        )
        logger.info(
            "scan %s (%s): %d proposals, %d verdicts",
            scan_id[:8],
            agent_type,
            len(proposals),
            len(evaluations),
        )

    except Exception as exc:  # noqa: BLE001
        logger.exception("scan %s (%s) failed: %s", scan_id[:8], agent_type, exc)
        _scans[scan_id].update(
            {
                "status": "error",
                "completed_at": datetime.now(timezone.utc).isoformat(),
                "error": str(exc),
            }
        )


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
    for record in _get_tracker().get_recent(limit=10_000):
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
    records = _get_tracker().get_recent(limit=10_000)

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
    all_records = tracker.get_recent(limit=1000)
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
# Endpoint 7 — alert webhook trigger
# ---------------------------------------------------------------------------


@app.post("/api/alert-trigger")
async def trigger_alert(alert: dict[str, Any]) -> dict:
    """Receive an Azure Monitor alert and trigger the monitoring agent.

    This endpoint acts as an Azure Monitor Action Group webhook target.
    When a metric alert fires (e.g. CPU > 80 % on vm-web-01), Azure POSTs
    the alert details here.  SentinelLayer then:

    1. Passes the alert to the ``MonitoringAgent`` for investigation.
    2. The agent queries real metrics, confirms the alert, and produces
       evidence-backed ``ProposedAction`` objects.
    3. Each proposal is evaluated by the full SentinelLayer governance
       pipeline (SRI scoring → APPROVED / ESCALATED / DENIED).
    4. All verdicts are returned and written to the audit trail.

    Request body (JSON)::

        {
            "resource_id": "vm-web-01",
            "metric": "Percentage CPU",
            "value": 95.0,
            "threshold": 80.0,
            "severity": "3",
            "resource_group": "sentinel-prod-rg"
        }

    All fields are optional — the agent can work with minimal context.

    Returns:
        Dict containing the original alert, list of proposals, and list of
        governance verdicts (each with decision, SRI composite, and reason).
    """
    from src.operational_agents.monitoring_agent import MonitoringAgent
    from src.core.pipeline import SentinelLayerPipeline

    logger.info("alert-trigger: received alert — %s", alert)

    agent = MonitoringAgent()
    proposals = await agent.scan(alert_payload=alert)

    pipeline = SentinelLayerPipeline()
    tracker = _get_tracker()
    verdicts: list[dict] = []

    for action in proposals:
        verdict = await pipeline.evaluate(action)
        tracker.record(verdict)
        verdicts.append(verdict.model_dump(mode="json"))

    logger.info(
        "alert-trigger: %d proposals evaluated — %s",
        len(proposals),
        [v.get("decision") for v in verdicts],
    )

    return {
        "alert": alert,
        "proposals_count": len(proposals),
        "proposals": [p.model_dump(mode="json") for p in proposals],
        "verdicts": verdicts,
    }


# ---------------------------------------------------------------------------
# Endpoint 8 — trigger cost optimisation scan
# ---------------------------------------------------------------------------


@app.post("/api/scan/cost")
async def trigger_cost_scan(
    background_tasks: BackgroundTasks,
    body: ScanRequest = Body(default=ScanRequest()),
) -> dict:
    """Trigger a background cost-optimisation agent scan.

    Returns immediately with a ``scan_id``.  Poll
    ``GET /api/scan/{scan_id}/status`` to retrieve results.

    Optional body::

        {"resource_group": "sentinel-prod-rg"}
    """
    scan_id = str(uuid.uuid4())
    _scans[scan_id] = {
        "status": "running",
        "agent_type": "cost",
        "started_at": datetime.now(timezone.utc).isoformat(),
        "proposed_actions": [],
        "evaluations": [],
        "error": None,
    }
    rg = body.resource_group or settings.default_resource_group or None
    background_tasks.add_task(_run_agent_scan, scan_id, "cost", rg)
    logger.info("scan %s (cost) started rg=%s", scan_id[:8], rg)
    return {"status": "started", "scan_id": scan_id, "agent_type": "cost"}


# ---------------------------------------------------------------------------
# Endpoint 9 — trigger SRE monitoring scan
# ---------------------------------------------------------------------------


@app.post("/api/scan/monitoring")
async def trigger_monitoring_scan(
    background_tasks: BackgroundTasks,
    body: ScanRequest = Body(default=ScanRequest()),
) -> dict:
    """Trigger a background SRE monitoring agent scan.

    Returns immediately with a ``scan_id``.  Poll
    ``GET /api/scan/{scan_id}/status`` to retrieve results.
    """
    scan_id = str(uuid.uuid4())
    _scans[scan_id] = {
        "status": "running",
        "agent_type": "monitoring",
        "started_at": datetime.now(timezone.utc).isoformat(),
        "proposed_actions": [],
        "evaluations": [],
        "error": None,
    }
    rg = body.resource_group or settings.default_resource_group or None
    background_tasks.add_task(_run_agent_scan, scan_id, "monitoring", rg)
    logger.info("scan %s (monitoring) started rg=%s", scan_id[:8], rg)
    return {"status": "started", "scan_id": scan_id, "agent_type": "monitoring"}


# ---------------------------------------------------------------------------
# Endpoint 10 — trigger deploy / security review scan
# ---------------------------------------------------------------------------


@app.post("/api/scan/deploy")
async def trigger_deploy_scan(
    background_tasks: BackgroundTasks,
    body: ScanRequest = Body(default=ScanRequest()),
) -> dict:
    """Trigger a background infrastructure / security review agent scan.

    Returns immediately with a ``scan_id``.  Poll
    ``GET /api/scan/{scan_id}/status`` to retrieve results.
    """
    scan_id = str(uuid.uuid4())
    _scans[scan_id] = {
        "status": "running",
        "agent_type": "deploy",
        "started_at": datetime.now(timezone.utc).isoformat(),
        "proposed_actions": [],
        "evaluations": [],
        "error": None,
    }
    rg = body.resource_group or settings.default_resource_group or None
    background_tasks.add_task(_run_agent_scan, scan_id, "deploy", rg)
    logger.info("scan %s (deploy) started rg=%s", scan_id[:8], rg)
    return {"status": "started", "scan_id": scan_id, "agent_type": "deploy"}


# ---------------------------------------------------------------------------
# Endpoint 11 — trigger all three agents simultaneously
# ---------------------------------------------------------------------------


@app.post("/api/scan/all")
async def trigger_all_scans(
    background_tasks: BackgroundTasks,
    body: ScanRequest = Body(default=ScanRequest()),
) -> dict:
    """Trigger all three ops agents as independent background scans.

    Three separate scan IDs are returned — one per agent.  Each can be
    polled independently via ``GET /api/scan/{scan_id}/status``.

    Optional body::

        {"resource_group": "sentinel-prod-rg"}
    """
    rg = body.resource_group or settings.default_resource_group or None
    scan_ids: list[str] = []
    for agent_type in ("cost", "monitoring", "deploy"):
        scan_id = str(uuid.uuid4())
        _scans[scan_id] = {
            "status": "running",
            "agent_type": agent_type,
            "started_at": datetime.now(timezone.utc).isoformat(),
            "proposed_actions": [],
            "evaluations": [],
            "error": None,
        }
        background_tasks.add_task(_run_agent_scan, scan_id, agent_type, rg)
        scan_ids.append(scan_id)
        logger.info("scan %s (%s) started via /scan/all rg=%s", scan_id[:8], agent_type, rg)

    return {"status": "started", "scan_ids": scan_ids}


# ---------------------------------------------------------------------------
# Endpoint 12 — poll scan status
# ---------------------------------------------------------------------------


@app.get("/api/scan/{scan_id}/status")
async def get_scan_status(scan_id: str) -> dict:
    """Return the current status of a background scan.

    Path parameter:
    - **scan_id**: UUID returned by ``POST /api/scan/*``.

    Returns::

        {
            "scan_id": "...",
            "status": "running" | "complete" | "error",
            "agent_type": "cost" | "monitoring" | "deploy",
            "started_at": "2025-...",
            "completed_at": "2025-..." | null,
            "proposals_count": 2,
            "proposed_actions": [...],
            "evaluations_count": 2,
            "evaluations": [...],
            "error": null
        }

    Returns 404 if the ``scan_id`` is unknown.
    """
    record = _scans.get(scan_id)
    if record is None:
        raise HTTPException(
            status_code=404,
            detail=f"Scan '{scan_id}' not found.",
        )
    return {
        "scan_id": scan_id,
        **record,
        "proposals_count": len(record.get("proposed_actions", [])),
        "evaluations_count": len(record.get("evaluations", [])),
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
