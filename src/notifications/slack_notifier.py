"""Slack Incoming Webhook notifier for RuriSkry governance events.

Three notification types:
1. send_verdict_notification  — DENIED / ESCALATED governance verdicts
2. send_alert_notification    — Azure Monitor alert received + investigation started
3. send_alert_resolved_notification — alert investigation complete (findings summary)

Design decisions
-----------------
- Fire-and-forget: callers use asyncio.create_task() — notifications never block.
- Silent skip: empty SLACK_WEBHOOK_URL or SLACK_NOTIFICATIONS_ENABLED=false returns
  True immediately — zero-config deployments work out of the box.
- Shared httpx.AsyncClient: module-level persistent client reuses TCP connections and
  TLS sessions across all notifications — avoids a new TLS handshake per call.
- Rate limiting: Slack webhooks allow ~1 msg/sec. A lock + minimum-interval guard
  queues concurrent notifications and spaces them _MIN_INTERVAL_S apart.
- Smart retry: 4xx errors are NOT retried (bad payload won't self-heal).
  429 respects the Retry-After header (capped at 30 s).
  5xx and network errors use exponential backoff (2 s → 4 s), up to 3 attempts.
- Structured logging: all log calls include an `extra` dict for centralized log
  queries (Azure Monitor / Log Analytics / Datadog).
- Never raises: all exceptions caught and logged — notification failure never
  affects governance outcomes.
- Block Kit: uses Slack `attachments` with colored sidebar for visual triage.
  Color: #E53E3E (red) = DENIED, #D69E2E (amber) = ESCALATED / alert fired,
         #38A169 (green) = alert resolved with no denials, #E53E3E if any denials.
"""

import asyncio
import logging
import time

import httpx

from src.config import settings
from src.core.models import GovernanceVerdict, ProposedAction, SRIVerdict

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Colour + label mapping
# ---------------------------------------------------------------------------

_VERDICT_STYLE: dict[SRIVerdict, dict] = {
    SRIVerdict.DENIED: {
        "color": "#E53E3E",
        "emoji": "🚫",
        "label": "DENIED",
    },
    SRIVerdict.ESCALATED: {
        "color": "#D69E2E",
        "emoji": "⚠️",
        "label": "ESCALATED",
    },
}

# ---------------------------------------------------------------------------
# Shared HTTP client — one connection pool for the process lifetime
# ---------------------------------------------------------------------------

_client: httpx.AsyncClient | None = None
_client_lock = asyncio.Lock()


async def _get_client() -> httpx.AsyncClient:
    """Return the shared AsyncClient, creating it on first call (lazy singleton)."""
    global _client
    if _client is None or _client.is_closed:
        async with _client_lock:
            if _client is None or _client.is_closed:
                _client = httpx.AsyncClient(
                    timeout=float(settings.slack_timeout),
                    headers={"Content-Type": "application/json"},
                )
    return _client


# ---------------------------------------------------------------------------
# Rate limiter — Slack allows ~1 msg/sec per webhook URL
# ---------------------------------------------------------------------------

_rate_lock = asyncio.Lock()
_last_send_at: float = 0.0
_MIN_INTERVAL_S: float = 1.1  # 10% safety margin above Slack's 1 msg/sec limit


async def _acquire_rate_slot() -> None:
    """Block until at least _MIN_INTERVAL_S has elapsed since the last send."""
    global _last_send_at
    async with _rate_lock:
        now = time.monotonic()
        wait = _last_send_at + _MIN_INTERVAL_S - now
        if wait > 0:
            await asyncio.sleep(wait)
        _last_send_at = time.monotonic()


# ---------------------------------------------------------------------------
# localhost URL warning — fires once so production misconfiguration is visible
# ---------------------------------------------------------------------------

_localhost_warned: bool = False


def _warn_localhost_once() -> None:
    """Log a warning once if DASHBOARD_URL still points to localhost."""
    global _localhost_warned
    if not _localhost_warned and "localhost" in settings.dashboard_url:
        logger.warning(
            "Slack notifications: DASHBOARD_URL is '%s'. The 'View in Dashboard' "
            "button in Slack messages will link to localhost. "
            "Set DASHBOARD_URL to the production Static Web App URL.",
            settings.dashboard_url,
            extra={
                "event": "slack_config_warning",
                "dashboard_url": settings.dashboard_url,
            },
        )
        _localhost_warned = True


