# RuriSkry Enterprise Architecture Plan

> **Purpose:** This document designs the architectural evolution from the current
> 5-resource proof-of-concept to an enterprise-grade governance engine capable of
> handling 500+ Azure resources across multiple subscriptions, with real compliance
> obligations and auditor-facing evidence trails.
>
> This is not a feature list. It is a structural blueprint for how every component
> in the system must change to work at enterprise scale without losing correctness,
> auditability, or response time guarantees.

---

## 1. The Core Problem

The current system processes every proposed action through the same pipeline:

```
ProposedAction → [4 LLM governance agents in parallel] → SRI composite → verdict
```

This works at 5 resources / 3 proposals. At 500 resources / 80 proposals per scan
cycle, it means **640 LLM calls per scan**. At 48 scans/day, that's **30,000+ LLM
calls daily**. The architecture doesn't degrade gracefully — it hits rate limits,
exceeds budgets, and produces inconsistent results under load.

But the LLM count is a symptom. The real problems are:

1. **No risk triage** — a routine tag compliance check consumes the same resources
   as a critical NSG port exposure evaluation
2. **No decision memory** — the same action type on the same resource type in the
   same environment is re-evaluated from scratch every time
3. **No organizational context** — governance decisions are made in a vacuum; the
   system doesn't know org structure, compliance frameworks, or operational priorities
4. **Flat policy model** — 11 policies with no hierarchy, no scoping, no inheritance
5. **Single-subscription assumption** — the pipeline, the resource graph, the
   scan triggers all assume one subscription

---

## 2. Architecture Layers (Enterprise)

```
┌─────────────────────────────────────────────────────────────────────┐
│                        LAYER 5: OBSERVABILITY                       │
│  Governance metrics · LLM cost tracking · Decision audit stream     │
├─────────────────────────────────────────────────────────────────────┤
│                     LAYER 4: DECISION MEMORY                        │
│  Verdict cache · Pattern library · Decision precedent search        │
├─────────────────────────────────────────────────────────────────────┤
│                LAYER 3: GOVERNANCE EVALUATION                       │
│  Tier 1 (deterministic) · Tier 2 (LLM-light) · Tier 3 (LLM-full)  │
├─────────────────────────────────────────────────────────────────────┤
│                   LAYER 2: RISK TRIAGE                              │
│  Risk classifier · Action fingerprinting · Routing engine           │
├─────────────────────────────────────────────────────────────────────┤
│              LAYER 1: MULTI-SCOPE DISCOVERY                         │
│  Multi-sub scanner · Resource graph federation · Change stream      │
└─────────────────────────────────────────────────────────────────────┘
```

Each layer is described below with specific design decisions, failure modes,
and migration path from the current codebase.

---

## 3. Layer 1 — Multi-Scope Discovery

### Current state
- `RuriSkryPipeline.__init__` loads `seed_resources.json` once
- Three operational agents each scan independently via `asyncio.gather()`
- Each agent creates its own `AsyncAzureOpenAI` client per scan
- Single subscription ID in config

### Enterprise design

**3.1 Subscription registry** (`src/config.py` → `src/core/subscription_registry.py`)

```python
class SubscriptionScope(BaseModel):
    subscription_id: str
    display_name: str
    environment: str              # "production" | "staging" | "development"
    compliance_frameworks: list[str]  # ["pci-dss", "soc2", "hipaa"]
    resource_groups: list[str] | None  # None = scan all
    scan_priority: int            # 1 = highest; determines scan frequency
    owner_team: str               # "platform-eng" | "security" | "finops"
```

The system no longer has a single `azure_subscription_id`. It has a registry of
scopes, each with compliance metadata that propagates into governance decisions.

**3.2 Change-driven scanning** (replaces polling)

Instead of "run all 3 agents every N minutes and hope they find something":

