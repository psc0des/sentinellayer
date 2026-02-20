"""SentinelLayer MCP Server — exposes governance tools to any AI agent.

Registers three MCP tools using FastMCP so that any MCP-capable client
(Claude Desktop, Copilot, etc.) can invoke SentinelLayer governance directly:

Tools
-----
sentinel_evaluate_action
    Accepts a description of a proposed infrastructure change, runs it through
    the full SentinelLayer pipeline (all four governance agents in parallel),
    records the verdict in the local audit trail, and returns a structured
    verdict with the SRI(tm) breakdown.

sentinel_query_history
    Returns recent governance decisions from the local audit trail, optionally
    filtered by resource ID.

sentinel_get_risk_profile
    Returns an aggregated risk summary for a specific resource — decision
    counts, average SRI composite, and the most commonly violated policies.

Usage (stdio transport, default for MCP clients)
-------------------------------------------------
    python -m src.mcp_server.server

or configure in your MCP client's settings.json:

    {
      "command": "python",
      "args": ["-m", "src.mcp_server.server"],
      "cwd": "<project root>"
    }
"""

import logging

from mcp.server.fastmcp import FastMCP

from src.core.decision_tracker import DecisionTracker
from src.core.models import (
    ActionTarget,
    ActionType,
    ProposedAction,
    Urgency,
)
from src.core.pipeline import SentinelLayerPipeline

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# FastMCP application instance
# ---------------------------------------------------------------------------

mcp = FastMCP(
    "SentinelLayer",
    instructions=(
        "SentinelLayer is an AI-action governance system for Azure cloud infrastructure. "
        "Use sentinel_evaluate_action to assess proposed changes, "
        "sentinel_query_history to review past decisions, and "
        "sentinel_get_risk_profile to understand a resource's risk history."
    ),
)

# ---------------------------------------------------------------------------
# Lazy singletons — initialised once on first use.
# Stored at module level so tests can monkeypatch them.
# ---------------------------------------------------------------------------

_pipeline: SentinelLayerPipeline | None = None
_tracker: DecisionTracker | None = None


def _get_pipeline() -> SentinelLayerPipeline:
    """Return the module-level pipeline singleton, creating it if needed."""
    global _pipeline
    if _pipeline is None:
        _pipeline = SentinelLayerPipeline()
    return _pipeline


def _get_tracker() -> DecisionTracker:
    """Return the module-level tracker singleton, creating it if needed."""
    global _tracker
    if _tracker is None:
        _tracker = DecisionTracker()
    return _tracker


# ---------------------------------------------------------------------------
# Tool 1 — evaluate a proposed action
# ---------------------------------------------------------------------------


