# Setup Guide

## Prerequisites

- Python 3.11+
- Node.js 18+ (for dashboard)
- Azure CLI (`az login` configured)
- Terraform 1.5+
- Azure subscription with credits/quota

## Infrastructure Is Terraform-Managed

Terraform in `infrastructure/terraform/` deploys:

1. Azure Resource Group
2. Azure AI Foundry account (`azurerm_ai_services`)
3. Foundry model deployment (`azurerm_cognitive_deployment`, default `gpt-4.1`)
4. Azure AI Search
5. Azure Cosmos DB (SQL API)
6. Azure Key Vault
7. Azure Log Analytics

Note:
- Foundry project/agent objects are not reliably Terraform-manageable yet for this account path.
- Keep `create_foundry_project=false` and create agents in the Foundry portal.
- Runtime secrets are read from Key Vault by default via `DefaultAzureCredential`.
  In Azure, use Managed Identity. Locally, `az login` is used by the same credential chain.

## Quick Setup

```bash
# 1. Clone
git clone https://github.com/<your-username>/sentinellayer.git
cd sentinellayer

# 2. Python environment
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# 3. Deploy Azure infrastructure
cd infrastructure/terraform
cp terraform.tfvars.example terraform.tfvars
# Edit terraform.tfvars: subscription_id + unique suffix
terraform init
terraform apply -input=false
cd ../..

# 4. Generate .env from Terraform outputs (Key Vault + Managed Identity mode)
bash scripts/setup_env.sh

# Optional local fallback (writes plaintext keys into .env)
# bash scripts/setup_env.sh --include-keys

# 5. Seed demo data
python scripts/seed_data.py

# 6. Run tests
pytest tests/ -v

# 7. Start SentinelLayer
python -m src.mcp_server.server
```