```
Azure Activity Log → Event Grid subscription → /api/change-event
    → classify change type (NSG? SKU? Tag? Config?)
    → route to appropriate operational agent
    → evaluate only the changed resource, not the entire subscription
```

This replaces the current "scan 500 resources hoping to find 3 problems" with
"a resource changed — evaluate that one change." Reduces discovery LLM calls
from O(N resources) to O(N changes).

**3.3 Resource context service** (`src/core/resource_context.py`)

A dedicated service that maintains a continuously-updated view of the resource
landscape. Populated by:
- Azure Resource Graph queries (periodic full sync)
- Change events (incremental updates)
- Manual enrichment (org-specific metadata not in Azure tags)

This replaces the current `_load_resource_graph() → seed_resources.json` pattern.
Every governance agent queries this service instead of hitting Azure directly
during evaluation.

---

## 4. Layer 2 — Risk Triage

This is the most important new component. It sits between discovery and evaluation,
and its job is to determine **how much governance attention** a proposed action needs.

### 4.1 Action fingerprint

Every `ProposedAction` gets a fingerprint:

```python
class ActionFingerprint(BaseModel):
    """Deterministic classification of a proposed action's risk profile."""
    action_type: ActionType
    resource_type: str            # ARM type, normalized
    environment: str              # from resource tags or scope
    compliance_scope: list[str]   # ["pci-dss"] if resource is in PCI scope
    has_network_exposure: bool    # NSG-related, public IP, etc.
    has_data_plane_impact: bool   # touches data (storage, DB, etc.)
    is_production: bool
    is_critical_resource: bool    # criticality=critical tag
    estimated_blast_radius: str   # "isolated" | "service" | "platform"
    change_reversibility: str     # "immediate" | "manual" | "destructive"
```

The fingerprint is computed **without any LLM call** — it uses resource metadata,
tags, and ARM type classification. This takes <1ms per action.

### 4.2 Triage tiers

Based on the fingerprint, each action is routed to one of three evaluation tiers:

```
┌──────────────────────────────────────────────────────────────────┐
│ TIER 1 — DETERMINISTIC ONLY                                      │
│ • Non-production, non-critical, reversible changes               │
│ • Tag-only changes (no infrastructure impact)                    │
│ • Actions matching a previously-approved pattern                 │
│ • Expected: ~50-60% of all actions                               │
│ • LLM calls: 0                                                   │
│ • Latency: <100ms                                                │
├──────────────────────────────────────────────────────────────────┤
│ TIER 2 — DETERMINISTIC + SINGLE LLM REVIEW                      │
│ • Production changes with moderate blast radius                  │
│ • Cost changes under threshold                                   │
│ • Non-network, non-data-plane changes in compliance scope        │
│ • Expected: ~30-35% of all actions                               │
│ • LLM calls: 1 (single consolidated governance call)             │
│ • Latency: <5s                                                   │
├──────────────────────────────────────────────────────────────────┤
│ TIER 3 — FULL 4-AGENT EVALUATION (current pipeline)              │
│ • Network exposure changes (NSG, firewall, public IP)            │
│ • Destructive actions on critical/production resources           │
│ • Actions in PCI/SOC2/HIPAA compliance scope                     │
│ • Novel action patterns (no precedent in decision memory)        │
│ • Expected: ~10-15% of all actions                               │
│ • LLM calls: 8 (current full pipeline)                           │
│ • Latency: <30s                                                  │
└──────────────────────────────────────────────────────────────────┘
```

**Why this matters for LLM costs:**

| Scale | Without triage | With triage |
|---|---|---|
| 80 proposals/scan | 640 LLM calls | ~80 LLM calls |
| 48 scans/day | 30,720 calls/day | ~3,840 calls/day |
| Cost (GPT-4.1) | ~$150/day | ~$19/day |

### 4.3 Tier routing logic

