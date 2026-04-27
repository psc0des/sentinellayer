"""Shared utilities for LLM-driven governance score adjustment.

All 4 governance agents (Policy, Blast Radius, Historical, Financial) use
these helpers to apply guardrailed LLM score adjustments on top of the
deterministic baseline scores.
"""

import logging
from typing import TYPE_CHECKING

logger = logging.getLogger(__name__)

# Imported lazily to avoid a circular import at module load time.
# `GovernanceAdjustment` and `LLMGovernanceOutput` are only needed inside
# `parse_llm_decision`, which is called at runtime, not at import time.
def _load_models():
    from src.core.models import GovernanceAdjustment, LLMGovernanceOutput  # noqa: PLC0415
    return GovernanceAdjustment, LLMGovernanceOutput

# Maximum points the LLM can adjust from the deterministic baseline in either direction.
MAX_ADJUSTMENT = 30


def clamp_score(baseline: float, adjusted: float) -> float:
    """Clamp LLM-adjusted score to within +/-MAX_ADJUSTMENT of baseline, bounded [0, 100]."""
    floor = max(0.0, baseline - MAX_ADJUSTMENT)
    ceiling = min(100.0, baseline + MAX_ADJUSTMENT)
    return round(max(floor, min(ceiling, adjusted)), 2)


def format_adjustment_text(
    baseline: float,
    clamped: float,
    adjustments: list[dict],
    llm_reasoning: str,
) -> str:
    """Format the LLM's decision into a human-readable reasoning block.

    When the guardrail clamps the effective delta below what was requested,
    a note is appended so the audit trail accurately reflects what happened.
    """
    lines = [
        "\nLLM Governance Analysis (GPT-4.1):",
        llm_reasoning,
        f"\nBaseline score: {baseline:.1f} -> LLM-adjusted score: {clamped:.1f}",
    ]
    requested_total = sum(adj.get("delta", 0) for adj in adjustments)
    for adj in adjustments:
        delta = adj.get("delta", 0)
        reason = adj.get("reason", "")
        lines.append(f"  - {reason}: {delta:+.0f} pts")
    actual_delta = clamped - baseline
    if adjustments and abs(actual_delta - requested_total) > 0.01:
        lines.append(
            f"  (Guardrail applied: requested total {requested_total:+.0f} pts, "
            f"effective {actual_delta:+.0f} pts due to +/-{MAX_ADJUSTMENT} pt clamp)"
        )
    return "\n".join(lines)


def parse_llm_decision(
    decision_holder: list[dict],
    baseline: float,
) -> tuple[float, str, list[dict]]:
    """Parse the LLM's submitted decision, apply guardrails.

    Validates the raw dict from ``submit_governance_decision`` against the
    ``LLMGovernanceOutput`` Pydantic schema so malformed LLM output is caught
    at the agent boundary rather than propagating silently.

    Returns:
        (clamped_score, adjustment_text, adjustments_list)

    Returns (baseline, "", []) if:
    - the LLM did not submit a decision (mock mode, LLM error)
    - the submitted dict fails Pydantic validation
    """
    if not decision_holder:
        return baseline, "", []

    GovernanceAdjustment, LLMGovernanceOutput = _load_models()
    raw = decision_holder[-1]

    try:
        decision = LLMGovernanceOutput(
            adjusted_score=float(raw.get("adjusted_score", baseline)),
            adjustments=[
                GovernanceAdjustment(**adj)
                for adj in raw.get("adjustments", [])
                if isinstance(adj, dict) and "reason" in adj and "delta" in adj
            ],
            reasoning=str(raw.get("reasoning", "")),
            confidence=float(raw.get("confidence", 0.8)),
        )
    except Exception as exc:
        logger.warning(
            "LLM governance decision failed validation: %s — using baseline %.1f",
            exc,
            baseline,
        )
        return baseline, "", []

    clamped = clamp_score(baseline, decision.adjusted_score)
    adj_dicts = [a.model_dump() for a in decision.adjustments]

    if clamped != decision.adjusted_score:
        logger.info(
            "LLM governance adjustment clamped: %.1f -> %.1f (baseline %.1f, max drift %d)",
            decision.adjusted_score,
            clamped,
            baseline,
            MAX_ADJUSTMENT,
        )

    text = format_adjustment_text(baseline, clamped, adj_dicts, decision.reasoning)
    return clamped, text, adj_dicts


