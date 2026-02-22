# 🛡️ SentinelLayer — AI Action Governance & Simulation Engine

> **Because autonomous AI needs accountable AI.**

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Python 3.11+](https://img.shields.io/badge/python-3.11+-blue.svg)](https://www.python.org/downloads/)
[![Azure](https://img.shields.io/badge/cloud-Azure-0078D4.svg)](https://azure.microsoft.com)
[![AI Dev Days Hackathon 2026](https://img.shields.io/badge/hackathon-AI%20Dev%20Days%202026-purple.svg)](https://microsoft.com)

SentinelLayer intercepts, simulates, and scores every AI agent action **before** it touches your infrastructure. It sits between operational AI agents (SRE bots, cost optimizers, deployment agents) and Azure cloud resources, acting as a supervisory intelligence layer.

<p align="center">
  <img src="docs/architecture.png" alt="SentinelLayer Architecture" width="800">
</p>

---

## The Problem

AI agents are increasingly managing cloud infrastructure autonomously — scaling clusters, restarting services, deleting idle resources, modifying network rules. But capability without accountability is dangerous:

- A **cost optimization agent** deletes a disaster recovery VM to save $800/month — not knowing it just compromised a compliance requirement
- An **SRE agent** restarts a payment service — unaware that identical restarts caused cascade failures three times before
- A **deployment agent** opens a network port — accidentally exposing internal admin dashboards to the public internet

Today's tooling offers two options: **block actions with static rules** or **monitor after execution**. Nobody simulates outcomes before allowing an agent to act.

## The Solution

SentinelLayer is the missing governance layer. Before any agent action executes, it runs through four specialized simulation agents that produce a branded **Sentinel Risk Index (SRI™)**:

```
┌─────────────────────────────────────────────────────┐
│              SENTINEL RISK INDEX (SRI™)              │
│                                                     │
│   SRI:Infrastructure ████████████░░░░░░░░  32/100   │
│   SRI:Policy         ████████████████░░░░  40/100   │
│   SRI:Historical     ██████░░░░░░░░░░░░░░  15/100   │
│   SRI:Cost           ████░░░░░░░░░░░░░░░░  10/100   │
│                                          ─────────  │
│   SRI Composite                           72/100    │
│                                                     │
│   Verdict: ❌ DENIED                                │
│   Reason: Critical policy violation + high blast    │
│           radius on production dependency chain     │
└─────────────────────────────────────────────────────┘
```

### SRI™ Dimensions

| Dimension | What It Measures | Agent |
|-----------|-----------------|-------|
| **SRI:Infrastructure** | Blast radius — downstream resources and services affected | Blast Radius Simulation Agent |
| **SRI:Policy** | Governance compliance — policy violations and severity | Policy & Compliance Agent |
| **SRI:Historical** | Precedent risk — similarity to past incidents | Historical Pattern Agent |
| **SRI:Cost** | Financial volatility — projected cost change and over-optimization | Financial Impact Agent |

### Decision Thresholds

- **SRI ≤ 25** → ✅ Auto-Approve — low risk, execute immediately
- **SRI 26–60** → ⚠️ Escalate — moderate risk, human review required
- **SRI > 60** → ❌ Deny — high risk, action blocked with explanation
- **Critical policy violation** → ❌ Deny — regardless of composite score

---

## Architecture

```
┌──────────────────────────────────────────────────────────┐
│                  OPERATIONAL AGENTS                       │
│     (SRE Agent)      (Cost Agent)      (Deploy Agent)    │
│         │                 │                 │            │
│         └─────────────────┼─────────────────┘            │
│                           │                              │
│                    Proposed Action                        │
│                    (via Azure MCP)                        │
└───────────────────────────┬──────────────────────────────┘
                            │
                            ▼
┌──────────────────────────────────────────────────────────┐
│                  SENTINELLAYER CORE                       │
│                                                          │
│  ┌──────────────┐                                        │
│  │ Interception  │◄── Captures action proposals via MCP  │
│  │    Engine     │                                        │
│  └──────┬───────┘                                        │
│         │                                                │
│         ▼                                                │
│  ┌────────────────────────────────────────────┐          │
│  │         SRI™ SIMULATION AGENTS              │          │
│  │                                             │          │
│  │  ┌─────────────────┐  ┌─────────────────┐  │          │
│  │  │ SRI:Infra        │  │ SRI:Policy      │  │          │
│  │  │ Blast Radius     │  │ Compliance      │  │          │
│  │  └─────────────────┘  └─────────────────┘  │          │
│  │  ┌─────────────────┐  ┌─────────────────┐  │          │
│  │  │ SRI:Historical   │  │ SRI:Cost        │  │          │
│  │  │ Pattern Match    │  │ Financial       │  │          │
│  │  └─────────────────┘  └─────────────────┘  │          │
│  └──────────────────┬─────────────────────────┘          │
│                     │                                    │
│                     ▼                                    │
│  ┌────────────────────────────────────────────┐          │
│  │       GOVERNANCE DECISION ENGINE            │          │
│  │  SRI™ Composite → APPROVE / ESCALATE / DENY│          │
│  └──────────────────┬─────────────────────────┘          │
│                     │                                    │
│  ┌────────────────────────────────────────────┐          │
│  │       DECISION LINEAGE TRACKER              │          │
│  │  Immutable audit trail → Cosmos DB          │          │
│  └────────────────────────────────────────────┘          │
└──────────────────────────────────────────────────────────┘
```

---

## Technology Stack

| Component | Technology | Purpose |
|-----------|-----------|---------|
| Agent Orchestration | Microsoft Agent Framework | Multi-agent coordination |
| Model Intelligence | Microsoft Foundry + Model Router | Cost-optimized model routing |
| Cloud Interception | Azure MCP (consumer + provider) | Intercept actions, query Azure |
| Infrastructure Graph | Azure Resource Graph | Real-time resource dependencies |
| LLM Reasoning | Microsoft Foundry — GPT-4.1 | Simulation reasoning |
| Vector Search | Azure AI Search | Incident history similarity |
| Graph + Audit DB | Cosmos DB (Gremlin + SQL API) | Dependencies + decision trail |
| Serverless Compute | Azure Functions | Event processing |
| Code Analysis | GitHub Copilot Agent Mode | IaC PR governance |
| Dashboard | React + Azure Static Web Apps | Governance visualization |

---

## Quick Start

### Prerequisites

- Python 3.11+
- Azure subscription (Terraform deploys Foundry, Search, Cosmos DB, Key Vault, and Log Analytics)
- Azure CLI (`az login` completed)
- Terraform 1.5+
- Node.js 18+ (for dashboard)

### Setup

```bash
# Clone the repository
git clone https://github.com/<your-username>/sentinellayer.git
cd sentinellayer

# Create virtual environment
python -m venv .venv
source .venv/bin/activate  # Linux/Mac
# .venv\Scripts\activate   # Windows

# Install dependencies
pip install -r requirements.txt

# Provision Azure infrastructure (Foundry-only)
cd infrastructure/terraform
cp terraform.tfvars.example terraform.tfvars
# Edit terraform.tfvars with subscription_id and unique suffix
terraform init
terraform apply -input=false
cd ../..

# Generate .env from Terraform outputs (Key Vault + Managed Identity mode)
bash scripts/setup_env.sh
# For local fallback with plaintext keys in .env:
# bash scripts/setup_env.sh --include-keys
# For CI/non-interactive mode:
# bash scripts/setup_env.sh --no-prompt

# Seed demo data
python scripts/seed_data.py

# Run SentinelLayer MCP server
python -m src.mcp_server.server

# Run operational agents (in separate terminal)
python -m src.operational_agents.run

# Run dashboard (in separate terminal)
cd dashboard
npm install
npm run dev
```

### Run Tests

```bash
pytest tests/ -v --cov=src --cov-report=term-missing
```

---

## Project Structure

```
sentinellayer/
├── src/
│   ├── operational_agents/     # The governed — SRE & cost agents
│   │   ├── monitoring_agent.py
│   │   └── cost_agent.py
│   ├── governance_agents/      # The governors — SRI™ dimension agents
│   │   ├── blast_radius_agent.py    # SRI:Infrastructure
│   │   ├── policy_agent.py          # SRI:Policy
│   │   ├── historical_agent.py      # SRI:Historical
│   │   └── financial_agent.py       # SRI:Cost
│   ├── core/                   # Decision engine & tracking
│   │   ├── governance_engine.py     # SRI™ scoring + verdicts
│   │   ├── decision_tracker.py      # Cosmos DB audit trail
│   │   ├── interception.py          # MCP action interception
│   │   └── models.py               # Pydantic data models
│   ├── mcp_server/             # SentinelLayer as MCP provider
│   │   └── server.py
│   ├── infrastructure/         # Azure service clients
│   │   ├── resource_graph.py
│   │   ├── cosmos_client.py
│   │   ├── search_client.py
│   │   └── openai_client.py
│   └── api/                    # Dashboard REST endpoints
│       └── dashboard_api.py
├── dashboard/                  # React governance dashboard
├── functions/                  # Azure Functions triggers
├── data/                       # Seed data for demo
│   ├── seed_incidents.json
│   ├── seed_resources.json
│   └── policies.json
├── tests/
├── docs/
└── scripts/
```

---

## Demo Scenarios

### Scenario A: Dangerous Action → DENIED
**Cost Agent** proposes deleting VM-23 (idle for 30 days, $847/mo).
SentinelLayer discovers VM-23 is tagged `disaster-recovery`, has 3 dependent services, and a similar deletion caused INC-2025-1204 ($50K damage).
**SRI™: 72 → ❌ DENIED**

### Scenario B: Safe Action → AUTO-APPROVED
**SRE Agent** proposes scaling web-tier from D4 to D8 during traffic spike.
SentinelLayer finds no dependencies affected, no policy violations, similar scale-ups succeeded before.
**SRI™: 7 → ✅ AUTO-APPROVED**

### Scenario C: Moderate Risk → ESCALATED
**Deploy Agent** proposes NSG rule change to open port 8080.
SentinelLayer finds the NSG governs multiple subnets; security policy requires review.
**SRI™: 45 → ⚠️ ESCALATED for human review**

---

## Hackathon

**Event**: Microsoft AI Dev Days Hackathon 2026
**Challenge**: Automate and Optimize Software Delivery — Leverage Agentic DevOps Principles
**Timeline**: February 10 – March 15, 2026

---

## License

This project is licensed under the MIT License — see the [LICENSE](LICENSE) file for details.

---

<p align="center">
  <b>SentinelLayer: Because autonomous AI needs accountable AI. 🛡️</b>
</p>