```python
def classify_tier(fp: ActionFingerprint, memory: DecisionMemory) -> int:
    """Deterministic tier classification — no LLM needed."""

    # Always Tier 3: network exposure + compliance scope
    if fp.has_network_exposure and fp.compliance_scope:
        return 3

    # Always Tier 3: destructive + production + critical
    if fp.change_reversibility == "destructive" and fp.is_production and fp.is_critical_resource:
        return 3

    # Always Tier 3: novel pattern (no precedent)
    if not memory.has_precedent(fp):
        return 3

    # Tier 1: non-production, isolated, reversible
    if not fp.is_production and fp.estimated_blast_radius == "isolated":
        return 1

    # Tier 1: matches previously-approved pattern with high confidence
    if memory.get_precedent_confidence(fp) > 0.95:
        return 1

    # Everything else: Tier 2
    return 2
```

### 4.4 Tier 2 — Consolidated LLM call

Instead of 4 separate governance agents, Tier 2 makes **one LLM call** with a
consolidated prompt that covers all four dimensions:

```python
# Instead of 4 agents × 2 tool calls = 8 LLM calls:
prompt = f"""
Evaluate this action across all governance dimensions:

ACTION: {action.model_dump_json()}
RESOURCE CONTEXT: {resource_context}
ORGANIZATIONAL CONTEXT: {org_context}

Deterministic baselines (pre-computed):
- Policy score: {policy_baseline} (violations: {violations})
- Blast radius score: {blast_baseline}
- Historical score: {historical_baseline}
- Financial score: {financial_baseline}

For each dimension, confirm or adjust the baseline (max ±30pts) with justification.
Return a single JSON with all four adjusted scores.
"""
```

This gives you 87.5% LLM cost reduction for Tier 2 actions while preserving the
LLM's ability to catch contextual issues.

---

## 5. Layer 3 — Governance Evaluation (Evolved)

### 5.1 Policy engine evolution

The current `data/policies.json` is flat — 11 policies, each evaluated independently.
At enterprise scale, policies need:

**Policy hierarchy:**
```json
{
  "id": "POL-SEC-002",
  "parent": "POL-SEC-001",
  "scope": {
    "subscriptions": ["prod-*"],
    "resource_groups": ["rg-payments-*"],
    "compliance_frameworks": ["pci-dss"]
  },
  "inherits_from": "POL-SEC-001",
  "overrides": {
    "severity": "critical",
    "conditions": {
      "blocked_actions": ["modify_nsg", "delete_resource", "update_config"]
    }
  }
}
```

**Scoped policies:** A policy like "no port 22 open" applies universally, but
"no configuration changes without CAB approval" might only apply to PCI-scoped
resources. Scope determines which policies are evaluated, reducing false positives.

**Policy categories with routing:**
```
security     → always evaluated (all tiers)
compliance   → evaluated when resource is in compliance scope
financial    → evaluated when cost impact > threshold
operational  → evaluated for production resources only
governance   → always evaluated (tag compliance, ownership)
```

### 5.2 Blast radius without full topology

Current blast radius agent queries Azure Resource Graph to find dependencies.
At enterprise scale with 500 resources, this is N expensive queries.

**Enterprise approach — pre-computed blast radius tiers:**

```python
BLAST_RADIUS_TIERS = {
    # ARM resource type → default blast radius tier
    "Microsoft.Network/networkSecurityGroups": "platform",    # affects all resources behind it
    "Microsoft.Network/virtualNetworks": "platform",
    "Microsoft.Compute/virtualMachines": "service",
    "Microsoft.Storage/storageAccounts": "service",           # shared dependency
    "Microsoft.Web/sites": "isolated",                        # single app
    "Microsoft.Sql/servers/databases": "service",
}
```

The resource context service (Layer 1) enriches each resource with:
- Number of dependent resources (from Resource Graph, cached)
- Service tier ("isolated", "service", "platform")
- Whether it's a shared dependency (referenced by >1 other resource)

