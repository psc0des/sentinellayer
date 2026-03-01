"""SentinelLayer -- end-to-end governance demo.

Runs three real-world scenarios through the full pipeline and prints the
SRI breakdown and governance verdict for each.

Scenarios
---------
1. Cost agent proposes deleting disaster-recovery VM vm-dr-01
   -> Expected: DENIED  (critical policy violation + SRI > 60)

2. SRE agent proposes scaling vm-web-01 B2ls_v2 -> B4ms for a CPU spike
   -> Expected: APPROVED  (SRI < 25, low blast radius, good precedent)

3. Deploy agent proposes opening port 8080 on nsg-east-prod
   -> Expected: ESCALATED  (SRI 26-60, high-severity policy + historical match)

Run from the project root:

    python demo.py
"""

import asyncio
import logging

# Suppress noisy library INFO logs so the demo output is clean.
logging.basicConfig(level=logging.WARNING)

from src.core.decision_tracker import DecisionTracker  # noqa: E402
from src.core.models import ActionTarget, ActionType, ProposedAction, Urgency  # noqa: E402
from src.core.pipeline import SentinelLayerPipeline  # noqa: E402


# ---------------------------------------------------------------------------
# Display helpers
# ---------------------------------------------------------------------------

_W = 67  # output width


def _bar() -> str:
    return "-" * _W


def _header() -> str:
    return "=" * _W


def _print_verdict(
    num: int,
    title: str,
    action: ProposedAction,
    verdict,
) -> None:
    """Pretty-print a single governance verdict with the full SRI breakdown."""
    sri = verdict.sentinel_risk_index
    decision = verdict.decision.value

    icons = {
        "approved":  "[APPROVED]",
        "escalated": "[ESCALATED]",
        "denied":    "[DENIED]",
    }
    hints = {
        "approved":  "SRI within auto-approve threshold -- cleared for execution",
        "escalated": "SRI in human-review band -- action paused pending approval",
        "denied":    "SRI exceeds denial threshold or critical policy violated",
    }
    icon = icons.get(decision, decision.upper())
    hint = hints.get(decision, "")

    # Sanitise to ASCII — the governance engine produces Unicode symbols
    # (e.g. ≤, –) that Windows cp1252 terminals cannot encode.
    reason_display = verdict.reason.encode("ascii", errors="replace").decode("ascii")
    if len(reason_display) > 120:
        reason_display = reason_display[:117] + "..."

    print(f"\n{_header()}")
    print(f"  SCENARIO {num}: {title}")
    print(_bar())
    print(f"  Agent   : {action.agent_id}")
    print(f"  Action  : {action.action_type.value}")
    print(f"  Target  : {action.target.resource_id.split('/')[-1]}")
    reason_short = action.reason[:75] + ("..." if len(action.reason) > 75 else "")
    print(f"  Reason  : {reason_short}")
    print(_bar())
    print("  SRI Breakdown:")
    print(f"    Infrastructure  : {sri.sri_infrastructure:6.1f} / 100   (weight 0.30)")
    print(f"    Policy          : {sri.sri_policy:6.1f} / 100   (weight 0.25)")
    print(f"    Historical      : {sri.sri_historical:6.1f} / 100   (weight 0.25)")
    print(f"    Cost            : {sri.sri_cost:6.1f} / 100   (weight 0.20)")
    print(f"    {'-' * 43}")
    print(f"    COMPOSITE SRI   : {sri.sri_composite:6.1f} / 100")
    print(_bar())
    print(f"  VERDICT : {icon}")
    print(f"            {hint}")
    print(_bar())
    print(f"  {reason_display}")


# ---------------------------------------------------------------------------
# Scenario definitions
# ---------------------------------------------------------------------------


