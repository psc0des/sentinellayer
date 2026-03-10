"""Dashboard REST API — serves governance data to the frontend.

Endpoints
---------
GET  /api/evaluations              Recent governance decisions (newest-first).
GET  /api/evaluations/{id}         Full detail for one evaluation.
GET  /api/metrics                  Aggregate stats across all evaluations.
GET  /api/resources/{id}/risk      Risk profile for one resource.
GET  /api/agents                   List all connected A2A agents with stats.
GET  /api/agents/{name}/history    Recent action history for one A2A agent.
GET  /api/agents/{name}/last-run   Most recent scan results for one agent.
POST /api/alert-trigger            Receive Azure Monitor alert → trigger MonitoringAgent
                                   → evaluate proposals → return verdicts.
POST /api/scan/cost                Trigger cost optimisation agent scan.
POST /api/scan/monitoring          Trigger SRE monitoring agent scan.
POST /api/scan/deploy              Trigger infrastructure deploy agent scan.
POST /api/scan/all                 Trigger all 3 agents simultaneously.
GET  /api/scan/{scan_id}/status    Check if a scan is complete + retrieve results.
GET  /api/scan/{scan_id}/stream    SSE stream of real-time scan progress events.
PATCH /api/scan/{scan_id}/cancel   Request cancellation of a running scan.
GET  /api/execution/pending-reviews              List all ESCALATED verdicts awaiting review.
GET  /api/execution/by-action/{action_id}        Execution status for a governance verdict.
POST /api/execution/{execution_id}/approve       Human approves an escalated verdict.
POST /api/execution/{execution_id}/dismiss       Human dismisses a verdict.
POST /api/execution/{execution_id}/create-pr     Create Terraform PR from manual_required record.
GET  /api/execution/{execution_id}/agent-fix-preview  Preview az CLI fix commands.
POST /api/execution/{execution_id}/agent-fix-execute  Execute az CLI fix commands.
POST /api/admin/reset                            ⚠ Dev/test only — wipe all local data and reset in-memory state.

Run
---
    python -m src.api.dashboard_api
"""

import asyncio
import json
import logging
import re
import uuid
from datetime import datetime, timezone
from typing import Any

import uvicorn
from fastapi import BackgroundTasks, Body, FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from src.a2a.agent_registry import AgentRegistry
from src.config import settings
from src.core.decision_tracker import DecisionTracker
from src.core.execution_gateway import ExecutionGateway
from src.core.models import (
    ActionTarget,
    ActionType,
    GovernanceVerdict,
    ProposedAction,
    SRIBreakdown,
    SRIVerdict,
    Urgency,
)
from src.core.scan_run_tracker import ScanRunTracker
from src.notifications.teams_notifier import send_teams_notification

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Resource tag lookup helper (Fix 1 — pass real tags to ExecutionGateway)
# ---------------------------------------------------------------------------

_resource_graph_cache: dict[str, dict] | None = None


def _load_seed_cache() -> dict[str, dict]:
    """Load seed_resources.json into a name-keyed dict (used as fallback)."""
    global _resource_graph_cache
    if _resource_graph_cache is None:
        try:
            from pathlib import Path  # noqa: PLC0415
            import json as _json  # noqa: PLC0415
            seed_path = Path(__file__).parent.parent.parent / "data" / "seed_resources.json"
            data = _json.loads(seed_path.read_text(encoding="utf-8"))
            _resource_graph_cache = {r["name"]: r for r in data.get("resources", [])}
        except Exception as exc:  # noqa: BLE001
            logger.warning("dashboard_api: could not load seed_resources.json — %s", exc)
            _resource_graph_cache = {}
    return _resource_graph_cache


async def _get_resource_tags(resource_id: str) -> dict[str, str]:
    """Look up a resource's tags by name or full ARM resource ID.

    In live mode (USE_LOCAL_MOCKS=false + subscription configured): queries
    Azure Resource Graph so tags on real resources are reflected immediately —
    APPROVED verdicts on IaC-managed resources route to Terraform PR creation
    instead of manual_required.

    In mock mode (or when the live query fails): falls back to seed_resources.json.

    Returns an empty dict if the resource is not found (safe default — APPROVED
    verdicts will route to manual_required).

    Sub-resources (e.g. .../securityRules/allow-ssh) have no tags — we walk up
    to the parent resource automatically so IaC detection works correctly.
    """
    # Sub-resources like /securityRules/<name> or /subnets/<name> don't carry
    # tags; resolve to the parent resource so IaC tags are found correctly.
    _SUB_RESOURCE_SEGMENTS = {"securityrules", "subnets", "networkinterfaces",
                               "virtualmachineextensions", "extensions"}
    parts = resource_id.split("/")
    lower_parts = [p.lower() for p in parts]
    for seg in _SUB_RESOURCE_SEGMENTS:
        if seg in lower_parts:
            idx = lower_parts.index(seg)
            resource_id = "/".join(parts[:idx])
            break

    # ------------------------------------------------------------------
    # Live mode: query Azure Resource Graph
    # ------------------------------------------------------------------
    if not settings.use_local_mocks and settings.azure_subscription_id:
        try:
            from src.infrastructure.resource_graph import ResourceGraphClient  # noqa: PLC0415
            client = ResourceGraphClient()
            resource = await client.get_resource_async(resource_id)
            if resource is not None:
                return resource.get("tags") or {}
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "dashboard_api: live tag lookup failed for %s — falling back to seed (%s)",
                resource_id, exc,
            )

    # ------------------------------------------------------------------
    # Mock / fallback: read from seed_resources.json
    # ------------------------------------------------------------------
    cache = _load_seed_cache()
    name = resource_id.split("/")[-1] if "/" in resource_id else resource_id
    resource = cache.get(resource_id) or cache.get(name)
    return resource.get("tags", {}) if resource else {}

# ---------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------

app = FastAPI(
    title="RuriSkry Dashboard API",
    description=(
        "Governance decision history and risk metrics for the RuriSkry dashboard."
    ),
    version="1.0.0",
)

# SEC-06: CORS is restricted to the exact dashboard origin + localhost.
# DASHBOARD_URL is set by Terraform from the Static Web App URL, which is
# known before the Container App is created (SWA is provisioned in Stage 1
# of scripts/deploy.sh, its URL is patched into terraform.tfvars, and the
# Container App is created in Stage 2 with the correct value already set).
# This avoids URL rotation issues without loosening CORS to a wildcard or
# domain-pattern match (which would allow any Azure SWA to call the API).
_dashboard_url = settings.dashboard_url.rstrip("/") if settings.dashboard_url else ""
_allowed_origins = (
    [_dashboard_url, "http://localhost:5173", "http://localhost:4173"]
    if _dashboard_url
    else ["http://localhost:5173", "http://localhost:4173"]
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=_allowed_origins,
    allow_methods=["GET", "POST", "PATCH"],
    allow_headers=["*"],
)

