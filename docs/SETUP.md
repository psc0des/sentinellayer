# Setup Guide

## Prerequisites

- Python 3.11+
- Node.js 18+ (for dashboard)
- Azure CLI (`az login` configured)
- Azure subscription with credits

## Azure Services to Deploy

1. **Azure AI Foundry** — Deploy a model (GPT-4o-mini, Llama, Mistral, Phi, etc.)
2. **Azure AI Search** — Create search service (Basic tier for hackathon)
3. **Azure Cosmos DB** — Create account with both SQL and Gremlin APIs
4. **Azure Monitor** — Log Analytics workspace
5. **Azure Key Vault** — Store secrets
6. **Azure Static Web Apps** — Host dashboard (Week 4)
7. **Azure Functions** — Serverless compute (Week 4)

## Quick Setup

```bash
# 1. Clone and configure
git clone https://github.com/<your-username>/sentinellayer.git
cd sentinellayer
cp .env.example .env
# Fill in .env with your Azure credentials

# 2. Python environment
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# 3. Seed demo data
python scripts/seed_data.py

# 4. Run tests
pytest tests/ -v

# 5. Start SentinelLayer
python -m src.mcp_server.server
```

## Azure Resource Deployment

See `scripts/deploy_azure.sh` for automated Azure resource deployment.
