"""Microsoft Teams webhook notifier for DENIED and ESCALATED verdicts.

When a governance evaluation results in a DENIED or ESCALATED verdict,
this module sends a rich Adaptive Card notification to a Microsoft Teams
channel via an Incoming Webhook URL.

Design decisions
-----------------
- **Fire-and-forget** — the caller uses ``asyncio.create_task()`` so the
  notification never blocks the governance pipeline.
- **Silent skip** — if ``TEAMS_WEBHOOK_URL`` is empty the function returns
  immediately without logging an error.  This lets every deployment work
  out-of-the-box with zero config.
- **Retry once** — on transient network failure we retry after a 2-second
  pause.  Two attempts keeps latency low while covering momentary blips.
- **Never raises** — all exceptions are caught and logged so a notification
  failure can never affect governance outcomes.

Adaptive Card
-------------
Microsoft Teams Incoming Webhooks accept an Adaptive Card JSON payload.
The card shows: verdict badge, resource info, agent name, SRI™ composite
with mini breakdown, agent reasoning (truncated to 300 chars), top policy
violation if any, a "View in Dashboard" link, and a timestamp.
"""

import asyncio
import logging
from datetime import timezone

import httpx

from src.config import settings
from src.core.models import GovernanceVerdict, ProposedAction, SRIVerdict

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Adaptive Card colour mapping
# ---------------------------------------------------------------------------

_VERDICT_STYLE = {
    SRIVerdict.DENIED: {
        "badge": "🚫 DENIED",
        "color": "attention",      # red in Adaptive Cards
    },
    SRIVerdict.ESCALATED: {
        "badge": "⚠️ ESCALATED",
        "color": "warning",        # orange/amber in Adaptive Cards
    },
}


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


async def send_teams_notification(
    verdict: GovernanceVerdict,
    proposed_action: ProposedAction,
) -> bool:
    """POST an Adaptive Card to the configured Teams Incoming Webhook.

    Parameters
    ----------
    verdict:
        The governance verdict.  Only DENIED and ESCALATED verdicts trigger
        a notification — APPROVED verdicts are silently skipped.
    proposed_action:
        The proposed action that was evaluated (used for resource and agent
        details in the card).

    Returns
    -------
    bool
        ``True`` if the notification was sent (or skipped because the
        verdict was APPROVED).  ``False`` if sending failed after retries.
    """
    # ── Guard: skip APPROVED verdicts ──────────────────────────────────
    if verdict.decision == SRIVerdict.APPROVED:
        return True

    # ── Guard: skip if webhook not configured ─────────────────────────
    webhook_url = settings.teams_webhook_url
    if not webhook_url:
        logger.debug("Teams notification skipped — TEAMS_WEBHOOK_URL not set.")
        return True

    if not settings.teams_notifications_enabled:
        logger.debug("Teams notification skipped — TEAMS_NOTIFICATIONS_ENABLED is false.")
        return True

    # ── Build the Adaptive Card payload ───────────────────────────────
    card = _build_adaptive_card(verdict, proposed_action)

    # ── Send with one retry ───────────────────────────────────────────
    for attempt in range(2):
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                response = await client.post(webhook_url, json=card)
                response.raise_for_status()

            logger.info(
                "Teams notification sent for %s verdict on %s (action_id=%s).",
                verdict.decision.value.upper(),
                proposed_action.target.resource_id,
                verdict.action_id,
            )
            return True

        except Exception:
            if attempt == 0:
                logger.warning(
                    "Teams notification failed (attempt 1/2) for action_id=%s — retrying in 2 s.",
                    verdict.action_id,
                )
                await asyncio.sleep(2)
            else:
                logger.error(
                    "Teams notification failed (attempt 2/2) for action_id=%s — giving up.",
                    verdict.action_id,
                    exc_info=True,
                )
                return False

    return False  # pragma: no cover — unreachable but keeps mypy happy


# ---------------------------------------------------------------------------
# Adaptive Card builder
# ---------------------------------------------------------------------------


