"""SentinelLayer — Phase 12 Live Intelligence Demo.

Two-Layer AI Governance in action:

  Layer 1  Operational agents INVESTIGATE real Azure data with GPT-4.1
           before proposing any action (no hardcoded rules).
  Layer 2  SentinelLayer INDEPENDENTLY evaluates every proposal with its
           own four-agent pipeline (blast radius, policy, historical, cost).

Demo scenarios
--------------
1. CPU Alert  → MonitoringAgent confirms vm-web-01 is under load → SCALE_UP
2. Cost Scan  → CostOptimizationAgent discovers vm-dr-01 is idle → SCALE_DOWN
3. Sec Review → DeployAgent audits nsg-east-prod security rules  → MODIFY_NSG

Each scenario shows:
  ✦ Which Azure tools the ops agent called (Resource Graph, Monitor metrics …)
  ✦ The evidence-backed reason the agent produced
  ✦ SentinelLayer's independent SRI score and governance verdict

Run
---
    python demo_live.py

For mock mode (no Azure credentials needed):
    USE_LOCAL_MOCKS=true python demo_live.py
"""

import argparse
import asyncio
import logging

# Keep demo output clean — suppress library noise.
logging.basicConfig(level=logging.WARNING)

from src.core.decision_tracker import DecisionTracker  # noqa: E402
from src.core.pipeline import SentinelLayerPipeline  # noqa: E402
from src.operational_agents.cost_agent import CostOptimizationAgent  # noqa: E402
from src.operational_agents.monitoring_agent import MonitoringAgent  # noqa: E402
from src.operational_agents.deploy_agent import DeployAgent  # noqa: E402

# ---------------------------------------------------------------------------
# Display helpers
# ---------------------------------------------------------------------------

_W = 70


def _bar() -> str:
    return "-" * _W


def _header() -> str:
    return "=" * _W


def _print_scenario_header(num: int, title: str, agent_type: str) -> None:
    print(f"\n{_header()}")
    print(f"  SCENARIO {num}: {title}")
    print(f"  Agent type: {agent_type}")
    print(_header())


def _print_proposal(proposal, idx: int) -> None:
    print(f"\n  Proposal {idx + 1}:")
    print(f"    Action  : {proposal.action_type.value}")
    target = proposal.target.resource_id.split("/")[-1]
    print(f"    Target  : {target}")
    print(f"    Urgency : {proposal.urgency.value}")
    reason_short = proposal.reason[:140] + ("..." if len(proposal.reason) > 140 else "")
    print(f"    Reason  : {reason_short}")


def _print_verdict(verdict) -> None:
    sri = verdict.sentinel_risk_index
    decision = verdict.decision.value
    icons = {
        "approved": "[APPROVED]",
        "escalated": "[ESCALATED]",
        "denied": "[DENIED]",
    }
    llm_used = "Agent Framework Analysis (GPT-4.1):" in verdict.reason
    llm_label = "[GPT-4.1 active]" if llm_used else "[rule-based fallback]"

    print(_bar())
    print(f"  SentinelLayer verdict : {icons.get(decision, decision.upper())}")
    print(f"  LLM                   : {llm_label}")
    print(f"  SRI Composite         : {sri.sri_composite:.1f} / 100")
    print(f"    Infrastructure      : {sri.sri_infrastructure:.1f}")
    print(f"    Policy              : {sri.sri_policy:.1f}")
    print(f"    Historical          : {sri.sri_historical:.1f}")
    print(f"    Cost                : {sri.sri_cost:.1f}")
    reason_display = verdict.reason.encode("ascii", errors="replace").decode("ascii")
    if len(reason_display) > 120:
        reason_display = reason_display[:117] + "..."
    print(f"  Reason : {reason_display}")
    print(_bar())


# ---------------------------------------------------------------------------
# Scenario 1 — Alert-driven scale-up
# ---------------------------------------------------------------------------


async def scenario_1_alert_driven_scaleup(
    pipeline: SentinelLayerPipeline,
    tracker: DecisionTracker,
    resource_group: str = "sentinel-prod-rg",
) -> None:
    """Azure Monitor alert fires for a VM CPU → agent investigates → APPROVED scale-up."""
    _print_scenario_header(
        1,
        f"CPU Alert: VM CPU > 80% in {resource_group} — monitoring agent investigates",
        "MonitoringAgent (alert-driven)",
    )

    alert_payload = {
        "metric": "Percentage CPU",
        "value": 95.0,
        "threshold": 80.0,
        "severity": "3",
        "resource_group": resource_group,
        "alert_name": "HighCPU-alert",
    }

    print(f"\n  Alert received: CPU {alert_payload['value']}% > threshold {alert_payload['threshold']}%")
    print("  MonitoringAgent investigating ...\n")

    agent = MonitoringAgent()
    proposals = await agent.scan(alert_payload=alert_payload)

    if not proposals:
        print("  [No proposals generated — MonitoringAgent found no action needed]")
        return

    for i, proposal in enumerate(proposals):
        _print_proposal(proposal, i)
        print("\n  Submitting to SentinelLayer for governance evaluation ...")
        verdict = await pipeline.evaluate(proposal)
        tracker.record(verdict)
        _print_verdict(verdict)