This gives the blast radius agent a meaningful estimate without querying Azure
during evaluation. The full topology query is reserved for Tier 3 actions only.

### 5.3 Historical pattern matching at scale

Current: 7 seeded incidents, BM25 search.

**Enterprise approach:**

```
Tier 1: industry-standard risk patterns (CIS benchmarks, Azure Well-Architected
         anti-patterns, common CVE patterns) — pre-loaded, no search needed
         → "opening port 22 to 0.0.0.0/0 is always risky"

Tier 2: organizational incident history — vector search over all past incidents
         and governance decisions from your own Cosmos DB history
         → "last time we scaled down this service, there was a capacity incident"

Tier 3: cross-organization pattern matching — (future) anonymized patterns from
         other organizations using the same Azure services
```

The current `data/seed_incidents.json` with 7 entries becomes:
- A **risk pattern library** (hundreds of pre-defined patterns, no LLM needed)
- Plus your actual **organizational decision history** (grows over time from
  Cosmos DB audit trail)

### 5.4 Financial impact at scale

Current: queries Azure Retail Prices API per resource.

**Enterprise approach:**
- Resource context service caches monthly cost for every discovered resource
- Financial agent evaluates cost **delta**, not absolute cost
- Org-level budget context: "this resource group is at 87% of quarterly budget"
- Cost aggregation: "this action saves $200/month, but the last 5 similar actions
  saved $12,000/month total — is over-optimization risk emerging?"

This data comes from `config.py` or a dedicated budget service, not from
per-evaluation Azure API calls.

---

## 6. Layer 4 — Decision Memory

This is the most impactful new capability. Every governance decision is stored as
a **precedent** that future evaluations can reference.

### 6.1 Decision precedent model

```python
class DecisionPrecedent(BaseModel):
    """A governance decision that can inform future evaluations."""
    fingerprint: ActionFingerprint    # what was the action?
    verdict: SRIVerdict               # what was decided?
    sri_breakdown: SRIBreakdown       # what were the scores?
    confidence: float                 # how confident was the system?
    human_reviewed: bool              # was a human involved?
    human_agreed: bool | None         # did the human agree with the system?
    created_at: datetime
    expires_at: datetime | None       # precedents can expire
    invalidated: bool = False         # human can mark as "don't use this again"
```

### 6.2 Precedent matching

When a new action arrives, before any LLM call:

```python
async def check_precedent(fp: ActionFingerprint) -> PrecedentMatch | None:
    """Find a matching precedent in decision memory."""
    # Exact match: same action_type + resource_type + environment
    exact = await memory.find_exact(fp)
    if exact and exact.confidence > 0.9 and exact.human_reviewed:
        return PrecedentMatch(precedent=exact, confidence=0.95, source="exact")

    # Pattern match: same action_type + resource_type, different resource
    pattern = await memory.find_pattern(fp)
    if pattern and len(pattern) >= 3:  # 3+ consistent decisions = pattern
        return PrecedentMatch(precedent=pattern[0], confidence=0.85, source="pattern")

    return None  # no precedent → Tier 3 evaluation
```

### 6.3 Precedent lifecycle

```
New action (no precedent) → Tier 3 full evaluation → store as precedent
                                                         ↓
Same action pattern later → Tier 1 (use precedent)   ← ─┘
                                                         ↓
Human overrides verdict   → invalidate precedent → Tier 3 next time
```

This creates a **learning loop**: the system starts at Tier 3 for everything
(current behavior), but progressively moves known-good patterns to Tier 1 as
humans validate decisions. Over months, 60-70% of actions resolve at Tier 1
with zero LLM calls.

---

## 7. Layer 5 — Observability

### 7.1 Governance metrics (new dashboard page)

