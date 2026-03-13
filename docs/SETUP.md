# Setup Guide

Detailed infra runbook: `infrastructure/terraform-core/deploy.md`

## Prerequisites

- Python 3.11+
- Node.js 18+ (for dashboard)
- Azure CLI (`az login` configured)
- Terraform 1.5+
- Azure subscription with credits/quota
- Docker Desktop — required to build and push the backend image (`scripts/deploy.sh` handles the build automatically)

## Infrastructure Is Terraform-Managed

Terraform in `infrastructure/terraform-core/` deploys (two providers: `hashicorp/azurerm` ~> 4.0 and `azure/azapi` ~> 2.0):

1. Azure Resource Group (`ruriskry-core-engine-rg`) — with a CanNotDelete management lock
2. Azure AI Foundry account (`azurerm_ai_services`)
3. Foundry model deployment (`azurerm_cognitive_deployment`, default `gpt-5-mini` version `2025-08-07`, GlobalStandard, 200K TPM)
4. **Foundry project** — fully Terraform-managed via AzAPI (`azapi_update_resource` to enable `allowProjectManagement`, `azapi_resource` to create the project). Set `create_foundry_project = true` in `terraform.tfvars`.
5. Azure AI Search
6. Azure Cosmos DB (SQL API) — four containers: `governance-decisions` (partition `/resource_id`), `governance-agents` (partition `/name`), `governance-scan-runs` (partition `/agent_type`, auto-created by `ScanRunTracker` if missing), `governance-alerts` (partition `/severity`, managed by `AlertTracker`). Managed Identity auth; no connection string stored in tfstate.
7. Azure Key Vault — purge protection enabled, 90-day soft-delete retention
8. Azure Log Analytics
9. Azure Container Registry (ACR) — admin disabled; Container App pulls via Managed Identity (`AcrPull` role)
10. Azure Container Apps Environment + Container App (backend)
11. Azure Static Web App (dashboard)

Security notes:
- ACR `admin_enabled = false` — credentials never appear in tfstate or env vars
- Foundry `local_authentication_enabled = false` — Managed Identity only; agents use `DefaultAzureCredential` so local dev (`az login`) still works unchanged. The Container App MI is granted the `Cognitive Services OpenAI User` role via `azurerm_role_assignment.foundry_openai_user` in Terraform — without this role, all agent scans fail with 401 PermissionDenied.
- Cosmos DB and Key Vault accessed via Managed Identity (no API keys in tfstate)
- Teams webhook stored as a Key Vault secret, injected via Container App secret mechanism
- CORS enforced at the FastAPI application layer using `DASHBOARD_URL` env var
- `terraform destroy` automatically removes the RG lock first (the lock `depends_on` all major resources, so Terraform destroys it before anything else)

Runtime secrets are read from Key Vault by default via `DefaultAzureCredential`.
In Azure, use Managed Identity. Locally, `az login` is used by the same credential chain.

## Quick Setup