def _build_adaptive_card(
    verdict: GovernanceVerdict,
    action: ProposedAction,
) -> dict:
    """Build a Microsoft Teams Adaptive Card JSON payload.

    The card layout:
    ┌──────────────────────────────────────────────┐
    │  🚫 DENIED  (or ⚠️ ESCALATED)                │
    │  Resource: vm-dr-01   ID: /subscriptions/... │
    │  Agent: cost-optimization-agent              │
    │  SRI™ Composite: 77.0                        │
    │    Infra: 65  Policy: 100  Hist: 62  Cost: 45│
    │  Reason: VM idle for 30 days…                │
    │  ⚠ Top violation: POL-DR-001                 │
    │  [View in Dashboard]                         │
    │  2026-03-02 08:05:24 UTC                     │
    └──────────────────────────────────────────────┘
    """
    style = _VERDICT_STYLE.get(verdict.decision, _VERDICT_STYLE[SRIVerdict.DENIED])
    sri = verdict.sentinel_risk_index

    # Resource name — extract short name from Azure resource ID
    resource_name = action.target.resource_id.split("/")[-1] if "/" in action.target.resource_id else action.target.resource_id

    # Truncate reasoning to 300 characters
    reason = verdict.reason[:300] + "…" if len(verdict.reason) > 300 else verdict.reason

    # Top policy violation (if any)
    agent_results = verdict.agent_results or {}
    policy_data = agent_results.get("policy_compliance", {})
    violations = policy_data.get("violations", [])
    top_violation = None
    if violations:
        v = violations[0]
        if isinstance(v, dict):
            top_violation = f"{v.get('policy_id', 'N/A')} — {v.get('name', 'Unknown')}"
        else:
            top_violation = str(v)

    # Timestamp formatted
    ts = verdict.timestamp
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    ts_str = ts.strftime("%Y-%m-%d %H:%M:%S UTC")

    # Build card body facts
    body: list[dict] = [
        # ── Verdict badge ─────────────────────────────────────────────
        {
            "type": "TextBlock",
            "text": style["badge"],
            "size": "Large",
            "weight": "Bolder",
            "color": style["color"],
        },
        # ── Resource info ─────────────────────────────────────────────
        {
            "type": "FactSet",
            "facts": [
                {"title": "Resource", "value": resource_name},
                {"title": "Resource ID", "value": action.target.resource_id},
                {"title": "Agent", "value": action.agent_id},
                {"title": "Action", "value": action.action_type.value},
            ],
        },
        # ── SRI™ Composite ────────────────────────────────────────────
        {
            "type": "TextBlock",
            "text": f"**SRI™ Composite: {sri.sri_composite:.1f}**",
            "spacing": "Medium",
        },
        {
            "type": "TextBlock",
            "text": (
                f"Infra: {sri.sri_infrastructure:.0f}  |  "
                f"Policy: {sri.sri_policy:.0f}  |  "
                f"Historical: {sri.sri_historical:.0f}  |  "
                f"Cost: {sri.sri_cost:.0f}"
            ),
            "size": "Small",
            "isSubtle": True,
        },
        # ── Reasoning ─────────────────────────────────────────────────
        {
            "type": "TextBlock",
            "text": f"**Reason:** {reason}",
            "wrap": True,
            "spacing": "Medium",
        },
    ]

    # ── Top violation (optional) ──────────────────────────────────────
    if top_violation:
        body.append({
            "type": "TextBlock",
            "text": f"⚠ **Top violation:** {top_violation}",
            "color": "attention",
            "spacing": "Small",
        })

    # ── Timestamp ─────────────────────────────────────────────────────
    body.append({
        "type": "TextBlock",
        "text": ts_str,
        "size": "Small",
        "isSubtle": True,
        "spacing": "Medium",
    })

    # Build the full card envelope
    card = {
        "type": "message",
        "attachments": [
            {
                "contentType": "application/vnd.microsoft.card.adaptive",
                "contentUrl": None,
                "content": {
                    "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
                    "type": "AdaptiveCard",
                    "version": "1.4",
                    "body": body,
                    "actions": [
                        {
                            "type": "Action.OpenUrl",
                            "title": "View in Dashboard",
                            "url": settings.dashboard_url,
                        }
                    ],
                },
            }
        ],
    }

    return card