# ---------------------------------------------------------------------------
# Guards shared by all three send functions
# ---------------------------------------------------------------------------


def _should_skip() -> bool:
    """Return True when Slack is not configured or disabled."""
    if not settings.slack_webhook_url:
        logger.debug(
            "Slack notification skipped — SLACK_WEBHOOK_URL not set.",
            extra={"event": "slack_skipped", "reason": "no_webhook_url"},
        )
        return True
    if not settings.slack_notifications_enabled:
        logger.debug(
            "Slack notification skipped — SLACK_NOTIFICATIONS_ENABLED is false.",
            extra={"event": "slack_skipped", "reason": "disabled"},
        )
        return True
    return False


# ---------------------------------------------------------------------------
# Core HTTP sender — smart retry with categorised error handling
# ---------------------------------------------------------------------------


async def _post(payload: dict, *, notification_type: str = "unknown") -> bool:
    """POST payload to the configured webhook URL. Never raises.

    Retry strategy:
    - 4xx (except 429): immediate fail — bad payload will not self-heal on retry.
    - 429: respect Retry-After header (capped at 30 s), then retry once more.
    - 5xx / network error: exponential backoff (2 s → 4 s), up to 3 attempts.
    - Other unexpected errors: immediate fail, no retry.
    """
    url = settings.slack_webhook_url
    log_extra: dict = {"event": "slack_post", "notification_type": notification_type}
    client = await _get_client()
    backoff = 2.0
    max_attempts = 3

    for attempt in range(1, max_attempts + 1):
        try:
            await _acquire_rate_slot()
            response = await client.post(url, json=payload)
            response.raise_for_status()
            return True

        except httpx.HTTPStatusError as exc:
            status = exc.response.status_code

            if status == 429:
                retry_after = min(
                    float(exc.response.headers.get("Retry-After", backoff)),
                    30.0,
                )
                logger.warning(
                    "Slack rate-limited (429) on attempt %d/%d — waiting %.1f s.",
                    attempt, max_attempts, retry_after,
                    extra={**log_extra, "http_status": status, "attempt": attempt},
                )
                if attempt < max_attempts:
                    await asyncio.sleep(retry_after)
                    continue
                logger.error(
                    "Slack notification permanently rate-limited after %d attempts.",
                    max_attempts,
                    extra={**log_extra, "http_status": status},
                )
                return False

            if 400 <= status < 500:
                # Client error — bad payload, revoked webhook, etc. Do not retry.
                logger.error(
                    "Slack notification rejected (HTTP %d) — not retrying.",
                    status,
                    extra={**log_extra, "http_status": status, "attempt": attempt},
                )
                return False

            # 5xx server error — fall through to retry logic below
            logger.warning(
                "Slack server error (HTTP %d) on attempt %d/%d.",
                status, attempt, max_attempts,
                extra={**log_extra, "http_status": status, "attempt": attempt},
            )

        except (httpx.ConnectTimeout, httpx.ReadTimeout, httpx.NetworkError) as exc:
            logger.warning(
                "Slack network error on attempt %d/%d: %s",
                attempt, max_attempts, type(exc).__name__,
                extra={
                    **log_extra,
                    "error_type": type(exc).__name__,
                    "attempt": attempt,
                },
            )

        except Exception as exc:
            # Unexpected error (e.g. invalid URL, serialisation failure) — abort.
            logger.error(
                "Slack notification unexpected error — aborting: %s",
                exc,
                extra={**log_extra, "error_type": type(exc).__name__},
                exc_info=True,
            )
            return False

        # Wait before next attempt (5xx or network-error path)
        if attempt < max_attempts:
            await asyncio.sleep(backoff)
            backoff *= 2  # 2 s → 4 s

    logger.error(
        "Slack notification failed after %d attempts.",
        max_attempts,
        extra={**log_extra, "attempts": max_attempts},
    )
    return False


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


