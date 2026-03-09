# RuriSkry — Deployment Runbook

Everything needed to go from zero to a fully live system.
Based on the actual deployment steps used in production.

---

## Architecture Overview

RuriSkry is **two deployable units** — a FastAPI backend and a React dashboard.
All agents (governance + operational) run in-process inside the backend container.
No separate agent services exist.

```
React dashboard  →  Azure Static Web Apps   (dashboard/)
FastAPI backend  →  Azure Container Apps    (src/)
LLM / Search / Cosmos / Key Vault  →  provisioned by Terraform
```

---

## What Terraform Provisions

`infrastructure/terraform-core/` manages all Azure resources in one apply:

| Resource | Name pattern | Purpose |
|----------|-------------|---------|
| Resource Group | `ruriskry-rg` | Container for all resources |
| Log Analytics | `ruriskry-log-<suffix>` | Container + infra logs |
| Key Vault | `ruriskry-kv-<suffix>` | Runtime secrets (API keys) |
| Azure AI Foundry | `ruriskry-foundry-<suffix>` | GPT-4.1 LLM |
| Azure AI Search | `ruriskry-search-<suffix>` | Historical incident BM25 |
| Cosmos DB | `ruriskry-cosmos-<suffix>` | Audit trail + agent registry |
| Container Registry | `ruriskry<suffix>` | Docker image store |
| Container Apps Env | `ruriskry-env-<suffix>` | Managed runtime environment |
| Container App | `ruriskry-backend-<suffix>` | FastAPI + all agents |
| Static Web App | `ruriskry-dashboard-<suffix>` | React dashboard (global CDN) |

---

## Prerequisites

- Azure CLI — `az login` completed
- Terraform 1.5+
- Docker Desktop running
- Node.js 18+ (for dashboard build)
- Python 3.11+ (for seeding + local dev)

