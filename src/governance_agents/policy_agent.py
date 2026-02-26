"""Policy & Compliance Agent — SRI:Policy dimension.

Validates proposed actions against organizational governance policies,
security baselines, and change window restrictions.

Microsoft Agent Framework integration (Phase 8)
------------------------------------------------
In live mode (USE_LOCAL_MOCKS=false), this agent is driven by a
Microsoft Agent Framework ``Agent`` backed by Azure OpenAI GPT-4.1.

The LLM agent calls our deterministic ``evaluate_policy_rules`` tool,
which checks all 6 governance policies and returns structured violation
data.  The LLM synthesises a plain-English compliance summary.

In mock mode the framework is skipped — only deterministic evaluation runs.
This means the agent works fully offline in tests and development.

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

from src.config import settings as _default_settings
from src.core.models import (
    ActionType,
    PolicyResult,
    PolicySeverity,
    PolicyViolation,
    ProposedAction,
)

import logging

logger = logging.getLogger(__name__)

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

# System instructions for the framework agent (live mode only).
_AGENT_INSTRUCTIONS = """\
You are SentinelLayer's Policy Compliance Evaluator — a specialist in cloud
governance, regulatory compliance, and change management policy enforcement.

Your job:
1. Call the `evaluate_policy_rules` tool with the action JSON and any resource
   metadata provided.
2. Receive the deterministic policy violation report.
3. Write a concise 2-3 sentence compliance summary in plain English.
   Highlight which policies were violated and the compliance risk they represent.
   Do NOT restate raw scores; interpret the compliance implications.

Always call the tool first before providing any analysis.
"""


# ---------------------------------------------------------------------------
# Agent
# ---------------------------------------------------------------------------


class PolicyComplianceAgent:
    """Evaluates proposed actions against organisational governance policies.

    In live mode the Microsoft Agent Framework drives GPT-4.1 to call the
    deterministic tool and synthesise a compliance narrative.

    Usage::

        agent = PolicyComplianceAgent()
        result: PolicyResult = agent.evaluate(action, resource_metadata={
            "tags": {"purpose": "disaster-recovery", "environment": "production"},
            "environment": "production",
        })
        print(result.sri_policy, result.violations)
    """

    def __init__(
        self,
        policies_path: str | Path | None = None,
        cfg=None,
    ) -> None:
        path = Path(policies_path) if policies_path else _DEFAULT_POLICIES_PATH
        with open(path, encoding="utf-8") as fh:
            self._policies: list[dict] = json.load(fh)

        self._cfg = cfg or _default_settings

        self._use_framework: bool = (
            not self._cfg.use_local_mocks
            and bool(self._cfg.azure_openai_endpoint)
        )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def evaluate(
        self,
        action: ProposedAction,
        resource_metadata: dict | None = None,
        now: datetime | None = None,
    ) -> PolicyResult:
        """Evaluate *action* against all loaded governance policies.

        Routes to the Microsoft Agent Framework agent in live mode, or to the
        deterministic rule-based engine in mock mode.

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
        if not self._use_framework:
            return self._evaluate_rules(action, resource_metadata, now)

        try:
            return await self._evaluate_with_framework(action, resource_metadata, now)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "PolicyComplianceAgent: framework call failed (%s) — falling back to rules.", exc
            )
            return self._evaluate_rules(action, resource_metadata, now)

    # ------------------------------------------------------------------
    # Microsoft Agent Framework path (live mode)
    # ------------------------------------------------------------------

    async def _evaluate_with_framework(
        self,
        action: ProposedAction,
        resource_metadata: dict | None,
        now: datetime | None,
    ) -> PolicyResult:
        """Run the framework agent with GPT-4.1 driving the tool call."""
        from openai import AsyncAzureOpenAI
        from azure.identity import DefaultAzureCredential, get_bearer_token_provider
        import agent_framework as af
        from agent_framework.openai import OpenAIResponsesClient

        credential = DefaultAzureCredential()
        token_provider = get_bearer_token_provider(
            credential, "https://cognitiveservices.azure.com/.default"
        )
        azure_openai = AsyncAzureOpenAI(
            azure_endpoint=self._cfg.azure_openai_endpoint,
            azure_ad_token_provider=token_provider,
            api_version="2025-03-01-preview",  # Responses API requires >=2025-03-01-preview
        )
        client = OpenAIResponsesClient(
            async_client=azure_openai,
            model_id=self._cfg.azure_openai_deployment,
        )

        result_holder: list[PolicyResult] = []

        @af.tool(
            name="evaluate_policy_rules",
            description=(
                "Run the deterministic policy compliance evaluation against all "
                "governance policies. Returns a JSON object with sri_policy score, "
                "violations list, total_policies_checked, policies_passed, and reasoning."
            ),
        )
        def evaluate_policy_rules(action_json: str, metadata_json: str = "{}") -> str:
            """Check all governance policies against the proposed action."""
            try:
                a = ProposedAction.model_validate_json(action_json)
            except Exception:
                a = action
            try:
                meta = json.loads(metadata_json) if metadata_json else resource_metadata
            except Exception:
                meta = resource_metadata
            r = self._evaluate_rules(a, meta, now)
            result_holder.append(r)
            return r.model_dump_json()

        agent = client.as_agent(
            name="policy-compliance-evaluator",
            instructions=_AGENT_INSTRUCTIONS,
            tools=[evaluate_policy_rules],
        )

        meta_str = json.dumps(resource_metadata or {})
        response = await agent.run(
            f"Evaluate policy compliance for this proposed action.\n"
            f"Action JSON: {action.model_dump_json()}\n"
            f"Resource metadata: {meta_str}"
        )

        if result_holder:
            base = result_holder[-1]
            enriched_reasoning = (
                base.reasoning
                + "\n\nAgent Framework Analysis (GPT-4.1): "
                + response.text
            )
            return PolicyResult(
                **{**base.model_dump(), "reasoning": enriched_reasoning}
            )

        return self._evaluate_rules(action, resource_metadata, now)

    # ------------------------------------------------------------------
    # Deterministic rule-based evaluation
    # ------------------------------------------------------------------

    def _evaluate_rules(
        self,
        action: ProposedAction,
        resource_metadata: dict | None = None,
        now: datetime | None = None,
    ) -> PolicyResult:
        """Run all governance policy checks deterministically."""
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