# ---------------------------------------------------------------------------
# Tracker singleton — created once, reused on every request.
# ---------------------------------------------------------------------------

_tracker: DecisionTracker | None = None
_registry: AgentRegistry | None = None
_scan_tracker: ScanRunTracker | None = None
_execution_gateway: ExecutionGateway | None = None

# ---------------------------------------------------------------------------
# Scan request model + in-memory scan store
# ---------------------------------------------------------------------------


class ScanRequest(BaseModel):
    """Optional body for POST /api/scan/* endpoints."""

    resource_group: str | None = None


# Keyed by scan_id (UUID str).  Values:
#   status          "running" | "complete" | "error" | "cancelled"
#   agent_type      "cost" | "monitoring" | "deploy"
#   started_at      ISO-8601 string
#   completed_at    ISO-8601 string (set when done)
#   proposed_actions list[dict]   — proposals from the ops agent
#   evaluations     list[dict]   — governance verdicts from the pipeline
#   error           str | None   — set on exception
_scans: dict[str, dict] = {}

# Per-scan asyncio.Queue for SSE streaming.
# Background task writes events; GET /api/scan/{id}/stream reads them.
# Events buffer in the queue until consumed — safe if SSE connects late.
_scan_events: dict[str, asyncio.Queue] = {}

# Scan IDs whose background tasks should stop at the next checkpoint.
_scan_cancelled: set[str] = set()

# Map from A2A agent name → scan agent_type.
_AGENT_TYPE_MAP: dict[str, str] = {
    "cost-optimization-agent": "cost",
    "monitoring-agent": "monitoring",
    "deploy-agent": "deploy",
}

# Reverse map: scan agent_type → AgentRegistry name.
# Used by _run_agent_scan() to update the Connected Agents panel after each verdict.
_AGENT_REGISTRY_NAMES: dict[str, str] = {v: k for k, v in _AGENT_TYPE_MAP.items()}


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


