# Setup Guide

Detailed infra runbook: `infrastructure/deploy.md`

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
5. Azure Cosmos DB (SQL API) — two containers: `governance-decisions` (partition `/resource_id`) and `governance-agents` (partition `/name`)
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

# Optional CI/non-interactive mode (no prompts)
# bash scripts/setup_env.sh --no-prompt

# 5. Seed demo data
python scripts/seed_data.py

# 6. Run tests (pytest-asyncio required — installs via requirements.txt)
pytest tests/ -v
# Expected: 398 passed, 10 xfailed, 0 failed

# 7a. Start SentinelLayer — MCP stdio server (for Claude Desktop)
python -m src.mcp_server.server

# 7b. Start SentinelLayer — A2A HTTP server (for agent-to-agent protocol)
uvicorn src.a2a.sentinel_a2a_server:app --host 0.0.0.0 --port 8000

# 7c. Start SentinelLayer — Dashboard REST API
uvicorn src.api.dashboard_api:app --reload

# 8. Run demos
python demo.py        # direct Python pipeline demo (3 scenarios)
python demo_a2a.py    # A2A protocol demo — starts server + 3 agent clients
```

## Optional: Deploy Mini Production Environment

`infrastructure/terraform-prod/` creates 5 real Azure resources that SentinelLayer governs
in live demos — turning mock IDs into actual Azure resource IDs on the dashboard.

```bash
cd infrastructure/terraform-prod
cp terraform.tfvars.example terraform.tfvars
# Fill in: subscription_id, suffix (e.g. "abc1234"), vm_admin_password, alert_email
terraform init
terraform apply

# After apply — paste real IDs into data/seed_resources.json:
terraform output seed_resources_ids

# Before each demo — start the VMs (auto-shutdown stops them at 22:00 UTC):
az vm start --resource-group sentinel-prod-rg --name vm-dr-01
az vm start --resource-group sentinel-prod-rg --name vm-web-01

# After demo — destroy to avoid charges (~$0.35/day while VMs run):
terraform destroy
```

Resources created and their governance roles:

| Resource | Demo Scenario | Expected Verdict |
|---|---|---|
| `vm-dr-01` (B1s) | Cost agent proposes DELETE (idle DR VM) | DENIED — `disaster-recovery=true` policy |
| `vm-web-01` (B1s) | SRE agent proposes SCALE UP (CPU >80%) | APPROVED — safe action |
| `payment-api-prod` (App Service F1) | Critical dependency of vm-web-01 | Raises blast radius score |
| `nsg-east-prod` (NSG) | Deploy agent proposes open port 8080 | ESCALATED — affects all governed workloads |
| `sentinelprod{suffix}` (Storage) | Shared dependency of all three above | Deletion → high blast radius |

See `infrastructure/terraform-prod/README.md` for full detail including cost estimates.

---

## Environment Variables

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `USE_LOCAL_MOCKS` | No | `true` | `true` = JSON files; `false` = live Azure |
| `AZURE_OPENAI_ENDPOINT` | Live only | — | Foundry endpoint URL |
| `AZURE_OPENAI_DEPLOYMENT` | Live only | `gpt-41` | Model deployment name |
| `AZURE_SEARCH_ENDPOINT` | Live only | — | Azure AI Search endpoint |
| `AZURE_SEARCH_INDEX` | Live only | `incident-history` | Search index name |
| `COSMOS_ENDPOINT` | Live only | — | Cosmos DB endpoint |
| `COSMOS_DATABASE` | Live only | `sentinellayer` | Database name |
| `COSMOS_CONTAINER_DECISIONS` | Live only | `governance-decisions` | Container name |
| `AZURE_KEYVAULT_URL` | Live only | — | Key Vault URL for secret resolution |
| `A2A_SERVER_URL` | No | `http://localhost:8000` | Base URL advertised in the A2A Agent Card |
