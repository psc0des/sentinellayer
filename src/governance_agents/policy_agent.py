"""Policy & Compliance Agent — SRI:Policy dimension.

Validates proposed actions against organizational governance policies,
security baselines, and change window restrictions.

Microsoft Agent Framework integration (Phase 8)
------------------------------------------------
In live mode (USE_LOCAL_MOCKS=false), this agent is driven by a
Microsoft Agent Framework ``Agent`` backed by Azure OpenAI GPT-4.1.

The LLM agent calls our deterministic ``evaluate_policy_rules`` tool,
which checks all governance policies and returns structured violation
data.  The LLM then acts as a decision-maker: it evaluates the context
(ops agent intent, remediation vs. creation) and calls
``submit_governance_decision`` with an adjusted score and justification.
The adjusted score is guardrail-clamped to +/-30 pts of the baseline.

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

Condition types (all must be True for a violation)
---------------------------------------------------
* tags_match        — resource has all specified key=value pairs
* tags_absent       — resource is MISSING one or more required tag keys
* resource_type_match — ARM resource type matches (prefix-aware for sub-resources)
* blocked_actions   — action_type is in the blocked list
* environment_match — environment string matches (inferred from ARM ID if absent)
* blocked_windows   — current time is inside a blocked change window
* cost_impact_threshold — estimated cost change exceeds threshold
* reason_pattern    — action.reason matches a regex pattern (case-insensitive)
* nsg_change_direction — action.nsg_change_direction equals the required value ("open" | "restrict")
"""

import json
import re
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
You are RuriSkry's Policy Compliance Governance Agent — an expert in cloud
governance with the authority to ADJUST compliance scores based on contextual reasoning.

## Your role
You receive a proposed infrastructure action along with a BASELINE compliance
score computed by deterministic policy rules. Your job is to reason about whether
that baseline score is appropriate given the FULL CONTEXT, including:

- The ops agent's INTENT (why it proposed this action)
- Whether the action REMEDIATES a problem vs. CREATES one
- The specific policies violated and whether they truly apply
- Edge cases the deterministic rules cannot handle

## Process
1. Call the `evaluate_policy_rules` tool to get the deterministic baseline score.
2. Review the baseline violations carefully.
3. For each violation, reason about whether it truly applies given the context:
   - Is the ops agent trying to FIX the issue the policy is designed to prevent?
   - Is the policy matching on a DESCRIPTION of a problem rather than the CREATION of one?
   - Are there mitigating factors (tags, environment, time) that the rules missed?
4. Call `submit_governance_decision` with your adjusted score and justification.

## Adjustment rules
- You may adjust the baseline score by at most +/-30 points
- Adjustments MUST have clear justification
- When an ops agent is REMEDIATING a known issue, consider reducing the score
- When context suggests HIGHER risk than rules detected, increase the score
- If the baseline is correct, set adjusted_score equal to the baseline
- When overriding a specific policy violation, include its policy_id in your adjustment
  so the audit trail can annotate which violation was overridden
  (e.g. {"reason": "Remediation intent", "delta": -40, "policy_id": "POL-DR-001"})

## Critical: Remediation Intent Detection
If the ops agent's reason text DESCRIBES a security problem it found and wants to fix,
that is REMEDIATION INTENT — not a policy violation. Examples:
- "Found SSH port 22 open to 0.0.0.0/0, restricting source" = REMEDIATION (reduce score)
- "Opening SSH port 22 to 0.0.0.0/0" = CREATING a violation (keep or raise score)

