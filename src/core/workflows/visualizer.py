"""Static Mermaid rendering of the governance workflow topology.

The graph shape is fixed at build time — it has no runtime branching beyond the
condition gate (which is already a node). Rendering once at import time and
caching the string is faster than rebuilding on every API hit and keeps the
visualizer side-effect-free.

Mock-mode agents are used for instantiation because the topology does not
depend on agent state, and we don't want hitting the diagram endpoint to
require Foundry credentials or live Cosmos.
"""

from __future__ import annotations

from functools import lru_cache


@lru_cache(maxsize=1)
def get_governance_workflow_mermaid() -> str:
    """Return the Mermaid `flowchart TD` for the governance workflow.

    Cached for the lifetime of the process — topology is immutable.
    """
    from agent_framework import WorkflowViz

    from src.core.governance_engine import GovernanceDecisionEngine
    from src.core.workflows.governance_workflow import build_governance_workflow
    from src.governance_agents.blast_radius_agent import BlastRadiusAgent
    from src.governance_agents.financial_agent import FinancialImpactAgent
    from src.governance_agents.historical_agent import HistoricalPatternAgent
    from src.governance_agents.policy_agent import PolicyComplianceAgent

    workflow = build_governance_workflow(
        blast=BlastRadiusAgent(),
        policy=PolicyComplianceAgent(),
        historical=HistoricalPatternAgent(),
        financial=FinancialImpactAgent(),
        engine=GovernanceDecisionEngine(),
    )
    return WorkflowViz(workflow).to_mermaid()
