"""Decision Explanation Engine — explainability and counterfactual analysis.

Generates a full :class:`DecisionExplanation` for any governance verdict:
ranked contributing factors, policy violation details, risk highlights, and
counterfactual scenarios showing what changes would alter the outcome.

Production design
-----------------
- **GPT-4.1 enrichment** — when Azure OpenAI is available, natural-language
  summaries are generated via ``run_with_throttle`` for rate-limited access.
- **Deterministic fallback** — when ``use_local_mocks`` is true or no endpoint
  is configured, all text is generated from templates.  Tests are fully
  deterministic with zero network calls.
- **In-memory cache** — explanations keyed by ``action_id`` so repeated
  requests return instantly.
"""

import logging
from typing import Optional

from src.config import settings
from src.core.models import (
    Counterfactual,
    DecisionExplanation,
    Factor,
    GovernanceVerdict,
    ProposedAction,
    SRIVerdict,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# SRI™ dimension metadata
# ---------------------------------------------------------------------------

_DIMENSIONS = [
    ("infrastructure", "Infrastructure (Blast Radius)", "sri_infrastructure", settings.sri_weight_infrastructure),
    ("policy", "Policy Compliance", "sri_policy", settings.sri_weight_policy),
    ("historical", "Historical Patterns", "sri_historical", settings.sri_weight_historical),
    ("cost", "Financial Impact", "sri_cost", settings.sri_weight_cost),
]

# Agent result keys → dimension mapping
_AGENT_RESULT_KEYS = {
    "infrastructure": "blast_radius",
    "policy": "policy",
    "historical": "historical",
    "cost": "financial",
}

# ---------------------------------------------------------------------------
# Cache
# ---------------------------------------------------------------------------

_explanation_cache: dict[str, DecisionExplanation] = {}


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


class DecisionExplainer:
    """Generate full explainability reports for governance verdicts.

    Usage::

        explainer = DecisionExplainer()
        explanation = await explainer.explain(verdict, action)
    """

    async def explain(
        self,
        verdict: GovernanceVerdict,
        action: ProposedAction,
    ) -> DecisionExplanation:
        """Build a comprehensive explanation with counterfactual analysis.

        Parameters
        ----------
        verdict : GovernanceVerdict
            The governance verdict to explain.
        action : ProposedAction
            The proposed action that was evaluated.

        Returns
        -------
        DecisionExplanation
            Full explainability report including summary, ranked factors,
            policy violations, risk highlights, and counterfactual scenarios.
        """
        # ── Cache hit ─────────────────────────────────────────────────
        cached = _explanation_cache.get(verdict.action_id)
        if cached is not None:
            logger.debug("Explanation cache hit for action_id=%s", verdict.action_id)
            return cached

        # ── Build contributing factors ────────────────────────────────
        factors = self._build_factors(verdict)

        # ── Extract policy violations ─────────────────────────────────
        policy_violations = self._extract_policy_violations(verdict)

        # ── Risk highlights ───────────────────────────────────────────
        risk_highlights = self._build_risk_highlights(verdict, action, factors)

        # ── Primary factor ────────────────────────────────────────────
        primary = factors[0] if factors else None
        primary_factor = (
            f"{primary.dimension} contributed {primary.weighted_contribution:.1f} "
            f"points to the composite score (score {primary.score:.0f} × weight {primary.weight})"
            if primary else "No primary factor identified"
        )

        # ── Counterfactuals ───────────────────────────────────────────
        counterfactuals = self._build_counterfactuals(verdict, action, factors)

        # ── Summary ───────────────────────────────────────────────────
        summary = await self._generate_summary(verdict, action, factors, policy_violations)

        explanation = DecisionExplanation(
            summary=summary,
            primary_factor=primary_factor,
            contributing_factors=factors,
            policy_violations=policy_violations,
            risk_highlights=risk_highlights,
            counterfactuals=counterfactuals,
        )

        # ── Cache ─────────────────────────────────────────────────────
        _explanation_cache[verdict.action_id] = explanation
        return explanation

    # ------------------------------------------------------------------
    # Factor analysis
    # ------------------------------------------------------------------

    def _build_factors(self, verdict: GovernanceVerdict) -> list[Factor]:
        """Rank all 4 SRI dimensions by weighted contribution (highest first)."""
        sri = verdict.skry_risk_index
        factors: list[Factor] = []

        for dim_key, dim_name, sri_attr, weight in _DIMENSIONS:
            score = getattr(sri, sri_attr, 0.0)
            weighted = round(score * weight, 2)

            # Extract per-agent reasoning from agent_results
            agent_key = _AGENT_RESULT_KEYS.get(dim_key, dim_key)
            agent_data = verdict.agent_results.get(agent_key, {})
            reasoning = agent_data.get("reasoning", "")

            factors.append(Factor(
                dimension=dim_name,
                score=score,
                weight=weight,
                weighted_contribution=weighted,
                reasoning=reasoning,
            ))

        # Sort by weighted contribution descending
        factors.sort(key=lambda f: f.weighted_contribution, reverse=True)
        return factors

    # ------------------------------------------------------------------
    # Policy violations
    # ------------------------------------------------------------------

    def _extract_policy_violations(self, verdict: GovernanceVerdict) -> list[str]:
        """Extract human-readable policy violation descriptions."""
        policy_data = verdict.agent_results.get("policy", {})
        violations = policy_data.get("violations", [])
        result: list[str] = []
        for v in violations:
            if isinstance(v, dict):
                pid = v.get("policy_id", "UNKNOWN")
                name = v.get("name", "Unknown Policy")
                rule = v.get("rule", "")
                severity = v.get("severity", "medium")
                result.append(f"{pid} ({severity.upper()}) — {name}{f': {rule}' if rule else ''}")
            else:
                result.append(str(v))
        return result

    # ------------------------------------------------------------------
    # Risk highlights
    # ------------------------------------------------------------------

    def _build_risk_highlights(
        self,
        verdict: GovernanceVerdict,
        action: ProposedAction,
        factors: list[Factor],
    ) -> list[str]:
        """Generate top 3 risk factors in plain English."""
        highlights: list[str] = []
        sri = verdict.skry_risk_index

        # 1 — Highest scoring dimension
        if factors:
            top = factors[0]
            highlights.append(
                f"{top.dimension} is the highest risk factor with a score of "
                f"{top.score:.0f}/100, contributing {top.weighted_contribution:.1f} "
                f"points to the composite SRI of {sri.sri_composite:.1f}."
            )

        # 2 — Policy violations
        policy_data = verdict.agent_results.get("policy", {})
        violations = policy_data.get("violations", [])
        if violations:
            count = len(violations)
            first = violations[0]
            pid = first.get("policy_id", "N/A") if isinstance(first, dict) else str(first)
            highlights.append(
                f"{count} policy violation(s) detected. "
                f"Most severe: {pid}."
            )
        elif sri.sri_policy > 0:
            highlights.append(
                f"Policy compliance score is {sri.sri_policy:.0f}/100 — "
                "elevated policy risk without explicit violations."
            )

        # 3 — Action-specific risk
        resource_name = action.target.resource_id.split("/")[-1] if "/" in action.target.resource_id else action.target.resource_id
        if action.action_type.value in ("delete_resource", "restart_service"):
            highlights.append(
                f"Destructive action ({action.action_type.value.replace('_', ' ')}) "
                f"on {resource_name} carries inherent risk."
            )
        elif sri.sri_infrastructure > 50:
            blast_data = verdict.agent_results.get("blast_radius", {})
            affected = blast_data.get("affected_resources", [])
            highlights.append(
                f"High blast radius: {len(affected)} dependent resource(s) "
                f"would be affected by this action on {resource_name}."
            )
        elif sri.sri_cost > 40:
            highlights.append(
                f"Financial impact score is {sri.sri_cost:.0f}/100 — "
                "significant cost implications detected."
            )

        return highlights[:3]

    # ------------------------------------------------------------------
    # Counterfactual analysis
    # ------------------------------------------------------------------

    def _build_counterfactuals(
        self,
        verdict: GovernanceVerdict,
        action: ProposedAction,
        factors: list[Factor],
    ) -> list[Counterfactual]:
        """Generate counterfactual scenarios based on verdict type."""
        sri = verdict.skry_risk_index
        decision = verdict.decision

        if decision == SRIVerdict.DENIED:
            return self._counterfactuals_for_denied(sri, verdict, factors)
        elif decision == SRIVerdict.ESCALATED:
            return self._counterfactuals_for_escalated(sri, verdict, factors)
        else:
            return self._counterfactuals_for_approved(sri, verdict, factors)

    def _recalculate_composite(
        self,
        infra: float,
        policy: float,
        historical: float,
        cost: float,
    ) -> float:
        """Recalculate SRI composite using the same formula as GovernanceDecisionEngine."""
        raw = (
            infra * settings.sri_weight_infrastructure
            + policy * settings.sri_weight_policy
            + historical * settings.sri_weight_historical
            + cost * settings.sri_weight_cost
        )
        # Round to 1 decimal so predicted_new_score and the embedded explanation
        # text (which formats the same value with :.1f) are always identical.
        return round(min(raw, 100.0), 1)

    def _verdict_for_score(self, composite: float, has_critical: bool = False) -> str:
        """Determine verdict label for a given composite score."""
        if has_critical:
            return "DENIED"
        if composite > settings.sri_human_review_threshold:
            return "DENIED"
        if composite > settings.sri_auto_approve_threshold:
            return "ESCALATED"
        return "APPROVED"

    def _counterfactuals_for_denied(
        self,
        sri,
        verdict: GovernanceVerdict,
        factors: list[Factor],
    ) -> list[Counterfactual]:
        """For DENIED verdicts, show paths to ESCALATED or APPROVED."""
        results: list[Counterfactual] = []
        scores = {
            "infrastructure": sri.sri_infrastructure,
            "policy": sri.sri_policy,
            "historical": sri.sri_historical,
            "cost": sri.sri_cost,
        }
        dim_to_key = {
            "Infrastructure (Blast Radius)": "infrastructure",
            "Policy Compliance": "policy",
            "Historical Patterns": "historical",
            "Financial Impact": "cost",
        }

        # Check if there are critical policy violations
        policy_data = verdict.agent_results.get("policy", {})
        violations = policy_data.get("violations", [])
        has_critical = any(
            (v.get("severity", "") == "critical" if isinstance(v, dict) else False)
            for v in violations
        )

        # 1 — Remove top policy violation
        if violations:
            first_v = violations[0]
            pid = first_v.get("policy_id", "N/A") if isinstance(first_v, dict) else str(first_v)
            vname = first_v.get("name", "policy") if isinstance(first_v, dict) else "policy"
            new_policy = max(0, sri.sri_policy - 40)  # removing one violation drops ~40 points
            new_composite = self._recalculate_composite(
                sri.sri_infrastructure, new_policy, sri.sri_historical, sri.sri_cost,
            )
            new_verdict = self._verdict_for_score(new_composite, has_critical=False)
            results.append(Counterfactual(
                change_description=f"Remove {pid} violation ({vname})",
                predicted_new_score=new_composite,
                predicted_new_verdict=new_verdict,
                explanation=(
                    f"Without {pid}, the Policy score drops from {sri.sri_policy:.0f} to "
                    f"{new_policy:.0f}. Composite SRI drops from {sri.sri_composite:.1f} to "
                    f"{new_composite:.1f} ({new_verdict})."
                ),
            ))

        # 2 — Target a non-critical resource (reduce blast radius)
        if sri.sri_infrastructure > 30:
            new_infra = 15.0  # typical score for a non-critical, isolated resource
            new_composite = self._recalculate_composite(
                new_infra, sri.sri_policy, sri.sri_historical, sri.sri_cost,
            )
            new_verdict = self._verdict_for_score(new_composite, has_critical=has_critical and not violations)
            results.append(Counterfactual(
                change_description="Target a non-critical resource with no dependencies",
                predicted_new_score=new_composite,
                predicted_new_verdict=new_verdict,
                explanation=(
                    f"Infrastructure score drops from {sri.sri_infrastructure:.0f} to 15. "
                    f"Composite SRI drops from {sri.sri_composite:.1f} to "
                    f"{new_composite:.1f} ({new_verdict})."
                ),
            ))

        # 3 — Remove all dependencies (reduce blast radius to minimal)
        if sri.sri_infrastructure > 20:
            new_infra = 5.0
            new_composite = self._recalculate_composite(
                new_infra, sri.sri_policy, sri.sri_historical, sri.sri_cost,
            )
            new_verdict = self._verdict_for_score(new_composite, has_critical=has_critical and not violations)
            results.append(Counterfactual(
                change_description="Remove all resource dependencies",
                predicted_new_score=new_composite,
                predicted_new_verdict=new_verdict,
                explanation=(
                    f"With zero dependencies, Infrastructure score drops to 5. "
                    f"Composite SRI becomes {new_composite:.1f} ({new_verdict})."
                ),
            ))

        return results[:3]

    def _counterfactuals_for_escalated(
        self,
        sri,
        verdict: GovernanceVerdict,
        factors: list[Factor],
    ) -> list[Counterfactual]:
        """For ESCALATED verdicts, show path to APPROVED."""
        results: list[Counterfactual] = []

        # 1 — Reduce financial impact
        if sri.sri_cost > 15:
            new_cost = 10.0
            new_composite = self._recalculate_composite(
                sri.sri_infrastructure, sri.sri_policy, sri.sri_historical, new_cost,
            )
            new_verdict = self._verdict_for_score(new_composite)
            results.append(Counterfactual(
                change_description="Reduce financial impact (target lower-cost resource)",
                predicted_new_score=new_composite,
                predicted_new_verdict=new_verdict,
                explanation=(
                    f"Cost score drops from {sri.sri_cost:.0f} to 10. "
                    f"Composite SRI drops from {sri.sri_composite:.1f} to "
                    f"{new_composite:.1f} ({new_verdict})."
                ),
            ))

        # 2 — No similar past incidents
        if sri.sri_historical > 15:
            new_hist = 5.0
            new_composite = self._recalculate_composite(
                sri.sri_infrastructure, sri.sri_policy, new_hist, sri.sri_cost,
            )
            new_verdict = self._verdict_for_score(new_composite)
            results.append(Counterfactual(
                change_description="No similar past incidents on record",
                predicted_new_score=new_composite,
                predicted_new_verdict=new_verdict,
                explanation=(
                    f"Historical score drops from {sri.sri_historical:.0f} to 5. "
                    f"Composite SRI drops from {sri.sri_composite:.1f} to "
                    f"{new_composite:.1f} ({new_verdict})."
                ),
            ))

        # 3 — Reduce blast radius
        if sri.sri_infrastructure > 15:
            new_infra = 10.0
            new_composite = self._recalculate_composite(
                new_infra, sri.sri_policy, sri.sri_historical, sri.sri_cost,
            )
            new_verdict = self._verdict_for_score(new_composite)
            results.append(Counterfactual(
                change_description="Target a low-dependency resource",
                predicted_new_score=new_composite,
                predicted_new_verdict=new_verdict,
                explanation=(
                    f"Infrastructure score drops from {sri.sri_infrastructure:.0f} to 10. "
                    f"Composite SRI drops from {sri.sri_composite:.1f} to "
                    f"{new_composite:.1f} ({new_verdict})."
                ),
            ))

        return results[:3]

    def _counterfactuals_for_approved(
        self,
        sri,
        verdict: GovernanceVerdict,
        factors: list[Factor],
    ) -> list[Counterfactual]:
        """For APPROVED verdicts, show what would have triggered escalation/denial."""
        results: list[Counterfactual] = []

        # 1 — If tagged critical=true
        new_infra = max(sri.sri_infrastructure, 65.0)
        new_composite = self._recalculate_composite(
            new_infra, sri.sri_policy, sri.sri_historical, sri.sri_cost,
        )
        new_verdict = self._verdict_for_score(new_composite)
        if new_verdict != "APPROVED":
            results.append(Counterfactual(
                change_description="If this resource were tagged critical=true",
                predicted_new_score=new_composite,
                predicted_new_verdict=new_verdict,
                explanation=(
                    f"Critical tag raises Infrastructure score to {new_infra:.0f}. "
                    f"Composite SRI rises from {sri.sri_composite:.1f} to "
                    f"{new_composite:.1f} ({new_verdict})."
                ),
            ))

        # 2 — If resource had many dependencies
        new_infra = 80.0
        new_composite = self._recalculate_composite(
            new_infra, sri.sri_policy, sri.sri_historical, sri.sri_cost,
        )
        new_verdict = self._verdict_for_score(new_composite)
        if new_verdict != "APPROVED":
            results.append(Counterfactual(
                change_description="If this resource had multiple downstream dependencies",
                predicted_new_score=new_composite,
                predicted_new_verdict=new_verdict,
                explanation=(
                    f"High dependency count raises Infrastructure score to 80. "
                    f"Composite SRI rises from {sri.sri_composite:.1f} to "
                    f"{new_composite:.1f} ({new_verdict})."
                ),
            ))

        # 3 — If a critical policy violation existed
        new_policy = 95.0
        new_composite_v = self._recalculate_composite(
            sri.sri_infrastructure, new_policy, sri.sri_historical, sri.sri_cost,
        )
        results.append(Counterfactual(
            change_description="If a critical policy violation (e.g. POL-DR-001) were triggered",
            predicted_new_score=new_composite_v,
            predicted_new_verdict="DENIED",
            explanation=(
                f"A critical policy violation forces DENIED regardless of composite. "
                f"Policy score would rise to {new_policy:.0f}, composite to "
                f"{new_composite_v:.1f}."
            ),
        ))

        return results[:3]

    # ------------------------------------------------------------------
    # Natural language summary
    # ------------------------------------------------------------------

    async def _generate_summary(
        self,
        verdict: GovernanceVerdict,
        action: ProposedAction,
        factors: list[Factor],
        policy_violations: list[str],
    ) -> str:
        """Generate a 2-3 sentence plain English summary.

        Uses GPT-4.1 when available; falls back to deterministic templates.
        """
        # Try GPT-4.1 enrichment in production
        llm_summary = await self._try_llm_summary(verdict, action, factors, policy_violations)
        if llm_summary:
            return llm_summary

        # Deterministic fallback
        return self._template_summary(verdict, action, factors, policy_violations)

    async def _try_llm_summary(
        self,
        verdict: GovernanceVerdict,
        action: ProposedAction,
        factors: list[Factor],
        policy_violations: list[str],
    ) -> Optional[str]:
        """Attempt to generate a summary via GPT-4.1."""
        endpoint = settings.azure_openai_endpoint
        if not endpoint or settings.use_local_mocks:
            return None

        try:
            from src.infrastructure.llm_throttle import run_with_throttle
            from openai import AsyncAzureOpenAI
            from azure.identity import DefaultAzureCredential, get_bearer_token_provider

            token_provider = get_bearer_token_provider(
                DefaultAzureCredential(),
                "https://cognitiveservices.azure.com/.default",
            )
            client = AsyncAzureOpenAI(
                azure_endpoint=endpoint,
                azure_ad_token_provider=token_provider,
                api_version=settings.azure_openai_api_version,
            )

            resource_name = (
                action.target.resource_id.split("/")[-1]
                if "/" in action.target.resource_id
                else action.target.resource_id
            )

            prompt = (
                f"You are an SRE governance explainer. Write a 2-3 sentence plain English "
                f"summary of this governance decision.\n\n"
                f"Action: {action.action_type.value} on {resource_name} by {action.agent_id}\n"
                f"Verdict: {verdict.decision.value.upper()} (SRI Composite: {verdict.skry_risk_index.sri_composite})\n"
                f"Top factor: {factors[0].dimension} ({factors[0].weighted_contribution:.1f} points)\n"
                f"Policy violations: {', '.join(policy_violations) if policy_violations else 'None'}\n"
                f"Agent reason: {action.reason[:200]}\n\n"
                f"Write a concise, non-technical summary a manager could understand."
            )

            async def _call():
                resp = await client.chat.completions.create(
                    model=settings.azure_openai_deployment,
                    messages=[{"role": "user", "content": prompt}],
                    max_tokens=200,
                    temperature=0.3,
                )
                return resp.choices[0].message.content.strip()

            return await run_with_throttle(_call)

        except Exception:
            logger.debug("LLM summary generation failed — using template fallback.", exc_info=True)
            return None

    def _template_summary(
        self,
        verdict: GovernanceVerdict,
        action: ProposedAction,
        factors: list[Factor],
        policy_violations: list[str],
    ) -> str:
        """Deterministic template-based summary (no LLM required)."""
        sri = verdict.skry_risk_index
        resource_name = (
            action.target.resource_id.split("/")[-1]
            if "/" in action.target.resource_id
            else action.target.resource_id
        )
        decision = verdict.decision.value.upper()
        top_factor = factors[0].dimension if factors else "Unknown"

        if verdict.decision == SRIVerdict.DENIED:
            if policy_violations:
                return (
                    f"RuriSkry {decision} the {action.action_type.value.replace('_', ' ')} "
                    f"action on {resource_name} proposed by {action.agent_id}. "
                    f"The primary driver was {top_factor}, contributing "
                    f"{factors[0].weighted_contribution:.1f} points to a composite SRI of "
                    f"{sri.sri_composite:.1f}. "
                    f"Policy violation(s) detected: {policy_violations[0].split(' — ')[0]}."
                )
            return (
                f"RuriSkry {decision} the {action.action_type.value.replace('_', ' ')} "
                f"action on {resource_name} proposed by {action.agent_id}. "
                f"The composite SRI of {sri.sri_composite:.1f} exceeds the denial threshold of "
                f"{settings.sri_human_review_threshold}. "
                f"The primary contributor was {top_factor} at "
                f"{factors[0].weighted_contribution:.1f} points."
            )
        elif verdict.decision == SRIVerdict.ESCALATED:
            return (
                f"RuriSkry {decision} the {action.action_type.value.replace('_', ' ')} "
                f"action on {resource_name} for human review. "
                f"The composite SRI of {sri.sri_composite:.1f} falls in the review band "
                f"({settings.sri_auto_approve_threshold}–{settings.sri_human_review_threshold}). "
                f"{top_factor} was the dominant factor at "
                f"{factors[0].weighted_contribution:.1f} points."
            )
        else:
            return (
                f"RuriSkry {decision} the {action.action_type.value.replace('_', ' ')} "
                f"action on {resource_name} proposed by {action.agent_id}. "
                f"The composite SRI of {sri.sri_composite:.1f} is within the auto-approval "
                f"threshold (≤ {settings.sri_auto_approve_threshold}), indicating low risk "
                f"across all dimensions."
            )
