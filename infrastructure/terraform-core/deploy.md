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
| Resource Group | `ruriskry-core-engine-rg` | Container for all resources |
| Log Analytics | `ruriskry-core-log-<suffix>` | Container + infra logs |
| Key Vault | `ruriskry-core-kv-<suffix>` | Runtime secrets (API keys) |
| Azure AI Foundry | `ruriskry-core-foundry-<suffix>` | gpt-5-mini LLM (version 2025-08-07, GlobalStandard, 200K TPM) |
| Azure AI Search | `ruriskry-core-search-<suffix>` | Historical incident BM25 |
| Cosmos DB | `ruriskry-core-cosmos-<suffix>` | Audit trail + agent registry |
| Container Registry | `ruriskrycore<suffix>` | Docker image store (alphanumeric only) |
| User-Assigned MI | `ruriskry-core-acr-pull-<suffix>` | ACR pull identity for Container App |
| Container Apps Env | `ruriskry-core-env-<suffix>` | Managed runtime environment |
| Container App | `ruriskry-core-backend-<suffix>` | FastAPI + all agents |
| Static Web App | `ruriskry-core-dashboard-<suffix>` | React dashboard (global CDN) |
| Monitor Action Group | `ruriskry-core-alert-handler-<suffix>` | Webhook receiver for Azure Monitor alerts → `/api/alert-trigger` |

---

## Prerequisites