@mcp.tool()
def sentinel_evaluate_action(
    resource_id: str,
    resource_type: str,
    action_type: str,
    agent_id: str,
    reason: str,
    urgency: str = "medium",
    current_monthly_cost: float | None = None,
    current_sku: str | None = None,
    proposed_sku: str | None = None,
) -> dict:
    """Evaluate a proposed infrastructure action through SentinelLayer governance.

    Runs the full pipeline — all four governance agents in parallel — and
    returns a structured verdict with the SRI(tm) breakdown and decision.

    Args:
        resource_id: Azure resource ID or short name (e.g. ``"vm-23"`` or the
            full ``/subscriptions/.../virtualMachines/vm-23`` path).
        resource_type: Azure resource type (e.g.
            ``"Microsoft.Compute/virtualMachines"``).
        action_type: One of: ``scale_up``, ``scale_down``, ``delete_resource``,
            ``restart_service``, ``modify_nsg``, ``create_resource``,
            ``update_config``.
        agent_id: ID of the agent proposing the action (e.g.
            ``"cost-optimization-agent"``).
        reason: Human-readable explanation of why the action is proposed.
        urgency: One of: ``low``, ``medium``, ``high``, ``critical``.
            Defaults to ``"medium"``.
        current_monthly_cost: Current monthly cost of the resource in USD,
            if known.
        current_sku: Current VM/resource SKU, if applicable.
        proposed_sku: Proposed new SKU after the action, if applicable.

    Returns:
        Dict with keys:
        - ``action_id`` — unique identifier for this evaluation
        - ``decision`` — ``"approved"``, ``"escalated"``, or ``"denied"``
        - ``reason`` — human-readable explanation of the decision
        - ``sri_composite`` — overall Sentinel Risk Index score (0–100)
        - ``sri_breakdown`` — per-dimension scores (infrastructure, policy,
          historical, cost)
        - ``thresholds`` — auto-approve and human-review thresholds used
    """
    try:
        action = ProposedAction(
            agent_id=agent_id,
            action_type=ActionType(action_type),
            target=ActionTarget(
                resource_id=resource_id,
                resource_type=resource_type,
                current_monthly_cost=current_monthly_cost,
                current_sku=current_sku,
                proposed_sku=proposed_sku,
            ),
            reason=reason,
            urgency=Urgency(urgency),
        )
    except ValueError as exc:
        return {"error": f"Invalid parameter: {exc}"}

    verdict = _get_pipeline().evaluate(action)
    _get_tracker().record(verdict)

    sri = verdict.sentinel_risk_index
    return {
        "action_id": verdict.action_id,
        "decision": verdict.decision.value,
        "reason": verdict.reason,
        "sri_composite": sri.sri_composite,
        "sri_breakdown": {
            "infrastructure": sri.sri_infrastructure,
            "policy": sri.sri_policy,
            "historical": sri.sri_historical,
            "cost": sri.sri_cost,
        },
        "thresholds": verdict.thresholds,
    }


# ---------------------------------------------------------------------------
# Tool 2 — query governance history
# ---------------------------------------------------------------------------


@mcp.tool()
def sentinel_query_history(
    limit: int = 10,
    resource_id: str | None = None,
) -> dict:
    """Return recent governance decisions from the local audit trail.

    Args:
        limit: Maximum number of decisions to return (default 10, max 100).
        resource_id: Optional filter — if provided, only decisions for this
            resource (or resources whose ID contains this string) are returned.

    Returns:
        Dict with keys:
        - ``count`` — number of decisions returned
        - ``decisions`` — list of decision dicts, newest first.  Each dict
          includes ``action_id``, ``timestamp``, ``decision``,
          ``sri_composite``, ``resource_id``, ``action_type``,
          ``agent_id``, and ``violations``.
    """
    limit = min(max(1, limit), 100)
    tracker = _get_tracker()

    if resource_id:
        records = tracker.get_by_resource(resource_id, limit=limit)
    else:
        records = tracker.get_recent(limit=limit)

    return {
        "count": len(records),
        "decisions": records,
    }


# ---------------------------------------------------------------------------
# Tool 3 — risk profile for a resource
# ---------------------------------------------------------------------------


@mcp.tool()
def sentinel_get_risk_profile(resource_id: str) -> dict:
    """Return an aggregated risk profile for a specific resource.

    Analyses all historical governance decisions for the given resource and
    returns summary statistics useful for understanding its risk history.

    Args:
        resource_id: Full or partial Azure resource ID / short name
            (e.g. ``"vm-23"`` or ``"nsg-east"``).

    Returns:
        Dict with keys:
        - ``resource_id`` — the queried resource ID
        - ``total_evaluations`` — how many times this resource was evaluated
        - ``decisions`` — counts per outcome (approved / escalated / denied)
        - ``avg_sri_composite`` — mean SRI composite across all evaluations
        - ``max_sri_composite`` — highest SRI composite ever recorded
        - ``top_violations`` — list of policy IDs most frequently violated
        - ``last_evaluated`` — ISO timestamp of the most recent evaluation
    """
    return _get_tracker().get_risk_profile(resource_id)


# ---------------------------------------------------------------------------
# Entry point — run as MCP server over stdio
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    mcp.run()
