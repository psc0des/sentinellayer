"""Governance Decision Engine — calculates SRI™ Composite and issues verdicts.

Aggregates the four SRI dimension scores produced by the governance agents,
applies a weighted average to compute the SRI™ Composite (0–100), and issues
a :class:`~src.core.models.GovernanceVerdict` according to decision rules.

Decision rules (applied in priority order)
------------------------------------------
1. **Non-overridden CRITICAL violation** — DENIED regardless of composite score.
1.5. **LLM-overridden CRITICAL violation** — ESCALATED floor. The LLM cannot
     substitute for the human approval required by CRITICAL policies (VP, CAB).
     Even with LLM context, the verdict must surface for human review.
2. **Composite > ``sri_human_review_threshold`` (default 60)** — DENIED.
3. **Composite > ``sri_auto_approve_threshold`` (default 25)** — ESCALATED.
3.5. **Any non-overridden HIGH violation** — ESCALATED floor even when composite ≤ 25.
4. **Otherwise** — APPROVED (auto-execute).

SRI™ dimension weights (configurable via :class:`~src.config.Settings`)
-----------------------------------------------------------------------
* SRI:Infrastructure  0.30
* SRI:Policy          0.25
* SRI:Historical      0.25
* SRI:Cost            0.20
"""

import uuid
from datetime import datetime, timezone

from src.config import settings as _default_settings
from src.core.models import (
    ActionType,
    ApprovalCondition,
    BlastRadiusResult,
    ConditionType,
    FinancialResult,
    GovernanceVerdict,
    HistoricalResult,
    PolicyResult,
    PolicySeverity,
    ProposedAction,
    SRIBreakdown,
    SRIVerdict,
)

import logging

logger = logging.getLogger(__name__)