| Tool | Version | Check |
|------|---------|-------|
| Azure CLI | any recent | `az --version` |
| Terraform | 1.5+ | `terraform -version` |
| Docker Desktop (or [Rancher Desktop](https://rancherdesktop.io)) | any | `docker info` |
| Node.js | 18+ | `node --version` |
| npm + npx | ships with Node 18 | `npx --version` |

Run `az login` before starting.

**Windows users:** `deploy.sh` is a bash script. Run it in **Git Bash** (ships with Git for Windows) or **WSL**. Do not use PowerShell or CMD — they cannot run `.sh` files directly. All other commands in this doc that start with `az`, `terraform`, `docker`, and `npm` work fine in PowerShell too.

---

## One-time Setup

Complete these steps once before running `deploy.sh` for the first time.

### Step 1 — Register the Container Apps provider

Azure subscriptions don't have this enabled by default:

```bash
az provider register --namespace Microsoft.App --wait
```

### Step 2 — Create remote state storage

Terraform stores state in Azure Blob Storage. Run once in **PowerShell** (Git Bash mangles storage account names on some systems):

> Replace `<suffix>` with the short unique suffix you'll use in `terraform.tfvars` (e.g. `jd4821`).
> The storage account name must be globally unique and lowercase alphanumeric only.

```powershell
az group create --name ruriskry-tfstate-rg --location eastus2
az storage account create --name ruriskrytfstate<suffix> --resource-group ruriskry-tfstate-rg --location eastus2 --sku Standard_LRS --allow-blob-public-access false
az storage container create --name tfstate --account-name ruriskrytfstate<suffix>
```

Then lock the storage account and enable blob versioning so every state write is recoverable:

```powershell
az lock create --name ruriskry-tfstate-lock --resource-group ruriskry-tfstate-rg --lock-type CanNotDelete --notes "Protects Terraform state from accidental deletion"
az storage account blob-service-properties update --account-name ruriskrytfstate<suffix> --enable-versioning true
```

> Skip this step if the storage account already exists — the tfstate storage account is
> intentionally NOT managed by Terraform so it survives `terraform destroy`.
>
> If you deleted and recreated the container, just recreate the container (`az storage container create`) — the account stays.

### Step 3 — Configure tfvars

```bash
cp infrastructure/terraform-core/terraform.tfvars.example \
   infrastructure/terraform-core/terraform.tfvars
```

Edit `terraform.tfvars` — mandatory fields:

```hcl
subscription_id = "xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx"
suffix          = "yourname"   # globally unique — used in all resource names
```

Other fields to review:

```hcl
iac_github_repo    = "owner/repo"                   # for Execution Gateway PR creation
iac_terraform_path = "infrastructure/terraform-prod"
use_github_pat     = false                          # set true after you store the PAT
enable_rg_lock     = false                          # set true in production
```

> **`dashboard_url` is not a tfvars variable.** Terraform reads the SWA URL directly from
> `azurerm_static_web_app.dashboard.default_host_name` and wires it into `DASHBOARD_URL`
> automatically — no patching or re-apply required.

> **`enable_rg_lock`** defaults to `false` — no CanNotDelete lock on the RG, allowing
> clean `terraform destroy` and redeploys. Set `true` in production.

---

## Quick Start

Once the one-time setup is done, deploy everything with one command from the repo root:

```bash
# Run in Git Bash or WSL from the repo root
bash scripts/deploy.sh
```

The script handles everything in the correct order:

| Step | What happens |
|------|--------------|
| 0 | Checks all tools are installed, Docker is running, `az login` is active |
| 1 | `terraform init` |
| 2 | **Stage 1 targeted apply** — creates ACR, User-Assigned Managed Identity, AcrPull role assignment |
| 3 | Docker build + push to ACR (skipped if image already exists) |
| 4 | **Stage 2 full apply** — creates all remaining resources; Terraform resolves the SWA URL natively and passes it into `DASHBOARD_URL` automatically |
| 4a | **Image swap** — `az containerapp update --image` replaces the MCR placeholder with the real ACR image (AcrPull role is guaranteed propagated by now) |
| 4b | GitHub PAT prompt — stores in Key Vault if `use_github_pat = true` and secret is missing |
| 5 | React dashboard build + deploy to Static Web Apps |
| 6 | Backend health check |
| 7 | Prints live URLs |

When it finishes:
```
  Dashboard  →  https://calm-cliff-xxxxxxx.eastus2.azurestaticapps.net
  Backend    →  https://ruriskry-core-backend-<suffix>.<hash>.eastus2.azurecontainerapps.io
```

### GitHub PAT (Execution Gateway)

If `use_github_pat = true` in `terraform.tfvars`, the script will prompt for the PAT and store it in Key Vault automatically. To skip the prompt, export it before running:

```bash
export GITHUB_PAT="github_pat_xxx..."
bash scripts/deploy.sh
```

Press Enter at the prompt to skip — the script will disable Execution Gateway gracefully.

### If the script fails partway through

The script is safe to re-run. Use the table below to decide:

| Where it failed | What to do |
|-----------------|------------|
| Stage 1 (targeted apply) | Fix the error, re-run `bash scripts/deploy.sh` |
| Docker build or push | Fix the error, re-run `bash scripts/deploy.sh` — already-pushed layers are cached in ACR |
| **Stage 2 (full apply)** | Re-run with `bash scripts/deploy.sh --stage2` — skips Stage 1 and Docker build entirely |
| Image swap / PAT / dashboard | Re-run with `bash scripts/deploy.sh --stage2` — Stage 2 is a no-op if infra already exists |

---

## After Deployment

### Generate local .env (for local development)

```bash
bash scripts/setup_env.sh                # safe mode — no plaintext keys
bash scripts/setup_env.sh --include-keys # includes raw API keys (local dev only)
bash scripts/setup_env.sh --no-prompt    # non-interactive — uses Azure CLI defaults
```

Reads Terraform outputs (works with both local and remote state) and writes `.env` from the `.env.example` template. Requires `terraform init` to have been run so the state backend is accessible.

### Set up Slack notifications (optional)

See [`docs/slack-setup.md`](../../docs/slack-setup.md) for the full guide. In short:

1. Create a Slack app at [api.slack.com/apps](https://api.slack.com/apps)
2. Enable Incoming Webhooks → add a webhook to your channel
3. Set `slack_webhook_url` in `terraform.tfvars` → `terraform apply`

Notifications fire for DENIED/ESCALATED verdicts and Azure Monitor alerts.

### Wire alert rules to the RuriSkry backend

**If you use `infrastructure/terraform-prod`** (the recommended path):

1. Get the backend URL from `terraform-core`:
   ```bash
   cd infrastructure/terraform-core
   terraform output -raw backend_url
   ```

2. Set it in `infrastructure/terraform-prod/terraform.tfvars`:
   ```hcl
   alert_webhook_url = "https://<backend-url>/api/alert-trigger"
   ```

3. Apply — this adds the webhook receiver to `ag-ruriskry-prod` alongside the existing email receiver. All alert rules already wired to `ag-ruriskry-prod` pick it up automatically:
   ```bash
   cd infrastructure/terraform-prod
   terraform apply -target=azurerm_monitor_action_group.prod
   ```

> This is the correct approach. Every alert rule in `terraform-prod` references `azurerm_monitor_action_group.prod`, so setting `alert_webhook_url` once wires all of them — no per-rule steps.

**If you have alert rules outside `terraform-prod`** (manually created rules or rules in other RGs):

Attach them to `ag-ruriskry-prod` — Action Groups are subscription-scoped, so they work cross-RG:

```bash
# Portal: Monitor → Alerts → Alert rules → Edit rule → Actions tab → Add action group → ag-ruriskry-prod
```

The `alert_webhook_url` output from `terraform-core` shows the exact URL being used:
```bash
cd infrastructure/terraform-core && terraform output alert_webhook_url
```

### (Optional) Seed demo incidents

> **Not required for production.** Historical context builds up organically as the
> system runs real scans — each governance decision is recorded by `DecisionTracker`
> (Cosmos DB in production, `data/decisions/` in mock mode). The `seed_data.py` script
> uploads 7 fictional incidents to AI Search and is intended for local dev/demo only.

```bash
# Only if you want demo data in AI Search for local testing:
python scripts/seed_data.py
```

---

## Redeploy Workflows

### Backend code changed

Run from the repo root:

```bash
ACR=ruriskrycore<suffix>.azurecr.io

az acr login --name ruriskrycore<suffix>
docker build -t $ACR/ruriskry-backend:latest .
docker push $ACR/ruriskry-backend:latest

az containerapp update \
  --name ruriskry-core-backend-<suffix> \
  --resource-group ruriskry-core-engine-rg \
  --image $ACR/ruriskry-backend:latest \
  --revision-suffix "r$(date +%Y%m%d%H%M)"
```

> **Why `--revision-suffix`?** Container Apps uses revision-based deployment. Without a new revision suffix, the update may serve the cached `:latest` digest from the previous push rather than the newly pushed image. The suffix forces a fresh pull every time.

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

> **Rule: all configuration changes go through `terraform.tfvars` + `terraform apply`. Never use `az containerapp update --set-env-vars` to change runtime config.**
>
> `az containerapp update --set-env-vars` is only used in two controlled places in `deploy.sh`: image swap (Step 4a) and GitHub PAT wiring (Step 4b). Using it for anything else causes Terraform state drift — `terraform plan` will show "No changes" even though tfvars and the live Container App disagree. New contributors won't notice, and `terraform apply` will silently do nothing.
>
> Every runtime setting (`LLM_TIMEOUT`, `LLM_CONCURRENCY_LIMIT`, `EXECUTION_GATEWAY_ENABLED`, `ORG_NAME`, etc.) has a corresponding variable in `variables.tf` and is passed to the Container App as an env var in `main.tf`. Change the value in `terraform.tfvars` and apply — that is the only path.

```bash
cd infrastructure/terraform-core
terraform validate
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
KV=ruriskry-core-kv-<suffix>
az keyvault secret show --vault-name $KV --name foundry-primary-key --query id -o tsv
az keyvault secret show --vault-name $KV --name search-primary-key  --query id -o tsv
az keyvault secret show --vault-name $KV --name cosmos-primary-key  --query id -o tsv

# 3. Backend health check (returns {"status": "ok"})
curl https://<backend_url>/health

# 4. Container App logs (if something looks wrong)
az containerapp logs show \
  --name ruriskry-core-backend-<suffix> \
  --resource-group ruriskry-core-engine-rg \
  --follow

# 5. Run test suite
pytest tests/ -v
```

---

## Manual Deploy (Advanced)

If you prefer to run each step individually instead of using `deploy.sh`:

### 1. terraform init

```bash
cd infrastructure/terraform-core
terraform init
terraform validate
```

### 2. Stage 1 — ACR + identity + role (targeted apply)

```bash
terraform apply -auto-approve \
  -target=azurerm_resource_group.ruriskry \
  -target=azurerm_container_registry.ruriskry \
  -target=azurerm_user_assigned_identity.acr_pull \
  -target=azurerm_role_assignment.acr_pull
```

No propagation sleep needed — the Container App starts with a public MCR
placeholder image (no ACR auth). The real ACR image is swapped in after
the full apply in Step 4a below.

### 3. Build and push Docker image

Run from the **repo root** (the directory that contains `Dockerfile`):

```bash
# From infrastructure/terraform-core/, go up two levels to repo root:
cd ../..

az acr login --name ruriskrycore<suffix>
docker build -t ruriskrycore<suffix>.azurecr.io/ruriskry-backend:latest .
docker push ruriskrycore<suffix>.azurecr.io/ruriskry-backend:latest
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
> Check with `az containerapp show --name ruriskry-core-backend-<suffix> --resource-group ruriskry-core-engine-rg --query provisioningState -o tsv`
> and import if needed (use PowerShell, not Git Bash):
> ```powershell
> terraform import azurerm_container_app.backend "/subscriptions/<sub-id>/resourceGroups/ruriskry-core-engine-rg/providers/Microsoft.App/containerApps/ruriskry-core-backend-<suffix>"
> ```

### 4a. Swap placeholder image → real ACR image

The Container App was created with a public MCR placeholder image (no ACR auth
needed at creation time). Now swap to the real ACR image — by this point 15+
minutes have passed since the AcrPull role assignment, so propagation is guaranteed.

```bash
az containerapp update \
  --name ruriskry-core-backend-<suffix> \
  --resource-group ruriskry-core-engine-rg \
  --image ruriskrycore<suffix>.azurecr.io/ruriskry-backend:latest
```

### 5. Build and deploy the React dashboard

```bash
# Get backend URL
cd infrastructure/terraform-core
terraform output backend_url

# Create env file
echo "VITE_API_URL=https://ruriskry-core-backend-<suffix>.<hash>.<region>.azurecontainerapps.io" \
  > ../../dashboard/.env.production

# Build
cd ../../dashboard
npm ci
npm run build

# Deploy
cd ../infrastructure/terraform-core
TOKEN=$(terraform output -raw dashboard_deployment_token)

cd ../../dashboard
npx @azure/static-web-apps-cli deploy ./dist \
  --deployment-token $TOKEN \
  --env production
```

---

## Known Gotchas

| Problem | Cause | Fix |
|---------|-------|-----|
| `MissingSubscriptionRegistration: Microsoft.App` | Container Apps provider not registered | `az provider register --namespace Microsoft.App --wait` |
| `MANIFEST_UNKNOWN: manifest tagged by "latest" is not found` | Docker image not pushed before Container App creation | Use `deploy.sh` — it pushes the image between Stage 1 and Stage 2. If running manually, follow the staged apply order above. |
| `BadRequest: allowProjectManagement set to true` | AzAPI `azapi_update_resource` failed | Check `az login` is active and AzAPI provider version is `~> 2.0` |
| `resource already exists — needs to be imported` | Apply failed mid-way; resource created in Azure but not in state | `terraform import` the resource (use PowerShell, not Git Bash) |
| Git Bash mangles resource ID (`C:/Program Files/Git/subscriptions/...`) | Git Bash converts leading `/` to Windows path | Run `terraform import` in PowerShell |
| SWA Production slot stuck on "Waiting for deployment" | SWA CLI deployed to preview slot (missing `--env production`) | Redeploy with `--env production` flag |
| `Cannot delete resource while nested resources exist` on `terraform destroy` | Foundry project created outside Terraform | Handled automatically by a `destroy` provisioner — deletes all projects via `az rest` before removing the account |
| `terraform destroy` fails with `ScopeLocked` | Only occurs if `enable_rg_lock = true` and lock removal fails. Default is `false` so this should not occur. | Remove manually: `az lock delete --name ruriskry-core-engine-rg-lock --resource-group ruriskry-core-engine-rg`, then retry |
| `409 Conflict: ResourceGroupBeingDeleted` on fresh deploy | Key Vault soft-delete recovery put the new RG into a deprovisioning state | Purge the soft-deleted KV first: `az keyvault purge --name ruriskry-core-kv-<suffix> --location eastus2`, wait, then re-run |
| Container App `unable to pull image using Managed identity` | Azure IAM role propagation race condition | Fixed by placeholder image pattern — should not occur with `deploy.sh` |
| Azure Monitor Agent silently drops VM telemetry (no metrics appear for `vm-dr-01` or `vm-web-01`) | VMs missing `SystemAssigned` managed identity and/or `Monitoring Metrics Publisher` role — AMA requires a valid MI to authenticate metric ingestion | Fixed in `infrastructure/terraform-prod/main.tf`: `identity { type = "SystemAssigned" }` block + `azurerm_role_assignment` (`Monitoring Metrics Publisher`) added to both VMs. Run `terraform apply` in `terraform-prod` to apply. |
| All agent scans return `401 PermissionDenied ... lacks Microsoft.CognitiveServices/accounts/OpenAI/responses/write` | `local_authentication_enabled = false` on Foundry disables API key auth; the Container App MI is missing the `Cognitive Services OpenAI User` role | Handled automatically by `azurerm_role_assignment.foundry_openai_user` in Terraform. If you're hitting this on an existing deploy that pre-dates the fix, run `terraform apply` to add the role assignment. |
| State lock stuck after network drop | Connection reset before Terraform could release the blob lease | Break the lease: `az storage blob lease break --account-name ruriskrytfstate<suffix> --container-name tfstate --blob-name terraform-core.tfstate` |
| `terraform plan` shows "No changes" after manually running `az containerapp update --set-env-vars` | The Container App's active revision already has the value you set, so Azure reports no drift — even if `terraform.tfvars` disagrees. Terraform state is now inconsistent with reality. | Never use `az containerapp update --set-env-vars` for config changes. Always update the value in `terraform.tfvars` and run `terraform apply`. To verify the live value: `az containerapp show --name ruriskry-core-backend-<suffix> --resource-group ruriskry-core-engine-rg --query "properties.template.containers[0].env" -o table` |
| "Scan log unavailable (backend was restarted while scan was running)" on every new scan | Container App running multiple replicas without sticky sessions — scan starts on Replica A (SSE queue created there) but browser SSE stream is load-balanced to Replica B/C (no queue) | `sticky_sessions_affinity = "sticky"` is already set in `main.tf`. If deploying to a pre-existing Container App, apply manually: `az containerapp ingress sticky-sessions set --name ruriskry-core-backend-<suffix> --resource-group ruriskry-core-engine-rg --affinity sticky` |

---

## Teardown

Deletes all Terraform-managed resources. Use after a demo to stop charges.

```bash
cd infrastructure/terraform-core
terraform destroy
```

> **Container App Environment deletion can take 10-30 minutes** — this is an Azure
> platform limitation. Let it run to completion.

**RG lock** — disabled by default (`enable_rg_lock = false`). If you enabled it,
Terraform removes the lock automatically before destroying anything else. If it fails:

```bash
az lock delete --name ruriskry-core-engine-rg-lock --resource-group ruriskry-core-engine-rg
# then retry:
terraform destroy
```

> The tfstate storage account (`ruriskry-tfstate-rg`) is **not** managed by Terraform
> and will survive `terraform destroy` — this is intentional so state history is preserved.
> Delete it manually only if you are fully done with the project.

---

## Cost Notes

| Resource | Approximate cost |
|----------|--------------------|
| Container App (1–3 replicas, 2 vCPU / 4 GiB, sticky sessions) | ~$70–210/month |
| Azure AI Foundry (gpt-5-mini GlobalStandard, 200K TPM) | Pay-per-token |
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
