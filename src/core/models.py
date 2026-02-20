"""SentinelLayer data models — Pydantic schemas for actions, SRI™, and verdicts."""

from datetime import datetime, timezone
from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field


# ============================================
# Enums
# ============================================

class ActionType(str, Enum):
    """Types of infrastructure actions agents can propose."""
    SCALE_UP = "scale_up"
    SCALE_DOWN = "scale_down"
    DELETE_RESOURCE = "delete_resource"
    RESTART_SERVICE = "restart_service"
    MODIFY_NSG = "modify_nsg"
    CREATE_RESOURCE = "create_resource"
    UPDATE_CONFIG = "update_config"


class Urgency(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class SRIVerdict(str, Enum):
    """SRI™ governance decision outcomes."""
    APPROVED = "approved"
    ESCALATED = "escalated"
    DENIED = "denied"


class PolicySeverity(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


# ============================================
# Action Models (from Operational Agents)
# ============================================

class ActionTarget(BaseModel):
    """Target resource for a proposed action."""
    resource_id: str
    resource_type: str
    resource_group: Optional[str] = None
    current_sku: Optional[str] = None
    proposed_sku: Optional[str] = None
    current_monthly_cost: Optional[float] = None


class ProposedAction(BaseModel):
    """An infrastructure action proposed by an operational agent."""
    agent_id: str
    action_type: ActionType
    target: ActionTarget
    reason: str
    urgency: Urgency = Urgency.LOW
    projected_savings_monthly: Optional[float] = None
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


# ============================================
# SRI™ Scoring Models
# ============================================

class SRIBreakdown(BaseModel):
    """Sentinel Risk Index — dimensional breakdown."""
    sri_infrastructure: float = Field(
        ge=0, le=100,
        description="SRI:Infrastructure — blast radius impact score"
    )
    sri_policy: float = Field(
        ge=0, le=100,
        description="SRI:Policy — governance compliance risk score"
    )
    sri_historical: float = Field(
        ge=0, le=100,
        description="SRI:Historical — incident pattern similarity score"
    )
    sri_cost: float = Field(
        ge=0, le=100,
        description="SRI:Cost — financial volatility score"
    )
    sri_composite: float = Field(
        ge=0, le=100,
        description="SRI™ Composite — weighted aggregate score"
    )


class PolicyViolation(BaseModel):
    """A governance policy violation detected by the Policy Agent."""
    policy_id: str
    name: str
    rule: str
    severity: PolicySeverity


class SimilarIncident(BaseModel):
    """A historical incident similar to the proposed action."""
    incident_id: str
    description: str
    action_taken: str
    outcome: str
    lesson: str
    service: str
    severity: str
    date: str
    similarity_score: Optional[float] = None


# ============================================
# Agent Result Models
# ============================================

class BlastRadiusResult(BaseModel):
    """Output from the Blast Radius Simulation Agent."""
    agent: str = "blast_radius"
    sri_infrastructure: float
    affected_resources: list = []
    affected_services: list = []
    single_points_of_failure: list = []
    availability_zones_impacted: list = []
    reasoning: str = ""


class PolicyResult(BaseModel):
    """Output from the Policy & Compliance Agent."""
    agent: str = "policy_compliance"
    sri_policy: float
    violations: list[PolicyViolation] = []
    total_policies_checked: int = 0
    policies_passed: int = 0
    reasoning: str = ""


class HistoricalResult(BaseModel):
    """Output from the Historical Pattern Agent."""
    agent: str = "historical_pattern"
    sri_historical: float
    similar_incidents: list[SimilarIncident] = []
    most_relevant_incident: Optional[SimilarIncident] = None
    recommended_procedure: Optional[str] = None
    reasoning: str = ""


class FinancialResult(BaseModel):
    """Output from the Financial Impact Agent."""
    agent: str = "financial_impact"
    sri_cost: float
    immediate_monthly_change: float = 0.0
    projection_90_day: Optional[dict] = None
    over_optimization_risk: Optional[dict] = None
    reasoning: str = ""


# ============================================
# Governance Verdict (Final Output)
# ============================================

class GovernanceVerdict(BaseModel):
    """Complete SRI™ governance verdict for a proposed action."""
    action_id: str
    timestamp: datetime
    proposed_action: ProposedAction
    sentinel_risk_index: SRIBreakdown
    decision: SRIVerdict
    reason: str
    agent_results: dict = {}
    thresholds: dict = {
        "auto_approve": 25,
        "human_review": 60,
    }


# ============================================
# Audit Trail Record
# ============================================

class AuditRecord(BaseModel):
    """Record stored in Cosmos DB for decision lineage tracking."""
    id: str
    partition_key: str
    timestamp: datetime
    action_summary: str
    sentinel_risk_index: SRIBreakdown
    sri_composite: float
    decision: SRIVerdict
    reason: str
    policy_violations: list[PolicyViolation] = []
    similar_incidents: list[str] = []
    full_evaluation: dict = {}