def format_overrides_for_prompt(overrides: list) -> str:
    """Format a list of VerdictOverride records as a prompt section for LLM agents.

    Returns an empty string when the list is empty — cold-start safe; no section
    is appended to the prompt when the override store has no relevant history.

    Each entry shows the date, action context, original verdict, how the operator
    overrode it, and the operator's stated reason — enough for the LLM to
    calibrate its score against real human decisions.

    Accepts both Pydantic ``VerdictOverride`` instances and raw ``dict`` records
    so it works whether the caller uses the typed model or a raw Cosmos document.
    """
    if not overrides:
        return ""

    lines = [
        "\n## Recent Operator Overrides",
        "The following are real human decisions that overrode this system's verdicts.",
        "Treat them as authoritative ground truth when calibrating your score.",
    ]
    for i, ov in enumerate(overrides, 1):
        if isinstance(ov, dict):
            ts_raw = ov.get("timestamp", "")
            action_type = ov.get("action_type", "?")
            resource_type = ov.get("resource_type", "?")
            original_verdict = ov.get("original_verdict", "?")
            original_sri = ov.get("original_sri", "?")
            override_type = ov.get("override_type", "?")
            reason = ov.get("operator_reason", "?")
        else:
            ts_raw = str(getattr(ov, "timestamp", ""))
            action_type = getattr(ov, "action_type", "?")
            resource_type = getattr(ov, "resource_type", "?")
            original_verdict = getattr(ov, "original_verdict", "?")
            original_sri = getattr(ov, "original_sri", "?")
            _ot = getattr(ov, "override_type", "?")
            override_type = _ot.value if hasattr(_ot, "value") else _ot
            reason = getattr(ov, "operator_reason", "?")

        date_str = ts_raw[:10] if isinstance(ts_raw, str) and len(ts_raw) >= 10 else str(ts_raw)
        sri_str = f"{original_sri:.1f}" if isinstance(original_sri, (int, float)) else str(original_sri)

        lines.append(
            f"\n{i}. [{date_str}] {action_type} on {resource_type}"
            f" (original verdict: {original_verdict}, SRI {sri_str})"
        )
        lines.append(f"   Operator action: {override_type}")
        lines.append(f"   Operator reason: {reason}")
    lines.append("")
    return "\n".join(lines)


def annotate_violations(
    violations: list,
    adj_list: list[dict],
    baseline: float,
    adjusted_score: float,
) -> list:
    """Annotate violations with LLM override reasons from adjustments.

    CRITICAL-severity violations are ONLY overridden when the LLM provided a
    policy_id-specific adjustment targeting that exact violation. The generic
    fallback reason (from the first adjustment) only applies to non-CRITICAL
    violations. This is a guardrail: the LLM must explicitly name the critical
    policy it is overriding.

    Args:
        violations: List of PolicyViolation instances from deterministic evaluation.
        adj_list: Validated adjustment dicts from parse_llm_decision.
        baseline: The deterministic baseline score.
        adjusted_score: The clamped adjusted score.

    Returns:
        New list of PolicyViolation instances with llm_override set where applicable.
    """
    if adjusted_score >= baseline or not adj_list:
        return violations

    # Imported here to avoid circular dependency at module load time.
    from src.core.models import PolicySeverity, PolicyViolation  # noqa: PLC0415

    override_by_id = {
        adj["policy_id"]: adj["reason"]
        for adj in adj_list
        if adj.get("policy_id")
    }
    generic_reason = adj_list[0].get("reason") if adj_list else None

    result = []
    for v in violations:
        specific = override_by_id.get(v.policy_id)
        if specific:
            # LLM explicitly targeted this violation by policy_id
            override = specific
        elif v.severity == PolicySeverity.CRITICAL:
            # CRITICAL violations require explicit policy_id to override.
            # Generic reason is NOT sufficient — this is a safety guardrail.
            override = None
        else:
            # Non-critical: allow generic reason as override
            override = generic_reason
        result.append(PolicyViolation(
            policy_id=v.policy_id,
            name=v.name,
            rule=v.rule,
            severity=v.severity,
            llm_override=override,
        ))
    return result
