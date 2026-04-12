"""Dashboard REST API — serves governance data to the frontend.

Endpoints
---------
GET  /api/auth/status              Check if admin account has been created.
POST /api/auth/setup               Create the initial admin account (first-time only).
POST /api/auth/login               Authenticate with username + password; returns session token.
GET  /api/auth/me                  Validate current session token; returns username.
POST /api/auth/logout              Revoke current session token.
GET  /api/evaluations              Recent governance decisions (newest-first).
GET  /api/evaluations/{id}         Full detail for one evaluation.
GET  /api/metrics                  Aggregate stats across all evaluations.
GET  /api/resources/{id}/risk      Risk profile for one resource.
GET  /api/agents                   List all connected A2A agents with stats.
GET  /api/agents/{name}/history    Recent action history for one A2A agent.
GET  /api/agents/{name}/last-run   Most recent scan results for one agent.
POST /api/alert-trigger                  Receive Azure Monitor alert → creates pending record (no auto-investigation).
POST /api/alerts/{alert_id}/investigate  Manually trigger investigation for a pending alert.
GET  /api/alerts                         List all alert records newest-first.
GET  /api/alerts/active-count            Count of currently pending/investigating alerts.
GET  /api/alerts/{alert_id}/status       Full detail for one alert.
GET  /api/alerts/{alert_id}/stream       SSE stream of real-time investigation progress.
POST /api/scan/cost                Trigger cost optimisation agent scan.
POST /api/scan/monitoring          Trigger SRE monitoring agent scan.
POST /api/scan/deploy              Trigger infrastructure deploy agent scan.
POST /api/scan/all                 Trigger all 3 agents simultaneously.
GET  /api/scan-history              List all scan runs newest-first — operational audit log.
GET  /api/scan/{scan_id}/status    Check if a scan is complete + retrieve results.
GET  /api/scan/{scan_id}/stream    SSE stream of real-time scan progress events.
PATCH /api/scan/{scan_id}/cancel   Request cancellation of a running scan.
GET  /api/execution/pending-reviews              List all ESCALATED verdicts awaiting review.
GET  /api/execution/by-action/{action_id}        Execution status for a governance verdict.
GET  /api/execution/{execution_id}/record        Fetch a single execution record by execution_id.
POST /api/execution/{execution_id}/approve       Human approves an escalated verdict.
POST /api/execution/{execution_id}/dismiss       Human dismisses a verdict.
GET  /api/github/repos                           List GitHub repos accessible via GITHUB_TOKEN (for PR overlay).
POST /api/execution/{execution_id}/create-pr     Create Terraform PR from manual_required record (body: iac_repo, iac_path optional).
GET  /api/execution/{execution_id}/agent-fix-preview  Preview az CLI fix commands.
POST /api/execution/{execution_id}/agent-fix-execute  Execute az CLI fix commands.
POST /api/execution/{execution_id}/rollback           Reverse a previously applied agent fix.
GET  /api/config                                  Safe system configuration — mode, timeouts, feature flags.
POST /api/admin/reset                            ⚠ Dev/test only — wipe all local data and reset in-memory state.

Run
---
    python -m src.api.dashboard_api
"""

import asyncio
import hashlib
import json
import logging
import re
import secrets
import uuid
from contextlib import asynccontextmanager
from contextvars import ContextVar
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import uvicorn
from fastapi import BackgroundTasks, Body, FastAPI, Header, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel
from starlette.middleware.base import BaseHTTPMiddleware

from src.a2a.agent_registry import AgentRegistry
from src.config import settings
from src.core.decision_tracker import DecisionTracker
from src.core.execution_gateway import ExecutionGateway
from src.core.models import (
    ActionTarget,
    ActionType,
    ExecutionStatus,
    GovernanceVerdict,
    ProposedAction,
    SRIBreakdown,
    SRIVerdict,
    Urgency,
)
from src.core.scan_run_tracker import ScanRunTracker
from src.notifications.slack_notifier import (
    send_alert_notification,
    send_alert_resolved_notification,
    send_verdict_notification,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# L4 — X-Request-ID: per-request correlation ID propagated through all logs
# and returned in the response header so callers can trace distributed calls.
# ---------------------------------------------------------------------------
_request_id_var: ContextVar[str] = ContextVar("request_id", default="-")


class _RequestIDLogFilter(logging.Filter):
    """Inject the current request_id into every log record."""
    def filter(self, record: logging.LogRecord) -> bool:
        record.request_id = _request_id_var.get("-")  # type: ignore[attr-defined]
        return True


# Attach to the root handler so ALL loggers in this process pick it up.
for _h in logging.root.handlers:
    _h.addFilter(_RequestIDLogFilter())

# ---------------------------------------------------------------------------
# Simple in-memory rate limiter — protects LLM-heavy endpoints from abuse.
# Tracks request timestamps per client IP with a sliding window.
# Not distributed (per-replica) — sufficient for single-node and small
# multi-replica deployments where sticky sessions are already in use.
# ---------------------------------------------------------------------------
import time as _time
from collections import defaultdict as _defaultdict

_rate_store: dict[str, list[float]] = _defaultdict(list)
_SCAN_RATE_LIMIT = 10       # requests per window
_SCAN_RATE_WINDOW = 60      # seconds


def _check_rate_limit(client_ip: str) -> None:
    """Raise 429 if the client has exceeded the scan rate limit."""
    now = _time.monotonic()
    window_start = now - _SCAN_RATE_WINDOW
    hits = _rate_store[client_ip]
    # Prune expired entries
    hits[:] = [t for t in hits if t > window_start]
    if len(hits) >= _SCAN_RATE_LIMIT:
        raise HTTPException(
            status_code=429,
            detail=f"Rate limit exceeded: max {_SCAN_RATE_LIMIT} scan/alert requests "
                   f"per {_SCAN_RATE_WINDOW}s per client.",
        )
    hits.append(now)


# ---------------------------------------------------------------------------
# C3 — reviewed_by validation
# Rejects empty strings and the generic default in live mode so the audit
# trail always contains a real reviewer identity on production deployments.
# ---------------------------------------------------------------------------
_GENERIC_REVIEWER = "dashboard-user"


def _validate_reviewed_by(raw: str) -> str:
    """Return a clean reviewed_by string or raise HTTP 400."""
    value = (raw or "").strip()
    if not value:
        raise HTTPException(status_code=400, detail="reviewed_by is required and must not be empty.")
    if not settings.use_local_mocks and value == _GENERIC_REVIEWER:
        raise HTTPException(
            status_code=400,
            detail=(
                f"reviewed_by '{_GENERIC_REVIEWER}' is not accepted in live mode. "
                "Supply the reviewer's name or email address."
            ),
        )
    return value


# ---------------------------------------------------------------------------
# Auth — admin account + browser session tokens
#
# The admin account is created once via POST /api/auth/setup (first-time flow).
# Subsequent access uses POST /api/auth/login → session token stored in the
# browser's localStorage. Session tokens are 32-byte URL-safe random strings
# stored in memory; they expire after SESSION_TTL hours.
#
# Password is hashed with PBKDF2-HMAC-SHA256 (260 000 iterations) + random
# salt — equivalent to bcrypt strength without an external dependency.
#
# The middleware accepts EITHER a valid session token (browser dashboard) OR a
# valid API key (machine-to-machine / CI-CD) on POST/PATCH endpoints.
# ---------------------------------------------------------------------------

_AUTH_FILE = Path("data") / "admin_auth.json"
_SESSION_TTL = 8 * 3600          # seconds a browser session stays valid
_PBKDF2_ITERS = 260_000          # NIST SP 800-132 recommended minimum

# Cached admin record — "unloaded" sentinel means not yet read from disk.
_admin_cache: dict | None | str = "unloaded"

# Lazy-initialised Cosmos admin client for durable admin auth storage.
_cosmos_admin = None


def _get_cosmos_admin():
    """Return the CosmosAdminClient singleton (lazy-init, never raises)."""
    global _cosmos_admin
    if _cosmos_admin is None:
        try:
            from src.infrastructure.cosmos_client import CosmosAdminClient  # noqa: PLC0415
            _cosmos_admin = CosmosAdminClient()
        except Exception as exc:  # noqa: BLE001
            logger.warning("_get_cosmos_admin: init failed — %s", exc)
    return _cosmos_admin


def _get_admin() -> dict | None:
    """Return the admin record dict, or None if no admin has been created.

    Load order:
    1. In-memory cache (fastest)
    2. Local file data/admin_auth.json (survives process restarts on same container)
    3. Cosmos DB governance-agents container (survives new container revisions)
    """
    global _admin_cache
    if _admin_cache != "unloaded":
        return _admin_cache  # type: ignore[return-value]

    # Layer 1: local file
    if _AUTH_FILE.exists():
        try:
            _admin_cache = json.loads(_AUTH_FILE.read_text(encoding="utf-8"))
            return _admin_cache  # type: ignore[return-value]
        except Exception:  # noqa: BLE001
            pass

    # Layer 2: Cosmos DB (survives container revision rotation)
    cosmos = _get_cosmos_admin()
    if cosmos is not None:
        record = cosmos.load()
        if record:
            logger.info("_get_admin: restored from Cosmos DB")
            _admin_cache = record
            # Write back to local file so subsequent reads are fast
            try:
                _AUTH_FILE.parent.mkdir(parents=True, exist_ok=True)
                _AUTH_FILE.write_text(json.dumps(record), encoding="utf-8")
            except Exception:  # noqa: BLE001
                pass
            return _admin_cache  # type: ignore[return-value]

    _admin_cache = None
    return None


def _save_admin(username: str, password: str) -> None:
    """Hash password and persist admin record to local file + Cosmos DB."""
    global _admin_cache
    salt = secrets.token_hex(32)
    pw_hash = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), salt.encode("utf-8"), _PBKDF2_ITERS
    ).hex()
    record: dict = {"username": username, "salt": salt, "pw_hash": pw_hash}
    # Local file (fast path for same-container reads)
    try:
        _AUTH_FILE.parent.mkdir(parents=True, exist_ok=True)
        _AUTH_FILE.write_text(json.dumps(record), encoding="utf-8")
    except Exception as exc:  # noqa: BLE001
        logger.warning("_save_admin: could not write local file — %s", exc)
    # Cosmos DB (durable across container revisions)
    cosmos = _get_cosmos_admin()
    if cosmos is not None:
        cosmos.save(record)
    _admin_cache = record