async def _emit_event(scan_id: str, event_type: str, **kwargs: Any) -> None:
    """Push one event onto the per-scan SSE queue.

    The queue is read by :func:`stream_scan_events`.  If no SSE client is
    connected the events buffer in the queue — they will be drained when
    (if) a client connects later.
    """
    timestamp = datetime.now(timezone.utc).isoformat()
    event = {
        "event": event_type,
        "timestamp": timestamp,
        "scan_id": scan_id,
        **kwargs,
    }

    queue = _scan_events.get(scan_id)
    if queue is not None:
        await queue.put(event)

    # Keep durable scan metadata in sync with emitted progress events.
    record = _scans.get(scan_id)
    if record is not None:
        record["event_count"] = int(record.get("event_count", 0)) + 1
        record["last_event_at"] = timestamp
        _persist_scan_record(scan_id)
    else:
        _get_scan_tracker().record_event(scan_id, timestamp)


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
    from src.core.pipeline import RuriSkryPipeline
    from src.operational_agents.cost_agent import CostOptimizationAgent
    from src.operational_agents.deploy_agent import DeployAgent
    from src.operational_agents.monitoring_agent import MonitoringAgent

    rg_label = resource_group or "whole subscription"
    logger.info("scan %s (%s): starting — rg=%s", scan_id[:8], agent_type, rg_label)
    await _emit_event(
        scan_id,
        "scan_started",
        agent=agent_type,
        resource_group=rg_label,
        message=f"Starting {agent_type} scan for resource_group={rg_label}",
    )

    try:
        # --- Pick the right ops agent and run scan ---
        if agent_type == "cost":
            agent = CostOptimizationAgent()
            proposals = await agent.scan(target_resource_group=resource_group)
        elif agent_type == "monitoring":
            agent = MonitoringAgent()
            proposals = await agent.scan(target_resource_group=resource_group)
        else:  # "deploy"
            agent = DeployAgent()
            proposals = await agent.scan(target_resource_group=resource_group)

        # Surface framework errors to the scan log so they are visible in the
        # dashboard live log (previously silent — only appeared in server terminal).
        scan_error = getattr(agent, "scan_error", None)
        if scan_error:
            await _emit_event(
                scan_id, "reasoning",
                agent=agent_type,
                message=f"⚠ Agent framework error — scan returned 0 proposals: {scan_error}",
            )

        # Emit tool-call visibility notes (what the LLM queried and found).
        for note in getattr(agent, "scan_notes", []):
            await _emit_event(scan_id, "reasoning", agent=agent_type, message=note)

        if not scan_error and not proposals and not settings.demo_mode:
            await _emit_event(
                scan_id, "reasoning",
                agent=agent_type,
                message="LLM scan complete — no actionable issues found in Azure environment.",
            )

        if settings.demo_mode:
            logger.info("scan %s (%s): DEMO_MODE is enabled", scan_id[:8], agent_type)
            await _emit_event(
                scan_id,
                "reasoning",
                agent=agent_type,
                message=(
                    "DEMO_MODE enabled: proposals are sample actions for local "
                    "pipeline testing."
                ),
            )

        # --- Re-flag unresolved manual_required issues (flag-until-fixed) ---
        # If a previous scan returned APPROVED verdicts that couldn't be routed
        # to a Terraform PR (no IaC tags), the resource still needs a manual fix.
        # Re-add those proposals so the governance pipeline evaluates them again
        # on every scan — UNLESS the agent just scanned that resource and found
        # it clean, in which case the fix was already applied → auto-dismiss.
        already_proposed = {
            (p.target.resource_id, p.action_type.value) for p in proposals
        }

        # Build a set of resource names the agent actively examined this scan
        # by parsing scan_notes (e.g. "found: vm-01, nsg-east" or "NSG 'nsg-x':")
        examined_names: set[str] = set()
        for note in getattr(agent, "scan_notes", []):
            # "Resource Graph query → N resource(s) found: name1, name2"
            found_match = re.search(r"found[:\s→]+(.+)", note, re.IGNORECASE)
            if found_match:
                for name in found_match.group(1).split(","):
                    examined_names.add(name.strip().lower())
            # "NSG 'nsg-name':" or similar single-quoted resource names
            for m in re.finditer(r"'([^']+)'", note):
                examined_names.add(m.group(1).lower())

        unresolved_pairs = _get_execution_gateway().get_unresolved_proposals()
        requeued = 0
        auto_dismissed = 0
        for unresolved_action, exec_record in unresolved_pairs:
            key = (unresolved_action.target.resource_id, unresolved_action.action_type.value)
            if key in already_proposed:
                continue  # current scan re-proposed it — will be evaluated fresh

            # For NSG security rule ARM IDs (.../securityRules/rule-name) the agent
            # scans the parent NSG, not the individual rule.  Extract the NSG name
            # (segment before 'securityRules') so the examined_names lookup works.
            rid_parts = unresolved_action.target.resource_id.lower().split("/")
            if "securityrules" in rid_parts:
                idx = rid_parts.index("securityrules")
                resource_name = rid_parts[idx - 1]
            else:
                resource_name = rid_parts[-1]
            if resource_name in examined_names:
                # Agent scanned this resource and found nothing wrong → fix was applied
                try:
                    await _get_execution_gateway().dismiss_execution(
                        exec_record.execution_id,
                        "auto-scan",
                        f"Auto-dismissed: {agent_type} re-scanned '{resource_name}' and found no issues",
                    )
                    auto_dismissed += 1
                    logger.info(
                        "scan %s (%s): auto-dismissed resolved issue for %s (exec %s)",
                        scan_id[:8], agent_type, resource_name, exec_record.execution_id[:8],
                    )
                except Exception as _e:  # noqa: BLE001
                    logger.warning("scan %s: auto-dismiss failed — %s", scan_id[:8], _e)
                continue

            # Resource was not scanned this run → keep re-flagging.
            # Strip any existing [Unresolved since ...] prefixes first so re-flag
            # passes don't stack them up into a double/triple prefix.
            clean_reason = re.sub(
                r'^(\[Unresolved since [^\]]+\]\s*)+', '', unresolved_action.reason
            )
            unresolved_action = unresolved_action.model_copy(update={
                "reason": (
                    f"[Unresolved since {exec_record.created_at.strftime('%d %b')}] "
                    + clean_reason
                )
            })
            proposals.append(unresolved_action)
            already_proposed.add(key)
            requeued += 1
            logger.info(
                "scan %s (%s): re-flagging unresolved %s on %s (exec %s)",
                scan_id[:8], agent_type,
                unresolved_action.action_type.value,
                unresolved_action.target.resource_id.split("/")[-1],
                exec_record.execution_id[:8],
            )
        if auto_dismissed:
            await _emit_event(
                scan_id, "info",
                agent=agent_type,
                message=(
                    f"Auto-dismissed {auto_dismissed} resolved issue(s): "
                    f"re-scanned and found clean."
                ),
            )
        if requeued:
            await _emit_event(
                scan_id, "discovery",
                agent=agent_type,
                message=(
                    f"Re-flagging {requeued} unresolved issue(s) from previous scans "
                    f"(manual_required — not yet dismissed)."
                ),
            )

        logger.info(
            "scan %s (%s): agent returned %d proposal(s)",
            scan_id[:8], agent_type, len(proposals),
        )
        summaries = [
            f"{p.action_type.value} on {p.target.resource_id.split('/')[-1]}"
            for p in proposals
        ]
        await _emit_event(
            scan_id,
            "discovery",
            agent=agent_type,
            count=len(proposals),
            proposals=summaries,
            message=(
                f"Found {len(proposals)} actionable finding(s)."
                if summaries
                else "No actionable findings discovered."
            ),
        )

        # --- Evaluate every proposal through the full governance pipeline ---
        pipeline = RuriSkryPipeline()
        tracker = _get_tracker()
        evaluations: list[dict] = []
        approved = escalated = denied = 0

        for i, action in enumerate(proposals, start=1):
            # Check cancellation flag before each evaluation
            if scan_id in _scan_cancelled:
                _scan_cancelled.discard(scan_id)
                logger.info("scan %s (%s): cancelled by request", scan_id[:8], agent_type)
                _scans[scan_id].update(
                    {
                        "status": "cancelled",
                        "completed_at": datetime.now(timezone.utc).isoformat(),
                        "proposed_actions": [p.model_dump(mode="json") for p in proposals],
                        "evaluations": evaluations,
                        "totals": {
                            "approved": approved,
                            "escalated": escalated,
                            "denied": denied,
                        },
                    }
                )
                _persist_scan_record(scan_id)
                await _emit_event(scan_id, "scan_error", message="Scan cancelled by user.")
                return

            resource_name = action.target.resource_id.split("/")[-1]
            await _emit_event(
                scan_id,
                "analysis",
                agent=agent_type,
                index=i,
                total=len(proposals),
                resource_id=resource_name,
                action_type=action.action_type.value,
                message=(
                    f"[{i}/{len(proposals)}] Analysing {resource_name} "
                    f"for action {action.action_type.value}."
                ),
            )
            await _emit_event(
                scan_id,
                "reasoning",
                agent=agent_type,
                resource_id=resource_name,
                action_type=action.action_type.value,
                message=f"Reasoning: {action.reason}",
            )
            await _emit_event(
                scan_id,
                "proposal",
                agent=agent_type,
                resource_id=resource_name,
                action_type=action.action_type.value,
                message=f"Proposing {action.action_type.value} on {resource_name}",
            )
            logger.info(
                "scan %s (%s): evaluating %d/%d — %s %s",
                scan_id[:8], agent_type, i, len(proposals),
                action.action_type.value, resource_name,
            )
            await _emit_event(
                scan_id, "evaluation",
                agent=agent_type,
                index=i, total=len(proposals),
                resource_id=resource_name,
                action_type=action.action_type.value,
                message=f"[{i}/{len(proposals)}] Evaluating {action.action_type.value} on {resource_name}…",
            )

            verdict = await pipeline.evaluate(action)
            decision = verdict.decision.value
            sri = verdict.skry_risk_index.sri_composite

            logger.info(
                "scan %s (%s): verdict for %s — %s (SRI %.1f)",
                scan_id[:8], agent_type, resource_name, decision, sri,
            )
            await _emit_event(
                scan_id, "verdict",
                agent=agent_type,
                resource_id=resource_name,
                decision=decision,
                sri_composite=sri,
                message=f"RuriSkry verdict: {decision.upper()} (SRI {sri:.1f}) — {resource_name}",
            )

            tracker.record(verdict)

            # --- Execution Gateway (Phase 21) ---
            # Routes verdict to IaC-safe execution path.
            # Pass real resource tags so IaC-managed resources are detected and
            # APPROVED verdicts route to Terraform PR, not manual_required.
            # Wrapped in try/except — gateway failure must never break the scan.
            try:
                resource_tags = await _get_resource_tags(action.target.resource_id)
                exec_record = await _get_execution_gateway().process_verdict(
                    verdict, resource_tags
                )
                logger.info(
                    "scan %s (%s): execution status=%s for %s",
                    scan_id[:8], agent_type,
                    exec_record.status.value, resource_name,
                )
                await _emit_event(
                    scan_id, "execution",
                    agent=agent_type,
                    resource_id=resource_name,
                    execution_id=exec_record.execution_id,
                    execution_status=exec_record.status.value,
                    iac_managed=exec_record.iac_managed,
                    pr_url=exec_record.pr_url,
                    message=(
                        f"Execution: {exec_record.status.value}"
                        + (f" — PR #{exec_record.pr_number}" if exec_record.pr_number else "")
                        + (f" — {exec_record.notes}" if exec_record.notes and exec_record.status.value in ("failed", "manual_required") else "")
                    ),
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "scan %s (%s): execution gateway failed — %s "
                    "(verdict still valid)",
                    scan_id[:8], agent_type, exc,
                )

            verdict_id = verdict.action_id
            logger.info(
                "scan %s (%s): verdict %s persisted to audit trail",
                scan_id[:8], agent_type, str(verdict_id)[:8],
            )
            await _emit_event(
                scan_id, "persisted",
                agent=agent_type,
                verdict_id=str(verdict_id),
                message=f"Verdict persisted to audit trail (ID: {str(verdict_id)[:8]}…)",
            )

            evaluations.append(verdict.model_dump(mode="json"))
            if decision == "approved":
                approved += 1
            elif decision == "escalated":
                escalated += 1
            else:
                denied += 1

            # --- Update Connected Agents panel ---
            # Each call increments total_actions_proposed + the right verdict
            # counter + refreshes last_seen in the AgentRegistry JSON file.
            # Without this the dashboard card shows stale counts from the last
            # A2A registration event.
            registry_name = _AGENT_REGISTRY_NAMES.get(agent_type)
            if registry_name:
                _get_registry().update_agent_stats(registry_name, decision)

        summary = f"{approved} approved, {escalated} escalated, {denied} denied"
        _scans[scan_id].update(
            {
                "status": "error" if scan_error else "complete",
                "scan_error": scan_error,
                "completed_at": datetime.now(timezone.utc).isoformat(),
                "proposed_actions": [p.model_dump(mode="json") for p in proposals],
                "evaluations": evaluations,
                "totals": {
                    "approved": approved,
                    "escalated": escalated,
                    "denied": denied,
                },
            }
        )
        _persist_scan_record(scan_id)

        # If there were no proposals, update_agent_stats was never called in the
        # loop above, so last_seen is still stale.  register_agent() only touches
        # last_seen when the agent already exists — a safe no-op otherwise.
        if not proposals:
            registry_name = _AGENT_REGISTRY_NAMES.get(agent_type)
            if registry_name:
                _get_registry().register_agent(registry_name)

        logger.info(
            "scan %s (%s): complete — %d proposals, %d verdicts (%s)",
            scan_id[:8], agent_type, len(proposals), len(evaluations), summary,
        )
        await _emit_event(
            scan_id, "scan_complete",
            agent=agent_type,
            total_actions=len(proposals),
            total_verdicts=len(evaluations),
            approved=approved,
            escalated=escalated,
            denied=denied,
            summary=summary,
            message=f"Scan complete — {len(evaluations)} verdict(s): {summary}",
        )

    except Exception as exc:  # noqa: BLE001
        logger.exception("scan %s (%s) failed: %s", scan_id[:8], agent_type, exc)
        _scans[scan_id].update(
            {
                "status": "error",
                "completed_at": datetime.now(timezone.utc).isoformat(),
                "error": str(exc),
                "totals": _scans[scan_id].get("totals", {"approved": 0, "escalated": 0, "denied": 0}),
            }
        )
        _persist_scan_record(scan_id)
        await _emit_event(
            scan_id, "scan_error",
            agent=agent_type,
            message=f"Scan failed: {exc}",
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


def _get_scan_tracker() -> ScanRunTracker:
    """Return the durable scan-run tracker singleton."""
    global _scan_tracker
    if _scan_tracker is None:
        _scan_tracker = ScanRunTracker()
    return _scan_tracker


def _get_execution_gateway() -> ExecutionGateway:
    """Return the module-level ExecutionGateway singleton."""
    global _execution_gateway
    if _execution_gateway is None:
        _execution_gateway = ExecutionGateway()
    return _execution_gateway


def _persist_scan_record(scan_id: str) -> None:
    """Persist the current in-memory record for one scan_id."""
    record = _scans.get(scan_id)
    if record is None:
        return
    payload = {
        "id": scan_id,
        "scan_id": scan_id,
        **record,
    }
    _get_scan_tracker().upsert(payload)


def _get_scan_record(scan_id: str) -> dict | None:
    """Read scan record from memory first, then durable store."""
    record = _scans.get(scan_id)
    if record is not None:
        return record
    persisted = _get_scan_tracker().get(scan_id)
    if persisted is None:
        return None
    restored = dict(persisted)
    restored.pop("id", None)
    restored.pop("scan_id", None)
    _scans[scan_id] = restored
    return restored


# ---------------------------------------------------------------------------
# Endpoint 1 — list evaluations
# ---------------------------------------------------------------------------


@app.get("/api/evaluations")
async def list_evaluations(
    limit: int = Query(default=20, ge=1, le=500, description="Max records to return"),
    resource_id: str | None = Query(
        default=None, description="Filter by resource ID substring"
    ),
) -> dict:
    """Return recent governance decisions, newest-first.

    Query parameters:
    - **limit**: 1–500, default 20
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
            "triage": {
                "tier_counts": {"tier_1": 0, "tier_2": 0, "tier_3": 0, "unknown": 0},
                "tier_percentages": {"tier_1": 0.0, "tier_2": 0.0, "tier_3": 0.0, "unknown": 0.0},
                "llm_calls_saved": 0,
                "deterministic_evaluations": 0,
                "full_evaluations": 0,
            },
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

    # --- Triage tier distribution (Phase 26) ---
    tier_counts: dict[str, int] = {"tier_1": 0, "tier_2": 0, "tier_3": 0, "unknown": 0}
    for r in records:
        tier = r.get("triage_tier")
        if tier == 1:
            tier_counts["tier_1"] += 1
        elif tier == 2:
            tier_counts["tier_2"] += 1
        elif tier == 3:
            tier_counts["tier_3"] += 1
        else:
            tier_counts["unknown"] += 1
    tier_percentages = {k: round(v / total * 100, 1) for k, v in tier_counts.items()}
    # LLM calls saved = all Tier 1 actions × 4 agents (no LLM in Tier 1)
    llm_calls_saved = tier_counts["tier_1"] * 4
    deterministic_count = sum(1 for r in records if r.get("triage_mode") == "deterministic")

    return {
        "total_evaluations": total,
        "decisions": counts,
        "decision_percentages": percentages,
        "sri_composite": sri_composite,
        "sri_dimensions": sri_dimensions,
        "top_violations": top_violations,
        "most_evaluated_resources": most_evaluated,
        "triage": {
            "tier_counts": tier_counts,
            "tier_percentages": tier_percentages,
            "llm_calls_saved": llm_calls_saved,
            "deterministic_evaluations": deterministic_count,
            "full_evaluations": total - deterministic_count,
        },
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
    """Return all A2A agents registered with RuriSkry.

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
# Endpoint 7 — most recent scan for one agent
# ---------------------------------------------------------------------------


@app.get("/api/agents/{agent_name}/last-run")
async def get_agent_last_run(agent_name: str) -> dict:
    """Return the most recent completed scan for an agent.

    Searches the in-memory scan store first (data survives until server restart),
    then falls back to querying the audit trail for that agent's evaluations.

    Path parameter:
    - **agent_name**: e.g. ``cost-optimization-agent``
    """
    agent_type = _AGENT_TYPE_MAP.get(agent_name)
    if agent_type:
        # Durable store first: survives process restarts.
        persisted = _get_scan_tracker().get_latest_completed_by_agent_type(agent_type)
        if persisted:
            scan_id = persisted.get("scan_id") or persisted.get("id")
            scan = dict(persisted)
            scan.pop("id", None)
            scan.pop("scan_id", None)
            return {
                "source": "scan_tracker",
                "scan_id": scan_id,
                **scan,
                "proposals_count": len(scan.get("proposed_actions", [])),
                "evaluations_count": len(scan.get("evaluations", [])),
            }

    # Fall back: read from audit trail
    records = _get_tracker().get_recent(limit=100)
    agent_records = [
        r for r in records
        if r.get("agent_id") == agent_name
    ][:10]
    return {
        "source": "tracker",
        "scan_id": None,
        "status": "complete" if agent_records else "no_data",
        "evaluations": agent_records,
        "proposed_actions": [],
        "started_at": None,
        "completed_at": None,
        "totals": {"approved": 0, "escalated": 0, "denied": 0},
        "proposals_count": 0,
        "evaluations_count": len(agent_records),
    }


# ---------------------------------------------------------------------------
# Endpoint 8 — alert webhook trigger
# ---------------------------------------------------------------------------


@app.post("/api/alert-trigger")
async def trigger_alert(alert: dict[str, Any]) -> dict:
    """Receive an Azure Monitor alert and trigger the monitoring agent.

    This endpoint acts as an Azure Monitor Action Group webhook target.
    When a metric alert fires (e.g. CPU > 80 % on vm-web-01), Azure POSTs
    the alert details here.  RuriSkry then:

    1. Passes the alert to the ``MonitoringAgent`` for investigation.
    2. The agent queries real metrics, confirms the alert, and produces
       evidence-backed ``ProposedAction`` objects.
    3. Each proposal is evaluated by the full RuriSkry governance
       pipeline (SRI scoring → APPROVED / ESCALATED / DENIED).
    4. All verdicts are returned and written to the audit trail.

    Request body (JSON)::

        {
            "resource_id": "vm-web-01",
            "metric": "Percentage CPU",
            "value": 95.0,
            "threshold": 80.0,
            "severity": "3",
            "resource_group": "ruriskry-prod-rg"
        }

    All fields are optional — the agent can work with minimal context.

    Returns:
        Dict containing the original alert, list of proposals, and list of
        governance verdicts (each with decision, SRI composite, and reason).
    """
    from src.operational_agents.monitoring_agent import MonitoringAgent
    from src.core.pipeline import RuriSkryPipeline

    logger.info("alert-trigger: received alert — %s", alert)

    agent = MonitoringAgent()
    proposals = await agent.scan(alert_payload=alert)

    pipeline = RuriSkryPipeline()
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
# Scan trigger helpers
# ---------------------------------------------------------------------------


def _make_scan_record(agent_type: str, resource_group: str | None) -> tuple[str, dict]:
    """Create a scan_id + initial scan record, plus its SSE event queue."""
    scan_id = str(uuid.uuid4())
    record = {
        "status": "running",
        "agent_type": agent_type,
        "resource_group": resource_group,
        "started_at": datetime.now(timezone.utc).isoformat(),
        "completed_at": None,
        "proposed_actions": [],
        "evaluations": [],
        "totals": {"approved": 0, "escalated": 0, "denied": 0},
        "event_count": 0,
        "last_event_at": None,
        "error": None,
    }
    _scans[scan_id] = record
    _scan_events[scan_id] = asyncio.Queue()
    _persist_scan_record(scan_id)
    return scan_id, record


# ---------------------------------------------------------------------------
# Endpoint 9 — trigger cost optimisation scan
# ---------------------------------------------------------------------------


@app.post("/api/scan/cost")
async def trigger_cost_scan(
    background_tasks: BackgroundTasks,
    body: ScanRequest = Body(default=ScanRequest()),
) -> dict:
    """Trigger a background cost-optimisation agent scan.

    Returns immediately with a ``scan_id``.  Poll
    ``GET /api/scan/{scan_id}/status`` to retrieve results, or connect
    ``GET /api/scan/{scan_id}/stream`` for real-time SSE progress events.

    Optional body::

        {"resource_group": "ruriskry-prod-rg"}
    """
    rg = body.resource_group or settings.default_resource_group or None
    scan_id, _ = _make_scan_record("cost", rg)
    background_tasks.add_task(_run_agent_scan, scan_id, "cost", rg)
    logger.info("scan %s (cost) started rg=%s", scan_id[:8], rg)
    return {"status": "started", "scan_id": scan_id, "agent_type": "cost"}


# ---------------------------------------------------------------------------
# Endpoint 10 — trigger SRE monitoring scan
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
    rg = body.resource_group or settings.default_resource_group or None
    scan_id, _ = _make_scan_record("monitoring", rg)
    background_tasks.add_task(_run_agent_scan, scan_id, "monitoring", rg)
    logger.info("scan %s (monitoring) started rg=%s", scan_id[:8], rg)
    return {"status": "started", "scan_id": scan_id, "agent_type": "monitoring"}


# ---------------------------------------------------------------------------
# Endpoint 11 — trigger deploy / security review scan
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
    rg = body.resource_group or settings.default_resource_group or None
    scan_id, _ = _make_scan_record("deploy", rg)
    background_tasks.add_task(_run_agent_scan, scan_id, "deploy", rg)
    logger.info("scan %s (deploy) started rg=%s", scan_id[:8], rg)
    return {"status": "started", "scan_id": scan_id, "agent_type": "deploy"}


# ---------------------------------------------------------------------------
# Endpoint 12 — trigger all three agents simultaneously
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

        {"resource_group": "ruriskry-prod-rg"}
    """
    rg = body.resource_group or settings.default_resource_group or None
    scan_ids: list[str] = []
    for agent_type in ("cost", "monitoring", "deploy"):
        scan_id, _ = _make_scan_record(agent_type, rg)
        background_tasks.add_task(_run_agent_scan, scan_id, agent_type, rg)
        scan_ids.append(scan_id)
        logger.info("scan %s (%s) started via /scan/all rg=%s", scan_id[:8], agent_type, rg)

    return {"status": "started", "scan_ids": scan_ids}


# ---------------------------------------------------------------------------
# Endpoint 13 — poll scan status
# ---------------------------------------------------------------------------


@app.get("/api/scan/{scan_id}/status")
async def get_scan_status(scan_id: str) -> dict:
    """Return the current status of a background scan.

    Path parameter:
    - **scan_id**: UUID returned by ``POST /api/scan/*``.

    Returns::

        {
            "scan_id": "...",
            "status": "running" | "complete" | "error" | "cancelled",
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
    record = _get_scan_record(scan_id)
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
# Endpoint 14 — SSE live log stream for a scan
# ---------------------------------------------------------------------------


@app.get("/api/scan/{scan_id}/stream")
async def stream_scan_events(scan_id: str):
    """Stream real-time scan progress as Server-Sent Events (SSE).

    Connect with ``EventSource`` from the browser:
    ``new EventSource('/api/scan/{scan_id}/stream')``

    Events are JSON objects delivered as SSE ``data:`` lines.  Each event
    has at minimum ``event`` (type string) and ``timestamp`` fields.

    The stream terminates when a ``scan_complete`` or ``scan_error`` event
    is emitted.  Events emitted before the client connects are buffered in
    the queue and delivered immediately on connection.

    Path parameter:
    - **scan_id**: UUID returned by ``POST /api/scan/*``.

    Returns 404 if the scan_id is not recognised.
    """
    if _get_scan_record(scan_id) is None:
        raise HTTPException(
            status_code=404,
            detail=f"Scan '{scan_id}' not found.",
        )

    async def generate():
        queue = _scan_events.get(scan_id)
        if queue is None:
            # Scan exists but queue is gone (e.g. already completed before client connected).
            # Check if scan is already done and emit a synthetic complete event.
            record = _get_scan_record(scan_id) or {}
            synthetic = {
                "event": "scan_complete" if record.get("status") == "complete" else "scan_error",
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "scan_id": scan_id,
                "agent": record.get("agent_type"),
                "message": "Scan already finished (connected after completion).",
                "status": record.get("status"),
            }
            yield f"data: {json.dumps(synthetic)}\n\n"
            return

        while True:
            try:
                event = await asyncio.wait_for(queue.get(), timeout=30.0)
                yield f"data: {json.dumps(event, default=str)}\n\n"
                if event.get("event") in ("scan_complete", "scan_error"):
                    break
            except asyncio.TimeoutError:
                # Keep the connection alive with a comment line.
                yield ": keepalive\n\n"

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )


# ---------------------------------------------------------------------------
# Endpoint 15 — cancel a running scan
# ---------------------------------------------------------------------------


@app.patch("/api/scan/{scan_id}/cancel")
async def cancel_scan(scan_id: str) -> dict:
    """Request cancellation of a running background scan.

    The background task checks for the cancellation flag before evaluating
    each proposal.  The scan stops cleanly at the next checkpoint.

    Path parameter:
    - **scan_id**: UUID returned by ``POST /api/scan/*``.

    Returns 404 if the scan_id is not recognised.
    Returns 400 if the scan is already complete or cancelled.
    """
    record = _get_scan_record(scan_id)
    if record is None:
        raise HTTPException(
            status_code=404,
            detail=f"Scan '{scan_id}' not found.",
        )
    if record.get("status") != "running":
        raise HTTPException(
            status_code=400,
            detail=f"Scan '{scan_id}' is not running (status: {record.get('status')}).",
        )
    _scan_cancelled.add(scan_id)
    return {"status": "cancellation_requested", "scan_id": scan_id}


# ---------------------------------------------------------------------------
# Endpoint 16 — Teams notification status
# ---------------------------------------------------------------------------


@app.get("/api/notification-status")
async def notification_status() -> dict:
    """Return the current Teams notification configuration status.

    The dashboard header uses this to show a 🔔 indicator.
    """
    return {
        "teams_configured": bool(settings.teams_webhook_url),
        "teams_enabled": settings.teams_notifications_enabled,
    }


# ---------------------------------------------------------------------------
# Endpoint 17 — send a test Teams notification
# ---------------------------------------------------------------------------


@app.post("/api/test-notification")
async def test_notification() -> dict:
    """Send a sample DENIED notification to the configured Teams webhook.

    Useful for judges to verify the Teams integration works without running
    a full governance evaluation.  Returns ``{"status": "sent"}`` on success,
    or ``{"status": "skipped", "reason": "..."}`` if the webhook is not
    configured.
    """
    webhook_url = settings.teams_webhook_url
    if not webhook_url:
        return {"status": "skipped", "reason": "TEAMS_WEBHOOK_URL not configured"}
    if not settings.teams_notifications_enabled:
        return {"status": "skipped", "reason": "TEAMS_NOTIFICATIONS_ENABLED is false"}

    # Build a sample DENIED verdict
    sample_action = ProposedAction(
        agent_id="cost-optimization-agent",
        action_type=ActionType.DELETE_RESOURCE,
        target=ActionTarget(
            resource_id="/subscriptions/demo/resourceGroups/prod"
            "/providers/Microsoft.Compute/virtualMachines/vm-dr-01",
            resource_type="Microsoft.Compute/virtualMachines",
            current_monthly_cost=847.0,
        ),
        reason="VM idle for 30 days — estimated savings $847/month. "
               "This is a test notification from RuriSkry.",
        urgency=Urgency.HIGH,
    )

    sample_verdict = GovernanceVerdict(
        action_id="test-notification-001",
        timestamp=datetime.now(timezone.utc),
        proposed_action=sample_action,
        skry_risk_index=SRIBreakdown(
            sri_infrastructure=65.0,
            sri_policy=100.0,
            sri_historical=62.0,
            sri_cost=45.0,
            sri_composite=77.0,
        ),
        decision=SRIVerdict.DENIED,
        reason="DENIED — Critical policy violation: POL-DR-001 "
               "(disaster-recovery protected resource). "
               "SRI Composite 77.0 exceeds threshold 60.",
        agent_results={
            "policy_compliance": {
                "violations": [
                    {
                        "policy_id": "POL-DR-001",
                        "name": "Disaster Recovery Protection",
                        "rule": "Cannot delete disaster-recovery tagged resources",
                        "severity": "critical",
                    }
                ]
            }
        },
    )

    success = await send_teams_notification(sample_verdict, sample_action)
    return {"status": "sent" if success else "failed"}


# ---------------------------------------------------------------------------
# Endpoint — Decision Explanation (Phase 17B)
# ---------------------------------------------------------------------------

_explainer = None


def _get_explainer():
    """Return the module-level DecisionExplainer singleton."""
    global _explainer
    if _explainer is None:
        from src.core.explanation_engine import DecisionExplainer
        _explainer = DecisionExplainer()
    return _explainer


@app.get("/api/evaluations/{evaluation_id}/explanation")
async def get_evaluation_explanation(evaluation_id: str) -> dict:
    """Return a full DecisionExplanation with counterfactual analysis.

    Path parameter:
    - **evaluation_id**: the ``action_id`` UUID from the governance verdict.

    Returns 404 if the evaluation is not found.
    """
    # Lookup the evaluation record
    record = None
    for r in _get_tracker().get_recent(limit=10_000):
        if r.get("action_id") == evaluation_id:
            record = r
            break
    if record is None:
        raise HTTPException(
            status_code=404,
            detail=f"Evaluation '{evaluation_id}' not found.",
        )

    # Reconstruct models from stored record (handles both full and flattened formats)
    # SRI breakdown — try full format first, then flattened format from DecisionTracker
    sri_data = record.get("skry_risk_index", {})
    sri_flat = record.get("sri_breakdown", {})
    sri_breakdown = SRIBreakdown(
        sri_infrastructure=sri_data.get("sri_infrastructure", sri_flat.get("infrastructure", 0)),
        sri_policy=sri_data.get("sri_policy", sri_flat.get("policy", 0)),
        sri_historical=sri_data.get("sri_historical", sri_flat.get("historical", 0)),
        sri_cost=sri_data.get("sri_cost", sri_flat.get("cost", 0)),
        sri_composite=sri_data.get("sri_composite", record.get("sri_composite", 0)),
    )

    # Rebuild ProposedAction — try nested proposed_action first, fallback to flat fields
    proposed = record.get("proposed_action", {})
    target_data = proposed.get("target", {})
    action = ProposedAction(
        agent_id=proposed.get("agent_id", record.get("agent_id", "unknown")),
        action_type=ActionType(proposed.get("action_type", record.get("action_type", "delete_resource"))),
        target=ActionTarget(
            resource_id=target_data.get("resource_id", record.get("resource_id", "")),
            resource_type=target_data.get("resource_type", record.get("resource_type", "")),
            resource_group=target_data.get("resource_group"),
            current_monthly_cost=target_data.get("current_monthly_cost"),
        ),
        reason=proposed.get("reason", record.get("action_reason", record.get("verdict_reason", ""))),
        urgency=Urgency(proposed.get("urgency", "low")),
    )

    verdict = GovernanceVerdict(
        action_id=evaluation_id,
        timestamp=datetime.fromisoformat(record["timestamp"]) if isinstance(record.get("timestamp"), str) else record.get("timestamp", datetime.now(timezone.utc)),
        proposed_action=action,
        skry_risk_index=sri_breakdown,
        decision=SRIVerdict(record.get("decision", "approved")),
        reason=record.get("verdict_reason", record.get("reason", "")),
        agent_results=record.get("agent_results", record.get("full_evaluation", {})),
    )

    explanation = await _get_explainer().explain(verdict, action)
    return explanation.model_dump()


# ---------------------------------------------------------------------------
# Endpoints 19-22 — Execution Gateway (Phase 21)
# NOTE: Static routes MUST be declared before dynamic /{param} routes to
# prevent FastAPI from capturing them as path parameters.
# ---------------------------------------------------------------------------


@app.get("/api/execution/pending-reviews")
async def get_pending_reviews() -> dict:
    """List all ESCALATED verdicts currently awaiting human review.

    These are records with ``status == "awaiting_review"`` — the human
    can approve or dismiss them via the dashboard buttons.

    This route is declared BEFORE ``/by-action/{action_id}`` so FastAPI does
    not mistake the literal string "pending-reviews" for an action_id.
    """
    gateway = _get_execution_gateway()
    pending = gateway.get_pending_reviews()
    return {
        "count": len(pending),
        "reviews": [r.model_dump(mode="json") for r in pending],
    }


@app.get("/api/execution/by-action/{action_id}")
async def get_execution_status(action_id: str) -> dict:
    """Return execution status for a governance verdict.

    Path parameter:
    - **action_id**: the ``action_id`` UUID from the governance verdict.

    Returns all ExecutionRecords linked to this verdict (usually one).
    Returns ``{"status": "no_execution"}`` if the gateway has not processed
    this verdict yet (e.g. EXECUTION_GATEWAY_ENABLED=false).

    Route renamed from ``/{action_id}`` to ``/by-action/{action_id}`` to
    prevent shadowing of the static ``/pending-reviews`` route.
    """
    gateway = _get_execution_gateway()
    records = gateway.get_records_for_verdict(action_id)
    if not records:
        return {
            "status": "no_execution",
            "action_id": action_id,
            "gateway_enabled": settings.execution_gateway_enabled,
        }
    return {
        "action_id": action_id,
        "executions": [r.model_dump(mode="json") for r in records],
    }


@app.post("/api/execution/{execution_id}/approve")
async def approve_execution(execution_id: str, body: dict = Body(default={})) -> dict:
    """Human approves an escalated verdict for execution.

    Reconstructs the original GovernanceVerdict from the stored snapshot and
    calls TerraformPRGenerator if the resource is IaC-managed and GitHub is
    configured.  Otherwise transitions to ``manual_required``.

    Request body (optional)::

        {"reviewed_by": "alice@example.com"}

    Returns 404 if execution_id is unknown.
    Returns 400 if the record is not in ``awaiting_review`` state.
    """
    gateway = _get_execution_gateway()
    reviewed_by = body.get("reviewed_by", "dashboard-user")
    try:
        record = await gateway.approve_execution(execution_id, reviewed_by)
        return record.model_dump(mode="json")
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/execution/{execution_id}/dismiss")
async def dismiss_execution(execution_id: str, body: dict = Body(default={})) -> dict:
    """Human dismisses a verdict — no execution will happen.

    Can dismiss any non-terminal execution record.

    Request body (optional)::

        {"reviewed_by": "alice@example.com", "reason": "Not needed this sprint"}

    Returns 404 if execution_id is unknown.
    """
    gateway = _get_execution_gateway()
    reviewed_by = body.get("reviewed_by", "dashboard-user")
    reason = body.get("reason", "")
    try:
        record = await gateway.dismiss_execution(execution_id, reviewed_by, reason)
        return record.model_dump(mode="json")
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


# ---------------------------------------------------------------------------
# Endpoints — HITL Agent Fix + PR from manual_required
# ---------------------------------------------------------------------------


@app.post("/api/execution/{execution_id}/create-pr")
async def create_pr_from_manual(
    execution_id: str, body: dict = Body(default={})
) -> dict:
    """Create a Terraform PR from a manual_required execution record.

    Reuses the existing TerraformPRGenerator flow.  If GitHub is not
    configured the record stays ``manual_required`` with an explanatory note.

    Request body (optional)::

        {"reviewed_by": "alice@example.com"}

    Returns 404 if execution_id is unknown.
    Returns 400 if the record is not ``manual_required`` or snapshot is missing.
    """
    gateway = _get_execution_gateway()
    reviewed_by = body.get("reviewed_by", "dashboard-user")
    try:
        record = await gateway.create_pr_from_manual(execution_id, reviewed_by)
        return record.model_dump(mode="json")
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/api/execution/{execution_id}/agent-fix-preview")
async def agent_fix_preview(execution_id: str) -> dict:
    """Preview the ``az`` CLI commands that would fix this issue.

    Pure read — no side effects.  Returns the list of shell commands and
    a warning message for the user to review before confirming.

    Returns 404 if execution_id is unknown.
    Returns 400 if the verdict snapshot is missing.
    """
    gateway = _get_execution_gateway()
    try:
        return gateway.generate_agent_fix_commands(execution_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/execution/{execution_id}/agent-fix-execute")
async def agent_fix_execute(
    execution_id: str, body: dict = Body(default={})
) -> dict:
    """Execute the ``az`` CLI fix commands for a manual_required record.

    In mock mode, simulates success.  In live mode, runs each ``az`` command
    and returns the result.

    Request body (optional)::

        {"reviewed_by": "alice@example.com"}

    Returns 404 if execution_id is unknown.
    Returns 400 if the record is not ``manual_required`` or snapshot is missing.
    """
    gateway = _get_execution_gateway()
    reviewed_by = body.get("reviewed_by", "dashboard-user")
    try:
        record = await gateway.execute_agent_fix(execution_id, reviewed_by)
        return record.model_dump(mode="json")
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


# ---------------------------------------------------------------------------
# Endpoint — Terraform stub (on-demand HCL generation for manual_required)
# ---------------------------------------------------------------------------


@app.get("/api/execution/{execution_id}/terraform")
async def get_terraform_stub(execution_id: str) -> dict:
    """Generate the Terraform HCL stub for a manual_required execution record.

    Used by the dashboard "Show Terraform Fix" button to display what change
    the human operator needs to apply.  Works for any execution record that
    has a verdict snapshot (manual_required, failed, awaiting_review, etc.).

    Returns:
        ``{"hcl": "<terraform code>"}`` on success.
        404 if execution_id is unknown.
        400 if the record has no verdict snapshot.
    """
    gateway = _get_execution_gateway()
    record = gateway._records.get(execution_id)  # noqa: SLF001
    if record is None:
        raise HTTPException(status_code=404, detail=f"Execution {execution_id} not found")

    verdict = gateway._reconstruct_verdict(record)  # noqa: SLF001
    if verdict is None:
        raise HTTPException(
            status_code=400,
            detail="No verdict snapshot stored — cannot generate Terraform stub.",
        )

    from src.core.terraform_pr_generator import TerraformPRGenerator  # noqa: PLC0415
    generator = TerraformPRGenerator()
    hcl = generator._generate_terraform_stub(verdict, record)  # noqa: SLF001
    return {"execution_id": execution_id, "hcl": hcl}


# ---------------------------------------------------------------------------
# Endpoint — dev/test reset (local JSON mode only)
# ---------------------------------------------------------------------------


@app.post("/api/admin/reset")
async def admin_reset() -> dict:
    """⚠ Development/testing only — wipe all local data and reset in-memory state.

    Deletes every JSON file in:
    - ``data/decisions/``  (governance verdicts / audit trail)
    - ``data/executions/`` (execution gateway records)
    - ``data/scans/``      (scan run history)

    Also resets the in-memory scan store (``_scans``) so the dashboard
    shows a clean slate immediately without restarting the server.

    Only operates on local JSON files — Cosmos DB data is never touched.
    Safe to call when ``USE_LOCAL_MOCKS=false`` (falls back to JSON anyway
    unless a real Cosmos endpoint + key are configured).

    Returns a summary of how many files were deleted per store.
    """
    from src.infrastructure.cosmos_client import _DEFAULT_DECISIONS_DIR
    from src.core.execution_gateway import _DEFAULT_EXECUTIONS_DIR
    from src.core.scan_run_tracker import _DEFAULT_SCANS_DIR

    deleted: dict[str, int] = {}

    for label, directory in [
        ("decisions", _DEFAULT_DECISIONS_DIR),
        ("executions", _DEFAULT_EXECUTIONS_DIR),
        ("scans", _DEFAULT_SCANS_DIR),
    ]:
        count = 0
        if directory.exists():
            for path in directory.glob("*.json"):
                try:
                    path.unlink()
                    count += 1
                except OSError as exc:
                    logger.warning("admin_reset: could not delete %s — %s", path.name, exc)
        deleted[label] = count
        logger.info("admin_reset: deleted %d %s records", count, label)

    # Reset in-memory scan store so the dashboard reflects the clean state
    # without needing a server restart.
    _scans.clear()
    _scan_cancelled.clear()

    # Reset the in-memory execution gateway so it doesn't serve stale records
    # from before the wipe.
    global _execution_gateway  # noqa: PLW0603
    _execution_gateway = None

    total = sum(deleted.values())
    logger.info("admin_reset: complete — %d total records deleted", total)

    return {
        "status": "ok",
        "deleted": deleted,
        "total": total,
        "note": "Cosmos DB data (if any) was NOT touched. Restart the server to reload seed data.",
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
