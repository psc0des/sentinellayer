"""SentinelLayer — A2A Protocol Demo (Phase 10).

What this demo shows
---------------------
Three operational agents talk to SentinelLayer using the A2A protocol:

  1. Cost agent    proposes deleting  vm-23 (disaster-recovery VM)  → DENIED
  2. Monitoring agent proposes scaling web-tier-01                  → APPROVED
  3. Deploy agent  proposes modifying nsg-east                      → ESCALATED

How it works
------------
1. SentinelLayer A2A server starts in a background thread (uvicorn).
2. Each operational agent resolves the Agent Card from
   /.well-known/agent-card.json (agent discovery).
3. Each agent sends its ProposedAction as a streaming A2A task.
4. The server streams SSE progress messages ("Evaluating blast radius...",
   "Checking policy compliance...", etc.).
5. The final GovernanceVerdict arrives as an A2A artifact.
6. The agent registry records each interaction.
7. The demo prints a summary of all connected agents.

Run
---
    python demo_a2a.py
"""

import asyncio
import logging
import os
import threading
import time
from typing import Any

import uvicorn

# Use local mocks in the demo so no Azure credentials are needed
os.environ.setdefault("USE_LOCAL_MOCKS", "true")
os.environ.setdefault("A2A_SERVER_URL", "http://127.0.0.1:8765")

from src.a2a.agent_registry import AgentRegistry
from src.a2a.operational_a2a_clients import send_action_to_sentinel
from src.core.models import ActionTarget, ActionType, ProposedAction, Urgency

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(name)-40s  %(message)s",
)
logger = logging.getLogger("demo_a2a")

_A2A_SERVER_URL = os.environ["A2A_SERVER_URL"]
_A2A_PORT = 8765  # Use a different port from the dashboard API (8000)


# ---------------------------------------------------------------------------
# Server management
# ---------------------------------------------------------------------------


def _run_server_in_thread() -> None:
    """Start the SentinelLayer A2A server in a background daemon thread.

    Why a daemon thread?
    A daemon thread is automatically killed when the main program exits.
    This means we don't need to manually shut down the server at the end
    of the demo — Python handles it for us.

    Why a new asyncio event loop?
    uvicorn.Server.serve() is a coroutine.  Since the main thread already
    has its own event loop (running the demo), we spin up a *separate*
    event loop in this background thread just for the server.
    This is the standard pattern for embedding uvicorn programmatically.
    """
    from src.a2a.sentinel_a2a_server import create_app

    server_app = create_app()
    config = uvicorn.Config(
        server_app,
        host="127.0.0.1",
        port=_A2A_PORT,
        log_level="warning",  # Suppress uvicorn access logs for cleaner demo output
    )
    server = uvicorn.Server(config)

    # Each thread needs its own event loop — asyncio.run() creates a fresh one
    asyncio.run(server.serve())


async def _wait_for_server(url: str, max_wait: float = 15.0) -> bool:
    """Poll the server's Agent Card endpoint until it responds (or timeout).

    Args:
        url: Base URL of the A2A server.
        max_wait: Maximum seconds to wait before giving up.

    Returns:
        True if the server is ready, False if it timed out.
    """
    import httpx

    deadline = time.monotonic() + max_wait
    while time.monotonic() < deadline:
        try:
            async with httpx.AsyncClient() as client:
                resp = await client.get(
                    f"{url}/.well-known/agent-card.json", timeout=2.0
                )
                if resp.status_code == 200:
                    return True
        except Exception:
            pass
        await asyncio.sleep(0.5)
    return False


# ---------------------------------------------------------------------------
# Demo scenarios
# ---------------------------------------------------------------------------


async def scenario_1_cost_agent_denied() -> dict[str, Any] | None:
    """Cost agent proposes deleting vm-23 (disaster-recovery VM) → DENIED.

    vm-23 is tagged purpose=disaster-recovery.
    POL-DR-001 (CRITICAL) fires and forces a DENIED decision regardless of
    the numeric SRI composite.
    """
    logger.info("=" * 60)
    logger.info("SCENARIO 1 — Cost Agent: DELETE vm-23 (disaster-recovery)")
    logger.info("=" * 60)

    action = ProposedAction(
        agent_id="cost-optimization-agent",
        action_type=ActionType.DELETE_RESOURCE,
        target=ActionTarget(
            resource_id="vm-23",
            resource_type="Microsoft.Compute/virtualMachines",
            resource_group="rg-sentinellayer",
            current_monthly_cost=847.0,
        ),
        reason="VM vm-23 appears idle — proposing deletion to save $847/month.",
        urgency=Urgency.MEDIUM,
        projected_savings_monthly=847.0,
    )

    verdict = await send_action_to_sentinel(
        action, _A2A_SERVER_URL, "cost-optimization-agent"
    )

    if verdict:
        _update_registry("cost-optimization-agent", verdict.decision.value)
        _print_verdict("Scenario 1", verdict)

    return verdict.model_dump() if verdict else None


