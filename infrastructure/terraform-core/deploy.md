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
| Resource Group | `ruriskry-core-rg` | Container for all resources |
| Log Analytics | `ruriskry-log-<suffix>` | Container + infra logs |
| Key Vault | `ruriskry-kv-<suffix>` | Runtime secrets (API keys) |
| Azure AI Foundry | `ruriskry-foundry-<suffix>` | GPT-4.1 LLM |
| Azure AI Search | `ruriskry-search-<suffix>` | Historical incident BM25 |
| Cosmos DB | `ruriskry-cosmos-<suffix>` | Audit trail + agent registry |
| Container Registry | `ruriskry<suffix>` | Docker image store |
| User-Assigned MI | `ruriskry-acr-pull-<suffix>` | ACR pull identity for Container App |
| Container Apps Env | `ruriskry-env-<suffix>` | Managed runtime environment |
| Container App | `ruriskry-backend-<suffix>` | FastAPI + all agents |
| Static Web App | `ruriskry-dashboard-<suffix>` | React dashboard (global CDN) |

---

## Providers

This configuration uses three Terraform providers:

| Provider | Source | Used for |
|----------|--------|----------|
| AzureRM | `hashicorp/azurerm` | All core Azure resources (RG, KV, Search, Cosmos, ACR, Container Apps, SWA) |
| AzAPI | `azure/azapi` | Foundry project management — patches `allowProjectManagement` and creates the project via the Azure REST API directly. Used because AzureRM doesn't expose these Foundry-specific fields yet. |
| Time | `hashicorp/time` | 90-second sleep after role assignment — waits for `AcrPull` to propagate before the Container App starts pulling the image |

All providers authenticate using the same `az login` session — no extra credentials needed.

---

## Prerequisites

