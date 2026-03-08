# ProposedActionV2 Implementation Plan

## Purpose

This document converts the `ProposedActionV2` design into an implementation plan without changing the code yet.

The intent is to strengthen the contract between:

- Ops agents that investigate and propose changes
- Governance agents that review those proposals
- The deterministic policy layer that enforces safety rails
- The LLM review layer that reasons about context, intent, tradeoffs, and exceptions

The core problem this solves is architectural:

- today, too much meaning is inferred from free-form `reason`
- risky changes can be misclassified because the system is guessing intent from prose
- governance should review an explicit change artifact, not reverse-engineer one

---

## Target Outcome

We want a proposal model where risky actions are described as:

- current state
- desired state
- direction of change
- structured deltas
- evidence
- confidence
- assumptions / uncertainty

This allows governance agents to act like strong senior reviewers:

- verify the facts
- understand whether the change reduces or increases risk
- reason about policy applicability
- escalate when the proposal is incomplete or ambiguous

---

## Non-Goals

This plan does not propose:

- removing the current `ProposedAction` immediately
- rewriting the whole pipeline in one step
- making the LLM the sole source of truth
- modeling every Azure resource type on day one

This is an incremental strengthening of the proposal/review contract.

---

## Core Design Principles

1. `reason` remains for explanation, not primary governance logic.
2. Structured fields are the primary signal for risky action families.
3. Deterministic rules evaluate explicit facts and deltas.
4. LLM governance reviews intent, applicability, tradeoffs, and evidence quality.
5. Missing critical structure on risky actions must default to `ESCALATED`, not silent allowance.
6. Unknown real-world scenarios are handled by invariant-based design, not by enumerating hundreds of handcrafted cases.

---

## ProposedActionV2 Summary

`ProposedActionV2` should extend the current proposal contract with:

- `change_domain`
- `change_direction`
- `current_state_summary`
- `desired_state_summary`
- `deltas[]`
- `evidence[]`
- `confidence`
- `assumptions[]`
- `uncertainties[]`
- domain-specific structured sections:
  - `network`
  - `capacity`
  - `lifecycle`

For the first rollout, the highest-priority domain is:

- `network_security`

because that is where the architectural failure was first exposed.

---

## Implementation Strategy

### Phase 1: Extend the Existing Proposal Model

Goal:
- evolve the current `ProposedAction` contract without breaking the pipeline

Approach:
- add optional V2 fields to the existing `ProposedAction`
- keep all current fields in place
- keep backward compatibility for all current producers and consumers

Fields to add first:

- `change_domain`
- `change_direction`
- `current_state_summary`
- `desired_state_summary`
- `deltas`
- `evidence`
- `confidence`
- `assumptions`
- `uncertainties`

Why:
- avoids maintaining two proposal types
- allows gradual producer migration
- keeps dashboard and persistence changes smaller

Acceptance criteria:
- existing code paths still deserialize old proposals
- new proposals can include structured fields
- current tests keep passing

---

### Phase 2: Add Domain-Specific Structured Sections

Goal:
- support high-value change families with explicit shape

First domain-specific section:
- `network`

Recommended network fields:

- `rule_name`
- `protocol`
- `direction`
- `access`
- `source_before`
- `source_after`
- `destination_before`
- `destination_after`
- `ports_before`
- `ports_after`
- `exposure_before`
- `exposure_after`
- `risk_pattern`
- `intent`

Later domains:

- `capacity`
- `lifecycle`
- `identity`
- `storage_exposure`
- `configuration`

Acceptance criteria:
- `modify_nsg` proposals can represent open vs restrict without relying on prose
- the proposal carries enough data for deterministic policy checks

---

### Phase 3: Upgrade Ops Agents to Emit Structured Proposals

#### 3.1 DeployAgent first

Why first:
- it exposed the architecture flaw
- NSG changes are high-risk and easy to represent as structured deltas

Required behavior:
- when proposing `modify_nsg`, the agent should emit:
  - structured before/after state
  - change direction
  - risk pattern
  - evidence from actual NSG rules

Do not infer `restrict` from action type alone.

Instead:
- derive intent from discovered current state and proposed delta

Examples:
- `* -> corp-cidr` => `reduce_risk`
- `corp-cidr -> *` => `increase_risk`