async def scenario_2_monitoring_agent_approved() -> dict[str, Any] | None:
    """Monitoring agent proposes scaling web-tier-01 → APPROVED.

    web-tier-01 is a Standard_D4s_v3 VM with no special tags.
    No critical policy violations → low SRI composite → APPROVED.
    """
    logger.info("=" * 60)
    logger.info("SCENARIO 2 — Monitoring Agent: SCALE_UP web-tier-01")
    logger.info("=" * 60)

    action = ProposedAction(
        agent_id="monitoring-agent",
        action_type=ActionType.SCALE_UP,
        target=ActionTarget(
            resource_id="web-tier-01",
            resource_type="Microsoft.Compute/virtualMachines",
            resource_group="rg-sentinellayer",
            current_sku="Standard_D4s_v3",
            proposed_sku="Standard_D8s_v3",
            current_monthly_cost=420.0,
        ),
        reason="web-tier-01 CPU utilization exceeded 90% for 30 minutes.",
        urgency=Urgency.HIGH,
    )

    verdict = await send_action_to_sentinel(
        action, _A2A_SERVER_URL, "monitoring-agent"
    )

    if verdict:
        _update_registry("monitoring-agent", verdict.decision.value)
        _print_verdict("Scenario 2", verdict)

    return verdict.model_dump() if verdict else None


async def scenario_3_deploy_agent_escalated() -> dict[str, Any] | None:
    """Deploy agent proposes modifying nsg-east → ESCALATED.

    POL-SEC-001 (HIGH severity, not CRITICAL) fires for NSG changes.
    The composite SRI lands in the 26-60 ESCALATED band.
    """
    logger.info("=" * 60)
    logger.info("SCENARIO 3 — Deploy Agent: MODIFY_NSG nsg-east")
    logger.info("=" * 60)

    action = ProposedAction(
        agent_id="deploy-agent",
        action_type=ActionType.MODIFY_NSG,
        target=ActionTarget(
            resource_id="nsg-east",
            resource_type="Microsoft.Network/networkSecurityGroups",
            resource_group="rg-sentinellayer",
        ),
        reason="Adding deny-all inbound rule to enforce zero-trust posture.",
        urgency=Urgency.MEDIUM,
    )

    verdict = await send_action_to_sentinel(
        action, _A2A_SERVER_URL, "deploy-agent"
    )

    if verdict:
        _update_registry("deploy-agent", verdict.decision.value)
        _print_verdict("Scenario 3", verdict)

    return verdict.model_dump() if verdict else None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _update_registry(agent_name: str, decision: str) -> None:
    """Register agent and update stats after an evaluation."""
    registry = AgentRegistry()
    registry.register_agent(agent_name, agent_card_url=_A2A_SERVER_URL)
    registry.update_agent_stats(agent_name, decision)


def _print_verdict(label: str, verdict: Any) -> None:
    """Pretty-print a GovernanceVerdict to the console."""
    sri = verdict.sentinel_risk_index
    print(f"\n{'━' * 60}")
    print(f"  {label} — {verdict.decision.value.upper()}")
    print(f"  Resource : {verdict.proposed_action.target.resource_id}")
    print(f"  SRI™ Composite  : {sri.sri_composite:.1f}")
    print(f"  ├─ Infrastructure : {sri.sri_infrastructure:.1f}")
    print(f"  ├─ Policy         : {sri.sri_policy:.1f}")
    print(f"  ├─ Historical     : {sri.sri_historical:.1f}")
    print(f"  └─ Cost           : {sri.sri_cost:.1f}")
    print(f"  Reason : {verdict.reason[:120]}")
    print(f"{'━' * 60}\n")


def _print_registry_summary() -> None:
    """Print the connected agents summary from the registry."""
    registry = AgentRegistry()
    agents = registry.get_connected_agents()

    print("\n" + "═" * 60)
    print("  CONNECTED A2A AGENTS SUMMARY")
    print("═" * 60)
    for agent in agents:
        total = agent.get("total_actions_proposed", 0)
        approved = agent.get("approval_count", 0)
        denied = agent.get("denial_count", 0)
        escalated = agent.get("escalation_count", 0)
        print(f"  Agent   : {agent['name']}")
        print(f"  Seen    : {agent.get('last_seen', 'N/A')}")
        print(f"  Actions : {total} total  ({approved} approved / {denied} denied / {escalated} escalated)")
        print()
    print("═" * 60 + "\n")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


async def main() -> None:
    """Run the full A2A demo end-to-end.

    Steps:
    1. Start the SentinelLayer A2A server in a background thread.
    2. Wait for it to be ready (polls /.well-known/agent-card.json).
    3. Run three scenarios sequentially.
    4. Print the agent registry summary.
    """
    print("\n" + "═" * 60)
    print("  SentinelLayer — A2A Protocol Demo (Phase 10)")
    print("═" * 60)

    # ── Start A2A server in a background thread ───────────────────────
    print(f"\nStarting SentinelLayer A2A server on {_A2A_SERVER_URL} ...")
    server_thread = threading.Thread(target=_run_server_in_thread, daemon=True)
    server_thread.start()

    # ── Wait until the server is accepting connections ────────────────
    ready = await _wait_for_server(_A2A_SERVER_URL, max_wait=20.0)
    if not ready:
        print("ERROR: A2A server did not start in time. Exiting.")
        return

    print("A2A server is ready!\n")

    # ── Resolve the Agent Card (demonstrates agent discovery) ─────────
    import httpx

    async with httpx.AsyncClient() as http_client:
        resp = await http_client.get(
            f"{_A2A_SERVER_URL}/.well-known/agent-card.json"
        )
        card = resp.json()
        print(f"Agent Card discovered:")
        print(f"  Name    : {card.get('name')}")
        print(f"  Version : {card.get('version')}")
        print(f"  Skills  : {[s['id'] for s in card.get('skills', [])]}")
        print()

    # ── Run the three scenarios ───────────────────────────────────────
    await scenario_1_cost_agent_denied()
    await scenario_2_monitoring_agent_approved()
    await scenario_3_deploy_agent_escalated()

    # ── Print connected agents summary ───────────────────────────────
    _print_registry_summary()


if __name__ == "__main__":
    asyncio.run(main())