```bash
# 1. Clone
git clone https://github.com/<your-username>/ruriskry.git
cd ruriskry

# 2. Python environment
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# 3. Deploy Azure infrastructure + backend + dashboard (one command)
cp infrastructure/terraform-core/terraform.tfvars.example \
   infrastructure/terraform-core/terraform.tfvars
# Edit terraform.tfvars: set subscription_id and suffix at minimum
# (see infrastructure/terraform-core/deploy.md for remote state setup — one-time)
bash scripts/deploy.sh
# If Stage 2 fails, resume without re-waiting or rebuilding:
#   bash scripts/deploy.sh --stage2

# 4. Generate .env from Terraform outputs (Key Vault + Managed Identity mode)
bash scripts/setup_env.sh

# Optional local fallback (writes plaintext keys into .env)
# bash scripts/setup_env.sh --include-keys

# Optional CI/non-interactive mode (no prompts)
# bash scripts/setup_env.sh --no-prompt

# 5. (Optional) Seed demo incidents — for local/mock dev only.
#    In production, historical context builds up organically via DecisionTracker.
# python scripts/seed_data.py

# 6. Run tests (pytest-asyncio required — installs via requirements.txt)
pytest tests/ -v
# Expected: 777 passed, 0 failed

# 7a. Start RuriSkry — MCP stdio server (for Claude Desktop)
python -m src.mcp_server.server

# 7b. Start RuriSkry — A2A HTTP server (for agent-to-agent protocol)
uvicorn src.a2a.ruriskry_a2a_server:app --host 0.0.0.0 --port 8000

# 7c. Start RuriSkry — Dashboard REST API
uvicorn src.api.dashboard_api:app --reload

# 7d. Start React dashboard (separate terminal)
cd dashboard && npm install && npm run dev
# → opens at http://localhost:5173 (or 5174 if port is busy)
# Fonts (DM Sans + JetBrains Mono) load from Google Fonts — internet connection required.
# Offline: dashboard renders with system-ui / monospace fallbacks.

# 8. Run demos
python demo.py        # direct Python pipeline demo (3 scenarios)
python demo_a2a.py    # A2A protocol demo — starts server + 3 agent clients
python demo_live.py                           # two-layer intelligence demo — starts A2A server + scans whole subscription
python demo_live.py --resource-group ruriskry-prod-rg  # scope to a specific resource group
```

## Optional: Deploy Mini Production Environment

`infrastructure/terraform-prod/` creates 5 real Azure resources that RuriSkry governs
in live demos — turning mock IDs into actual Azure resource IDs on the dashboard.

```bash
cd infrastructure/terraform-prod
cp terraform.tfvars.example terraform.tfvars
# Fill in: subscription_id, suffix (e.g. "abc1234"), vm_admin_password, alert_email
# Optional: alert_webhook_url = "http://<your-host>/api/alert-trigger"
#   → wires Azure Monitor CPU/heartbeat alerts to POST directly into RuriSkry
#   → leave empty to disable (alerts will email only)
terraform init
terraform apply

# After apply — paste real IDs into data/seed_resources.json:
terraform output seed_resources_ids

# Before each demo — start the VMs (auto-shutdown stops them at 22:00 UTC):
az vm start --resource-group ruriskry-prod-rg --name vm-dr-01
az vm start --resource-group ruriskry-prod-rg --name vm-web-01

# After demo — destroy to avoid charges (~$0.35/day while VMs run):
terraform destroy
```

Resources created and their governance roles:

| Resource | Demo Scenario | Expected Verdict |
|---|---|---|
| `vm-dr-01` (B2ls_v2) | Cost agent proposes DELETE (idle DR VM) | DENIED — `disaster-recovery=true` policy |
| `vm-web-01` (B2ls_v2) | SRE agent proposes SCALE UP (CPU >80%) — cloud-init stress cron fires automatically | APPROVED — safe action |
| `payment-api-prod` (App Service F1, free) | Critical dependency of vm-web-01 | Raises blast radius score |
| `nsg-east-prod` (NSG) | Deploy agent proposes open port 8080 | ESCALATED — affects all governed workloads |
| `ruriskryprod{suffix}` (Storage) | Shared dependency of all three above | Deletion → high blast radius |

See `infrastructure/terraform-prod/README.md` for full detail including cost estimates.

---

## Optional: Configure Teams Notifications (Phase 17)

RuriSkry can post an Adaptive Card to a Microsoft Teams channel whenever a verdict is
DENIED or ESCALATED. Zero config required to run without it — just leave `TEAMS_WEBHOOK_URL`
empty.

**Step 1 — Create an Incoming Webhook in Teams:**
1. Open Teams → go to the channel you want alerts in
2. Click **···** (More options) → **Connectors** → search "Incoming Webhook" → **Configure**
3. Give it a name (e.g. "RuriSkry Governance") → click **Create**
4. Copy the webhook URL (looks like `https://xxx.webhook.office.com/webhookb2/...`)