async def send_verdict_notification(
    verdict: GovernanceVerdict,
    proposed_action: ProposedAction,
) -> bool:
    """Post a Block Kit message for DENIED or ESCALATED governance verdicts.

    APPROVED verdicts are silently skipped (return True).
    """
    if verdict.decision == SRIVerdict.APPROVED:
        return True
    if _should_skip():
        return True

    _warn_localhost_once()
    payload = _build_verdict_payload(verdict, proposed_action)
    success = await _post(payload, notification_type="verdict")
    if success:
        logger.info(
            "Slack verdict notification sent.",
            extra={
                "event": "slack_sent",
                "notification_type": "verdict",
                "verdict": verdict.decision.value.upper(),
                "resource_id": proposed_action.target.resource_id,
                "action_id": verdict.action_id,
                "sri_composite": verdict.skry_risk_index.sri_composite,
            },
        )
    return success


async def send_alert_notification(
    alert_id: str,
    resource_id: str,
    metric: str,
    severity: str,
    description: str = "",
) -> bool:
    """Post a Block Kit message when an Azure Monitor alert is received."""
    if _should_skip():
        return True

    _warn_localhost_once()
    resource_name = (resource_id.split("/")[-1] if "/" in resource_id else resource_id)[:100]
    desc_block = (
        [{"type": "section", "text": {"type": "mrkdwn", "text": f"_{description[:200]}_"}}]
        if description else []
    )

    payload = {
        "attachments": [{
            "color": "#D69E2E",
            "blocks": [
                {
                    "type": "header",
                    "text": {
                        "type": "plain_text",
                        "text": "🔔 Azure Monitor Alert — Investigation Started",
                        "emoji": True,
                    },
                },
                {
                    "type": "section",
                    "fields": [
                        {"type": "mrkdwn", "text": f"*Resource*\n`{resource_name}`"},
                        {"type": "mrkdwn", "text": f"*Metric*\n{metric}"},
                        {"type": "mrkdwn", "text": f"*Severity*\n{severity}"},
                        {"type": "mrkdwn", "text": f"*Alert ID*\n`{alert_id[:8]}…`"},
                    ],
                },
                *desc_block,
                {"type": "divider"},
                {
                    "type": "actions",
                    "elements": [{
                        "type": "button",
                        "text": {"type": "plain_text", "text": "View in Dashboard", "emoji": True},
                        "url": f"{settings.dashboard_url}/alerts",
                        "style": "primary",
                    }],
                },
            ],
        }],
    }

    success = await _post(payload, notification_type="alert_fired")
    if success:
        logger.info(
            "Slack alert-fired notification sent.",
            extra={
                "event": "slack_sent",
                "notification_type": "alert_fired",
                "alert_id": alert_id,
                "resource_id": resource_id,
                "metric": metric,
                "severity": severity,
            },
        )
    return success


async def send_alert_resolved_notification(
    alert_id: str,
    resource_id: str,
    approved: int,
    escalated: int,
    denied: int,
) -> bool:
    """Post a Block Kit summary when an alert investigation completes."""
    if _should_skip():
        return True

    _warn_localhost_once()
    resource_name = (resource_id.split("/")[-1] if "/" in resource_id else resource_id)[:100]
    total = approved + escalated + denied
    color = "#E53E3E" if denied > 0 else ("#D69E2E" if escalated > 0 else "#38A169")
    headline = (
        f"{'🚫' if denied else ('⚠️' if escalated else '✅')} "
        f"Alert Investigated — `{resource_name}`"
    )

    payload = {
        "attachments": [{
            "color": color,
            "blocks": [
                {
                    "type": "header",
                    "text": {
                        "type": "plain_text",
                        "text": f"Alert Investigated — {resource_name}",
                        "emoji": True,
                    },
                },
                {
                    "type": "section",
                    "text": {"type": "mrkdwn", "text": headline},
                },
                {
                    "type": "section",
                    "fields": [
                        {"type": "mrkdwn", "text": f"*Findings*\n{total}"},
                        {"type": "mrkdwn", "text": f"*Approved*\n✅ {approved}"},
                        {"type": "mrkdwn", "text": f"*Escalated*\n⚠️ {escalated}"},
                        {"type": "mrkdwn", "text": f"*Denied*\n🚫 {denied}"},
                    ],
                },
                {"type": "divider"},
                {
                    "type": "actions",
                    "elements": [{
                        "type": "button",
                        "text": {"type": "plain_text", "text": "View in Dashboard", "emoji": True},
                        "url": f"{settings.dashboard_url}/alerts",
                        "style": "primary",
                    }],
                },
            ],
        }],
    }

    success = await _post(payload, notification_type="alert_resolved")
    if success:
        logger.info(
            "Slack alert-resolved notification sent.",
            extra={
                "event": "slack_sent",
                "notification_type": "alert_resolved",
                "alert_id": alert_id,
                "resource_id": resource_id,
                "total": total,
                "approved": approved,
                "escalated": escalated,
                "denied": denied,
            },
        )
    return success