| Tool | Version | Check |
|------|---------|-------|
| Azure CLI | any recent | `az --version` |
| Terraform | 1.5+ | `terraform -version` |
| Docker Desktop (or [Rancher Desktop](https://rancherdesktop.io)) | any | `docker info` |
| Node.js | 18+ | `node --version` |
| npm + npx | ships with Node 18 | `npx --version` |
| Python | 3.6+ | `python3 --version` or `python --version` |

Run `az login` before starting.

**Windows users:** `deploy.sh` is a bash script. Run it in **Git Bash** (ships with Git for Windows) or **WSL**. Do not use PowerShell or CMD — they cannot run `.sh` files directly. All other commands in this doc that start with `az`, `terraform`, `docker`, and `npm` work fine in PowerShell too.

**One-time: register the Container Apps provider** (required before first apply —
Azure subscriptions don't have this enabled by default):

```bash
az provider register --namespace Microsoft.App --wait
```

---

## Quick Start (Recommended)

For a first-time deploy, run the one-command script from the repo root:

```bash
# 1. Create your tfvars (from repo root)
cp infrastructure/terraform-core/terraform.tfvars.example \
   infrastructure/terraform-core/terraform.tfvars
# Edit terraform.tfvars — fill in at minimum:
#   subscription_id = "your-azure-sub-id"
#   suffix          = "yourname"   (short, globally unique)
# Leave dashboard_url = "" — the script fills it in automatically.

# 2. Create remote state storage (one-time — see section below)

# 3. Deploy everything (from repo root, in Git Bash or WSL)
bash scripts/deploy.sh
```

The script handles everything in the correct order:

| Step | What happens |
|------|--------------|
| 0 | Checks all tools are installed, Docker is running, `az login` is active |
| 1 | `terraform init` |
| 2 | **Stage 1 targeted apply** — creates ACR, User-Assigned Managed Identity, AcrPull role, 90s propagation sleep, **and Static Web App** — SWA URL is immediately patched into `terraform.tfvars` |
| 3 | Docker build + push to ACR (skipped if image already exists) |
| 4 | **Stage 2 full apply** — Container App created with correct `DASHBOARD_URL` already set (Cosmos, Foundry, Search, everything else) |
| 5 | React dashboard build + deploy to Static Web Apps |
| 6 | Backend health check |
| 7 | Prints live URLs and the 3 remaining manual steps |

When it finishes:
```
  Dashboard  →  https://calm-cliff-xxxxxxx.eastus2.azurestaticapps.net
  Backend    →  https://ruriskry-backend-<suffix>.<hash>.eastus2.azurecontainerapps.io
```

### If the script fails partway through

The script is designed to be re-run safely. Use the table below to decide:

| Where it failed | What to do |
|-----------------|------------|
| Stage 1 (targeted apply) | Fix the error, re-run `bash scripts/deploy.sh` |
| Docker build or push | Fix the error (Docker running? `az login` expired?), re-run `bash scripts/deploy.sh` — script detects image already in ACR and skips rebuild if push succeeded |
| **Stage 2 (full apply)** | Re-run with `bash scripts/deploy.sh --stage2` — skips the 90s wait and Docker rebuild entirely |
| Dashboard build/deploy | Re-run with `bash scripts/deploy.sh --stage2` — Stage 2 is a no-op if infra already exists |

---

## Create Remote State Storage (one-time)

Terraform state is stored in Azure Blob Storage — required before `terraform init` will work.
Run once in PowerShell (Git Bash mangles the storage account name on some systems).

> Replace `<suffix>` with your actual suffix from `terraform.tfvars` (e.g. `jd4821`).
> The storage account name must be globally unique and lowercase alphanumeric only.

```powershell
az group create --name ruriskry-tfstate-rg --location eastus2
az storage account create --name ruriskrytfstate<suffix> --resource-group ruriskry-tfstate-rg --location eastus2 --sku Standard_LRS --allow-blob-public-access false
az storage container create --name tfstate --account-name ruriskrytfstate<suffix>
```

Then lock the storage account and enable blob versioning so every state write is recoverable (SEC-08):

```powershell
az lock create --name ruriskry-tfstate-lock --resource-group ruriskry-tfstate-rg --lock-type CanNotDelete --notes "Protects Terraform state from accidental deletion"
az storage account blob-service-properties update --account-name ruriskrytfstate<suffix> --enable-versioning true
```

> Skip this step if the storage account already exists (e.g. after a `terraform destroy` —
> the tfstate storage account is intentionally NOT managed by Terraform so it survives destroys).

---

## Configure tfvars

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
dashboard_url      = ""                             # leave empty — deploy.sh writes this automatically
use_github_pat     = false                          # set true after you store the PAT in Key Vault
```

> **Do not pre-fill `dashboard_url`.** Azure Static Web Apps generates a random subdomain on
> creation. `deploy.sh` creates the SWA in Stage 1, reads the URL immediately from Terraform
> outputs, and patches `terraform.tfvars` before Stage 2 runs — so the Container App is created
> with the correct `DASHBOARD_URL` on first apply (no re-apply needed).
> If running the manual deploy path, fill it in after Step 2 (Stage 1 apply).

---

## After Deployment

> `deploy.sh` handles infrastructure, backend, and dashboard automatically.
> The steps below are the three things it intentionally leaves for you to do manually.

### Store GitHub PAT (for Execution Gateway)

Required for Terraform PR creation. Skip if not using it.

```bash
az keyvault secret set \
  --vault-name ruriskry-kv-<suffix> \
  --name github-pat \
  --value "github_pat_xxx..."
```

Then in `terraform.tfvars` set `use_github_pat = true` and re-apply:

```bash
cd infrastructure/terraform-core
terraform plan -out=tfplan
terraform apply tfplan
```

### Seed the AI Search index

Required for `HistoricalPatternAgent` to find incidents in live mode.
Run from the **repo root**:

```bash
python scripts/seed_data.py
```

Expected output: `Uploaded 7/7 incidents`

### Generate local .env (for local development)

```bash
bash scripts/setup_env.sh
```

Writes endpoints and Key Vault secret names to `.env` from Terraform outputs.

---

## Redeploy Workflows

### Backend code changed

Run from repo root:

```bash
ACR=ruriskry<suffix>.azurecr.io

az acr login --name ruriskry<suffix>
docker build -t $ACR/ruriskry-backend:latest .
docker push $ACR/ruriskry-backend:latest

az containerapp update \
  --name ruriskry-backend-<suffix> \
  --resource-group ruriskry-core-rg \
  --image $ACR/ruriskry-backend:latest
```

### Dashboard changed

```bash
# Ensure dashboard/.env.production exists with the correct backend URL.
# VITE_API_URL has no fallback — missing env file = broken build.
# If it's missing, recreate it:
#   BACKEND=$(cd infrastructure/terraform-core && terraform output -raw backend_url)
#   echo "VITE_API_URL=$BACKEND" > dashboard/.env.production

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
terraform plan -out=tfplan
terraform apply tfplan
```

---

## Manual Deploy (Advanced)

If you prefer to run each step individually instead of using `deploy.sh`:

### 1. terraform init

```bash
cd infrastructure/terraform-core
terraform init -upgrade
terraform validate
```

### 2. Stage 1 — ACR + identity + role + Static Web App (targeted apply)

```bash
terraform apply -auto-approve \
  -target=azurerm_resource_group.ruriskry \
  -target=azurerm_container_registry.ruriskry \
  -target=azurerm_user_assigned_identity.acr_pull \
  -target=azurerm_role_assignment.acr_pull \
  -target=time_sleep.acr_role_propagation \
  -target=azurerm_static_web_app.dashboard
```

This includes a 90-second sleep for role propagation. The SWA is created here
so its URL is known before the Container App is provisioned — patch it into
`terraform.tfvars` immediately:

```bash
DASHBOARD_URL=$(terraform output -raw dashboard_url)
# Update dashboard_url in terraform.tfvars with the real URL before Stage 2
```

### 3. Build and push Docker image

Run from the **repo root** (the directory that contains `Dockerfile`):

```bash
# From infrastructure/terraform-core/, go up two levels to repo root:
cd ../..

az acr login --name ruriskry<suffix>
docker build -t ruriskry<suffix>.azurecr.io/ruriskry-backend:latest .
docker push ruriskry<suffix>.azurecr.io/ruriskry-backend:latest
```

### 4. Full apply

```bash
cd infrastructure/terraform-core
terraform plan -out=tfplan
terraform apply tfplan
```

> **Full apply takes ~10-15 minutes** on first run.
>
> If apply fails partway through, some resources may exist in Azure but not in state.
> Check with `az containerapp show --name ruriskry-backend-<suffix> --resource-group ruriskry-core-rg --query provisioningState -o tsv`
> and import if needed (use PowerShell, not Git Bash):
> ```powershell
> terraform import azurerm_container_app.backend "/subscriptions/<sub-id>/resourceGroups/ruriskry-core-rg/providers/Microsoft.App/containerApps/ruriskry-backend-<suffix>"
> ```

### 5. Build and deploy the React dashboard

```bash
# Get backend URL
cd infrastructure/terraform-core
terraform output backend_url

# Create env file
echo "VITE_API_URL=https://ruriskry-backend-<suffix>.<hash>.<region>.azurecontainerapps.io" \
  > ../../dashboard/.env.production

# Build
cd ../../dashboard
npm install
npm run build

# Deploy
cd ../infrastructure/terraform-core
TOKEN=$(terraform output -raw dashboard_deployment_token)

cd ../../dashboard
npx @azure/static-web-apps-cli deploy ./dist \
  --deployment-token $TOKEN \
  --env production
```

### 6. Wire dashboard URL back

After getting the Static Web App URL, add it to `terraform.tfvars`:

```hcl
dashboard_url = "https://calm-cliff-xxxxxxx.eastus2.azurestaticapps.net"
```

Re-apply so the Container App picks it up as the `DASHBOARD_URL` env var:

```bash
cd infrastructure/terraform-core
terraform plan -out=tfplan
terraform apply tfplan
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
  --resource-group ruriskry-core-rg \
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
| `MANIFEST_UNKNOWN: manifest tagged by "latest" is not found` | Docker image not pushed before Container App creation | Use `deploy.sh` — it pushes the image between stage 1 and stage 2. If running manually, follow the staged apply order in Manual Deploy above. |
| `BadRequest: allowProjectManagement set to true` | AzAPI `azapi_update_resource` failed | Check `az login` is active and AzAPI provider version is `~> 2.0` |
| `resource already exists — needs to be imported` | Apply failed mid-way; resource created in Azure but not in state | `terraform import` the resource (use PowerShell, not Git Bash) |
| Git Bash mangles resource ID (`C:/Program Files/Git/subscriptions/...`) | Git Bash converts leading `/` to Windows path | Run `terraform import` in PowerShell |
| SWA Production slot stuck on "Waiting for deployment" | SWA CLI deployed to preview slot (missing `--env production`) | Redeploy with `--env production` flag |
| `Cannot delete resource while nested resources exist` on `terraform destroy` | Foundry project created outside Terraform | Handled automatically by a `destroy` provisioner — deletes all projects via `az rest` before removing the account |
| `terraform destroy` fails with `ScopeLocked` | Lock not removed in time — should not occur (lock `depends_on` all major resources so Terraform removes it first). If it does occur: `az lock delete --name ruriskry-core-rg-lock --resource-group ruriskry-core-rg`, then retry |
| Container App `Operation expired` after 16+ minutes | Previously a chicken-and-egg issue with System-Assigned identity. Fixed: a User-Assigned Managed Identity is created first and AcrPull is granted before the Container App exists. Should not occur. |

---

## Teardown

Deletes all Terraform-managed resources. Use after a demo to stop charges.

The `CanNotDelete` lock on the resource group is managed by Terraform and is
automatically removed first during `terraform destroy` (the lock `depends_on`
all major resources, so Terraform removes it before anything else).

```bash
cd infrastructure/terraform-core
terraform destroy
```

> **Container App Environment deletion can take 10-30 minutes** — this is an Azure
> platform limitation. Let it run to completion.

**If `terraform destroy` fails with `ScopeLocked`** (e.g. state is out of sync
or lock was manually created), remove it first:

```bash
az lock delete --name ruriskry-core-rg-lock --resource-group ruriskry-core-rg
# then retry:
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
|----------|--------------------|
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

Then `terraform plan -out=tfplan && terraform apply tfplan`. Cold start on first request is ~10–15 seconds.