**One-time: register the Container Apps provider** (required before first apply —
Azure subscriptions don't have this enabled by default):

```bash
az provider register --namespace Microsoft.App --wait
```

---

## First-Time Deploy

### 1. Configure tfvars

```bash
cd infrastructure/terraform-core
cp terraform.tfvars.example terraform.tfvars
```

Edit `terraform.tfvars` — mandatory fields:

```hcl
subscription_id = "xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx"
suffix          = "yourname"   # globally unique — used in all resource names
```

Other fields to fill in:

```hcl
iac_github_repo    = "owner/repo"                   # for Execution Gateway PR creation
iac_terraform_path = "infrastructure/terraform-prod"
dashboard_url      = ""                             # leave empty — fill in after step 7
use_github_pat     = false                          # set true after step 3
```

### 2. Apply infrastructure

```bash
terraform init
terraform validate
terraform apply
```

Terraform prints a `next_steps` output with the real ACR name, backend URL, and
dashboard URL after apply completes.

> **If apply fails partway through** (e.g. Container App fails because image doesn't
> exist yet), some resources may have been created in Azure but not recorded in
> Terraform state. Fix with import before re-applying:
> ```powershell
> # Run in PowerShell (not Git Bash — Git Bash mangles the leading / into a Windows path)
> terraform import azurerm_container_app.backend "/subscriptions/<sub-id>/resourceGroups/ruriskry-rg/providers/Microsoft.App/containerApps/ruriskry-backend-<suffix>"
> ```

### 3. Store GitHub PAT in Key Vault

Required for the Execution Gateway (Terraform PR creation). Skip if not using it.

```bash
az keyvault secret set \
  --vault-name ruriskry-kv-<suffix> \
  --name github-pat \
  --value "github_pat_xxx..."
```

Then in `terraform.tfvars` set `use_github_pat = true` and re-apply:

```bash
terraform apply
```

### 4. Seed the AI Search index

Required for `HistoricalPatternAgent` to find incidents in live mode.
Run from the **repo root**:

```bash
cd /path/to/sentinellayer
python scripts/seed_data.py
```

Expected output: `Uploaded 7/7 incidents`

### 5. Build and push the backend Docker image

Run from the **repo root** (where `Dockerfile` lives — not from `terraform-core/`):

```bash
az acr login --name ruriskry<suffix>
docker build -t ruriskry<suffix>.azurecr.io/ruriskry-backend:latest .
docker push ruriskry<suffix>.azurecr.io/ruriskry-backend:latest
```

> **Must push the image before `terraform apply` if the Container App doesn't exist yet.**
> Terraform tries to start a revision immediately — if the image tag doesn't exist in ACR,
> the apply will fail with `MANIFEST_UNKNOWN`.

Force the Container App to pull the new image:

```bash
az containerapp update \
  --name ruriskry-backend-<suffix> \
  --resource-group ruriskry-rg \
  --image ruriskry<suffix>.azurecr.io/ruriskry-backend:latest
```

Verify the backend is live:

```bash
curl https://<backend_url>/health
# → {"status": "ok"}
```

Get the exact backend URL from Terraform:

```bash
cd infrastructure/terraform-core
terraform output backend_url
```

### 6. Build and deploy the React dashboard

```bash
# Step 1 — get the backend URL
cd infrastructure/terraform-core
terraform output backend_url
# example: "https://ruriskry-backend-psc0des.wonderfulpond-71ad231f.eastus2.azurecontainerapps.io"

# Step 2 — create dashboard/.env.production with the real backend URL
# (run from repo root)
echo "VITE_API_URL=https://ruriskry-backend-<suffix>.<hash>.<region>.azurecontainerapps.io" \
  > dashboard/.env.production

# Step 3 — build
cd dashboard
npm install
npm run build

# Step 4 — get deployment token
cd ../infrastructure/terraform-core
terraform output -raw dashboard_deployment_token

# Step 5 — deploy to production slot
# IMPORTANT: --env production is required. Without it, the SWA CLI deploys to
# a preview slot and Production stays stuck on "Waiting for deployment".
cd ../../dashboard
npx @azure/static-web-apps-cli deploy ./dist \
  --deployment-token <token> \
  --env production
```

Dashboard is live at:

```bash
cd infrastructure/terraform-core
terraform output dashboard_url
```

### 7. Wire the dashboard URL back into Terraform

After getting the Static Web App URL, add it to `terraform.tfvars`:

```hcl
dashboard_url = "https://calm-cliff-xxxxxxx.eastus2.azurestaticapps.net"
```

Re-apply so the Container App picks it up as the `DASHBOARD_URL` env var
(used in Teams notification card "View in Dashboard" button):

```bash
cd infrastructure/terraform-core
terraform apply
```

### 8. Generate local .env (for local development)

```bash
cd ../..
bash scripts/setup_env.sh
```

Writes endpoints and Key Vault secret names to `.env` from Terraform outputs.

---

## Redeploy Workflows

### Backend code changed

Run from repo root:

```bash
docker build -t ruriskry<suffix>.azurecr.io/ruriskry-backend:latest .
docker push ruriskry<suffix>.azurecr.io/ruriskry-backend:latest

az containerapp update \
  --name ruriskry-backend-<suffix> \
  --resource-group ruriskry-rg \
  --image ruriskry<suffix>.azurecr.io/ruriskry-backend:latest
```

### Dashboard changed

```bash
# Ensure dashboard/.env.production exists with the correct backend URL.
# VITE_API_URL has no fallback — missing env file = broken build.
# cat dashboard/.env.production  →  VITE_API_URL=https://ruriskry-backend-<suffix>...

cd dashboard
npm run build

cd ../infrastructure/terraform-core
TOKEN=$(terraform output -raw dashboard_deployment_token)

cd ../../dashboard
npx @azure/static-web-apps-cli deploy ./dist \
  --deployment-token $TOKEN \
  --env production
```

### Infrastructure changed (tfvars / main.tf)

```bash
cd infrastructure/terraform-core
terraform validate
terraform plan
terraform apply
```

---

## Validation Checklist

After a fresh deploy, verify each layer:

```bash
# 1. Terraform state is clean
cd infrastructure/terraform-core
terraform state list
terraform output

# 2. Key Vault secrets are accessible
KV=ruriskry-kv-<suffix>
az keyvault secret show --vault-name $KV --name foundry-primary-key --query id -o tsv
az keyvault secret show --vault-name $KV --name search-primary-key  --query id -o tsv
az keyvault secret show --vault-name $KV --name cosmos-primary-key  --query id -o tsv

# 3. Backend health check
curl https://<backend_url>/health

# 4. Container App logs (if something looks wrong)
az containerapp logs show \
  --name ruriskry-backend-<suffix> \
  --resource-group ruriskry-rg \
  --follow

# 5. Search index seeded
python scripts/seed_data.py

# 6. Run test suite
pytest tests/ -v
```

---

## Known Gotchas

| Problem | Cause | Fix |
|---------|-------|-----|
| `MissingSubscriptionRegistration: Microsoft.App` | Container Apps provider not registered | `az provider register --namespace Microsoft.App --wait` |
| `MANIFEST_UNKNOWN: manifest tagged by "latest" is not found` | Docker image not pushed to ACR before `terraform apply` | Push image first, then apply |
| `resource already exists — needs to be imported` | Apply failed mid-way; resource created in Azure but not in state | `terraform import` the resource (use PowerShell, not Git Bash) |
| Git Bash mangles resource ID (`C:/Program Files/Git/subscriptions/...`) | Git Bash converts leading `/` to Windows path | Run `terraform import` in PowerShell |
| SWA Production slot stuck on "Waiting for deployment" | SWA CLI deployed to preview slot (missing `--env production`) | Redeploy with `--env production` flag |

---

## Teardown

Deletes all Terraform-managed resources. Use after a demo to stop charges.

```bash
cd infrastructure/terraform-core
terraform destroy
```

The mini prod environment has its own teardown:

```bash
cd infrastructure/terraform-prod
terraform destroy
```

---

## Cost Notes

| Resource | Approximate cost |
|----------|-----------------|
| Container App (1 replica, 1 vCPU / 2 GiB) | ~$35/month |
| Azure AI Foundry (GPT-4.1 GlobalStandard) | Pay-per-token |
| Azure AI Search (free tier) | $0 |
| Cosmos DB (free tier) | $0 |
| Static Web App (free tier) | $0 |
| Container Registry (Basic) | ~$5/month |

To avoid charges between demos, scale to zero:

```hcl
# terraform.tfvars
backend_min_replicas = 0
```

Then `terraform apply`. Cold start on first request is ~10–15 seconds.
