"""Governance Decision Engine — calculates SRI™ Composite and issues verdicts.

Aggregates the four SRI dimension scores produced by the governance agents,
applies a weighted average to compute the SRI™ Composite (0–100), and issues
a :class:`~src.core.models.GovernanceVerdict` according to decision rules.

Decision rules (applied in priority order)
------------------------------------------
1. **Critical policy violation** — DENIED regardless of composite score.
2. **Composite > ``sri_human_review_threshold`` (default 60)** — DENIED.
3. **Composite > ``sri_auto_approve_threshold`` (default 25)** — ESCALATED.
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
    BlastRadiusResult,
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
        print(verdict.decision, verdict.sentinel_risk_index.sri_composite)

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
            * ``sentinel_risk_index`` — all four dimension scores + composite
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

        decision, reason = self._determine_verdict(composite, policy)

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
            sentinel_risk_index=sri_breakdown,
            decision=decision,
            reason=reason,
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
    ) -> tuple[SRIVerdict, str]:
        """Apply decision rules in priority order and return (verdict, reason).

        Rules are evaluated top-to-bottom; the first matching rule wins.

        1. Critical policy violation  → always DENIED
        2. composite > review_threshold → DENIED
        3. composite > approve_threshold → ESCALATED
        4. Otherwise → APPROVED
        """
        # Rule 1 — critical policy violation overrides the numeric score
        critical = [
            v for v in policy.violations
            if v.severity == PolicySeverity.CRITICAL
        ]
        if critical:
            ids = ", ".join(v.policy_id for v in critical)
            return (
                SRIVerdict.DENIED,
                f"DENIED — critical policy violation(s) detected: {ids}. "
                "Critical violations block execution regardless of composite SRI score.",
            )

        # Rule 2 — composite too high to allow even with review
        if composite > self._review_threshold:
            return (
                SRIVerdict.DENIED,
                f"DENIED — SRI Composite {composite:.1f} exceeds the denial threshold "
                f"of {self._review_threshold}. Action blocked due to unacceptable risk.",
            )

        # Rule 3 — composite in the human-review band
        if composite > self._approve_threshold:
            return (
                SRIVerdict.ESCALATED,
                f"ESCALATED — SRI Composite {composite:.1f} requires human review "
                f"(band: {self._approve_threshold}–{self._review_threshold}). "
                "Action paused pending approval.",
            )

        # Rule 4 — safe to auto-execute
        return (
            SRIVerdict.APPROVED,
            f"APPROVED — SRI Composite {composite:.1f} is within the auto-approval "
            f"threshold (≤ {self._approve_threshold}). Action cleared for execution.",
        )