def _verify_password(password: str, salt: str, pw_hash: str) -> bool:
    candidate = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), salt.encode("utf-8"), _PBKDF2_ITERS
    ).hex()
    return secrets.compare_digest(candidate, pw_hash)


# In-memory session store: {token: {"username": str, "expires_at": float}}
_sessions: dict[str, dict] = {}


def _create_session(username: str) -> str:
    token = secrets.token_urlsafe(32)
    _sessions[token] = {"username": username, "expires_at": _time.time() + _SESSION_TTL}
    return token


def _validate_session(token: str) -> str | None:
    """Return username if the session token is valid and unexpired, else None."""
    rec = _sessions.get(token)
    if not rec:
        return None
    if _time.time() > rec["expires_at"]:
        del _sessions[token]
        return None
    return rec["username"]


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

@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup: mark any orphaned 'running' scans as error (process died mid-scan)."""
    from src.core.scan_run_tracker import ScanRunTracker
    try:
        tracker = ScanRunTracker()
        recent = tracker.get_recent(500)
        orphaned = [r for r in recent if r.get("status") == "running"]
        for record in orphaned:
            sid = record.get("scan_id") or record.get("id")
            if not sid:
                continue
            record["status"] = "error"
            record["scan_error"] = "Scan interrupted by backend restart."
            record["completed_at"] = datetime.now(timezone.utc).isoformat()
            _scans[sid] = record
            _persist_scan_record(sid)
            logger.warning("Startup: marked orphaned scan %s as error (was running)", sid[:8])
    except Exception as exc:
        logger.warning("Startup: could not clean up orphaned scans: %s", exc)

    if settings.use_local_mocks:
        logger.warning(
            "⚠ MOCK MODE ACTIVE (USE_LOCAL_MOCKS=true) — all agents return canned "
            "data. No real Azure calls will be made. Set USE_LOCAL_MOCKS=false in "
            ".env to connect to live Azure resources."
        )

    yield  # application runs


app = FastAPI(
    title="RuriSkry Dashboard API",
    description=(
        "Governance decision history and risk metrics for the RuriSkry dashboard."
    ),
    version="1.0.0",
    lifespan=lifespan,
)

# SEC-06: CORS is restricted to the exact dashboard origin + localhost.
# DASHBOARD_URL is set by Terraform directly from the Static Web App's
# default_host_name attribute. Terraform creates the SWA before the Container
# App (implicit dependency), reads the exact URL, and passes it as an env var
# in the same apply — no tfvars patching, no re-apply, no stale CORS window.
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
# C2 + Auth — combined auth middleware (mutating endpoints only)
#
# Accepts EITHER a valid API key (machine-to-machine) OR a valid browser
# session token. Enforcement kicks in when:
#   • API_KEY env var is set (existing machine-to-machine gate), OR
#   • An admin account has been created (browser login flow).
#
# Exempt paths: /api/alert-trigger (own ALERT_WEBHOOK_SECRET) and all
# /api/auth/* endpoints (public by design).
# GET endpoints remain open — reads are low-risk and used by the browser
# without credentials. secrets.compare_digest prevents timing attacks.
# ---------------------------------------------------------------------------
class _APIKeyMiddleware(BaseHTTPMiddleware):
    _EXEMPT_PATHS = {"/api/alert-trigger"}
    _AUTH_EXEMPT_PREFIX = "/api/auth/"

    async def dispatch(self, request: Request, call_next):
        path = request.url.path
        if request.method in ("POST", "PATCH"):
            if path not in self._EXEMPT_PATHS and not path.startswith(self._AUTH_EXEMPT_PREFIX):
                need_api_key = bool(settings.api_key)
                need_session = _get_admin() is not None

                if need_api_key or need_session:
                    # Check API key (X-API-Key header)
                    api_key_ok = need_api_key and secrets.compare_digest(
                        request.headers.get("X-API-Key", ""), settings.api_key
                    )
                    # Check session token (Authorization: Bearer <token>)
                    auth_hdr = request.headers.get("Authorization", "")
                    raw_token = auth_hdr[7:].strip() if auth_hdr.startswith("Bearer ") else ""
                    session_ok = bool(raw_token and _validate_session(raw_token))

                    if not api_key_ok and not session_ok:
                        return JSONResponse(
                            {"detail": "Authentication required. Log in via the dashboard or supply X-API-Key."},
                            status_code=401,
                        )
        return await call_next(request)


# ---------------------------------------------------------------------------
# L4 — X-Request-ID middleware
# Reads incoming X-Request-ID or generates a UUID. Sets the context var so
# all log lines within the request carry it, then echoes it in the response.
# ---------------------------------------------------------------------------
class _RequestIDMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        request_id = request.headers.get("X-Request-ID") or str(uuid.uuid4())
        _request_id_var.set(request_id)
        response = await call_next(request)
        response.headers["X-Request-ID"] = request_id
        return response


# Middleware order: last add_middleware call = outermost (first to process request).
# Request path: RequestID → APIKey → CORS → App
app.add_middleware(_APIKeyMiddleware)
app.add_middleware(_RequestIDMiddleware)

# ---------------------------------------------------------------------------
# Tracker singleton — created once, reused on every request.
# ---------------------------------------------------------------------------

_tracker: DecisionTracker | None = None
_registry: AgentRegistry | None = None
_scan_tracker: ScanRunTracker | None = None
_execution_gateway: ExecutionGateway | None = None
_alert_tracker = None  # AlertTracker | None — lazy import to avoid circular deps

# ---------------------------------------------------------------------------
# Scan request model + in-memory scan store
# ---------------------------------------------------------------------------


class ScanRequest(BaseModel):
    """Optional body for POST /api/scan/* endpoints."""

    resource_group: str | None = None
    subscription_id: str | None = None          # override configured subscription
    inventory_mode: str = "existing"            # "existing" | "refresh" | "skip"


class InventoryRefreshRequest(BaseModel):
    """Optional body for POST /api/inventory/refresh."""

    subscription_id: str | None = None   # default: settings.azure_subscription_id
    resource_group: str | None = None    # optional scope filter


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

# ---------------------------------------------------------------------------
# Inventory refresh in-memory state
# Keyed by refresh_id (UUID str).  Values:
#   status          "running" | "complete" | "failed"
#   resource_count  int (set on completion)
#   error           str | None
# ---------------------------------------------------------------------------

_inventory_refreshes: dict[str, dict] = {}

# Module-level CosmosInventoryClient singleton (lazy).
_cosmos_inventory = None


def _get_cosmos_inventory():
    """Return the CosmosInventoryClient singleton."""
    global _cosmos_inventory  # noqa: PLW0603
    if _cosmos_inventory is None:
        from src.infrastructure.cosmos_client import CosmosInventoryClient  # noqa: PLC0415
        _cosmos_inventory = CosmosInventoryClient()
    return _cosmos_inventory


# ---------------------------------------------------------------------------
# Alert in-memory state (mirrors _scans pattern)
# ---------------------------------------------------------------------------

_alerts: dict[str, dict] = {}
_alert_events: dict[str, asyncio.Queue] = {}

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


async def _snapshot_scanned_resources(resource_group: str | None) -> list[dict]:
    """Return a compact list of all Azure resources currently in scope.

    Called once per scan before the agent runs.  In live mode this queries
    Azure Resource Graph; in mock mode it reads seed_resources.json.

    Only minimal fields are kept (id, name, type, location) so the scan record
    stays compact.  Errors are swallowed — a Resource Graph failure must never
    abort a scan.
    """
    try:
        from src.infrastructure.resource_graph import ResourceGraphClient  # noqa: PLC0415
        client = ResourceGraphClient()
        all_resources = await client.list_all_async()
        snapshot = []
        for r in all_resources:
            if not r.get("id") and not r.get("name"):
                continue
            entry: dict = {
                "id":       r.get("id", ""),
                "name":     r.get("name", ""),
                "type":     r.get("type", ""),
                "location": r.get("location", ""),
            }
            if resource_group:
                # Filter to the scoped RG when one was specified
                rg_in_id = (f"/resourceGroups/{resource_group}/").lower()
                if rg_in_id not in entry["id"].lower():
                    continue
            snapshot.append(entry)
        return snapshot
    except Exception as exc:  # noqa: BLE001
        logger.warning("_snapshot_scanned_resources: failed — %s", exc)
        return []


async def _run_agent_scan(
    scan_id: str,
    agent_type: str,
    resource_group: str | None,
    subscription_id: str | None = None,
    inventory_mode: str = "existing",
) -> None:
    """Background coroutine: run one ops agent, evaluate all proposals, persist results.

    Called by FastAPI BackgroundTasks — runs after the HTTP response is sent,
    so the caller receives the scan_id immediately without waiting for the
    (potentially slow) LLM + Azure calls.

    Parameters
    ----------
    scan_id:          UUID string used as the key in ``_scans``.
    agent_type:       One of ``"cost"``, ``"monitoring"``, or ``"deploy"``.
    resource_group:   Optional Azure resource group to scope the scan to.
    subscription_id:  Optional override for the configured Azure subscription.
    inventory_mode:   "existing" | "refresh" | "skip"
    """
    from src.core.pipeline import RuriSkryPipeline
    from src.operational_agents.cost_agent import CostOptimizationAgent
    from src.operational_agents.deploy_agent import DeployAgent
    from src.operational_agents.monitoring_agent import MonitoringAgent

    rg_label = resource_group or "whole subscription"
    sub_id = subscription_id or settings.azure_subscription_id or ""
    logger.info("scan %s (%s): starting — rg=%s inv=%s", scan_id[:8], agent_type, rg_label, inventory_mode)
    await _emit_event(
        scan_id,
        "scan_started",
        agent=agent_type,
        resource_group=rg_label,
        message=f"Starting {agent_type} scan for resource_group={rg_label}",
    )

    # Snapshot all resources in scope now, before the agent runs.
    # This gives the Audit Log a complete picture of what was examined —
    # not just the resources that were flagged.
    scanned_resources = await _snapshot_scanned_resources(resource_group)
    _scans[scan_id]["scanned_resources"] = scanned_resources
    _persist_scan_record(scan_id)
    logger.info(
        "scan %s (%s): resource snapshot — %d resources in scope",
        scan_id[:8], agent_type, len(scanned_resources),
    )

    # ------------------------------------------------------------------
    # Build or fetch inventory based on inventory_mode
    # ------------------------------------------------------------------
    inventory: list[dict] | None = None
    if inventory_mode != "skip" and sub_id:
        try:
            cosmos_inv = _get_cosmos_inventory()
            if inventory_mode == "refresh":
                from src.infrastructure.inventory_builder import build_inventory  # noqa: PLC0415
                await _emit_event(
                    scan_id, "info", agent=agent_type,
                    message="Refreshing resource inventory from Azure…",
                )

                def _on_progress(msg: str) -> None:
                    asyncio.ensure_future(
                        _emit_event(scan_id, "info", agent=agent_type, message=msg)
                    )

                inv_doc = await build_inventory(sub_id, resource_group, on_progress=_on_progress)
                cosmos_inv.upsert(inv_doc)
                inventory = inv_doc.get("resources", [])
                await _emit_event(
                    scan_id, "info", agent=agent_type,
                    message=f"Inventory refreshed — {inv_doc.get('resource_count', 0)} resources.",
                )
            else:  # "existing"
                inv_doc = cosmos_inv.get_latest(sub_id)
                if inv_doc:
                    inventory = inv_doc.get("resources", [])
                    refreshed = inv_doc.get("refreshed_at", "unknown")
                    count = inv_doc.get("resource_count", len(inventory))
                    await _emit_event(
                        scan_id, "info", agent=agent_type,
                        message=f"Using existing inventory ({count} resources, refreshed {refreshed}).",
                    )
                else:
                    await _emit_event(
                        scan_id, "info", agent=agent_type,
                        message="No inventory found — agent will discover resources via tools.",
                    )
        except Exception as _inv_exc:  # noqa: BLE001
            logger.warning(
                "scan %s (%s): inventory fetch failed (%s) — falling back to LLM discovery",
                scan_id[:8], agent_type, _inv_exc,
            )
            await _emit_event(
                scan_id, "info", agent=agent_type,
                message=f"⚠ Inventory unavailable ({_inv_exc}) — agent will discover resources.",
            )

    try:
        # --- Pick the right ops agent and run scan ---
        if agent_type == "cost":
            agent = CostOptimizationAgent()
            proposals = await agent.scan(target_resource_group=resource_group, inventory=inventory)
        elif agent_type == "monitoring":
            agent = MonitoringAgent()
            proposals = await agent.scan(target_resource_group=resource_group, inventory=inventory)
        else:  # "deploy"
            agent = DeployAgent()
            proposals = await agent.scan(target_resource_group=resource_group, inventory=inventory)

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
        # Only re-flag proposals that belong to THIS agent — never cross-contaminate
        # scan records with unresolved issues from other agent types.
        current_agent_id = _AGENT_REGISTRY_NAMES.get(agent_type, "")
        unresolved_pairs = [
            (a, r) for a, r in unresolved_pairs
            if getattr(a, "agent_id", None) == current_agent_id
        ]
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
        # Pass the inventory so the pipeline can resolve REAL Azure resource
        # tags for tag-based policies (POL-DR-001, POL-CRIT-001, POL-PROD-001).
        # Without this, _find_resource only sees seed_resources.json (mock data)
        # and tags={} reaches the policy agent → tag-based policies cannot fire.
        pipeline = RuriSkryPipeline(inventory=inventory)
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


def _get_alert_tracker():
    """Return the durable alert tracker singleton."""
    global _alert_tracker  # noqa: PLW0603
    if _alert_tracker is None:
        from src.core.alert_tracker import AlertTracker
        _alert_tracker = AlertTracker()
    return _alert_tracker


def _persist_alert_record(alert_id: str) -> None:
    """Persist the current in-memory alert record."""
    record = _alerts.get(alert_id)
    if record is None:
        return
    payload = {"id": alert_id, "alert_id": alert_id, **record}
    _get_alert_tracker().upsert(payload)


def _get_alert_record(alert_id: str) -> dict | None:
    """Read alert record from memory first, then durable store."""
    record = _alerts.get(alert_id)
    if record is not None:
        return record
    return _get_alert_tracker().get(alert_id)


async def _emit_alert_event(alert_id: str, event_type: str, **kwargs: Any) -> None:
    """Push one event onto the per-alert SSE queue and append to investigation_log."""
    timestamp = datetime.now(timezone.utc).isoformat()
    event = {
        "event": event_type,
        "timestamp": timestamp,
        "alert_id": alert_id,
        **kwargs,
    }
    queue = _alert_events.get(alert_id)
    if queue is not None:
        await queue.put(event)

    # Persist lightweight log entry so the panel can poll and show a live log
    if event_type != "heartbeat" and alert_id in _alerts:
        log_entry: dict[str, Any] = {
            "event": event_type,
            "timestamp": timestamp,
            "message": kwargs.get("message", ""),
        }
        for key in ("decision", "sri_composite", "count", "execution_status"):
            if key in kwargs:
                log_entry[key] = kwargs[key]
        _alerts[alert_id].setdefault("investigation_log", []).append(log_entry)


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
    offset: int = Query(default=0, ge=0, description="Records to skip (for pagination)"),
    resource_id: str | None = Query(
        default=None, description="Filter by resource ID substring"
    ),
) -> dict:
    """Return recent governance decisions, newest-first.

    Query parameters:
    - **limit**: 1–500, default 20
    - **offset**: records to skip, default 0 (use with limit for pagination)
    - **resource_id**: optional substring filter on the resource ID field
    """
    tracker = _get_tracker()
    if resource_id:
        records = tracker.get_by_resource(resource_id, limit=limit)
    else:
        records = tracker.get_recent(limit=limit, offset=offset)
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
            "executions": {
                "total": 0, "applied": 0, "failed": 0, "pr_created": 0,
                "dismissed": 0, "pending": 0,
                "agent_fix_rate": 0.0, "success_rate": 0.0,
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

    # --- Execution gateway stats ---
    gateway = _get_execution_gateway()
    all_exec = gateway.list_all()
    exec_applied   = sum(1 for r in all_exec if r.status == ExecutionStatus.applied)
    exec_failed    = sum(1 for r in all_exec if r.status == ExecutionStatus.failed)
    exec_pr        = sum(1 for r in all_exec if r.status in (ExecutionStatus.pr_created, ExecutionStatus.pr_merged))
    exec_dismissed = sum(1 for r in all_exec if r.status == ExecutionStatus.dismissed)
    exec_pending   = sum(1 for r in all_exec if r.status in (ExecutionStatus.manual_required, ExecutionStatus.awaiting_review))
    agent_tried    = exec_applied + exec_failed
    executions_block = {
        "total":         len(all_exec),
        "applied":       exec_applied,
        "failed":        exec_failed,
        "pr_created":    exec_pr,
        "dismissed":     exec_dismissed,
        "pending":       exec_pending,
        "agent_fix_rate": round(exec_applied / agent_tried * 100, 1) if agent_tried else 0.0,
        "success_rate":   round(exec_applied / agent_tried * 100, 1) if agent_tried else 0.0,
    }

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
        "executions": executions_block,
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
# Endpoint 8 — alert webhook trigger (async with SSE + dashboard visibility)
# ---------------------------------------------------------------------------


async def _run_alert_investigation(alert_id: str, alert_payload: dict) -> None:
    """Background coroutine: investigate one Azure Monitor alert.

    Mirrors ``_run_agent_scan()`` pattern — creates SSE events, persists
    state transitions, and writes governance verdicts to the audit trail.
    """
    from src.operational_agents.monitoring_agent import MonitoringAgent
    from src.core.pipeline import RuriSkryPipeline

    resource_id = alert_payload.get("resource_id", "unknown")
    metric = alert_payload.get("metric", "unknown")
    logger.info("alert %s: investigating — resource=%s metric=%s", alert_id[:8], resource_id, metric)

    # Transition: pending → investigating
    _alerts[alert_id]["status"] = "investigating"
    _alerts[alert_id]["investigating_at"] = datetime.now(timezone.utc).isoformat()
    _persist_alert_record(alert_id)
    await _emit_alert_event(alert_id, "alert_investigating", message=f"Agent investigating {resource_id}")

    # Fire-and-forget Slack notification — alert received
    try:
        asyncio.create_task(send_alert_notification(
            alert_id=alert_id,
            resource_id=resource_id,
            metric=alert_payload.get("metric", "unknown"),
            severity=alert_payload.get("severity", "unknown"),
            description=alert_payload.get("description", ""),
        ))
    except Exception:
        logger.debug("Slack alert-fired notification task could not be created.", exc_info=True)

    try:
        agent = MonitoringAgent()
        proposals = await agent.scan(alert_payload=alert_payload)

        # Emit scan notes for SSE visibility
        for note in getattr(agent, "scan_notes", []):
            await _emit_alert_event(alert_id, "reasoning", message=note)

        logger.info("alert %s: %d proposal(s) from agent", alert_id[:8], len(proposals))
        await _emit_alert_event(
            alert_id, "discovery",
            count=len(proposals),
            message=f"Agent found {len(proposals)} actionable finding(s).",
        )

        # Evaluate each proposal through the governance pipeline.
        # Load inventory from Cosmos so tag-based policies (POL-DR-001 etc.)
        # can fire on real Azure resources — same fix as _run_agent_scan.
        alert_inventory: list[dict] | None = None
        try:
            sub_id = settings.azure_subscription_id or ""
            if sub_id:
                inv_doc = _get_cosmos_inventory().get_latest(sub_id)
                if inv_doc:
                    alert_inventory = inv_doc.get("resources", [])
        except Exception:
            pass  # inventory unavailable — pipeline falls back to seed topology

        pipeline = RuriSkryPipeline(inventory=alert_inventory)
        tracker = _get_tracker()
        verdicts: list[dict] = []
        approved = escalated = denied = 0

        for i, action in enumerate(proposals, start=1):
            resource_name = action.target.resource_id.split("/")[-1]
            await _emit_alert_event(
                alert_id, "evaluation",
                index=i, total=len(proposals),
                resource_id=resource_name,
                message=f"[{i}/{len(proposals)}] Evaluating {resource_name}…",
            )

            verdict = await pipeline.evaluate(action)
            decision = verdict.decision.value
            sri = verdict.skry_risk_index.sri_composite

            await _emit_alert_event(
                alert_id, "verdict",
                resource_id=resource_name,
                decision=decision,
                sri_composite=sri,
                message=f"Verdict: {decision.upper()} (SRI {sri:.1f}) — {resource_name}",
            )

            tracker.record(verdict)

            # Route through execution gateway
            exec_id: str | None = None
            exec_status_val: str | None = None
            try:
                resource_tags = await _get_resource_tags(action.target.resource_id)
                exec_record = await _get_execution_gateway().process_verdict(verdict, resource_tags)
                exec_id = exec_record.execution_id
                exec_status_val = exec_record.status.value
                await _emit_alert_event(
                    alert_id, "execution",
                    resource_id=resource_name,
                    execution_status=exec_status_val,
                    message=f"Execution: {exec_status_val}",
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning("alert %s: execution gateway failed — %s", alert_id[:8], exc)

            # Store verdict + execution linkage so the dashboard can show action buttons
            verdicts.append({
                **verdict.model_dump(mode="json"),
                "execution_id": exec_id,
                "execution_status": exec_status_val,
            })
            if decision == "approved":
                approved += 1
            elif decision == "escalated":
                escalated += 1
            else:
                denied += 1

            # Update agent registry stats
            registry_name = _AGENT_REGISTRY_NAMES.get("monitoring")
            if registry_name:
                _get_registry().update_agent_stats(registry_name, decision)

        # Transition: investigating → resolved
        _alerts[alert_id].update({
            "status": "resolved",
            "resolved_at": datetime.now(timezone.utc).isoformat(),
            "proposals_count": len(proposals),
            "proposals": [p.model_dump(mode="json") for p in proposals],
            "verdicts": verdicts,
            "totals": {"approved": approved, "escalated": escalated, "denied": denied},
        })
        _persist_alert_record(alert_id)

        # Fire-and-forget Slack notification — investigation complete
        try:
            asyncio.create_task(send_alert_resolved_notification(
                alert_id=alert_id,
                resource_id=resource_id,
                approved=approved,
                escalated=escalated,
                denied=denied,
            ))
        except Exception:
            logger.debug("Slack alert-resolved notification task could not be created.", exc_info=True)

        summary = f"{approved} approved, {escalated} escalated, {denied} denied"
        logger.info("alert %s: resolved — %d verdicts (%s)", alert_id[:8], len(verdicts), summary)
        await _emit_alert_event(
            alert_id, "alert_resolved",
            total_verdicts=len(verdicts),
            summary=summary,
            message=f"Alert resolved — {len(verdicts)} verdict(s): {summary}",
        )

    except Exception as exc:  # noqa: BLE001
        logger.exception("alert %s: investigation failed — %s", alert_id[:8], exc)
        _alerts[alert_id].update({
            "status": "error",
            "resolved_at": datetime.now(timezone.utc).isoformat(),
            "error": str(exc),
        })
        _persist_alert_record(alert_id)
        await _emit_alert_event(alert_id, "alert_error", message=f"Investigation failed: {exc}")


def _normalize_azure_alert_payload(raw: dict[str, Any]) -> dict[str, Any]:
    """Normalise an Azure Monitor webhook payload to our flat internal format.

    Handles four shapes:
    1. Already-flat format  (resource_id / metric keys at top level) — pass-through.
    2. Common Alert Schema  (data.essentials + data.alertContext)
       — used by scheduled-query (Log Alert) rules when use_common_alert_schema=true.
    3. Non-common Log Alert schema  (schemaId = Microsoft.Insights/scheduledQueryRules)
       — used when use_common_alert_schema=false (our default).
    4. Classic Metric Alert schema  (data.context + data.status)
       — used by azurerm_monitor_metric_alert rules (CPU, memory, etc).
       Payload has data.context.resourceId, data.context.condition.allOf[].metricName, etc.

    Returns a dict with keys:  resource_id, metric, value, threshold, severity,
                                resource_group, fired_at, alert_rule_name.
    All keys default to "" / None when not present in the source.
    """
    # Already flat — no Azure Monitor envelope
    if "resource_id" in raw or "metric" in raw:
        return raw

    # ── Shape 4: Classic Metric Alert (data.context) ─────────────────────────
    # azurerm_monitor_metric_alert sends data.context instead of data.essentials.
    # Detect by presence of data.context.resourceId or data.context.condition.
    metric_ctx = raw.get("data", {}).get("context", {})
    if metric_ctx.get("resourceId") or metric_ctx.get("condition"):
        condition = metric_ctx.get("condition", {})
        all_of = condition.get("allOf", [{}])
        first = all_of[0] if all_of else {}
        resource_id = metric_ctx.get("resourceId", "")
        resource_group = metric_ctx.get("resourceGroupName", "")
        metric = first.get("metricName", metric_ctx.get("name", ""))
        value = first.get("metricValue")
        threshold = first.get("threshold")
        sev_raw = str(metric_ctx.get("severity", ""))
        severity = sev_raw.replace("Sev", "").replace("sev", "") if sev_raw else ""
        logger.info(
            "alert-normalizer: classic metric alert schema — resource=%s metric=%s",
            resource_id.split("/")[-1] if "/" in resource_id else resource_id,
            metric,
        )
        return {
            "resource_id": resource_id,
            "resource_name": resource_id.split("/")[-1] if "/" in resource_id else resource_id,
            "metric": metric,
            "value": value,
            "threshold": threshold,
            "severity": severity,
            "resource_group": resource_group,
            "fired_at": metric_ctx.get("timestamp", ""),
            "alert_rule_name": metric_ctx.get("name", ""),
            "description": metric_ctx.get("description", ""),
            "_raw_azure_payload": raw,
        }

    essentials = raw.get("data", {}).get("essentials", {})
    ctx = raw.get("data", {}).get("alertContext", {})

    # ── resolve resource identifier ──────────────────────────────────────────
    # Scheduled-query rules report AffectedConfigurationItems (VM names) and
    # alertTargetIDs (workspace ARM IDs) — prefer the config item (the actual VM).
    config_items: list = ctx.get("AffectedConfigurationItems") or essentials.get("configurationItems") or []
    target_ids: list = essentials.get("alertTargetIDs") or []
    if config_items:
        resource_id = config_items[0]
    elif target_ids:
        resource_id = target_ids[0]
    else:
        resource_id = essentials.get("targetResourceName", "")

    # ── Log Analytics workspace pivot ────────────────────────────────────────
    # For Log Alerts V2 (azurerm_monitor_scheduled_query_rules_alert_v2), Azure
    # always reports the *workspace* as alertTargetID, not the monitored VM.
    # When the query fires because count = 0 (e.g. no heartbeat), there are no
    # result rows, so configurationItems is also empty.
    # Workaround: when resource_id is a Log Analytics workspace, try to extract
    # the actual affected resource name from the alert description or rule name
    # using a regex for common Azure resource naming patterns (vm-*, web-*, etc).
    # If found and we have the subscription + resource group, construct a full
    # VM ARM ID so the MonitoringAgent investigates the right resource.
    if "operationalinsights/workspaces" in resource_id.lower():
        import re as _re
        description = essentials.get("description", "")
        rule_name   = essentials.get("alertRule", "")
        # Match typical Azure resource name patterns: vm-dr-01, web-server-01, etc.
        _hint = (_re.search(r'\b([a-z][a-z0-9]+-[a-z0-9][-a-z0-9]*)\b', description, _re.I)
                 or _re.search(r'\b([a-z][a-z0-9]+-[a-z0-9][-a-z0-9]*)\b', rule_name, _re.I))
        if _hint:
            _name = _hint.group(1)
            # Extract subscription ID and resource group directly from workspace ARM ID:
            # /subscriptions/{sub}/resourceGroups/{rg}/providers/...
            _parts = resource_id.split("/")
            _sub = _parts[2] if len(_parts) > 2 else ""
            _rg  = _parts[4] if len(_parts) > 4 else essentials.get("targetResourceGroup", "")
            if _sub and _rg:
                resource_id = (
                    f"/subscriptions/{_sub}/resourceGroups/{_rg}"
                    f"/providers/Microsoft.Compute/virtualMachines/{_name}"
                )
                logger.info(
                    "alert-normalizer: workspace pivot → extracted resource '%s' from description/rule",
                    _name,
                )

    # ── resource group ────────────────────────────────────────────────────────
    # Try to extract from ARM ID; fall back to essentials field.
    resource_group = ""
    arm_candidate = target_ids[0] if target_ids else resource_id
    parts = arm_candidate.lower().split("/")
    if "resourcegroups" in parts:
        idx = parts.index("resourcegroups")
        if idx + 1 < len(parts):
            resource_group = arm_candidate.split("/")[idx + 1]
    if not resource_group:
        resource_group = essentials.get("targetResourceGroup", "")

    # ── metric / signal details ───────────────────────────────────────────────
    # Metric alerts:  ctx.Condition.{metricName, metricValue, threshold}
    # Log alerts:     ctx.{SearchQuery, Threshold, Operator, ResultCount}
    condition = ctx.get("condition") or ctx.get("Condition") or {}
    metric = (
        condition.get("metricName")
        or (condition.get("allOf") or [{}])[0].get("metricName", "")
        if condition
        else (
            # Log alerts: prefer the human-readable rule name over the raw KQL query
            essentials.get("alertRule", "")
            or ctx.get("SearchQuery", essentials.get("monitoringService", ""))
        )
    )
    value = condition.get("metricValue") if condition else ctx.get("ResultCount")
    threshold = (
        condition.get("threshold")
        or (condition.get("allOf", [{}])[0].get("threshold") if condition else None)
        or ctx.get("Threshold")
    )

    # ── severity ─────────────────────────────────────────────────────────────
    # essentials.severity format: "Sev0" – "Sev4"  →  strip "Sev" prefix
    sev_raw = essentials.get("severity", "3")
    severity = sev_raw.replace("Sev", "").replace("sev", "") if isinstance(sev_raw, str) else str(sev_raw)

    return {
        "resource_id": resource_id,
        "resource_name": resource_id.split("/")[-1] if "/" in resource_id else resource_id,
        "metric": metric or essentials.get("alertRule", ""),
        "value": value,
        "threshold": threshold,
        "severity": severity,
        "resource_group": resource_group,
        "fired_at": essentials.get("firedDateTime", ""),
        "alert_rule_name": essentials.get("alertRule", ""),
        "description": essentials.get("description", ""),
        # Keep raw payload so the agent gets full context
        "_raw_azure_payload": raw,
    }


@app.post("/api/alert-trigger")
async def trigger_alert(
    alert: dict[str, Any],
    authorization: str | None = Header(default=None),
) -> dict:
    """Receive an Azure Monitor alert and create a pending alert record.

    This endpoint acts as an Azure Monitor Action Group webhook target.
    When a metric alert fires (e.g. CPU > 80 % on vm-web-01), Azure POSTs
    the alert details here.  RuriSkry creates an alert record with status
    ``pending`` and returns immediately — no investigation is started
    automatically.  A user must manually trigger investigation via
    ``POST /api/alerts/{alert_id}/investigate``.

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
        ``{"status": "pending", "alert_id": "<uuid>"}`` — immediately.
    """
    # Webhook secret verification — only enforced when ALERT_WEBHOOK_SECRET is set.
    # Azure Monitor Action Group sends the value in the Authorization header as
    # "Bearer <secret>".  Callers that know the URL but not the secret get 401.
    if settings.alert_webhook_secret:
        expected = f"Bearer {settings.alert_webhook_secret}"
        if authorization != expected:
            raise HTTPException(status_code=401, detail="Invalid or missing webhook secret")

    alert = _normalize_azure_alert_payload(alert)
    now = datetime.now(timezone.utc).isoformat()
    alert_id = str(uuid.uuid4())
    resource_id = alert.get("resource_id", "")
    resource_name = alert.get("resource_name") or (resource_id.split("/")[-1] if "/" in resource_id else resource_id)

    # Check for duplicate: same resource + metric already pending/investigating,
    # or resolved/investigated within the last 30 minutes (cooldown window).
    # Azure Monitor evaluates on a schedule — without cooldown, a persistent outage
    # generates a new alert every cycle after each one resolves.
    _ALERT_COOLDOWN_MINUTES = 30
    cutoff = (datetime.now(timezone.utc) - timedelta(minutes=_ALERT_COOLDOWN_MINUTES)).isoformat()
    for existing in _alerts.values():
        if (
            existing.get("resource_id") == resource_id
            and existing.get("metric") == alert.get("metric")
            and (
                existing.get("status") in ("pending", "investigating")
                or (
                    existing.get("status") in ("resolved", "investigated")
                    and (existing.get("resolved_at") or "") >= cutoff
                )
            )
        ):
            logger.info(
                "alert-trigger: duplicate for %s/%s — returning existing %s",
                resource_id, alert.get("metric"), existing.get("alert_id"),
            )
            return {
                "status": existing["status"],
                "alert_id": existing["alert_id"],
                "duplicate": True,
            }

    # Create alert record (pending — investigation is manual, not automatic)
    record: dict[str, Any] = {
        "alert_id": alert_id,
        "status": "pending",
        "resource_id": resource_id,
        "resource_name": resource_name,
        "metric": alert.get("metric", ""),
        "value": alert.get("value"),
        "threshold": alert.get("threshold"),
        "severity": str(alert.get("severity", "3")),
        "resource_group": alert.get("resource_group", ""),
        "fired_at": alert.get("fired_at", now),
        "received_at": now,
        "investigating_at": None,
        "resolved_at": None,
        "proposals_count": 0,
        "proposals": [],
        "verdicts": [],
        "totals": {"approved": 0, "escalated": 0, "denied": 0},
        "error": None,
        "investigation_log": [],
        "alert_payload": alert,
    }
    _alerts[alert_id] = record
    _alert_events[alert_id] = asyncio.Queue()
    _persist_alert_record(alert_id)

    logger.info("alert-trigger: created %s (resource=%s, metric=%s)", alert_id[:8], resource_name, alert.get("metric"))

    return {"status": "pending", "alert_id": alert_id}


# ---------------------------------------------------------------------------
# Alert API endpoints
# ---------------------------------------------------------------------------


@app.get("/api/alerts/active-count")
async def alert_active_count() -> dict:
    """Return count of currently pending or investigating alerts."""
    # Check in-memory first (most responsive), fall back to tracker
    count = sum(
        1 for a in _alerts.values()
        if a.get("status") in ("pending", "investigating")
    )
    if count == 0:
        count = _get_alert_tracker().count_active()
    return {"active_count": count}


@app.get("/api/alerts")
async def list_alerts(
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0, description="Records to skip (for pagination)"),
) -> dict:
    """List all alert records newest-first.

    Returns both in-memory (recent) and durable (historical) alerts,
    merged and deduplicated by alert_id.
    """
    # Merge in-memory + durable records
    seen: set[str] = set()
    merged: list[dict] = []

    # In-memory first (most up-to-date state)
    for aid, record in _alerts.items():
        entry = {"id": aid, "alert_id": aid, **record}
        merged.append(entry)
        seen.add(aid)

    # Durable records not yet in memory (fetch enough to cover offset+limit)
    for record in _get_alert_tracker().get_recent(limit + offset):
        aid = record.get("alert_id") or record.get("id")
        if aid and aid not in seen:
            merged.append(record)
            seen.add(aid)

    # Sort newest-first, apply offset and limit
    merged.sort(key=lambda r: r.get("received_at", ""), reverse=True)
    result = merged[offset:offset + limit]

    # Strip Cosmos internal fields
    cleaned = []
    for r in result:
        entry = {k: v for k, v in r.items() if k not in ("_rid", "_self", "_etag", "_attachments", "_ts")}
        cleaned.append(entry)

    return {"count": len(cleaned), "alerts": cleaned}


@app.get("/api/alerts/{alert_id}/status")
async def alert_status(alert_id: str) -> dict:
    """Return the full alert record for one alert."""
    record = _get_alert_record(alert_id)
    if record is None:
        raise HTTPException(status_code=404, detail=f"Alert {alert_id} not found")
    return {"alert_id": alert_id, **record}


@app.get("/api/alerts/{alert_id}/stream")
async def stream_alert_events(alert_id: str):
    """SSE stream for real-time alert investigation progress.

    Identical pattern to ``stream_scan_events()`` — one event per line,
    terminates on ``alert_resolved`` or ``alert_error``.
    """
    queue = _alert_events.get(alert_id)
    if queue is None:
        raise HTTPException(status_code=404, detail=f"No active SSE stream for alert {alert_id}")

    async def event_generator():
        while True:
            try:
                event = await asyncio.wait_for(queue.get(), timeout=30.0)
            except asyncio.TimeoutError:
                yield f"data: {json.dumps({'event': 'heartbeat'})}\n\n"
                continue

            yield f"data: {json.dumps(event, default=str)}\n\n"
            if event.get("event") in ("alert_resolved", "alert_error"):
                break

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# ---------------------------------------------------------------------------
# Manual investigation trigger
# ---------------------------------------------------------------------------


@app.post("/api/alerts/{alert_id}/investigate")
async def investigate_alert(
    alert_id: str,
    background_tasks: BackgroundTasks,
) -> dict:
    """Manually trigger investigation for a pending alert.

    Looks up the alert record, verifies it is in ``pending`` status, then
    launches ``_run_alert_investigation`` in the background and returns
    immediately.  The caller can subscribe to
    ``GET /api/alerts/{alert_id}/stream`` for real-time SSE progress.

    Returns:
        ``{"status": "investigating", "alert_id": "<uuid>"}``

    Raises:
        404 if the alert does not exist.
        409 if the alert is not in ``pending`` status (already investigating
            or already resolved).
    """
    record = _get_alert_record(alert_id)
    if record is None:
        raise HTTPException(status_code=404, detail=f"Alert {alert_id} not found")

    current_status = record.get("status")
    if current_status != "pending":
        raise HTTPException(
            status_code=409,
            detail=f"Alert {alert_id} is in '{current_status}' status — only pending alerts can be investigated",
        )

    # Ensure the alert is in _alerts (in-memory) so status transitions work.
    # If it was loaded from Cosmos after a restart, bring it into memory first.
    if alert_id not in _alerts:
        _alerts[alert_id] = dict(record)

    # Create SSE queue if not already present (e.g. after a server restart)
    if alert_id not in _alert_events:
        _alert_events[alert_id] = asyncio.Queue()

    alert_payload = record.get("alert_payload") or record
    background_tasks.add_task(_run_alert_investigation, alert_id, alert_payload)

    logger.info("alert %s: manual investigation triggered", alert_id[:8])
    return {"status": "investigating", "alert_id": alert_id}


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
    request: Request,
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
    _check_rate_limit(request.client.host if request.client else "unknown")
    rg = body.resource_group or settings.default_resource_group or None
    sub = body.subscription_id or settings.azure_subscription_id or None
    scan_id, _ = _make_scan_record("cost", rg)
    background_tasks.add_task(_run_agent_scan, scan_id, "cost", rg, sub, body.inventory_mode)
    logger.info("scan %s (cost) started rg=%s inv=%s", scan_id[:8], rg, body.inventory_mode)
    return {"status": "started", "scan_id": scan_id, "agent_type": "cost"}


# ---------------------------------------------------------------------------
# Endpoint 10 — trigger SRE monitoring scan
# ---------------------------------------------------------------------------


@app.post("/api/scan/monitoring")
async def trigger_monitoring_scan(
    request: Request,
    background_tasks: BackgroundTasks,
    body: ScanRequest = Body(default=ScanRequest()),
) -> dict:
    """Trigger a background SRE monitoring agent scan.

    Returns immediately with a ``scan_id``.  Poll
    ``GET /api/scan/{scan_id}/status`` to retrieve results.
    """
    _check_rate_limit(request.client.host if request.client else "unknown")
    rg = body.resource_group or settings.default_resource_group or None
    sub = body.subscription_id or settings.azure_subscription_id or None
    scan_id, _ = _make_scan_record("monitoring", rg)
    background_tasks.add_task(_run_agent_scan, scan_id, "monitoring", rg, sub, body.inventory_mode)
    logger.info("scan %s (monitoring) started rg=%s inv=%s", scan_id[:8], rg, body.inventory_mode)
    return {"status": "started", "scan_id": scan_id, "agent_type": "monitoring"}


# ---------------------------------------------------------------------------
# Endpoint 11 — trigger deploy / security review scan
# ---------------------------------------------------------------------------


@app.post("/api/scan/deploy")
async def trigger_deploy_scan(
    request: Request,
    background_tasks: BackgroundTasks,
    body: ScanRequest = Body(default=ScanRequest()),
) -> dict:
    """Trigger a background infrastructure / security review agent scan.

    Returns immediately with a ``scan_id``.  Poll
    ``GET /api/scan/{scan_id}/status`` to retrieve results.
    """
    _check_rate_limit(request.client.host if request.client else "unknown")
    rg = body.resource_group or settings.default_resource_group or None
    sub = body.subscription_id or settings.azure_subscription_id or None
    scan_id, _ = _make_scan_record("deploy", rg)
    background_tasks.add_task(_run_agent_scan, scan_id, "deploy", rg, sub, body.inventory_mode)
    logger.info("scan %s (deploy) started rg=%s inv=%s", scan_id[:8], rg, body.inventory_mode)
    return {"status": "started", "scan_id": scan_id, "agent_type": "deploy"}


# ---------------------------------------------------------------------------
# Endpoint 12 — trigger all three agents simultaneously
# ---------------------------------------------------------------------------


@app.post("/api/scan/all")
async def trigger_all_scans(
    request: Request,
    background_tasks: BackgroundTasks,
    body: ScanRequest = Body(default=ScanRequest()),
) -> dict:
    """Trigger all three ops agents as independent background scans.

    Three separate scan IDs are returned — one per agent.  Each can be
    polled independently via ``GET /api/scan/{scan_id}/status``.

    Optional body::

        {"resource_group": "ruriskry-prod-rg"}
    """
    _check_rate_limit(request.client.host if request.client else "unknown")
    rg = body.resource_group or settings.default_resource_group or None
    sub = body.subscription_id or settings.azure_subscription_id or None
    scan_ids: list[str] = []
    for agent_type in ("cost", "monitoring", "deploy"):
        scan_id, _ = _make_scan_record(agent_type, rg)
        background_tasks.add_task(_run_agent_scan, scan_id, agent_type, rg, sub, body.inventory_mode)
        scan_ids.append(scan_id)
        logger.info("scan %s (%s) started via /scan/all rg=%s inv=%s", scan_id[:8], agent_type, rg, body.inventory_mode)

    return {"status": "started", "scan_ids": scan_ids}


# ---------------------------------------------------------------------------
# Inventory endpoints (Phase 30)
# ---------------------------------------------------------------------------


async def _run_inventory_refresh(
    refresh_id: str,
    subscription_id: str,
    resource_group: str | None,
) -> None:
    """Background coroutine: build inventory snapshot and persist to Cosmos."""
    from src.infrastructure.inventory_builder import build_inventory  # noqa: PLC0415

    try:
        def _on_progress(msg: str) -> None:
            _inventory_refreshes[refresh_id]["message"] = msg

        inv_doc = await build_inventory(subscription_id, resource_group, on_progress=_on_progress)
        _get_cosmos_inventory().upsert(inv_doc)
        _get_cosmos_inventory().delete_old(subscription_id, keep=5)
        _inventory_refreshes[refresh_id].update({
            "status": "complete",
            "resource_count": inv_doc.get("resource_count", 0),
            "refreshed_at": inv_doc.get("refreshed_at"),
            "type_summary": inv_doc.get("type_summary", {}),
        })
        logger.info(
            "inventory refresh %s: complete — %d resources",
            refresh_id[:8], inv_doc.get("resource_count", 0),
        )
    except Exception as exc:  # noqa: BLE001
        _inventory_refreshes[refresh_id].update({"status": "failed", "error": str(exc)})
        logger.exception("inventory refresh %s failed: %s", refresh_id[:8], exc)


@app.post("/api/inventory/refresh")
async def trigger_inventory_refresh(
    background_tasks: BackgroundTasks,
    body: InventoryRefreshRequest = Body(default=InventoryRefreshRequest()),
) -> dict:
    """Trigger a background resource inventory refresh.

    Fetches ALL resources from Azure Resource Graph, enriches VM power states,
    and persists the snapshot to Cosmos DB.

    Returns immediately with a ``refresh_id`` for polling.
    """
    sub_id = body.subscription_id or settings.azure_subscription_id or ""
    rg = body.resource_group or None
    refresh_id = str(uuid.uuid4())
    _inventory_refreshes[refresh_id] = {
        "status": "running",
        "subscription_id": sub_id,
        "resource_group": rg,
        "started_at": datetime.now(timezone.utc).isoformat(),
        "message": "Starting…",
    }
    background_tasks.add_task(_run_inventory_refresh, refresh_id, sub_id, rg)
    logger.info("inventory refresh %s started sub=%s rg=%s", refresh_id[:8], sub_id[:8] if sub_id else "?", rg)
    return {"status": "started", "refresh_id": refresh_id}


@app.get("/api/inventory/refresh/{refresh_id}")
async def get_inventory_refresh_status(refresh_id: str) -> dict:
    """Poll the status of a background inventory refresh.

    Returns:
        ``{ status: "running"|"complete"|"failed", resource_count?, error?, message? }``
    """
    record = _inventory_refreshes.get(refresh_id)
    if record is None:
        raise HTTPException(status_code=404, detail=f"Refresh '{refresh_id}' not found.")
    return record


@app.get("/api/inventory")
async def get_inventory(
    subscription_id: str | None = Query(default=None),
    summary_only: bool = Query(default=False),
) -> dict:
    """Return the latest resource inventory snapshot.

    Query parameters:
    - **subscription_id**: optional override (default: configured subscription)
    - **summary_only**: if true, omit the ``resources`` array (lightweight)

    Returns 404 if no inventory snapshot exists.
    """
    sub_id = subscription_id or settings.azure_subscription_id or ""
    inv = _get_cosmos_inventory().get_latest(sub_id)
    if inv is None:
        raise HTTPException(status_code=404, detail="No inventory snapshot found.")
    if summary_only:
        inv = {k: v for k, v in inv.items() if k != "resources"}
    return inv


@app.get("/api/inventory/status")
async def get_inventory_status(
    subscription_id: str | None = Query(default=None),
) -> dict:
    """Return lightweight inventory metadata — no resource list.

    Returns:
        ``{ exists, refreshed_at, resource_count, type_summary, age_hours, stale }``
    """
    sub_id = subscription_id or settings.azure_subscription_id or ""
    inv = _get_cosmos_inventory().get_latest(sub_id)
    if inv is None:
        return {"exists": False, "refreshed_at": None, "resource_count": 0,
                "type_summary": {}, "age_hours": None, "stale": True}

    refreshed_at = inv.get("refreshed_at")
    age_hours: float | None = None
    stale = True
    if refreshed_at:
        try:
            ts = datetime.fromisoformat(refreshed_at.replace("Z", "+00:00"))
            age_hours = (datetime.now(timezone.utc) - ts).total_seconds() / 3600
            stale = age_hours > settings.inventory_stale_hours
        except (ValueError, TypeError):
            pass

    return {
        "exists": True,
        "refreshed_at": refreshed_at,
        "resource_count": inv.get("resource_count", 0),
        "type_summary": inv.get("type_summary", {}),
        "age_hours": round(age_hours, 1) if age_hours is not None else None,
        "stale": stale,
    }


# ---------------------------------------------------------------------------
# Endpoint 12b — scan history (operational audit log)
# ---------------------------------------------------------------------------


@app.get("/api/scan-history")
async def list_scan_history(
    limit: int = Query(default=50, ge=1, le=500, description="Max records to return"),
    offset: int = Query(default=0, ge=0, description="Records to skip (for pagination)"),
) -> dict:
    """Return recent scan runs newest-first — one record per scan execution.

    Each record contains:
    - ``scan_id``, ``agent_type``, ``status``, ``started_at``, ``completed_at``
    - ``proposals_count`` — number of resources the agent flagged
    - ``evaluations_count`` — number of governance verdicts produced
    - ``totals`` — ``{approved, escalated, denied}`` breakdown
    - ``proposed_actions`` — list of every resource examined
    - ``evaluations`` — full governance verdict per proposal
    - ``scan_error`` — error message if ``status == "error"``

    This powers the Audit Log tab (scan-level operational view).
    The ``/api/evaluations`` endpoint covers governance-verdict-level detail.
    """
    records = _get_scan_tracker().get_recent(limit=limit, offset=offset)
    # Strip internal Cosmos/_id fields and add computed counts
    cleaned: list[dict] = []
    for r in records:
        entry = {k: v for k, v in r.items() if k not in ("_rid", "_self", "_etag", "_attachments", "_ts")}
        entry.setdefault("proposals_count", len(r.get("proposed_actions", [])))
        entry.setdefault("evaluations_count", len(r.get("evaluations", [])))
        entry.setdefault("scanned_resources_count", len(r.get("scanned_resources", [])))
        cleaned.append(entry)
    return {"count": len(cleaned), "scans": cleaned}


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
    # If cancellation has been requested but the background task hasn't
    # processed it yet (still mid-evaluation), report cancelled immediately.
    # This prevents the two-layer restore in the UI from re-adding it as running.
    effective_status = (
        "cancelled" if scan_id in _scan_cancelled else record.get("status")
    )
    return {
        "scan_id": scan_id,
        **record,
        "status": effective_status,
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
            status = record.get("status")
            if status == "complete":
                msg = f"Scan completed — {record.get('proposals_count', 0)} proposal(s) found. View results in Decisions."
                evt = "scan_complete"
            elif status == "error":
                msg = f"Scan ended with an error: {record.get('scan_error') or 'unknown error'}."
                evt = "scan_error"
            else:
                msg = "Scan log unavailable (backend was restarted while scan was running)."
                evt = "scan_error"
            synthetic = {
                "event": evt,
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "scan_id": scan_id,
                "agent": record.get("agent_type"),
                "message": msg,
                "status": status,
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
# Endpoint 16 — Slack notification status
# ---------------------------------------------------------------------------


@app.get("/api/notification-status")
async def notification_status() -> dict:
    """Return the current Slack notification configuration status.

    The dashboard header uses this to show a 🔔 indicator.
    """
    return {
        "slack_configured": bool(settings.slack_webhook_url),
        "slack_enabled": settings.slack_notifications_enabled,
    }


# ---------------------------------------------------------------------------
# Endpoint 17 — send a test Slack notification
# ---------------------------------------------------------------------------


@app.post("/api/test-notification")
async def test_notification() -> dict:
    """Send a sample DENIED notification to the configured Slack webhook.

    Useful for verifying the Slack integration works without running
    a full governance evaluation.  Returns ``{"status": "sent"}`` on success,
    or ``{"status": "skipped", "reason": "..."}`` if the webhook is not
    configured.
    """
    webhook_url = settings.slack_webhook_url
    if not webhook_url:
        return {"status": "skipped", "reason": "SLACK_WEBHOOK_URL not configured"}
    if not settings.slack_notifications_enabled:
        return {"status": "skipped", "reason": "SLACK_NOTIFICATIONS_ENABLED is false"}

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

    success = await send_verdict_notification(sample_verdict, sample_action)
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


@app.get("/api/execution/{execution_id}/record")
async def get_execution_record(execution_id: str) -> dict:
    """Return a single execution record by its execution_id.

    Used by the dashboard to sync local component state with the real backend
    record after a redeployment or page refresh.

    Returns 404 if the execution_id is unknown.
    """
    gateway = _get_execution_gateway()
    gateway._ensure_loaded()
    record = gateway._records.get(execution_id)
    if not record:
        raise HTTPException(status_code=404, detail=f"Execution record not found: {execution_id!r}")
    return record.model_dump(mode="json")


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
    reviewed_by = _validate_reviewed_by(body.get("reviewed_by", ""))
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
    reviewed_by = _validate_reviewed_by(body.get("reviewed_by", ""))
    reason = body.get("reason", "")
    try:
        record = await gateway.dismiss_execution(execution_id, reviewed_by, reason)
        return record.model_dump(mode="json")
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


# ---------------------------------------------------------------------------
# Endpoints — HITL Agent Fix + PR from manual_required
# ---------------------------------------------------------------------------


@app.get("/api/github/repos")
async def list_github_repos() -> dict:
    """Return GitHub repos accessible via the configured GITHUB_TOKEN.

    Used by the "Create Terraform PR" overlay to populate the repo dropdown.
    Only returns repos that match the configured IAC_GITHUB_REPO — if the token
    has broader access the extras are filtered out and a warning is logged.
    IAC_GITHUB_REPO is always included as a fallback even if the token cannot
    list repos (e.g. fine-grained PATs without list permission).

    Returns 503 if GitHub token is not configured.
    Returns 502 on GitHub API error.
    """
    token = settings.github_token
    if not token:
        raise HTTPException(
            status_code=503,
            detail="GITHUB_TOKEN is not configured — cannot list repos",
        )

    configured_repo = settings.iac_github_repo  # e.g. "psc0des/ruriskry-iac-test"

    try:
        from github import Github, GithubException  # noqa: PLC0415
        gh = Github(token, per_page=100)
        user = gh.get_user()
        # Omit `type` — passing type="all" raises 422 for fine-grained PATs.
        all_repos = [r.full_name for r in user.get_repos(sort="updated")]

        # Security guard: warn if the token can see repos beyond the configured one.
        # This indicates a broader-scope token is in use (e.g. a classic PAT).
        if configured_repo and any(r != configured_repo for r in all_repos):
            unexpected = [r for r in all_repos if r != configured_repo]
            logger.warning(
                "list_github_repos: GITHUB_TOKEN has access to %d unexpected repo(s): %s. "
                "Use a fine-grained PAT scoped only to IAC_GITHUB_REPO (%s).",
                len(unexpected), unexpected, configured_repo,
            )
            # Filter to only the intended repo — do not expose unintended access
            all_repos = [r for r in all_repos if r == configured_repo]

        # Always ensure the configured repo is present (fine-grained PATs may
        # not return it via list if the token lacks metadata:read on other repos)
        if configured_repo and configured_repo not in all_repos:
            all_repos.append(configured_repo)

        repos = sorted(all_repos, key=str.lower)
        return {"repos": repos}

    except Exception as exc:  # noqa: BLE001
        msg = str(exc)
        if "401" in msg or "Bad credentials" in msg:
            detail = "GitHub token is invalid or expired — regenerate GITHUB_TOKEN"
        elif "403" in msg:
            detail = "GitHub token lacks repo access — add 'Contents' + 'Pull requests' permissions"
        else:
            detail = f"GitHub API error: {msg}"
        raise HTTPException(status_code=502, detail=detail) from exc


def _fetch_tf_files_sync(github_token: str, repo_name: str, iac_path: str):
    """Fetch all .tf and .tfvars files from a GitHub repo path (synchronous).

    Returns a list of TfFile instances.  Run via asyncio.to_thread so the
    event loop is not blocked by PyGithub network calls.
    """
    from github import Github  # noqa: PLC0415
    from src.core.tf_block_finder import TfFile  # noqa: PLC0415

    gh = Github(github_token)
    repo = gh.get_repo(repo_name)

    tf_files: list[TfFile] = []
    try:
        items = repo.get_contents(iac_path or "")
    except Exception as exc:  # noqa: BLE001
        logger.warning("resolve_tf_change: get_contents('%s') failed — %s", iac_path, exc)
        return tf_files

    queue = list(items) if not isinstance(items, list) else items
    for item in queue:
        if item.type == "dir":
            try:
                sub = repo.get_contents(item.path)
                queue.extend(sub if isinstance(sub, list) else [sub])
            except Exception:  # noqa: BLE001
                pass
        elif item.name.endswith((".tf", ".tfvars")):
            try:
                content = item.decoded_content.decode("utf-8")
                tf_files.append(TfFile(
                    file_path=item.path,
                    file_sha=item.sha,
                    content=content,
                ))
            except Exception:  # noqa: BLE001
                pass
    return tf_files


@app.post("/api/execution/{execution_id}/resolve-tf-change")
async def resolve_tf_change(
    execution_id: str, body: dict = Body(default={})
) -> dict:
    """Analyse Terraform files to locate the block managing the target resource
    and propose a human-confirmable attribute:value change.

    Request body::

        {
            "iac_repo": "owner/repo",
            "iac_path": "infrastructure/terraform-demo"
        }

    Returns::

        {
            "found": true,
            "tf_block_address": "azurerm_service_plan.prod",
            "file_path": "infrastructure/terraform-demo/main.tf",
            "file_sha": "<git sha>",
            "attribute": "sku_name",
            "current_value": "F1",
            "proposed_value": "S1",
            "confidence": "high | medium | llm_assisted",
            "raw_block_preview": "resource ..."
        }

    Or when the block cannot be found::

        {"found": false, "reason": "..."}
    """
    from src.core.tf_block_finder import (  # noqa: PLC0415
        ARM_TO_TF_TYPE, TF_SKU_ATTRIBUTE,
        find_tf_block, find_tf_block_with_llm,
        get_attribute_value, resolve_config_change_with_llm,
    )

    iac_repo = (body.get("iac_repo") or "").strip()
    iac_path = (body.get("iac_path") or "").strip()

    if not iac_repo:
        raise HTTPException(status_code=400, detail="iac_repo is required")

    gateway = _get_execution_gateway()
    record = gateway.get_record(execution_id)
    if not record:
        raise HTTPException(status_code=404, detail=f"Execution {execution_id!r} not found")
    if not record.verdict_snapshot:
        raise HTTPException(status_code=400, detail="No verdict snapshot on record")

    # Extract action details from the stored verdict snapshot
    snap = record.verdict_snapshot
    action_data = snap.get("proposed_action", {})
    action_type = action_data.get("action_type", "")
    target = action_data.get("target", {})
    resource_type = target.get("resource_type", "")
    resource_id = target.get("resource_id", "")
    resource_name = resource_id.split("/")[-1] if "/" in resource_id else resource_id
    reason = action_data.get("reason", "")
    proposed_sku = target.get("proposed_sku")
    config_changes = action_data.get("config_changes")  # Optional[dict[str, str]]

    # ARM type → TF resource types
    tf_types = ARM_TO_TF_TYPE.get(resource_type.lower(), [])
    if not tf_types:
        return {
            "found": False,
            "reason": (
                f"No Terraform resource type mapping for ARM type '{resource_type}'. "
                f"Supported types: {sorted(ARM_TO_TF_TYPE)}"
            ),
        }

    # Fetch .tf files from GitHub (synchronous PyGithub → thread)
    github_token = settings.github_token
    if not github_token:
        return {"found": False, "reason": "GITHUB_TOKEN not configured"}

    try:
        tf_files = await asyncio.to_thread(
            _fetch_tf_files_sync, github_token, iac_repo, iac_path
        )
    except Exception as exc:  # noqa: BLE001
        return {"found": False, "reason": f"GitHub fetch error: {exc}"}

    if not tf_files:
        return {
            "found": False,
            "reason": f"No .tf files found in {iac_repo}/{iac_path or '(root)'}",
        }

    # Block finder — deterministic passes first, LLM fallback if needed
    block = find_tf_block(tf_files, resource_name, tf_types)
    confidence = "high"

    if not block:
        block = await find_tf_block_with_llm(tf_files, resource_name, tf_types)
        confidence = "llm_assisted"

    if not block:
        return {
            "found": False,
            "reason": (
                f"Could not locate a Terraform block of types {tf_types} "
                f"matching '{resource_name}' in {iac_repo}/{iac_path or '(root)'}"
            ),
        }

    # Determine attribute + proposed_value based on action type
    attribute: str | None = None
    current_value: str | None = None
    proposed_value: str | None = None

    if action_type in ("scale_up", "scale_down"):
        # Known attribute from TF_SKU_ATTRIBUTE; value from proposed_sku on the model
        attribute = TF_SKU_ATTRIBUTE.get(block.tf_type)
        if attribute:
            current_value = get_attribute_value(block, attribute)
        proposed_value = proposed_sku
        if not attribute:
            confidence = "llm_assisted"  # nested SKU (e.g. azurerm_app_service_plan)

    elif config_changes:
        # Agent provided explicit attribute:value pairs — use the first one
        attribute = next(iter(config_changes))
        proposed_value = config_changes[attribute]
        current_value = get_attribute_value(block, attribute)

    else:
        # UPDATE_CONFIG without structured data — ask LLM to determine what to change
        llm_change = await resolve_config_change_with_llm(block, reason, resource_name)
        if llm_change:
            attribute = llm_change.get("attribute")
            proposed_value = llm_change.get("proposed_value")
            current_value = (
                llm_change.get("current_value")
                or (get_attribute_value(block, attribute) if attribute else None)
            )
            confidence = llm_change.get("confidence", "llm_assisted")
        else:
            confidence = "low"

    return {
        "found": True,
        "tf_block_address": block.address,
        "file_path": block.file_path,
        "file_sha": block.file_sha,
        "attribute": attribute,
        "current_value": current_value,
        "proposed_value": proposed_value,
        "confidence": confidence,
        "raw_block_preview": block.raw_block[:800],
    }


@app.post("/api/execution/{execution_id}/create-pr")
async def create_pr_from_manual(
    execution_id: str, body: dict = Body(default={})
) -> dict:
    """Create a Terraform PR from a manual_required execution record.

    Reuses the existing TerraformPRGenerator flow.  If GitHub is not
    configured the record stays ``manual_required`` with an explanatory note.

    Request body (optional)::

        {
            "reviewed_by": "alice@example.com",
            "iac_repo": "owner/repo",
            "iac_path": "infra/terraform",
            "confirmed_change": {
                "tf_block_address": "azurerm_service_plan.prod",
                "file_path": "infrastructure/terraform-demo/main.tf",
                "file_sha": "<sha>",
                "attribute": "sku_name",
                "new_value": "S1"
            }
        }

    ``confirmed_change`` is optional.  When provided (via the 2-step overlay),
    the PR generator patches the existing file instead of creating a stub.

    Returns 404 if execution_id is unknown.
    Returns 400 if the record is not ``manual_required`` or snapshot is missing.
    """
    gateway = _get_execution_gateway()
    reviewed_by = _validate_reviewed_by(body.get("reviewed_by", ""))
    iac_repo = (body.get("iac_repo") or "").strip()
    iac_path = (body.get("iac_path") or "").strip()
    confirmed_change = body.get("confirmed_change") or None  # dict | None
    try:
        record = await gateway.create_pr_from_manual(
            execution_id, reviewed_by,
            iac_repo=iac_repo, iac_path=iac_path,
            confirmed_change=confirmed_change,
        )
        return record.model_dump(mode="json")
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/api/execution/{execution_id}/agent-fix-preview")
async def agent_fix_preview(execution_id: str) -> dict:
    """Generate the LLM-driven execution plan for this issue.

    Pure read — no side effects.  Returns a structured plan with steps,
    summary, estimated impact, rollback hint, and backward-compat commands list.

    Returns 404 if execution_id is unknown.
    Returns 400 if the verdict snapshot is missing.
    """
    gateway = _get_execution_gateway()
    try:
        return await gateway.generate_agent_fix_plan(execution_id)
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
    reviewed_by = _validate_reviewed_by(body.get("reviewed_by", ""))
    try:
        record = await gateway.execute_agent_fix(execution_id, reviewed_by)
        return record.model_dump(mode="json")
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/execution/{execution_id}/rollback")
async def rollback_agent_fix(
    execution_id: str, body: dict = Body(default={})
) -> dict:
    """Reverse a previously applied agent fix.

    Only valid when the record status is ``applied``.
    Uses the ``rollback_hint`` stored in the execution plan.

    Request body (optional)::

        {"reviewed_by": "alice@example.com"}

    Returns 404 if execution_id is unknown.
    Returns 400 if the record is not in ``applied`` state or has no stored plan.
    """
    gateway = _get_execution_gateway()
    reviewed_by = _validate_reviewed_by(body.get("reviewed_by", ""))
    try:
        record = await gateway.rollback_agent_fix(execution_id, reviewed_by)
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
# Endpoint — system configuration (safe — no secrets)
# ---------------------------------------------------------------------------


@app.get("/api/config")
async def get_config() -> dict:
    """Return safe system configuration — no secrets.

    Returns mode, timeout, concurrency, and feature flags.
    """
    mode = "live" if (not settings.use_local_mocks and settings.azure_openai_endpoint) else "mock"
    return {
        "mode": mode,
        "llm_timeout": settings.llm_timeout,
        "llm_concurrency_limit": settings.llm_concurrency_limit,
        "execution_gateway_enabled": settings.execution_gateway_enabled,
        "use_live_topology": settings.use_live_topology,
        "azure_subscription_id": settings.azure_subscription_id,
        "inventory_stale_hours": settings.inventory_stale_hours,
        "version": "1.0.0",
    }


# ---------------------------------------------------------------------------
# Endpoint — health check
# ---------------------------------------------------------------------------


@app.get("/")
async def root() -> dict:
    """Root route — satisfies Azure Container Apps default HTTP liveness probe."""
    return {"status": "ok", "service": settings.service_name}


@app.get("/health")
async def health_check() -> dict:
    """Liveness probe for Container App and deploy scripts."""
    return {"status": "ok"}


# ---------------------------------------------------------------------------
# Auth endpoints — public (no session required)
# ---------------------------------------------------------------------------


@app.get("/api/auth/status")
async def auth_status() -> dict:
    """Check whether the initial admin account has been created.

    The frontend calls this on first load to decide whether to show the
    one-time setup form or the regular login form.
    """
    return {"setup_required": _get_admin() is None}


@app.post("/api/auth/setup")
async def auth_setup(body: dict = Body(default={})) -> dict:
    """Create the initial admin account.

    Only succeeds once — returns 409 if an admin already exists.
    On success returns a session token so the user is logged in immediately
    without a second round-trip.

    Password requirements: ≥ 8 characters.
    """
    if _get_admin() is not None:
        raise HTTPException(status_code=409, detail="Admin account already exists.")
    username = (body.get("username") or "").strip()
    password = body.get("password") or ""
    if not username:
        raise HTTPException(status_code=400, detail="username is required.")
    if len(password) < 8:
        raise HTTPException(status_code=400, detail="password must be at least 8 characters.")
    _save_admin(username, password)
    token = _create_session(username)
    logger.info("Admin account created for '%s'", username)
    return {"token": token, "username": username}


@app.post("/api/auth/login")
async def auth_login(body: dict = Body(default={})) -> dict:
    """Authenticate with username + password.

    Returns a session token valid for 8 hours. The frontend stores this in
    localStorage and sends it as ``Authorization: Bearer <token>`` on every
    mutating request.
    """
    admin = _get_admin()
    if admin is None:
        raise HTTPException(status_code=400, detail="Admin account not set up yet.")
    username = (body.get("username") or "").strip()
    password = body.get("password") or ""
    if not username or not password:
        raise HTTPException(status_code=400, detail="username and password are required.")
    # constant-time comparison for both fields to prevent user enumeration
    username_ok = secrets.compare_digest(username.lower(), admin["username"].lower())
    pw_ok = _verify_password(password, admin["salt"], admin["pw_hash"])
    if not username_ok or not pw_ok:
        raise HTTPException(status_code=401, detail="Invalid username or password.")
    token = _create_session(admin["username"])
    logger.info("Successful login for '%s'", admin["username"])
    return {"token": token, "username": admin["username"]}


@app.get("/api/auth/me")
async def auth_me(request: Request) -> dict:
    """Validate the current session token and return the logged-in username.

    Called by the frontend on page load to check if a stored token is still
    valid before deciding whether to show the app or the login screen.
    """
    auth_hdr = request.headers.get("Authorization", "")
    raw_token = auth_hdr[7:].strip() if auth_hdr.startswith("Bearer ") else ""
    username = _validate_session(raw_token) if raw_token else None
    if not username:
        raise HTTPException(status_code=401, detail="Session expired or invalid. Please log in again.")
    return {"username": username}


@app.post("/api/auth/logout")
async def auth_logout(request: Request) -> dict:
    """Revoke the current session token.

    Exempt from the auth middleware — a user should always be able to log out
    even if they somehow hold an already-expired token.
    """
    auth_hdr = request.headers.get("Authorization", "")
    raw_token = auth_hdr[7:].strip() if auth_hdr.startswith("Bearer ") else ""
    if raw_token and raw_token in _sessions:
        del _sessions[raw_token]
    return {"status": "logged out"}


# ---------------------------------------------------------------------------
# Endpoint — dev/test reset (local JSON mode only)
# ---------------------------------------------------------------------------


@app.post("/api/admin/reset")
async def admin_reset(request: Request) -> dict:
    """⚠ Development/testing only — wipe all local data and reset in-memory state.

    Deletes every JSON file in:
    - ``data/decisions/``  (governance verdicts / audit trail)
    - ``data/executions/`` (execution gateway records)
    - ``data/scans/``      (scan run history)
    - ``data/alerts/``     (alert investigation records)

    Also resets the in-memory scan + alert stores so the dashboard
    shows a clean slate immediately without restarting the server.

    Only operates on local JSON files — Cosmos DB data is never touched.

    Raises 403 in live mode (USE_LOCAL_MOCKS=false) to prevent accidental
    state wipes in deployed environments.

    Returns a summary of how many files were deleted per store.
    """
    if not settings.use_local_mocks:
        raise HTTPException(
            status_code=403,
            detail="admin/reset is disabled in live mode (USE_LOCAL_MOCKS=false). "
                   "Set USE_LOCAL_MOCKS=true or use your database console to clear data.",
        )

    from src.infrastructure.cosmos_client import _DEFAULT_DECISIONS_DIR
    from src.core.execution_gateway import _DEFAULT_EXECUTIONS_DIR
    from src.core.scan_run_tracker import _DEFAULT_SCANS_DIR
    from src.core.alert_tracker import _DEFAULT_ALERTS_DIR

    deleted: dict[str, int] = {}

    for label, directory in [
        ("decisions", _DEFAULT_DECISIONS_DIR),
        ("executions", _DEFAULT_EXECUTIONS_DIR),
        ("scans", _DEFAULT_SCANS_DIR),
        ("alerts", _DEFAULT_ALERTS_DIR),
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

    # Reset in-memory scan + alert stores so the dashboard reflects the
    # clean state without needing a server restart.
    _scans.clear()
    _scan_cancelled.clear()
    _alerts.clear()

    # Reset the in-memory execution gateway so it doesn't serve stale records
    # from before the wipe.
    global _execution_gateway, _alert_tracker  # noqa: PLW0603
    _execution_gateway = None
    _alert_tracker = None

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