#### 3.2 MonitoringAgent next

Why:
- scale and restart actions also need richer semantics

Structured data to emit:
- utilization metrics
- incident duration
- current vs proposed SKU
- change justification
- confidence and uncertainties

#### 3.3 CostOptimizationAgent next

Why:
- cost actions can be risky when they are destructive or reduce resilience

Structured data to emit:
- current cost
- projected savings
- utilization evidence
- whether action is destructive or reversible

Acceptance criteria:
- all three ops agents can emit richer proposal artifacts
- `reason` remains human-readable, but governance does not depend on it for primary classification

---

### Phase 4: Refactor Policy Agent to Prefer Structured Facts

Goal:
- move risky policy checks off prose and onto proposal semantics

Policy agent should:

- prefer structured fields whenever present
- use `reason` only as secondary human explanation
- treat missing required structured fields as ambiguity for risky domains

For network policies:

- dangerous-port policies should check:
  - `change_domain == network_security`
  - `network.intent`
  - `change_direction`
  - `exposure_before`
  - `exposure_after`
  - affected ports / risk pattern

This should support:

- opening dangerous exposure => strong policy hit
- restricting dangerous exposure => remediation path
- unclear proposal => escalate, not silently pass

Acceptance criteria:
- dangerous NSG policy no longer depends on matching words like `SSH` in prose
- remediation vs creation is derived from structured delta

---

### Phase 5: Refactor Other Governance Agents to Consume Structured Context

#### Blast Radius Agent

Should use:
- change domain
- deltas
- explicit resource relationships
- rollback / reversibility

Examples:
- delete shared resource => high risk
- restart non-critical stateless node => lower risk

#### Historical Agent

Should use:
- change family
- risk pattern
- before/after state
- intent

Examples:
- “opening internet exposure” should match exposure incidents
- “restricting exposure” should not be treated as equivalent to “creating exposure”

#### Financial Agent

Should use:
- cost delta
- confidence of estimate
- whether cost increase is due to remediation
- reversibility / optimization risk

Acceptance criteria:
- all governance agents review structured proposal semantics, not just human prose

---

### Phase 6: Tighten Governance Engine Rules Around Missing Structure

Goal:
- make the system safe for unknown real-world cases

Rules:

- risky action + missing structured intent => `ESCALATED`
- contradictory data => `ESCALATED`
- invalid delta semantics => `ESCALATED`
- unsupported high-risk domain => `ESCALATED`

This does not mean distrusting ops agents.

It means:
- if the proposal package is incomplete, governance requests review instead of pretending certainty

Acceptance criteria:
- missing structure cannot silently downgrade risk on dangerous actions

---

## Migration Strategy

### Migration Principle

Do not perform a flag day rewrite.

Migrate in layers:

1. Schema
2. Producer
3. Consumer
4. Guardrails
5. Dashboard / explainability

### Migration Phases

#### Migration Phase A: Additive schema

- add optional V2 fields
- old proposals continue to work
- no governance behavior change yet except where explicitly enabled

#### Migration Phase B: DeployAgent emits structured network proposals

- start with NSG changes only
- policy agent prefers V2 network fields for `modify_nsg`

#### Migration Phase C: Policy rules migrate off prose

- remove dependence on regex for risky network checks
- use structured exposure and direction fields

#### Migration Phase D: Other ops agents emit structured proposals

- scale actions
- delete actions
- restart/config actions

#### Migration Phase E: Governance agents consume richer proposal context

- blast radius
- historical
- financial

#### Migration Phase F: Escalate on missing structure for risky actions

- once producer coverage is sufficient, missing V2 fields on risky actions becomes a review trigger

---

## Test Strategy

This is the most important part of the architecture.

You cannot model every real-world cloud incident.

So the test strategy must focus on:

- invariants
- action families
- matrix coverage
- ambiguous / adversarial inputs

### 1. Invariant-Based Tests

Write tests that must hold across many scenarios.

#### Network invariants

- reducing exposure must not score riskier than increasing exposure
- `open -> internet` is riskier than `restrict -> corp_only`
- remediation of an existing dangerous state must not be treated as equivalent to creation of that state
- missing direction on risky network change must escalate

#### Lifecycle invariants