**Step 2 — Set the env var (local development):**
```bash
# .env (local only — in deployed environments the webhook URL is stored as a Key Vault secret
#        and injected via the Container App secret mechanism, not as a plain env var)
TEAMS_WEBHOOK_URL=https://xxx.webhook.office.com/webhookb2/...
TEAMS_NOTIFICATIONS_ENABLED=true
DASHBOARD_URL=http://localhost:5173   # URL in the "View in Dashboard" card button
```

**Step 3 — Test it:**
```bash
# With the API running, click the green 🔔 Teams button in the dashboard header
# — or call the endpoint directly:
curl -X POST http://localhost:8000/api/test-notification
```

This sends a realistic sample DENIED card for `vm-dr-01` (SRI 77.0, POL-DR-001 violation).
If the webhook URL is empty or wrong, the endpoint returns `{"status": "skipped"}` or
`{"status": "failed"}` — it never crashes the API.

---

## Optional: Decision Explanation Drilldown (Phase 18)

No setup required — the explanation engine runs automatically. Click any row in the
Live Activity Feed to open the 6-section drilldown:

- SRI™ dimensional bars (which dimension drove the verdict)
- Plain-English explanation generated by gpt-5-mini (or a template in mock mode)
- Counterfactual analysis — "what would change this outcome?"
- Per-governance-agent reasoning
- Collapsible raw JSON audit trail

In mock mode (`USE_LOCAL_MOCKS=true`) the gpt-5-mini summary is replaced by a deterministic
template string — the rest of the drilldown (factors, counterfactuals, violations) is always
fully computed regardless of mode.

---

## Optional: Execution Gateway & HITL (Phase 21)

The Execution Gateway routes APPROVED verdicts to IaC-safe paths — generating Terraform PRs
instead of directly modifying Azure resources. This prevents IaC state drift.

**Step 1 — Add IaC tags to your Terraform resources:**

All resources in `infrastructure/terraform-prod/main.tf` need:
```hcl
tags = {
  managed_by = "terraform"
  iac_repo   = "psc0des/ruriskry"
  iac_path   = "infrastructure/terraform-prod"
}
```

Run `terraform apply` to push the tags to Azure.

**Step 2 — Create a GitHub Personal Access Token:**
1. GitHub → Settings → Developer settings → Personal access tokens → Fine-grained tokens
2. Repository access: select your `ruriskry` repo
3. Permissions: Contents (Read & Write), Pull requests (Read & Write)
4. Copy the token

**Step 3 — Store the PAT:**

In a production (Azure) deployment, `deploy.sh` prompts for the PAT during deployment and
stores it in Key Vault automatically. The Container App reads it via Managed Identity —
it never appears in `.env` or Terraform state.

For local development only:
```bash
# .env
GITHUB_TOKEN=ghp_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
IAC_GITHUB_REPO=owner/ruriskry
IAC_TERRAFORM_PATH=infrastructure/terraform-prod
EXECUTION_GATEWAY_ENABLED=true
```

**Step 4 — Test it:**
Run a scan from the dashboard. When an APPROVED verdict is issued for an IaC-managed
resource, the gateway will create a PR in your repo with the proposed Terraform change.
Check the drilldown panel for execution status and a link to the PR.

See `Adding-Terraform-Feature.md` for full implementation guide.

---

## Cloud Deployment

RuriSkry is a **single FastAPI application** — all agents (governance + operational) run
in-process. Deploying to Azure requires exactly two services:

| Service | What to deploy | Terraform |
|---------|---------------|-----------|
| **Azure Container Apps** | FastAPI backend (`src/api/dashboard_api.py` + all agents) | `infrastructure/terraform-core/` |
| **Azure Static Web Apps** | React dashboard (`dashboard/`) | `infrastructure/terraform-core/` |