```
LLM Usage:
├── Calls today: 2,847 / 30,000 budget
├── Tier distribution: T1: 67% · T2: 24% · T3: 9%
├── Avg latency: T1: 42ms · T2: 3.2s · T3: 18.4s
├── Token usage: 1.2M input / 340K output
└── Estimated daily cost: $14.20

Decision Quality:
├── Human override rate: 4.2% (good: <10%)
├── Precedent hit rate: 67% (growing)
├── False positive rate: 2.1% (DENIED actions that humans approved)
└── False negative rate: 0.0% (APPROVED actions that caused incidents)

Policy Coverage:
├── Total policies: 47
├── Policies fired today: 23
├── Actions evaluated: 312
└── Compliance frameworks covered: PCI-DSS (100%), SOC2 (94%)
```

### 7.2 Decision audit stream

Every governance decision emits a structured event:

```python
class GovernanceAuditEvent(BaseModel):
    event_id: str
    timestamp: datetime
    action_fingerprint: ActionFingerprint
    tier: int                          # 1, 2, or 3
    precedent_used: str | None         # precedent ID if Tier 1
    llm_calls_made: int
    total_latency_ms: int
    token_usage: dict                  # {"input": N, "output": N}
    verdict: SRIVerdict
    sri_breakdown: SRIBreakdown
    human_review_required: bool
    human_verdict: SRIVerdict | None   # filled later if reviewed
```

This feeds into Azure Monitor / Log Analytics for operational dashboards and
into Cosmos DB for the decision memory system.

---

## 8. Model Strategy

### 8.1 Tiered model routing

```
Tier 1: No LLM (deterministic + precedent)
Tier 2: GPT-4.1-mini or GPT-5-mini (single consolidated call)
Tier 3: GPT-4.1 or GPT-5 (full reasoning, multi-tool)
```

### 8.2 Why not one model for everything?

| Concern | Single model | Tiered |
|---|---|---|
| Cost at 500 resources | ~$150/day | ~$19/day |
| Latency for routine changes | 15-30s | <100ms |
| Quota pressure | constant 429s | stays under limits |
| Reasoning quality for critical decisions | same model diluted by volume | best model reserved for hard cases |

### 8.3 Model selection criteria

The model used for Tier 2 and Tier 3 should be selected based on:
- **Structured output reliability**: must produce valid JSON for `parse_llm_decision()`
- **Tool-calling accuracy**: must call the right tools in the right order
- **Reasoning about intent**: must distinguish remediation from creation
- **Cost per token**: matters at Tier 2 volume, less so at Tier 3

Evaluate candidates by running the same 10 governance scenarios through each
model and comparing:
1. Did it produce valid structured output? (binary)
2. Did it correctly identify remediation intent? (binary)
3. Did the adjusted score match a human expert's judgment? (±5 points)
4. Cost per evaluation

---

## 9. Organizational Context

### 9.1 What's missing today

The LLM makes governance decisions without knowing:
- How many resources depend on this one (beyond what Resource Graph shows)
- What compliance frameworks apply to this resource group
- What the org's risk tolerance is
- Who owns this resource and whether they've been notified
- Whether this change is part of a larger planned maintenance window
- What the business impact of downtime would be

### 9.2 Organizational context model

```python
class OrgContext(BaseModel):
    """Injected into every governance evaluation prompt."""
    org_name: str
    total_resource_count: int
    compliance_frameworks: list[str]
    risk_tolerance: str  # "conservative" | "moderate" | "aggressive"
    change_advisory_board_required_for: list[str]  # ["production", "pci-scope"]
    business_critical_resource_groups: list[str]
    planned_maintenance_windows: list[dict]  # current active windows
    escalation_contacts: dict[str, str]  # severity → team/person
```

This is loaded from a configuration file or API at startup, not from Azure.
It represents **organizational knowledge that Azure doesn't have** — things
like "we're in a SOC2 audit this month, so be extra conservative" or
"the payments team is doing planned maintenance on rg-payments tonight."

### 9.3 How it flows into decisions