- `delete_resource` is riskier than `restart_service`
- deleting production is riskier than deleting non-production
- deleting shared resources is riskier than deleting isolated resources

#### Capacity invariants

- scaling down under high utilization must not score safer than scaling up under the same utilization
- scale-up for sustained saturation should not be denied purely for cost if evidence is strong

#### Configuration invariants

- closing public access is safer than enabling public access
- encryption enablement is safer than encryption disablement

### 2. Paired Tests

For each risky action family, add pairs:

- create bad state
- fix bad state
- ambiguous state

Examples:

- open SSH to `*`
- restrict SSH from `*` to corp CIDR
- change SSH rule without stating before/after

### 3. Matrix Tests

Build combinational coverage across:

- environment: prod / non-prod
- criticality: critical / standard
- direction: increase_risk / reduce_risk / unknown
- evidence quality: strong / weak / missing
- rollback: possible / unclear / impossible
- action family: network / lifecycle / capacity / config

You do not need hundreds of handcrafted stories.
You need matrix coverage over these dimensions.

### 4. Adversarial Tests

Add cases where prose is misleading:

- reason says “hardening” but delta broadens exposure
- reason says “emergency access” but before/after clearly opens the internet
- reason is vague, but structured fields show destructive change

Goal:
- governance should trust structured facts over narrative tone

### 5. Producer Contract Tests

For each ops agent:

- verify risky proposals include required structured fields
- verify derived direction is correct
- verify evidence is attached

This is important because the architecture only works if producers populate the contract correctly.

### 6. End-to-End Behavioral Tests

For major action families:

- proposal emitted by ops agent
- governance agents consume structured fields
- deterministic rules score baseline
- LLM adjustment path reasons over applicability
- governance engine produces final verdict

These should be mocked where necessary, but behavior should be asserted end-to-end.

### 7. Regression Corpus

Maintain a curated set of historically tricky proposals:

- remediation mistaken for creation
- low-risk changes described in scary language
- high-risk changes described in soft language
- incomplete proposals

Run this corpus on every major governance change.

---

## Rules for Missing or Ambiguous Structure

This is the safety model for real-world unknowns.

For risky action families:

- missing `change_direction` => `ESCALATED`
- missing before/after delta => `ESCALATED`
- missing evidence => `ESCALATED`
- contradictory fields => `ESCALATED`
- unsupported risky domain => `ESCALATED`

This is not distrust of ops agents.

This mirrors real review:
- an intelligent engineer can still submit an incomplete change request
- a good reviewer escalates incomplete change requests

---

## Domain Rollout Priority

### Priority 1: Network Security

Reason:
- highest architectural value
- proven failure already existed
- highly structured delta model is possible

### Priority 2: Resource Lifecycle

Reason:
- destructive changes need clear before/after semantics
- shared vs isolated deletion needs explicit evidence

### Priority 3: Capacity / Scaling

Reason:
- cost vs resilience tradeoffs need richer evidence

### Priority 4: Config / Access / Exposure

Reason:
- same structured-delta pattern applies broadly

---

## Acceptance Criteria for the Whole Migration

1. Risky policy decisions are no longer driven primarily by `reason` text.
2. Network proposals clearly distinguish remediation from creation.
3. Missing structure on risky changes does not silently pass.
4. Governance agents review structured proposal artifacts, not just narratives.
5. Ops agents emit enough evidence for governance to perform senior-review-style reasoning.
6. Test coverage is based on invariants and matrices, not only a list of handcrafted incidents.
7. The architecture remains backward-compatible during migration.

---

## Recommended First Concrete Slice

If implementation starts tomorrow, the best first slice is:

1. extend `ProposedAction`
2. fully structure `modify_nsg`
3. update `DeployAgent` to emit true NSG deltas
4. update `PolicyComplianceAgent` to prefer structured NSG fields
5. add invariant + paired tests for:
   - open SSH to internet
   - restrict SSH from internet
   - ambiguous NSG change

This gives the highest confidence gain with the smallest blast radius.

---

## Final Position

This is not a drift away from the current architecture.

It is the next necessary maturation step:

- from intelligent agents with weak proposal contracts
- to intelligent agents reviewed through strong, explicit change artifacts

That is how this system becomes robust against the hundreds of real-world cloud cases you will never be able to handcraft one by one.
