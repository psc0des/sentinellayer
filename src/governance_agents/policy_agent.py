"""Policy & Compliance Agent — SRI:Policy dimension.

Validates proposed actions against organizational governance policies,
security baselines, and change window restrictions.

All evaluation is deterministic — no LLM call required.  The agent loads
policies from ``data/policies.json``, checks each condition against the
proposed action and optional resource metadata, and computes an SRI:Policy
score (0–100) that reflects the aggregate severity of any violations found.

Score semantics
---------------
* 0   – fully compliant, no violations detected
* 1–39 – minor / medium violations (finance or change-window concerns)
* 40–99 – high-severity violations (NSG changes, critical resources)
* 100  – one or more critical violations (disaster-recovery protection)

Severity → score contribution
------------------------------
* critical : 40 pts
* high     : 25 pts
* medium   : 15 pts
* low      :  5 pts
Scores accumulate and are capped at 100.
"""

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from src.core.models import (
    ActionType,
    PolicyResult,
    PolicySeverity,
    PolicyViolation,
    ProposedAction,
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_SEVERITY_SCORE: dict[PolicySeverity, float] = {
    PolicySeverity.CRITICAL: 40.0,
    PolicySeverity.HIGH: 25.0,
    PolicySeverity.MEDIUM: 15.0,
    PolicySeverity.LOW: 5.0,
}

_DEFAULT_POLICIES_PATH = Path(__file__).parent.parent.parent / "data" / "policies.json"

_DAY_MAP: dict[str, int] = {
    "Monday": 0,
    "Tuesday": 1,
    "Wednesday": 2,
    "Thursday": 3,
    "Friday": 4,
    "Saturday": 5,
    "Sunday": 6,
}


# ---------------------------------------------------------------------------
# Agent
# ---------------------------------------------------------------------------


class PolicyComplianceAgent:
    """Evaluates proposed actions against organisational governance policies.

    Usage::

        agent = PolicyComplianceAgent()
        result: PolicyResult = agent.evaluate(action, resource_metadata={
            "tags": {"purpose": "disaster-recovery", "environment": "production"},
            "environment": "production",
        })
        print(result.sri_policy, result.violations)
    """

    def __init__(self, policies_path: str | Path | None = None) -> None:
        path = Path(policies_path) if policies_path else _DEFAULT_POLICIES_PATH
        with open(path, encoding="utf-8") as fh:
            self._policies: list[dict] = json.load(fh)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def evaluate(
        self,
        action: ProposedAction,
        resource_metadata: dict | None = None,
        now: datetime | None = None,
    ) -> PolicyResult:
        """Evaluate *action* against all loaded governance policies.

        Args:
            action: The proposed infrastructure action to validate.
            resource_metadata: Optional resource context dict with keys:
                ``"tags"`` (``dict[str, str]``) and ``"environment"``
                (``str``).  When absent the agent infers from the action
                target's resource identifiers.
            now: Override the current UTC datetime — used in tests to
                pin the clock and exercise change-window logic.

        Returns:
            :class:`~src.core.models.PolicyResult` with:
            * ``sri_policy`` — 0–100 risk score
            * ``violations`` — list of :class:`~src.core.models.PolicyViolation`
            * ``total_policies_checked`` / ``policies_passed``
            * ``reasoning`` — human-readable summary
        """
        metadata = resource_metadata or {}
        tags: dict[str, str] = metadata.get("tags", {})
        environment: str = metadata.get("environment") or self._infer_environment(action)
        ts: datetime = now or datetime.now(timezone.utc)

        violations: list[PolicyViolation] = []
        for policy in self._policies:
            violation = self._check_policy(policy, action, tags, environment, ts)
            if violation:
                violations.append(violation)

        total = len(self._policies)
        passed = total - len(violations)

        return PolicyResult(
            sri_policy=self._calculate_sri(violations),
            violations=violations,
            total_policies_checked=total,
            policies_passed=passed,
            reasoning=self._build_reasoning(violations, passed, total),
        )

    # ------------------------------------------------------------------
    # Per-policy evaluation
    # ------------------------------------------------------------------

    def _check_policy(
        self,
        policy: dict,
        action: ProposedAction,
        tags: dict[str, str],
        environment: str,
        now: datetime,
    ) -> PolicyViolation | None:
        """Return a :class:`PolicyViolation` if the policy is violated, else ``None``.

        A policy is violated when **all** of its conditions evaluate to True.
        Conditions that are absent from the policy definition are skipped.
        """
        conditions: dict = policy.get("conditions", {})
        checks: list[bool] = []

        # -- Tag matching ------------------------------------------------
        if "tags_match" in conditions:
            checks.append(self._tags_match(tags, conditions["tags_match"]))

        # -- Resource-type matching --------------------------------------
        if "resource_type_match" in conditions:
            checks.append(action.target.resource_type == conditions["resource_type_match"])

        # -- Blocked action list -----------------------------------------
        if "blocked_actions" in conditions:
            checks.append(action.action_type.value in conditions["blocked_actions"])

        # -- Environment match -------------------------------------------
        if "environment_match" in conditions:
            checks.append(environment == conditions["environment_match"])

        # -- Change-window enforcement -----------------------------------
        if "blocked_windows" in conditions:
            checks.append(self._in_change_window(conditions["blocked_windows"], now))

        # -- Cost-impact threshold ---------------------------------------
        if "cost_impact_threshold" in conditions:
            impact = self._estimate_cost_impact(action)
            checks.append(impact is not None and impact > conditions["cost_impact_threshold"])

        # All present conditions must be True to constitute a violation
        if not checks or not all(checks):
            return None

        return PolicyViolation(
            policy_id=policy["id"],
            name=policy["name"],
            rule=policy["description"],
            severity=PolicySeverity(policy["severity"]),
        )

    # ------------------------------------------------------------------
    # Condition helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _tags_match(resource_tags: dict[str, str], required: dict) -> bool:
        """Return ``True`` if all *required* key-value pairs exist in *resource_tags*."""
        return all(resource_tags.get(k) == v for k, v in required.items())

    @staticmethod
    def _in_change_window(windows: list[dict], now: datetime) -> bool:
        """Return ``True`` if *now* falls inside any of the blocked change windows.

        Handles weekend-wrapping windows (e.g. Friday 17:00 → Monday 08:00).
        """
        wd = now.weekday()  # Monday=0 … Sunday=6
        t_min = now.hour * 60 + now.minute

        for win in windows:
            s_day = _DAY_MAP[win["day_start"]]
            e_day = _DAY_MAP[win["day_end"]]
            sh, sm = map(int, win["time_start"].split(":"))
            eh, em = map(int, win["time_end"].split(":"))
            s_min = sh * 60 + sm
            e_min = eh * 60 + em

            if s_day > e_day:
                # Weekend wrap-around (e.g. Fri=4 → Mon=0)
                # Days strictly between: wd > s_day (Sat/Sun) — or — wd < e_day (none for Mon=0)
                if wd > s_day or wd < e_day:
                    return True
                if wd == s_day and t_min >= s_min:
                    return True
                if wd == e_day and t_min < e_min:
                    return True
            elif s_day == e_day:
                # Same-day window (e.g. Monday 17:00-20:00)
                if wd == s_day and s_min <= t_min < e_min:
                    return True
            else:
                # Forward window spanning multiple days within the same week
                if s_day < wd < e_day:
                    return True
                if wd == s_day and t_min >= s_min:
                    return True
                if wd == e_day and t_min < e_min:
                    return True

        return False

    @staticmethod
    def _estimate_cost_impact(action: ProposedAction) -> float | None:
        """Return estimated absolute monthly cost change, or ``None`` if unknown.

        Resolution order:
        1. ``projected_savings_monthly`` — explicit estimate from the proposing agent.
        2. ``current_monthly_cost`` — used as the full cost delta for DELETE actions.
        3. ``None`` — insufficient data; cost-threshold policy is not triggered.
        """
        if action.projected_savings_monthly is not None:
            return abs(action.projected_savings_monthly)
        if action.action_type == ActionType.DELETE_RESOURCE:
            return action.target.current_monthly_cost
        return None

    @staticmethod
    def _infer_environment(action: ProposedAction) -> str:
        """Heuristic environment detection from resource identifiers.

        Looks for ``"prod"`` in the resource ID and resource group name.
        Returns ``"production"`` if found, else ``"unknown"``.
        """
        haystack = (
            action.target.resource_id + "/" + (action.target.resource_group or "")
        ).lower()
        return "production" if "prod" in haystack else "unknown"

    # ------------------------------------------------------------------
    # Scoring & reasoning
    # ------------------------------------------------------------------

    @staticmethod
    def _calculate_sri(violations: list[PolicyViolation]) -> float:
        """Aggregate violation severities into a 0–100 SRI:Policy score."""
        if not violations:
            return 0.0
        raw = sum(_SEVERITY_SCORE.get(v.severity, 0.0) for v in violations)
        return min(round(raw, 2), 100.0)

    @staticmethod
    def _build_reasoning(
        violations: list[PolicyViolation], passed: int, total: int
    ) -> str:
        if not violations:
            return f"All {total} policies passed. Action is fully compliant."
        lines = [
            f"Evaluated {total} policies — {passed} passed, "
            f"{len(violations)} violation(s) detected:"
        ]
        for v in violations:
            lines.append(f"  [{v.severity.upper()}] {v.policy_id}: {v.name}")
        return "\n".join(lines)
