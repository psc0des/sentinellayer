"""Financial Impact Agent — SRI:Cost dimension.

Estimates the financial impact of a proposed action and detects
over-optimisation risk — cases where short-term cost savings would
trigger expensive recovery events that outweigh the savings.

Cost change is determined from these sources in priority order
--------------------------------------------------------------
1. ``action.projected_savings_monthly`` — explicit figure from the proposing
   agent.  Most accurate; treated as exact (not uncertain).
2. ``action.target.current_monthly_cost`` — cost stated in the action target
   for DELETE actions.
3. ``seed_resources.json`` — looked up by resource name; used as a fallback
   when the action does not carry cost metadata.
4. Estimated percentage of current cost:
   - SCALE_DOWN → ~30 % reduction
   - SCALE_UP   → ~50 % increase
   These are flagged as uncertain and incur a scoring penalty.
5. Zero — actions with no expected cost impact (RESTART, MODIFY_NSG, etc.).
   Returned as certain because zero *is* the correct value.

SRI:Cost score (0–100)
-----------------------
* 0–25   — minimal financial risk (auto-approve)
* 26–60  — moderate financial risk (escalate for review)
* 61–100 — significant financial risk (deny)

Score formula
--------------
``score = magnitude_score(|monthly_change|) × action_multiplier``
``      + over_optimisation_penalty   (if detected)``
``      + cost_uncertainty_penalty    (if estimated, not exact)``
Capped at 100.

Magnitude thresholds (absolute monthly change → base pts)
----------------------------------------------------------
* ≥ $1000 → 70 pts
* $600–$999  → 50 pts
* $300–$599  → 30 pts
* $100–$299  → 15 pts
* $0.01–$99  →  5 pts
* $0         →  0 pts

Action multipliers
------------------
* DELETE_RESOURCE : 1.5   (irreversible — full cost disappears; highest risk)
* SCALE_DOWN      : 1.2   (availability risk; partial cost reduction)
* UPDATE_CONFIG   : 0.8
* SCALE_UP        : 0.6   (planned spend; cost increase is controlled)
* CREATE_RESOURCE : 0.5
* RESTART_SERVICE : 0.3   (no cost change expected)
* MODIFY_NSG      : 0.3   (no cost change expected)
"""

import json
import logging
from pathlib import Path

from src.core.models import ActionType, FinancialResult, ProposedAction
from src.infrastructure.openai_client import AzureOpenAIClient

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_DEFAULT_RESOURCES_PATH = (
    Path(__file__).parent.parent.parent / "data" / "seed_resources.json"
)

# Magnitude thresholds: (minimum_abs_change, score_pts).
# Evaluated from highest to lowest; first match wins.
_MAGNITUDE_THRESHOLDS: list[tuple[float, float]] = [
    (1000.0, 70.0),
    (600.0,  50.0),
    (300.0,  30.0),
    (100.0,  15.0),
    (0.01,    5.0),
]
# A change of exactly $0 yields 0 pts (implicit default).

# How much each action type amplifies the magnitude score
_ACTION_MULTIPLIER: dict[ActionType, float] = {
    ActionType.DELETE_RESOURCE: 1.5,
    ActionType.SCALE_DOWN:      1.2,
    ActionType.UPDATE_CONFIG:   0.8,
    ActionType.SCALE_UP:        0.6,
    ActionType.CREATE_RESOURCE: 0.5,
    ActionType.RESTART_SERVICE: 0.3,
    ActionType.MODIFY_NSG:      0.3,
}

_OVER_OPTIMISATION_PENALTY: float = 20.0
_COST_UNCERTAINTY_PENALTY: float = 10.0

# Assumed cost reduction / increase for scale operations when no explicit data
_SCALE_DOWN_ESTIMATE: float = 0.30  # 30 % monthly cost reduction
_SCALE_UP_ESTIMATE:   float = 0.50  # 50 % monthly cost increase

# Estimated recovery cost per dependent service if over-optimisation occurs
_RECOVERY_COST_PER_SERVICE: float = 10_000.0

# Minimum number of dependents required to trigger over-optimisation detection
_OVER_OPT_THRESHOLD: int = 1


# ---------------------------------------------------------------------------
# Agent
# ---------------------------------------------------------------------------