Both services are now provisioned by `terraform apply`. All other Azure services (OpenAI Foundry, AI Search, Cosmos DB, Key Vault, ACR) are also provisioned in the same apply.

### Container Apps — quick deploy

The `Dockerfile` at the repo root builds the FastAPI backend. For a first-time deploy, `scripts/deploy.sh` handles ACR creation, Docker build, push, and Container App provisioning in the correct order. To push a subsequent code update:

```bash
# Build and push using local Docker (requires Docker Desktop)
ACR_NAME=$(terraform -chdir=infrastructure/terraform-core output -raw acr_name)
ACR_SERVER=$(terraform -chdir=infrastructure/terraform-core output -raw acr_login_server)
az acr login --name $ACR_NAME
docker build -t $ACR_SERVER/ruriskry-backend:latest .
docker push $ACR_SERVER/ruriskry-backend:latest

# Update the Container App to pull the new image.
# Use --revision-suffix to force a new revision — Azure Container Apps caches
# the image digest per revision, so updating a mutable tag (e.g. :latest) without
# creating a new revision will NOT pull the updated image.
az containerapp update \
  --name $(terraform -chdir=infrastructure/terraform-core output -raw backend_container_app_name) \
  --resource-group ruriskry-core-engine-rg \
  --image $ACR_SERVER/ruriskry-backend:latest \
  --revision-suffix "r$(date +%Y%m%d%H%M)"

# Get the backend URL
terraform -chdir=infrastructure/terraform-core output backend_url
```

All env vars (endpoints, feature flags, org context) are wired automatically by
Terraform from the other provisioned resources. Secrets (API keys, Teams webhook URL) are
stored in Key Vault and injected at runtime via the Container App's Managed Identity —
no `.env` file goes inside the container. ACR pulls also use the same Managed Identity
(`AcrPull` role) — no registry credentials anywhere in tfstate or the container.

### Static Web Apps — quick deploy

```bash
# Build the React app (dashboard/.env.production must exist with VITE_API_URL set)
cd dashboard
echo "VITE_API_URL=$(terraform -chdir=../infrastructure/terraform-core output -raw backend_url)" > .env.production
npm run build

# Deploy to Static Web Apps (token output by terraform apply)
DEPLOY_TOKEN=$(terraform -chdir=../infrastructure/terraform-core output -raw dashboard_deployment_token)
npx @azure/static-web-apps-cli deploy dist --deployment-token $DEPLOY_TOKEN --env production
```

### Why all agents run in-process (not as separate services)

The 4 governance agents are called via `asyncio.gather()` inside `pipeline.py` — they
run as Python coroutines in the same event loop, sharing a single process. There is no
inter-service HTTP, no message broker, no service mesh. This gives:

- True parallelism without network overhead
- Single deployment unit — one `docker build`, one `az containerapp update`
- No distributed tracing setup needed for the core evaluation path

Scale by increasing Container App CPU/memory limits and replica count. For very high
throughput, the governance agent calls can be extracted to worker replicas behind a queue
— but that is not needed for the current workload.

---