async def scenario_1(pipeline: SentinelLayerPipeline, tracker: DecisionTracker) -> None:
    """Cost agent deletes idle disaster-recovery VM. Expect: DENIED."""
    action = ProposedAction(
        agent_id="cost-optimization-agent",
        action_type=ActionType.DELETE_RESOURCE,
        target=ActionTarget(
            resource_id=(
                "/subscriptions/44caae19-24a6-4c48-9f94-b3aa3865a3f7"
                "/resourceGroups/sentinel-prod-rg"
                "/providers/Microsoft.Compute/virtualMachines/vm-dr-01"
            ),
            resource_type="Microsoft.Compute/virtualMachines",
            current_monthly_cost=36.0,
        ),
        reason=(
            "VM vm-dr-01 has had near-zero CPU utilisation for 30 consecutive days. "
            "Estimated monthly savings: $36."
        ),
        urgency=Urgency.HIGH,
        projected_savings_monthly=36.0,
    )
    verdict = await pipeline.evaluate(action)
    tracker.record(verdict)
    _print_verdict(
        1,
        "Cost agent deletes disaster-recovery VM  ->  expect  DENIED",
        action,
        verdict,
    )


async def scenario_2(pipeline: SentinelLayerPipeline, tracker: DecisionTracker) -> None:
    """SRE agent scales vm-web-01 B2ls_v2 to B4ms for CPU spike. Expect: APPROVED."""
    action = ProposedAction(
        agent_id="monitoring-agent",
        action_type=ActionType.SCALE_UP,
        target=ActionTarget(
            resource_id=(
                "/subscriptions/44caae19-24a6-4c48-9f94-b3aa3865a3f7"
                "/resourceGroups/sentinel-prod-rg"
                "/providers/Microsoft.Compute/virtualMachines/vm-web-01"
            ),
            resource_type="Microsoft.Compute/virtualMachines",
            current_sku="Standard_B2ls_v2",
            proposed_sku="Standard_B4ms",
            current_monthly_cost=36.0,
        ),
        reason=(
            "vm-web-01 CPU averaging 87% over the past 15 minutes (Azure Monitor alert fired). "
            "Scaling from B2ls_v2 to B4ms to restore headroom."
        ),
        urgency=Urgency.MEDIUM,
    )
    verdict = await pipeline.evaluate(action)
    tracker.record(verdict)
    _print_verdict(
        2,
        "SRE agent scales vm-web-01 B2ls_v2->B4ms ->  expect  APPROVED",
        action,
        verdict,
    )


async def scenario_3(pipeline: SentinelLayerPipeline, tracker: DecisionTracker) -> None:
    """Deploy agent opens port 8080 on nsg-east-prod. Expect: ESCALATED."""
    action = ProposedAction(
        agent_id="deploy-agent",
        action_type=ActionType.MODIFY_NSG,
        target=ActionTarget(
            resource_id=(
                "/subscriptions/44caae19-24a6-4c48-9f94-b3aa3865a3f7"
                "/resourceGroups/sentinel-prod-rg"
                "/providers/Microsoft.Network/networkSecurityGroups/nsg-east-prod"
            ),
            resource_type="Microsoft.Network/networkSecurityGroups",
        ),
        reason=(
            "New microservice deployment requires inbound access on port 8080 "
            "through nsg-east-prod to reach the internal API subnet."
        ),
        urgency=Urgency.MEDIUM,
    )
    verdict = await pipeline.evaluate(action)
    tracker.record(verdict)
    _print_verdict(
        3,
        "Deploy agent modifies NSG -- port 8080   ->  expect  ESCALATED",
        action,
        verdict,
    )


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


async def main() -> None:
    print()
    print(_header())
    print("       SentinelLayer -- AI Action Governance Demo")
    print("       Sentinel Risk Index (SRI) End-to-End Pipeline")
    print(_header())
    print()
    print("  Thresholds:")
    print("    SRI <= 25           ->  APPROVED   (auto-execute)")
    print("    SRI 26-60           ->  ESCALATED  (human review)")
    print("    SRI > 60            ->  DENIED     (blocked)")
    print("    Critical violation  ->  DENIED     (always, regardless of score)")
    print()
    print("  Initialising pipeline...")

    pipeline = SentinelLayerPipeline()
    tracker = DecisionTracker()
    print("  Pipeline ready.")
    print()

    await scenario_1(pipeline, tracker)
    await scenario_2(pipeline, tracker)
    await scenario_3(pipeline, tracker)

    print(f"\n{_bar()}")
    print("  Demo complete -- 3 scenarios evaluated.")
    print(_bar())
    print()


if __name__ == "__main__":
    asyncio.run(main())