class FinancialImpactAgent:
    """Estimates the financial impact and over-optimisation risk of a proposed action.

    Loads resource cost data from ``seed_resources.json`` (mock for Azure Cost
    Management), then computes an SRI:Cost score (0–100), a monthly cost delta,
    a 90-day projection, and optionally an over-optimisation risk assessment.

    Usage::

        agent = FinancialImpactAgent()
        result: FinancialResult = agent.evaluate(action)
        print(result.sri_cost, result.immediate_monthly_change)
    """

    def __init__(self, resources_path: str | Path | None = None) -> None:
        path = Path(resources_path) if resources_path else _DEFAULT_RESOURCES_PATH
        with open(path, encoding="utf-8") as fh:
            data: dict = json.load(fh)

        # Fast name → resource dict lookup
        self._resources: dict[str, dict] = {
            r["name"]: r for r in data.get("resources", [])
        }

        # LLM client — enriches rule-based reasoning with GPT-4.1 in live mode
        self._llm = AzureOpenAIClient()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def evaluate(self, action: ProposedAction) -> FinancialResult:
        """Evaluate the financial impact of a proposed infrastructure action.

        Args:
            action: The proposed action from an operational agent.

        Returns:
            :class:`~src.core.models.FinancialResult` containing:

            * ``sri_cost`` — 0–100 financial risk score
            * ``immediate_monthly_change`` — estimated USD change per month
              (negative = savings, positive = additional spend)
            * ``projection_90_day`` — 3-month cost forecast dict
            * ``over_optimization_risk`` — risk assessment dict if detected,
              else None
            * ``reasoning`` — human-readable explanation
        """
        resource = self._find_resource(action.target.resource_id)
        monthly_change, cost_uncertain = self._estimate_cost_change(action, resource)
        over_opt = self._detect_over_optimisation(action, resource, monthly_change)
        projection = self._build_projection(monthly_change)
        score = self._calculate_score(action, monthly_change, cost_uncertain, over_opt)

        logger.info(
            "FinancialImpactAgent: action=%s change=%.2f uncertain=%s score=%.1f",
            action.action_type.value,
            monthly_change,
            cost_uncertain,
            score,
        )

        reasoning = self._build_reasoning(action, monthly_change, cost_uncertain, over_opt, score)

        # Augment rule-based reasoning with GPT-4.1 analysis in live mode
        if not self._llm.is_mock:
            direction = (
                "savings" if monthly_change < 0
                else "cost increase" if monthly_change > 0
                else "no cost change"
            )
            llm_text = self._llm.analyze(
                action_context=(
                    f"Action: {action.action_type.value} on '{action.target.resource_id}'\n"
                    f"Monthly cost impact: ${abs(monthly_change):,.2f} {direction}"
                    f"{'(estimated)' if cost_uncertain else '(exact)'}\n"
                    f"90-day total: ${monthly_change * 3:,.2f} | "
                    f"Annualised: ${monthly_change * 12:,.2f}\n"
                    f"Over-optimisation risk: {'YES — ' + over_opt['reason'] if over_opt else 'none'}\n"
                    f"SRI:Cost score: {score:.1f}/100\n"
                    f"Agent reason: {action.reason}"
                ),
                agent_role="financial impact and cost risk assessor",
            )
            reasoning = reasoning + "\n\nGPT-4.1 Analysis: " + llm_text

        return FinancialResult(
            sri_cost=score,
            immediate_monthly_change=monthly_change,
            projection_90_day=projection,
            over_optimization_risk=over_opt,
            reasoning=reasoning,
        )

    # ------------------------------------------------------------------
    # Cost estimation
    # ------------------------------------------------------------------

    def _find_resource(self, resource_id: str) -> dict | None:
        """Look up a resource by name or the last segment of its Azure resource ID."""
        if resource_id in self._resources:
            return self._resources[resource_id]
        name = resource_id.split("/")[-1]
        return self._resources.get(name)

    def _estimate_cost_change(
        self, action: ProposedAction, resource: dict | None
    ) -> tuple[float, bool]:
        """Return ``(monthly_change_usd, cost_uncertain)`` for the action.

        ``monthly_change_usd`` — negative for savings, positive for new spend.
        ``cost_uncertain``     — True when the figure is an estimate, not exact.

        Resolution order:
        1. ``projected_savings_monthly`` — exact, agent-supplied.
        2. DELETE: full current cost from target metadata or resource graph.
        3. SCALE_DOWN: 30 % of current cost (estimated).
        4. SCALE_UP: 50 % of current cost (estimated).
        5. RESTART / MODIFY_NSG / UPDATE_CONFIG / CREATE — zero cost change
           (certain by design; these operations do not alter billing).
        6. Fallback: 0.0, uncertain (cost-impacting action with no data).
        """
        # 1. Proposing agent supplied an explicit savings figure
        if action.projected_savings_monthly is not None:
            return (-action.projected_savings_monthly, False)

        # Helper — resolve current monthly cost from target or resource graph
        current_cost: float | None = (
            action.target.current_monthly_cost
            if action.target.current_monthly_cost is not None
            else (resource.get("monthly_cost") if resource else None)
        )

        # 2. DELETE removes the resource's full monthly cost
        if action.action_type == ActionType.DELETE_RESOURCE:
            if current_cost is not None:
                return (-current_cost, False)
            return (0.0, True)  # cost cannot be determined

        # 3. SCALE_DOWN — estimated 30 % reduction
        if action.action_type == ActionType.SCALE_DOWN:
            if current_cost is not None:
                return (-round(current_cost * _SCALE_DOWN_ESTIMATE, 2), True)
            return (0.0, True)

        # 4. SCALE_UP — estimated 50 % increase
        if action.action_type == ActionType.SCALE_UP:
            if current_cost is not None:
                return (round(current_cost * _SCALE_UP_ESTIMATE, 2), True)
            return (0.0, True)

        # 5. All other action types — no meaningful cost change
        return (0.0, False)

    # ------------------------------------------------------------------
    # Over-optimisation detection
    # ------------------------------------------------------------------

    def _detect_over_optimisation(
        self,
        action: ProposedAction,
        resource: dict | None,
        monthly_change: float,
    ) -> dict | None:
        """Detect over-optimisation risk — saving money but risking a bigger loss.

        Triggered when:

        1. The action is ``DELETE_RESOURCE`` or ``SCALE_DOWN`` (cost-reducing).
        2. The resource has at least one dependent, consumer, or hosted service.

        Returns a risk dict if detected, else ``None``.
        """
        if action.action_type not in (
            ActionType.DELETE_RESOURCE,
            ActionType.SCALE_DOWN,
        ):
            return None

        if resource is None:
            return None

        # Collect all services/resources that depend on this one
        dependents: list[str] = (
            resource.get("dependents", [])
            + resource.get("consumers", [])
            + resource.get("services_hosted", [])
        )

        count = len(dependents)
        if count < _OVER_OPT_THRESHOLD:
            return None

        monthly_savings = abs(monthly_change)
        recovery_cost = count * _RECOVERY_COST_PER_SERVICE
        preview = dependents[:3]
        ellipsis = "..." if count > 3 else ""

        return {
            "detected": True,
            "affected_services": dependents,
            "affected_count": count,
            "monthly_savings": round(monthly_savings, 2),
            "estimated_recovery_cost": recovery_cost,
            "reason": (
                f"'{resource['name']}' has {count} dependent service(s): "
                f"{', '.join(preview)}{ellipsis}. "
                f"Saving ${monthly_savings:,.0f}/month risks "
                f"${recovery_cost:,.0f} in unplanned recovery costs."
            ),
        }

    # ------------------------------------------------------------------
    # 90-day projection
    # ------------------------------------------------------------------

    @staticmethod
    def _build_projection(monthly_change: float) -> dict:
        """Build a simple linear 90-day cost projection."""
        return {
            "month_1": round(monthly_change, 2),
            "month_2": round(monthly_change, 2),
            "month_3": round(monthly_change, 2),
            "total_90_day": round(monthly_change * 3, 2),
            "annualized": round(monthly_change * 12, 2),
            "note": (
                "Linear projection — does not account for usage growth, "
                "scaling events, or variable workloads."
            ),
        }

    # ------------------------------------------------------------------
    # Scoring
    # ------------------------------------------------------------------

    def _calculate_score(
        self,
        action: ProposedAction,
        monthly_change: float,
        cost_uncertain: bool,
        over_opt: dict | None,
    ) -> float:
        """Compute SRI:Cost score (0–100).

        Formula::

            score = magnitude_score(|monthly_change|) × action_multiplier
                  + over_optimisation_penalty   (if detected)
                  + cost_uncertainty_penalty    (if uncertain)
        """
        magnitude = self._magnitude_score(abs(monthly_change))
        multiplier = _ACTION_MULTIPLIER.get(action.action_type, 1.0)
        score = magnitude * multiplier

        if over_opt:
            score += _OVER_OPTIMISATION_PENALTY

        if cost_uncertain:
            score += _COST_UNCERTAINTY_PENALTY

        return round(min(score, 100.0), 2)

    @staticmethod
    def _magnitude_score(abs_change: float) -> float:
        """Map an absolute monthly cost change to a base magnitude score."""
        for threshold, pts in _MAGNITUDE_THRESHOLDS:
            if abs_change >= threshold:
                return pts
        return 0.0

    # ------------------------------------------------------------------
    # Reasoning
    # ------------------------------------------------------------------

    def _build_reasoning(
        self,
        action: ProposedAction,
        monthly_change: float,
        cost_uncertain: bool,
        over_opt: dict | None,
        score: float,
    ) -> str:
        """Build a human-readable explanation of the financial risk assessment."""
        abs_change = abs(monthly_change)
        if monthly_change < 0:
            direction = "reduction"
        elif monthly_change > 0:
            direction = "increase"
        else:
            direction = "no change"

        estimate_tag = " (estimated)" if cost_uncertain else ""

        lines = [
            f"Financial analysis for '{action.action_type.value}': "
            f"${abs_change:,.2f}/month {direction}{estimate_tag}.",
            f"90-day outlook: ${monthly_change * 3:,.2f}  |  "
            f"Annualised: ${monthly_change * 12:,.2f}.",
        ]

        if over_opt:
            lines.append(f"Over-optimisation risk detected: {over_opt['reason']}")

        lines.append(f"SRI:Cost score: {score:.1f}/100.")
        return "\n".join(lines)