## Environment Variables

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `USE_LOCAL_MOCKS` | No | `true` | `true` = JSON files; `false` = live Azure |
| `USE_LIVE_TOPOLOGY` | No | `false` | `true` = governance agents query Azure Resource Graph for real dependency topology and SKU cost (Phase 19). Only effective when `USE_LOCAL_MOCKS=false` and `AZURE_SUBSCRIPTION_ID` is set. |
| `AZURE_OPENAI_ENDPOINT` | Live only | — | Foundry endpoint URL |
| `AZURE_OPENAI_DEPLOYMENT` | Live only | `gpt-41` | Model deployment name |
| `AZURE_SEARCH_ENDPOINT` | Live only | — | Azure AI Search endpoint |
| `AZURE_SEARCH_INDEX` | Live only | `incident-history` | Search index name |
| `COSMOS_ENDPOINT` | Live only | — | Cosmos DB endpoint |
| `COSMOS_DATABASE` | Live only | `ruriskry` | Database name |
| `COSMOS_CONTAINER_DECISIONS` | Live only | `governance-decisions` | Container for verdict audit trail |
| `COSMOS_CONTAINER_SCAN_RUNS` | Live only | `governance-scan-runs` | Container for scan-run records (auto-created) |
| `COSMOS_CONTAINER_ALERTS` | Live only | `governance-alerts` | Container for alert investigation records (auto-created) |
| `DEMO_MODE` | No | `false` | `true` = ops agents return hardcoded sample proposals (no Azure OpenAI needed). Full governance pipeline still runs. |
| `TEAMS_WEBHOOK_URL` | No | `""` | Microsoft Teams Incoming Webhook URL. Empty = notifications disabled (zero-config default). |
| `TEAMS_NOTIFICATIONS_ENABLED` | No | `true` | Master on/off switch for Teams notifications. Has no effect if `TEAMS_WEBHOOK_URL` is empty. |
| `DASHBOARD_URL` | No | `http://localhost:5173` | URL embedded in the "View in Dashboard" button on Teams Adaptive Cards. |
| `AZURE_KEYVAULT_URL` | Live only | — | Key Vault URL for secret resolution |
| `A2A_SERVER_URL` | No | `http://localhost:8000` | Base URL advertised in the A2A Agent Card |
| `DEFAULT_RESOURCE_GROUP` | No | `""` | Default Azure resource group for dashboard scan endpoints. Empty = scan whole subscription. Body `resource_group` overrides this. |
| `GITHUB_TOKEN` | Phase 21 | `""` | GitHub PAT with repo write access (Contents + Pull requests). Required for Terraform PR generation. |
| `IAC_GITHUB_REPO` | Phase 21 | `""` | GitHub repo for IaC PRs (e.g. `psc0des/ruriskry`). |
| `IAC_TERRAFORM_PATH` | Phase 21 | `infrastructure/terraform-prod` | Path within the repo to the Terraform config directory. |
| `EXECUTION_GATEWAY_ENABLED` | No | `false` | Enable the Execution Gateway. When `false`, verdicts are informational only (no PRs created). |
| `LLM_TIMEOUT` | No | `600` | Hard timeout (seconds) for any single agentic LLM call. Applied at two layers: (1) each individual HTTP request to Azure OpenAI, (2) the entire `agent.run()` agentic loop via `asyncio.wait_for`. gpt-5-mini multi-step audit loops need 10+ minutes; 600s is the production-tested minimum. Scans that exceed this limit set `scan_error` and show a red Error badge. |
| `LLM_CONCURRENCY_LIMIT` | No | `3` | Maximum simultaneous LLM calls across all governance agents (shared semaphore). Set to `1` for very tight quota deployments. |
| `ORG_NAME` | No | `Contoso` | Display name for your organisation — used in triage context and future reporting. |
| `ORG_RESOURCE_COUNT` | No | `0` | Approximate total Azure resources under management. Used by risk triage for scale-aware context. |
| `ORG_COMPLIANCE_FRAMEWORKS` | No | `""` | Comma-separated compliance frameworks in scope (e.g. `HIPAA,PCI-DSS,SOC2`). Any production resource is treated as compliance-scoped when this is non-empty, routing it to Tier 3 governance. |
| `ORG_RISK_TOLERANCE` | No | `moderate` | Organisation-wide risk posture: `conservative`, `moderate`, or `aggressive`. Informs triage context; `conservative` is recommended for regulated industries. |
| `ORG_BUSINESS_CRITICAL_RGS` | No | `""` | Comma-separated resource group names that contain P0 workloads (e.g. `rg-prod-payments,rg-prod-identity`). Actions targeting these RGs are always scoped as compliance-relevant (Tier 3 minimum). |
