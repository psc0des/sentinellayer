"""Message types exchanged between workflow executors."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Union

from src.core.models import (
    BlastRadiusResult,
    FinancialResult,
    HistoricalResult,
    PolicyResult,
    ProposedAction,
)

AgentResultPayload = Union[BlastRadiusResult, PolicyResult, HistoricalResult, FinancialResult]


@dataclass
class GovernanceInput:
    """Initial message fed into the workflow by pipeline.py."""

    action: ProposedAction
    resource_metadata: dict | None
    force_deterministic: bool
    triage_tier: int


@dataclass
class GovernanceAgentResult:
    """Output from one governance agent executor, routed to the scoring executor."""

    agent_name: str  # "blast_radius" | "policy" | "historical" | "financial"
    action: ProposedAction  # carried forward for the scoring executor
    result: AgentResultPayload
    triage_tier: int