class GovernanceDecisionEngine:
    """Aggregates four SRI dimension scores into a single governance verdict.

    Usage::

        engine = GovernanceDecisionEngine()
        verdict: GovernanceVerdict = engine.evaluate(
            action, blast_radius_result, policy_result,
            historical_result, financial_result,
        )
        print(verdict.decision, verdict.skry_risk_index.sri_composite)

    Pass a custom ``settings`` object to override thresholds and weights —
    this is useful in tests that need non-default configuration.
    """

    def __init__(self, settings=None) -> None:
        cfg = settings or _default_settings
        # Dimension weights
        self._w_infra: float = cfg.sri_weight_infrastructure
        self._w_policy: float = cfg.sri_weight_policy
        self._w_historical: float = cfg.sri_weight_historical
        self._w_cost: float = cfg.sri_weight_cost
        # Decision thresholds
        self._approve_threshold: int = cfg.sri_auto_approve_threshold
        self._review_threshold: int = cfg.sri_human_review_threshold

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def evaluate(
        self,
        action: ProposedAction,
        blast_radius: BlastRadiusResult,
        policy: PolicyResult,
        historical: HistoricalResult,
        financial: FinancialResult,
    ) -> GovernanceVerdict:
        """Aggregate four SRI dimension scores and return a governance verdict.

        Args:
            action: The proposed infrastructure action being evaluated.
            blast_radius: Result from the Blast Radius Simulation Agent.
            policy: Result from the Policy & Compliance Agent.
            historical: Result from the Historical Pattern Agent.
            financial: Result from the Financial Impact Agent.

        Returns:
            :class:`~src.core.models.GovernanceVerdict` containing:
            * ``decision`` — APPROVED / ESCALATED / DENIED
            * ``skry_risk_index`` — all four dimension scores + composite
            * ``reason`` — human-readable explanation of the decision
            * ``agent_results`` — raw outputs from all four agents
        """
        composite = self._calculate_composite(
            blast_radius.sri_infrastructure,
            policy.sri_policy,
            historical.sri_historical,
            financial.sri_cost,
        )

        sri_breakdown = SRIBreakdown(
            sri_infrastructure=blast_radius.sri_infrastructure,
            sri_policy=policy.sri_policy,
            sri_historical=historical.sri_historical,
            sri_cost=financial.sri_cost,
            sri_composite=composite,
        )

        decision, reason, conditions = self._determine_verdict(composite, policy, action, blast_radius)

        logger.info(
            "GovernanceVerdict: action=%s composite=%.1f decision=%s",
            action.action_type.value,
            composite,
            decision.value,
        )

        return GovernanceVerdict(
            action_id=str(uuid.uuid4()),
            timestamp=datetime.now(timezone.utc),
            proposed_action=action,
            skry_risk_index=sri_breakdown,
            decision=decision,
            reason=reason,
            conditions=conditions,
            agent_results={
                "blast_radius": blast_radius.model_dump(),
                "policy": policy.model_dump(),
                "historical": historical.model_dump(),
                "financial": financial.model_dump(),
            },
            thresholds={
                "auto_approve": self._approve_threshold,
                "human_review": self._review_threshold,
            },
        )

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _derive_conditions(
        self,
        action: ProposedAction,
        blast_radius: BlastRadiusResult | None,
    ) -> list[ApprovalCondition]:
        """Derive ApprovalConditions from action context and blast radius data.

        All conditions are deterministic — no LLM involved.  The four scenarios:

        1. Production resource + resize/restart → TIME_WINDOW (off-hours, 00:00–06:00 UTC)
        2. Shared infrastructure (NSG with 2+ affected resources) → BLAST_RADIUS_CONFIRMED
        3. Resource has owner tag → OWNER_NOTIFIED (24h response window)
        4. SCALE_DOWN with peak_cpu_14d > 50% in evidence → METRIC_THRESHOLD guard

        Returns an empty list when none of the scenarios apply.
        """
        conditions: list[ApprovalCondition] = []
        resource_id = action.target.resource_id.lower()
        is_prod = "prod" in resource_id
        is_resize = action.action_type in (ActionType.SCALE_UP, ActionType.SCALE_DOWN)
        is_restart = action.action_type == ActionType.RESTART_SERVICE
        is_nsg = action.action_type == ActionType.MODIFY_NSG

        # Scenario 1 — production resource undergoing a potentially disruptive action
        if is_prod and (is_resize or is_restart):
            conditions.append(ApprovalCondition(
                condition_type=ConditionType.TIME_WINDOW,
                description="Execute during off-hours maintenance window (00:00–06:00 UTC)",
                parameters={"window_start": "00:00", "window_end": "06:00", "tz": "UTC"},
                auto_checkable=True,
            ))

        # Scenario 2 — NSG change affecting shared infrastructure
        if is_nsg and blast_radius:
            affected_count = len(blast_radius.affected_resources)
            if affected_count >= 2:
                conditions.append(ApprovalCondition(
                    condition_type=ConditionType.BLAST_RADIUS_CONFIRMED,
                    description=(
                        f"Confirm blast radius: this NSG change affects "
                        f"{affected_count} resources. "
                        "Acknowledge all dependent services are aware."
                    ),
                    parameters={"affected_count": affected_count},
                    auto_checkable=False,
                ))

        # Scenario 4 — dangerous scale-down hidden by low average CPU
        if action.action_type == ActionType.SCALE_DOWN and action.evidence:
            peak = action.evidence.metrics.get("peak_cpu_14d")
            if peak is not None and peak > 50.0:
                conditions.append(ApprovalCondition(
                    condition_type=ConditionType.METRIC_THRESHOLD,
                    description=(
                        f"Verify current CPU is below 50% before resizing "
                        f"(14-day peak was {peak:.1f}%)"
                    ),
                    parameters={"metric": "cpu_percent", "max_threshold": 50.0},
                    auto_checkable=True,
                ))

        return conditions

    def _calculate_composite(
        self,
        sri_infrastructure: float,
        sri_policy: float,
        sri_historical: float,
        sri_cost: float,
    ) -> float:
        """Weighted sum of the four SRI dimensions, rounded and capped at 100.

        Formula::

            composite = (infra  * w_infra)
                      + (policy * w_policy)
                      + (hist   * w_historical)
                      + (cost   * w_cost)
        """
        raw = (
            sri_infrastructure * self._w_infra
            + sri_policy * self._w_policy
            + sri_historical * self._w_historical
            + sri_cost * self._w_cost
        )
        return round(min(raw, 100.0), 2)

    def _determine_verdict(
        self,
        composite: float,
        policy: PolicyResult,
        action: ProposedAction | None = None,
        blast_radius: BlastRadiusResult | None = None,
    ) -> tuple[SRIVerdict, str, list[ApprovalCondition]]:
        """Apply decision rules in priority order and return (verdict, reason, conditions).

        Rules are evaluated top-to-bottom; the first matching rule wins.

        1.   Critical policy violation  → always DENIED
        1.5. LLM-overridden CRITICAL   → ESCALATED floor
        2.   composite > review_threshold → DENIED
        3.   composite > approve_threshold → ESCALATED
        3.5. Any non-overridden HIGH violation → ESCALATED floor
        3.75 composite safe but conditions derive → APPROVED_IF
        4.   Otherwise → APPROVED
        """
        # Rule 1 — non-overridden CRITICAL violations always DENY.
        critical = [
            v for v in policy.violations
            if v.severity == PolicySeverity.CRITICAL and not v.llm_override
        ]
        if critical:
            ids = ", ".join(v.policy_id for v in critical)
            return (
                SRIVerdict.DENIED,
                f"DENIED — critical policy violation(s) detected: {ids}. "
                "Critical violations block execution regardless of composite SRI score.",
                [],
            )

        # Rule 1.5 — LLM-overridden CRITICAL violations floor at ESCALATED.
        # The LLM can provide context and reasoning, but it cannot substitute for
        # the human approval required by CRITICAL policies (e.g., VP approval for
        # POL-DR-001, CAB approval for POL-CRIT-001). Even when the LLM annotates
        # a CRITICAL violation with llm_override, the verdict must surface for
        # human review — it can never auto-APPROVE.
        critical_overridden = [
            v for v in policy.violations
            if v.severity == PolicySeverity.CRITICAL and v.llm_override
        ]
        if critical_overridden:
            ids = ", ".join(v.policy_id for v in critical_overridden)
            return (
                SRIVerdict.ESCALATED,
                f"ESCALATED — critical policy violation(s) require human approval: {ids}. "
                "LLM context noted but CRITICAL violations cannot be auto-approved — "
                "human review is mandatory.",
                [],
            )

        # Rule 2 — composite too high to allow even with review
        if composite > self._review_threshold:
            return (
                SRIVerdict.DENIED,
                f"DENIED — SRI Composite {composite:.1f} exceeds the denial threshold "
                f"of {self._review_threshold}. Action blocked due to unacceptable risk.",
                [],
            )

        # Rule 3 — composite in the human-review band
        if composite > self._approve_threshold:
            return (
                SRIVerdict.ESCALATED,
                f"ESCALATED — SRI Composite {composite:.1f} requires human review "
                f"(band: {self._approve_threshold}–{self._review_threshold}). "
                "Action paused pending approval.",
                [],
            )

        # Rule 3.5 — HIGH violations floor the verdict at ESCALATED.
        # The composite score may be low (e.g. small blast radius, negligible cost)
        # while a HIGH policy violation still requires a human reviewer. Without
        # this floor, a high sri_policy value can be "diluted" by low values in
        # the other three dimensions and produce a composite below the auto-approve
        # threshold — incorrectly auto-approving a flagged action.
        high_violations = [
            v for v in policy.violations
            if v.severity == PolicySeverity.HIGH and not v.llm_override
        ]
        if high_violations:
            ids = ", ".join(v.policy_id for v in high_violations)
            return (
                SRIVerdict.ESCALATED,
                f"ESCALATED — SRI Composite {composite:.1f} is within the auto-approval "
                f"threshold, but HIGH-severity policy violation(s) require human review: "
                f"{ids}. Action paused pending approval.",
                [],
            )

        # Rule 3.75 — composite is safe but contextual conditions apply.
        # Derive conditions deterministically from action + blast radius context.
        # If conditions are found, emit APPROVED_IF instead of APPROVED.
        conditions = self._derive_conditions(action, blast_radius) if action else []
        if conditions:
            n = len(conditions)
            return (
                SRIVerdict.APPROVED_IF,
                f"APPROVED_IF — SRI Composite {composite:.1f} is within the auto-approval "
                f"threshold, but execution is conditional on {n} requirement(s): "
                + ", ".join(c.description for c in conditions),
                conditions,
            )

        # Rule 4 — safe to auto-execute
        return (
            SRIVerdict.APPROVED,
            f"APPROVED — SRI Composite {composite:.1f} is within the auto-approval "
            f"threshold (≤ {self._approve_threshold}). Action cleared for execution.",
            [],
        )