# ---------------------------------------------------------------------------
# Payload builder — verdicts
# ---------------------------------------------------------------------------


def _build_verdict_payload(verdict: GovernanceVerdict, action: ProposedAction) -> dict:
    """Build the Slack Block Kit payload for a DENIED or ESCALATED verdict.

    Layout:
    ┌─────────────────────────────────────────────────────┐
    │ [header] 🚫 DENIED — RuriSkry™ Governance Alert     │
    │ [fields]  Resource | Action | Agent | SRI™          │
    │ [section] Infra · Policy · Historical · Cost        │
    │ [section] Reason: ...                               │
    │ [section] ⚠ Top Violation: POL-DR-001 (optional)   │
    │ [divider]                                           │
    │ [button]  View in Dashboard                         │
    └─────────────────────────────────────────────────────┘
    """
    style = _VERDICT_STYLE[verdict.decision]
    sri = verdict.skry_risk_index
    resource_name = (
        action.target.resource_id.split("/")[-1]
        if "/" in action.target.resource_id
        else action.target.resource_id
    )[:100]  # guard against very long ARM IDs
    reason = verdict.reason[:300] + "…" if len(verdict.reason) > 300 else verdict.reason

    # Top policy violation
    agent_results = verdict.agent_results or {}
    policy_data = agent_results.get("policy_compliance", agent_results.get("policy", {}))
    violations = policy_data.get("violations", [])
    top_violation: str | None = None
    if violations:
        v = violations[0]
        if isinstance(v, dict):
            top_violation = f"{v.get('policy_id', 'N/A')} — {v.get('name', 'Unknown')}"
        else:
            top_violation = str(v)

    blocks: list[dict] = [
        {
            "type": "header",
            "text": {
                "type": "plain_text",
                "text": f"{style['emoji']} {style['label']} — RuriSkry™ Governance Alert",
                "emoji": True,
            },
        },
        {
            "type": "section",
            "fields": [
                {"type": "mrkdwn", "text": f"*Resource*\n`{resource_name}`"},
                {"type": "mrkdwn", "text": f"*Action*\n`{action.action_type.value}`"},
                {"type": "mrkdwn", "text": f"*Agent*\n{action.agent_id}"},
                {"type": "mrkdwn", "text": f"*SRI™ Composite*\n{sri.sri_composite:.1f} / 100"},
            ],
        },
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": (
                    f"*SRI™ Breakdown*  "
                    f"Infra `{sri.sri_infrastructure:.0f}` · "
                    f"Policy `{sri.sri_policy:.0f}` · "
                    f"Historical `{sri.sri_historical:.0f}` · "
                    f"Cost `{sri.sri_cost:.0f}`"
                ),
            },
        },
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": f"*Reason*\n{reason}"},
        },
    ]

    if top_violation:
        blocks.append({
            "type": "section",
            "text": {"type": "mrkdwn", "text": f"⚠ *Top Violation*  {top_violation}"},
        })

    blocks.append({"type": "divider"})
    blocks.append({
        "type": "actions",
        "elements": [{
            "type": "button",
            "text": {"type": "plain_text", "text": "View in Dashboard", "emoji": True},
            "url": settings.dashboard_url,
            "style": "primary",
        }],
    })

    return {
        "attachments": [{
            "color": style["color"],
            "blocks": blocks,
        }],
    }
