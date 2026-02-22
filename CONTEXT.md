# CONTEXT.md — SentinelLayer Project Context
> This file is the single source of truth for any AI coding agent working on this project.

## What Is This Project?
SentinelLayer is an AI Action Governance & Simulation Engine for the Microsoft AI Dev Days Hackathon 2026. It intercepts AI agent infrastructure actions, simulates their impact, and scores them using the Sentinel Risk Index (SRI™) before allowing execution.

## Project Structure
```
src/
├── operational_agents/     # Agents that PROPOSE actions (the governed)
│   ├── monitoring_agent.py # SRE monitoring + anomaly detection
│   └── cost_agent.py       # Cost optimization proposals
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
├── infrastructure/              # Azure service clients (mock for now)
│   ├── resource_graph.py
│   ├── cosmos_client.py
│   ├── search_client.py
│   └── openai_client.py
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
- Core logic is complete and tested with LOCAL MOCKS (data/ JSON files).
- Azure infrastructure is provisioned via Terraform (Foundry, Search, Cosmos, Key Vault, Log Analytics).
- LLM runtime is Foundry-only and Terraform-managed (`azurerm_ai_services` + `azurerm_cognitive_deployment`).
- Real-environment secret flow is Key Vault + `DefaultAzureCredential` (Managed Identity in Azure, `az login` locally).
  Set `USE_LOCAL_MOCKS=false`, `AZURE_KEYVAULT_URL`, and secret-name vars in `.env` to enable live calls without plaintext keys.
- All agents still work without any cloud connection (mock mode is the default).

## Coding Standards
- Python 3.11+
- Use type hints everywhere
- Use Pydantic models from src/core/models.py for all inputs/outputs
- Use async/await pattern for agent methods
- Every agent must have an `async def evaluate(self, action: ProposedAction)` method
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