# ---------------------------------------------------------------------------
# Scenario 2 — Cost optimisation scan
# ---------------------------------------------------------------------------


async def scenario_2_cost_scan(
    pipeline: SentinelLayerPipeline,
    tracker: DecisionTracker,
    resource_group: str | None = None,
) -> None:
    """CostOptimizationAgent scans for idle or over-provisioned resources."""
    rg_label = resource_group or "whole subscription"
    _print_scenario_header(
        2,
        f"FinOps Scan: CostOptimizationAgent scans {rg_label}",
        "CostOptimizationAgent (proactive scan)",
    )

    print("\n  CostOptimizationAgent scanning for wasteful resources ...")
    print("  Querying Resource Graph + Azure Monitor metrics ...\n")

    agent = CostOptimizationAgent()
    proposals = await agent.scan(target_resource_group=resource_group)

    if not proposals:
        print("  [No cost optimisation proposals — no wasteful resources found]")
        return

    print(f"  Agent identified {len(proposals)} proposal(s):")
    for i, proposal in enumerate(proposals):
        _print_proposal(proposal, i)
        print("\n  Submitting to SentinelLayer for governance evaluation ...")
        verdict = await pipeline.evaluate(proposal)
        tracker.record(verdict)
        _print_verdict(verdict)
        await asyncio.sleep(1)  # brief pause between proposals


# ---------------------------------------------------------------------------
# Scenario 3 — Security configuration review
# ---------------------------------------------------------------------------


async def scenario_3_security_review(
    pipeline: SentinelLayerPipeline,
    tracker: DecisionTracker,
    resource_group: str | None = None,
) -> None:
    """DeployAgent audits NSG rules and configuration in the target environment."""
    rg_label = resource_group or "whole subscription"
    _print_scenario_header(
        3,
        f"Security Review: DeployAgent audits NSGs in {rg_label}",
        "DeployAgent (security scan)",
    )

    print("\n  DeployAgent reviewing NSG rules and activity logs ...")
    print("  Querying Resource Graph + NSG rules + activity log ...\n")

    agent = DeployAgent()
    proposals = await agent.scan(target_resource_group=resource_group)

    if not proposals:
        print("  [No proposals — security configuration looks good]")
        return

    print(f"  Agent identified {len(proposals)} proposal(s):")
    for i, proposal in enumerate(proposals):
        _print_proposal(proposal, i)
        print("\n  Submitting to SentinelLayer for governance evaluation ...")
        verdict = await pipeline.evaluate(proposal)
        tracker.record(verdict)
        _print_verdict(verdict)
        await asyncio.sleep(1)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


async def main(resource_group: str | None = None) -> None:
    print()
    print(_header())
    print("  SentinelLayer — Phase 12: Two-Layer AI Governance Demo")
    print("  Intelligent Ops Agents + Independent Governance Evaluation")
    print(_header())
    print()
    print("  Layer 1: Ops agents query REAL Azure data, reason with GPT-4.1,")
    print("           and submit EVIDENCE-BACKED proposals.")
    print("  Layer 2: SentinelLayer INDEPENDENTLY evaluates each proposal")
    print("           with its four-agent SRI pipeline.")
    print()
    rg_display = resource_group or "whole subscription (no --resource-group specified)"
    print(f"  Target: {rg_display}")
    print()
    print("  SRI Thresholds:")
    print("    <= 25     → APPROVED   (auto-execute)")
    print("    26 - 60   → ESCALATED  (human review)")
    print("    > 60      → DENIED     (blocked)")
    print("    Critical policy violation → DENIED (always)")
    print()
    print("  Initialising pipeline ...")
    pipeline = SentinelLayerPipeline()
    tracker = DecisionTracker()
    print("  Pipeline ready.")

    await scenario_1_alert_driven_scaleup(pipeline, tracker, resource_group or "sentinel-prod-rg")
    await asyncio.sleep(2)

    await scenario_2_cost_scan(pipeline, tracker, resource_group)
    await asyncio.sleep(2)

    await scenario_3_security_review(pipeline, tracker, resource_group)

    print(f"\n{_bar()}")
    print("  Demo complete — 3 scenarios evaluated.")
    print("  Check dashboard: python -m src.api.dashboard_api")
    print(_bar())
    print()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="SentinelLayer Phase 12 two-layer intelligence demo"
    )
    parser.add_argument(
        "--resource-group",
        "-g",
        default=None,
        help=(
            "Azure resource group to scope all three agent scans to. "
            "Omit to scan the whole subscription."
        ),
    )
    args = parser.parse_args()
    asyncio.run(main(resource_group=args.resource_group))
