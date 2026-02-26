# CONTEXT.md — SentinelLayer Project Context
> This file is the single source of truth for any AI coding agent working on this project.

## What Is This Project?
SentinelLayer is an AI Action Governance & Simulation Engine for the Microsoft AI Dev Days Hackathon 2026. It intercepts AI agent infrastructure actions, simulates their impact, and scores them using the Sentinel Risk Index (SRI™) before allowing execution.

## Project Structure
```
src/
├── operational_agents/     # Agents that PROPOSE actions (the governed)
│   ├── monitoring_agent.py # SRE monitoring + anomaly detection
│   ├── cost_agent.py       # Cost optimization proposals
│   └── deploy_agent.py     # Infrastructure deployment + config proposals (Phase 8)
├── governance_agents/      # Agents that EVALUATE actions (the governors)
│   ├── blast_radius_agent.py    # SRI:Infrastructure (0-100)
│   ├── policy_agent.py          # SRI:Policy (0-100)
│   ├── historical_agent.py      # SRI:Historical (0-100)
│   └── financial_agent.py       # SRI:Cost (0-100)
├── core/
│   ├── governance_engine.py     # Calculates SRI™ Composite + verdict
│   ├── decision_tracker.py      # Audit trail storage
│   ├── interception.py          # MCP action interception
│   └── models.py                # ALL Pydantic data models (READ THIS FIRST)
├── mcp_server/
│   └── server.py                # Exposes governance tools via MCP
├── infrastructure/              # Azure service clients (live + mock fallback)
│   ├── resource_graph.py        # Azure Resource Graph (mock: seed_resources.json)
│   ├── cosmos_client.py         # Cosmos DB decisions (mock: data/decisions/*.json)
│   ├── search_client.py         # Azure AI Search incidents (mock: seed_incidents.json)
│   ├── openai_client.py         # Azure OpenAI / GPT-4.1 (mock: canned string)
│   └── secrets.py               # Key Vault secret resolver (env → KV → empty)
├── api/
│   └── dashboard_api.py         # FastAPI REST endpoints
└── config.py                    # Environment config with SRI thresholds
```

## Key Concepts

### Sentinel Risk Index (SRI™)
Every proposed action gets scored across 4 dimensions:
- **SRI:Infrastructure** (weight 0.30) — blast radius on dependent resources
- **SRI:Policy** (weight 0.25) — governance policy violations
- **SRI:Historical** (weight 0.25) — similarity to past incidents
- **SRI:Cost** (weight 0.20) — financial impact and volatility

### SRI™ Decision Thresholds
- SRI ≤ 25 → APPROVED (auto-execute)
- SRI 26-60 → ESCALATED (human review)
- SRI > 60 → DENIED (blocked)
- Any critical policy violation → DENIED regardless of score

### Data Flow
```
Operational Agent proposes action (ProposedAction)
    → SentinelLayer intercepts
    → 4 governance agents evaluate in parallel
    → GovernanceDecisionEngine calculates SRI™ Composite
    → GovernanceVerdict returned (approved/escalated/denied)
    → Decision logged to audit trail
```

## Important Files to Read First
1. `src/core/models.py` — ALL Pydantic models. Every agent uses these.
2. `src/config.py` — SRI thresholds and weights are configurable here.
3. `data/policies.json` — 6 governance policies for PolicyComplianceAgent.
4. `data/seed_incidents.json` — 7 past incidents for HistoricalPatternAgent.
5. `data/seed_resources.json` — Mock Azure resource topology with dependencies.

## Current Development Phase
> For detailed progress tracking see **STATUS.md** at the project root.

**Phase 8 — Microsoft Agent Framework SDK (current)**

- All 4 governance agents + all 3 operational agents rebuilt on `agent-framework-core`.
- Each agent defines its rule-based logic as an `@af.tool`; GPT-4.1 (the "brain") calls the tool
  and synthesises a human-readable reasoning narrative.
- Auth: `AzureCliCredential` + `get_bearer_token_provider` → `AsyncAzureOpenAI` (no API key in code).
  Responses API requires `api_version="2025-03-01-preview"`.
- `_use_framework = not use_local_mocks and bool(azure_openai_endpoint)` — mock mode skips the
  entire async/framework path; `except Exception` fallback also handles live-mode failures.
- New `src/operational_agents/deploy_agent.py`: proposes NSG rule updates, lifecycle tag additions,
  and observability resource creation.
- `pipeline.py` now exposes `scan_operational_agents()` which runs all 3 operational agents.
- `USE_LOCAL_MOCKS=false` is set in `.env` — live Azure services are the default.
- `DecisionTracker` delegates to `CosmosDecisionClient` — decisions persist to Cosmos DB in live mode.
- `HistoricalPatternAgent` uses `AzureSearchClient` (BM25) in live mode; keyword matching in mock mode.
- Mock mode still works with zero cloud connection — every client falls back gracefully.
- 361/388 tests pass; 27 pre-existing failures (CosmosDB `_dir` attribute, dashboard API) unrelated
  to Phase 8.

## Coding Standards
- Python 3.11+
- Use type hints everywhere
- Use Pydantic models from src/core/models.py for all inputs/outputs
- Governance agents use **synchronous** `evaluate()` (called from ThreadPoolExecutor in pipeline)
- Every governance agent must have a `def evaluate(self, action: ProposedAction)` method
- Return the appropriate Pydantic result model (e.g., PolicyResult, BlastRadiusResult)
- Keep code clean with docstrings
- Follow existing patterns in models.py

## Testing
- Tests go in tests/ directory
- Use pytest with pytest-asyncio
- Run tests: `pytest tests/ -v`
- Test each agent independently with mock ProposedAction data

## Git Commits
Use conventional commits:
- `feat(policy): implement PolicyComplianceAgent`
- `feat(engine): implement SRI Composite scoring`
- `test(policy): add unit tests for policy evaluation`
- `fix(blast-radius): handle missing dependencies gracefully`

## What NOT To Do
- Do NOT make tests depend on live Azure APIs (keep tests mock-first and deterministic)
- Do NOT install new dependencies without checking requirements.txt first
- Do NOT modify src/core/models.py without asking (it's the shared contract)
- Do NOT use print() for logging — use Python's logging module