Always distinguish between describing a problem to fix it vs. describing a problem to create it.
"""


# ---------------------------------------------------------------------------
# Agent
# ---------------------------------------------------------------------------


class PolicyComplianceAgent:
    """Evaluates proposed actions against organisational governance policies.

    In live mode the Microsoft Agent Framework drives GPT-4.1 to call the
    deterministic tool, evaluate context (remediation intent, policy applicability),
    and submit an adjusted score via ``submit_governance_decision``.

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
        force_deterministic: bool = False,
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
        if not self._use_framework or force_deterministic:
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
            timeout=float(self._cfg.llm_timeout),
        )
        client = OpenAIResponsesClient(
            async_client=azure_openai,
            model_id=self._cfg.azure_openai_deployment,
        )

        result_holder: list[PolicyResult] = []
        llm_decision_holder: list[dict] = []

        @af.tool(
            name="evaluate_policy_rules",
            description=(
                "Run the deterministic policy compliance evaluation against all "
                "governance policies. Returns a JSON object with sri_policy score, "
                "violations list, total_policies_checked, policies_passed, and reasoning."
            ),
        )
        async def evaluate_policy_rules(action_json: str) -> str:
            """Check all governance policies against the proposed action.

            Resource metadata is provided by the system via closure — the LLM
            does not need to supply it. This ensures the evaluation always uses
            the real resource state, not whatever the LLM might guess.
            """
            try:
                a = ProposedAction.model_validate_json(action_json)
            except Exception:
                a = action
            r = self._evaluate_rules(a, resource_metadata, now)
            result_holder.append(r)
            return r.model_dump_json()

        @af.tool(
            name="submit_governance_decision",
            description=(
                "Submit your final governance decision after reviewing the baseline score. "
                "adjusted_score must be within +/-30 of the baseline score. "
                "Provide an adjustment entry for each score change with a clear reason."
            ),
        )
        async def submit_governance_decision(
            adjusted_score: float,
            adjustments_json: str = "[]",
            reasoning: str = "",
            confidence: float = 0.8,
        ) -> str:
            """Record the LLM's governance decision with justification."""
            try:
                adjustments = json.loads(adjustments_json)
            except Exception:
                adjustments = []
            llm_decision_holder.append({
                "adjusted_score": adjusted_score,
                "adjustments": adjustments,
                "reasoning": reasoning,
                "confidence": confidence,
            })
            return "Decision recorded."

        agent = client.as_agent(
            name="policy-compliance-evaluator",
            instructions=_AGENT_INSTRUCTIONS,
            tools=[evaluate_policy_rules, submit_governance_decision],
        )

        from src.infrastructure.llm_throttle import run_with_throttle
        from src.governance_agents._llm_governance import parse_llm_decision, annotate_violations

        meta_str = json.dumps(resource_metadata or {})
        policies_summary = json.dumps(self._policies, indent=2)
        prompt = (
            f"## Proposed Action\n{action.model_dump_json()}\n\n"
            f"## Ops Agent's Reasoning\n{action.reason}\n\n"
            f"## Resource Metadata\n{meta_str}\n\n"
            f"## Organization Policies\n{policies_summary}\n\n"
            "INSTRUCTIONS: First call evaluate_policy_rules to get the baseline score. "
            "Reason about each violation and whether it truly applies given the ops agent's intent. "
            "Then call submit_governance_decision with your adjusted score and justification."
        )
        await run_with_throttle(agent.run, prompt)

        if result_holder:
            base = result_holder[-1]
            adjusted_score, adjustment_text, adj_list = parse_llm_decision(
                llm_decision_holder, base.sri_policy
            )

            # Annotate violations with LLM override reasons.
            # CRITICAL violations require explicit policy_id targeting — generic
            # reasons only apply to non-CRITICAL violations (safety guardrail).
            violations = annotate_violations(
                base.violations, adj_list, base.sri_policy, adjusted_score
            )

            return PolicyResult(
                sri_policy=adjusted_score,
                violations=violations,
                total_policies_checked=base.total_policies_checked,
                policies_passed=base.policies_passed,
                reasoning=base.reasoning + adjustment_text,
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
        # Use prefix match so sub-resources (e.g. .../securityRules/foo) also
        # match their parent type (e.g. Microsoft.Network/networkSecurityGroups).
        if "resource_type_match" in conditions:
            rt = (action.target.resource_type or "").lower()
            match_type = conditions["resource_type_match"].lower()
            checks.append(rt == match_type or rt.startswith(match_type + "/"))

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

        # -- Reason pattern (regex) ----------------------------------------
        if "reason_pattern" in conditions:
            checks.append(self._reason_matches(action.reason, conditions["reason_pattern"]))

        # -- NSG change direction ----------------------------------------
        # Fires only when the agent explicitly declared the change opens access.
        # If nsg_change_direction is None or "restrict", this condition is False
        # and the policy does NOT fire — remediation proposals are not blocked.
        if "nsg_change_direction" in conditions:
            checks.append(
                getattr(action, "nsg_change_direction", None) == conditions["nsg_change_direction"]
            )

        # -- NSG direction unset -----------------------------------------
        # Fires when nsg_change_direction is missing (None) on a modify_nsg action.
        # Used by POL-SEC-003 to surface "producer did not declare intent" explicitly
        # in the audit trail. Severity is HIGH (not CRITICAL) because we do not know
        # whether the change opens or restricts access.
        if conditions.get("nsg_direction_unset"):
            checks.append(getattr(action, "nsg_change_direction", None) is None)

        # -- Required tags absent ------------------------------------------
        if "tags_absent" in conditions:
            checks.append(self._tags_absent(tags, conditions["tags_absent"]))

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
    def _reason_matches(reason: str, pattern: str) -> bool:
        """Return ``True`` if *reason* matches the regex *pattern* (case-insensitive).

        Used by policies that need to inspect the action's reason text for
        specific content — e.g. detecting open ports, wildcard sources, or
        dangerous protocol names.
        """
        return bool(re.search(pattern, reason, re.IGNORECASE))

    @staticmethod
    def _tags_absent(resource_tags: dict[str, str], required_keys: list[str]) -> bool:
        """Return ``True`` if *any* key in *required_keys* is missing from *resource_tags*.

        Used for tag enforcement policies — fires when mandatory governance
        tags are not present on a resource.
        """
        return any(k not in resource_tags for k in required_keys)

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