```python
# In the governance agent prompt:
f"""
ORGANIZATIONAL CONTEXT:
- {org.org_name} operates {org.total_resource_count} resources
- Compliance: {', '.join(org.compliance_frameworks)}
- Risk tolerance: {org.risk_tolerance}
- This resource group is {'business-critical' if rg in org.business_critical_resource_groups else 'standard'}
- {'ACTIVE MAINTENANCE WINDOW' if in_maintenance else 'No active maintenance'}

Given this context, evaluate the following action...
"""
```

---

## 10. Migration Path

### Phase 26 — Foundation (estimated: 1-2 sessions)
- [ ] Add `ActionFingerprint` model to `models.py`
- [ ] Build `classify_tier()` function in new `src/core/risk_triage.py`
- [ ] Wire triage into `pipeline.evaluate()` — log tier but still run Tier 3 for all
- [ ] Add `OrgContext` model and config loading
- [ ] Add triage metrics to `/api/metrics` response

### Phase 27 — Decision Memory (estimated: 2-3 sessions)
- [ ] Add `DecisionPrecedent` model
- [ ] Build `DecisionMemory` service (Cosmos DB backed)
- [ ] Wire precedent check into pipeline — Tier 1 actions skip LLM
- [ ] Add precedent management endpoints (invalidate, expire)
- [ ] Dashboard: precedent hit rate metrics

### Phase 28 — Tier 2 Consolidated Evaluation (estimated: 1-2 sessions)
- [ ] Build `ConsolidatedGovernanceAgent` — single LLM call covering all 4 dimensions
- [ ] Wire into pipeline for Tier 2 actions
- [ ] A/B test: compare consolidated vs full pipeline on same actions
- [ ] Validate structured output reliability

### Phase 29 — Policy Engine v2 (estimated: 2-3 sessions)
- [ ] Policy scoping (subscription, resource group, compliance framework)
- [ ] Policy hierarchy (inheritance, overrides)
- [ ] Policy categories with tier routing
- [ ] Expand from 11 to 40+ policies (CIS benchmarks, Well-Architected)

### Phase 30 — Multi-Subscription Support (estimated: 2-3 sessions)
- [ ] Subscription registry
- [ ] Change-driven scanning via Event Grid
- [ ] Resource context service with continuous sync
- [ ] Per-subscription scan scheduling

### Phase 31 — Observability (estimated: 1-2 sessions)
- [ ] Governance metrics dashboard page
- [ ] LLM cost tracking per tier
- [ ] Decision quality metrics (override rate, false positive rate)
- [ ] Structured audit event stream

---

## 11. What Does NOT Change

Some things in the current architecture are already enterprise-grade:

- **SRI weighted composite scoring** — the 4-dimension model is sound
- **Policy violation severity hierarchy** — CRITICAL/HIGH/MEDIUM/LOW with auto-DENY
- **LLM guardrails** — ±30pt clamp, Pydantic-validated output, `annotate_violations`
- **Execution Gateway** — IaC-safe Terraform PR flow
- **HITL flow** — human-in-the-loop approve/dismiss/escalate
- **Audit trail** — Cosmos DB with full verdict snapshots
- **Slack notifications** — real-time alerting on DENIED/ESCALATED

These are the foundation. The enterprise evolution builds on top of them.

---

## 12. Success Criteria

The enterprise architecture is working when:

1. **70% of actions resolve at Tier 1** (no LLM, <100ms) after 30 days of operation
2. **Human override rate < 5%** — the system agrees with human judgment 95%+ of the time
3. **Zero false negatives** — no APPROVED action ever causes a production incident
4. **LLM cost < $25/day** at 500 resources with 48 scan cycles
5. **Full audit trail** — every decision can be explained to an auditor with the
   specific policies, precedents, and reasoning that led to the verdict
6. **< 30s latency for Tier 3** — even the hardest decisions complete within SLA
7. **Multi-subscription** — governance covers all production subscriptions from one instance
