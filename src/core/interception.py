"""Action Interception Engine — single entry point for all SentinelLayer governance.

The ActionInterceptor is the façade that operational agents (and the MCP server)
call to have a ProposedAction evaluated.  It wires together two components that
already exist and does *not* duplicate their logic:

    SentinelLayerPipeline  — runs the four governance agents in parallel and
                              produces a GovernanceVerdict.
    DecisionTracker        — writes the verdict to the local JSON audit trail.

Two entry points are provided so the interception engine can be called from
different contexts:

    intercept(action: ProposedAction) -> GovernanceVerdict
        Direct Python call.  The caller already holds a validated
        ProposedAction object (e.g. an operational agent that builds the
        object itself).

    intercept_from_dict(data: dict) -> dict
        MCP-compatible call.  The caller passes a plain Python dict (as MCP
        tools receive JSON arguments).  This method validates the fields,
        constructs a ProposedAction, runs the pipeline, records the verdict,
        and returns a flat dict that is safe for JSON serialisation.

A module-level singleton (``get_interceptor()``) is also provided so the MCP
server and any other module can share a single pre-warmed instance without
creating extra pipeline / tracker objects.

Data flow
---------
    Operational agent (or MCP tool)
        │
        ▼
    ActionInterceptor.intercept() ──► SentinelLayerPipeline.evaluate()
                                              │
                               4 governance agents run in parallel
                                              │
                                        GovernanceVerdict
                                              │
        ◄──────── DecisionTracker.record() ──┘  (audit trail)
        │
        ▼
    GovernanceVerdict returned to caller
"""

import logging

from src.core.decision_tracker import DecisionTracker
from src.core.models import (
    ActionTarget,
    ActionType,
    GovernanceVerdict,
    ProposedAction,
    Urgency,
)
from src.core.pipeline import SentinelLayerPipeline

logger = logging.getLogger(__name__)


