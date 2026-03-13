"""RuriSkry data models — Pydantic schemas for actions, SRI™, and verdicts."""

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
    nsg_change_direction: Optional[str] = None  # "open" | "restrict" — set by NSG agents
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


# ============================================
# SRI™ Scoring Models
# ============================================

class SRIBreakdown(BaseModel):
    """Skry Risk Index — dimensional breakdown."""
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
    llm_override: Optional[str] = None  # Set when LLM determined violation doesn't truly apply


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
    skry_risk_index: SRIBreakdown
    decision: SRIVerdict
    reason: str
    agent_results: dict = {}
    thresholds: dict = {
        "auto_approve": 25,
        "human_review": 60,
    }
    triage_tier: Optional[int] = None  # 1 | 2 | 3 — set by risk_triage (Phase 26)
    triage_mode: Optional[str] = None  # "full" | "deterministic" | None (pre-Phase-27)


# ============================================
# Risk Triage Models (Phase 26)
# ============================================

class ActionFingerprint(BaseModel):
    """Deterministic risk descriptor for an action — computed without any LLM call.

    All fields are derived from the ProposedAction and resource metadata in <1 ms.
    Used by ``classify_tier()`` to route actions to a processing tier (1, 2, or 3).
    """
    action_type: str
    resource_type: str
    environment: str                   # "production" | "staging" | "development" | "unknown"
    compliance_scope: bool             # True → resource is in a compliance-relevant context
    has_network_exposure: bool         # True → action touches network security surface
    has_data_plane_impact: bool        # True → action affects data storage/databases
    is_production: bool
    is_critical_resource: bool         # Tagged critical / disaster-recovery
    estimated_blast_radius: str        # "isolated" | "service" | "environment" | "global"
    change_reversibility: str          # "reversible" | "semi-reversible" | "destructive"


class OrgContext(BaseModel):
    """Organizational knowledge injected into governance evaluation.

    Provides the triage engine with context it cannot derive from a single
    resource: compliance obligations, risk posture, and critical workloads.
    Loaded from config at startup and shared across all evaluations.
    """
    org_name: str = "Contoso"
    resource_count: int = 0                    # Total resources under management
    compliance_frameworks: list[str] = []      # e.g. ["HIPAA", "PCI-DSS", "SOC2"]
    risk_tolerance: str = "moderate"           # "conservative" | "moderate" | "aggressive"
    business_critical_rgs: list[str] = []      # Resource groups with P0 workloads


# ============================================
# LLM Governance Output Models (Phase 22)
# ============================================

class GovernanceAdjustment(BaseModel):
    """A single score adjustment made by the LLM governance agent."""
    reason: str
    delta: float
    policy_id: Optional[str] = None


class LLMGovernanceOutput(BaseModel):
    """Structured output from an LLM governance agent."""
    adjusted_score: float
    adjustments: list[GovernanceAdjustment] = []
    reasoning: str
    confidence: float = 0.8


# ============================================
# Decision Explanation Models (Phase 17B)
# ============================================

class Factor(BaseModel):
    """One SRI™ dimension's contribution to the composite score."""
    dimension: str
    score: float
    weight: float
    weighted_contribution: float
    reasoning: str = ""


class Counterfactual(BaseModel):
    """A hypothetical change and its predicted effect on the verdict."""
    change_description: str
    predicted_new_score: float
    predicted_new_verdict: str
    explanation: str


class DecisionExplanation(BaseModel):
    """Full explainability report for a governance verdict."""
    summary: str
    primary_factor: str
    contributing_factors: list[Factor] = []
    policy_violations: list[str] = []
    risk_highlights: list[str] = []
    counterfactuals: list[Counterfactual] = []


# ============================================
# Execution Gateway Models (Phase 21)
# ============================================

class ExecutionStatus(str, Enum):
    """Lifecycle states for a verdict's execution path."""
    pending = "pending"                  # Verdict issued, not yet processed
    blocked = "blocked"                  # DENIED — no execution
    awaiting_review = "awaiting_review"  # ESCALATED — waiting for human
    pr_created = "pr_created"            # APPROVED + IaC-managed — PR opened
    pr_merged = "pr_merged"              # PR merged by human
    applied = "applied"                  # terraform apply succeeded
    manual_required = "manual_required"  # APPROVED but not IaC-managed
    dismissed = "dismissed"              # Human chose to skip
    failed = "failed"                    # PR creation or apply failed
    rolled_back = "rolled_back"          # Agent fix was applied then reversed


class ExecutionRecord(BaseModel):
    """Tracks the execution lifecycle of a governance verdict."""
    execution_id: str
    action_id: str                   # Links to GovernanceVerdict.action_id
    verdict: SRIVerdict
    status: ExecutionStatus
    iac_managed: bool = False        # True if resource has managed_by tag
    iac_tool: str = ""               # "terraform" | "bicep" | "" (unknown)
    iac_repo: str = ""               # e.g. "psc0des/ruriskry"
    iac_path: str = ""               # e.g. "infrastructure/terraform-prod"
    pr_url: str = ""                 # GitHub PR URL (when created)
    pr_number: int = 0               # GitHub PR number
    reviewed_by: str = ""            # Human who approved/dismissed
    created_at: datetime
    updated_at: datetime
    notes: str = ""                  # Human-added context
    verdict_snapshot: dict = {}      # GovernanceVerdict.model_dump() — used to reconstruct
                                     # the verdict when approving ESCALATED records for PR creation
    execution_plan: Optional[dict] = None   # Stored after plan phase — read by execute phase
    execution_log: Optional[list] = None    # Stored after execute phase — step-by-step audit
    verification: Optional[dict] = None    # Set after execute — {confirmed, message, checked_at}
    rollback_log: Optional[list] = None    # Stored after rollback — step-by-step audit


# ============================================
# Audit Trail Record
# ============================================

class AuditRecord(BaseModel):
    """Record stored in Cosmos DB for decision lineage tracking."""
    id: str
    partition_key: str
    timestamp: datetime
    action_summary: str
    skry_risk_index: SRIBreakdown
    sri_composite: float
    decision: SRIVerdict
    reason: str
    policy_violations: list[PolicyViolation] = []
    similar_incidents: list[str] = []
    full_evaluation: dict = {}