class ActionInterceptor:
    """Façade that routes ProposedAction objects through the governance pipeline.

    This class is the *entry point* for all governance decisions in
    SentinelLayer.  Every action that an operational agent wants to execute
    must pass through here before it is allowed to proceed.

    Think of it like an airport security checkpoint:
    - Your bag (the action) comes in.
    - It goes through the scanner (the pipeline with 4 agents).
    - The result is stamped (verdict recorded).
    - You either board (APPROVED), wait for a supervisor (ESCALATED), or are
      turned away (DENIED).

    Usage (direct Python call)::

        interceptor = ActionInterceptor()
        verdict = interceptor.intercept(action)
        print(verdict.decision.value)   # "approved" | "escalated" | "denied"

    Usage (MCP / dict-based call)::

        result = interceptor.intercept_from_dict({
            "resource_id":   "vm-23",
            "resource_type": "Microsoft.Compute/virtualMachines",
            "action_type":   "delete_resource",
            "agent_id":      "cost-optimization-agent",
            "reason":        "VM idle for 30 days",
        })
        # result is a plain dict — JSON-safe, ready for the MCP response

    Args:
        pipeline: Optional pre-built SentinelLayerPipeline.  When omitted, a
            new pipeline is created (agents load their data files).  Pass a
            mock here in unit tests to avoid loading real data.
        tracker: Optional pre-built DecisionTracker.  When omitted, a new
            tracker is created (writes to data/decisions/).  Pass a mock in
            tests to avoid writing files.
    """

    def __init__(
        self,
        pipeline: SentinelLayerPipeline | None = None,
        tracker: DecisionTracker | None = None,
    ) -> None:
        self._pipeline: SentinelLayerPipeline = pipeline or SentinelLayerPipeline()
        self._tracker: DecisionTracker = tracker or DecisionTracker()
        logger.info("ActionInterceptor initialised and ready.")

    # ------------------------------------------------------------------
    # Public API — Python entry point
    # ------------------------------------------------------------------

    async def intercept(self, action: ProposedAction) -> GovernanceVerdict:
        """Route a ProposedAction through the full governance pipeline.

        This is the main method.  Call it whenever an operational agent
        wants to execute an infrastructure action.

        Steps (in order):
        1. Log the incoming action so we have a trace in the server logs.
        2. Ask SentinelLayerPipeline to evaluate the action (runs all four
           governance agents in parallel and returns a GovernanceVerdict).
        3. Ask DecisionTracker to record the verdict (writes a JSON file
           to data/decisions/ for the audit trail and the dashboard).
        4. Log the outcome and return the verdict to the caller.

        Args:
            action: A validated :class:`~src.core.models.ProposedAction`
                created by any operational agent.

        Returns:
            :class:`~src.core.models.GovernanceVerdict` containing the
            SRI™ breakdown, decision (APPROVED / ESCALATED / DENIED),
            and a human-readable reason for the decision.
        """
        logger.info(
            "Intercepting action: agent=%s action_type=%s resource=%s urgency=%s",
            action.agent_id,
            action.action_type.value,
            action.target.resource_id,
            action.urgency.value,
        )

        # Step 2 — run the four governance agents in parallel
        verdict: GovernanceVerdict = await self._pipeline.evaluate(action)

        # Step 3 — write the verdict to the audit trail
        self._tracker.record(verdict)

        logger.info(
            "Interception complete: action_id=%s decision=%s SRI_composite=%.1f",
            verdict.action_id,
            verdict.decision.value,
            verdict.sentinel_risk_index.sri_composite,
        )

        return verdict

    # ------------------------------------------------------------------
    # Public API — MCP / dict entry point
    # ------------------------------------------------------------------

    async def intercept_from_dict(self, data: dict) -> dict:
        """MCP-compatible entry point — accepts a plain dict, returns a plain dict.

        MCP tools receive their arguments as JSON objects (which Python
        converts to plain dicts).  This method acts as the bridge:
        it validates those plain values, builds a proper ProposedAction,
        runs the full pipeline, and returns a flat dict that is safe
        for JSON serialisation.

        Expected keys in ``data``:

        ==================== ======== =================================
        Key                  Required Notes
        ==================== ======== =================================
        resource_id          Yes      Azure resource ID or short name
        resource_type        Yes      Azure resource type string
        action_type          Yes      Must be a valid ActionType value
        agent_id             Yes      ID of the proposing agent
        reason               Yes      Human-readable justification
        urgency              No       Default: "medium"
        current_monthly_cost No       Current cost in USD (float)
        current_sku          No       Current VM SKU
        proposed_sku         No       New SKU after the action
        ==================== ======== =================================

        Args:
            data: Dictionary with action parameters (as described above).

        Returns:
            Dict with keys: ``action_id``, ``timestamp``, ``decision``,
            ``reason``, ``sri_composite``, ``sri_breakdown``,
            ``thresholds``, ``resource_id``, ``agent_id``.

        Raises:
            ValueError: If a required field is missing or ``action_type``
                / ``urgency`` contains an invalid enum value.
        """
        # --- Step 1: validate input and build a ProposedAction ---
        try:
            action = self._build_action_from_dict(data)
        except (KeyError, ValueError) as exc:
            logger.warning("intercept_from_dict: invalid input — %s", exc)
            raise ValueError(f"Invalid action data: {exc}") from exc

        # --- Step 2: run the full pipeline (same as intercept()) ---
        verdict = await self.intercept(action)

        # --- Step 3: return a flat, JSON-safe dict ---
        return self._format_verdict(verdict)

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _build_action_from_dict(data: dict) -> ProposedAction:
        """Construct a validated ProposedAction from a raw dict.

        The ``@staticmethod`` decorator means this method does not use
        ``self`` — it is just a helper function that lives inside the class
        for organisation purposes.

        Args:
            data: Raw dict from an MCP tool call.

        Returns:
            A fully validated :class:`~src.core.models.ProposedAction`.

        Raises:
            KeyError: If a required field (resource_id, resource_type,
                action_type, agent_id, reason) is missing from ``data``.
            ValueError: If ``action_type`` or ``urgency`` is not a
                recognised enum value.
        """
        return ProposedAction(
            agent_id=data["agent_id"],
            action_type=ActionType(data["action_type"]),
            target=ActionTarget(
                resource_id=data["resource_id"],
                resource_type=data["resource_type"],
                current_monthly_cost=data.get("current_monthly_cost"),
                current_sku=data.get("current_sku"),
                proposed_sku=data.get("proposed_sku"),
            ),
            reason=data["reason"],
            urgency=Urgency(data.get("urgency", "medium")),
        )

    @staticmethod
    def _format_verdict(verdict: GovernanceVerdict) -> dict:
        """Flatten a GovernanceVerdict into a JSON-safe dict.

        The ``GovernanceVerdict`` Pydantic model contains nested objects
        (SRIBreakdown, ProposedAction).  This helper extracts the fields
        that MCP callers care about and returns them in a flat structure
        that JSON can serialise directly.

        Args:
            verdict: The GovernanceVerdict to flatten.

        Returns:
            A plain dict with string / float / dict values only.
        """
        sri = verdict.sentinel_risk_index
        return {
            "action_id": verdict.action_id,
            "timestamp": verdict.timestamp.isoformat(),
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
            "resource_id": verdict.proposed_action.target.resource_id,
            "agent_id": verdict.proposed_action.agent_id,
        }


# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------

_interceptor: ActionInterceptor | None = None


def get_interceptor() -> ActionInterceptor:
    """Return the module-level ActionInterceptor singleton.

    Creates the singleton on first call (lazy initialisation — the pipeline
    and tracker are only built when first needed, not when the module is
    imported).  Subsequent calls return the same instance without rebuilding
    anything.

    This pattern (called a *singleton*) is useful when:
    - Creating the object is expensive (agents load data files).
    - You want every caller to share the same warmed-up instance.

    Returns:
        The shared :class:`ActionInterceptor` instance.
    """
    global _interceptor
    if _interceptor is None:
        logger.info("get_interceptor: creating module-level ActionInterceptor singleton.")
        _interceptor = ActionInterceptor()
    return _interceptor
