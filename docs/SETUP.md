# Setup Guide

Detailed infra runbook: `infrastructure/terraform-core/deploy.md`

## Required Permissions

Two distinct identities are involved — the person deploying RuriSkry and RuriSkry itself (its Managed Identity).

### Deploying user (`az login` account running `terraform apply`)

`Owner` is the simplest but rarely granted in enterprise environments. The table below shows the minimum roles needed and **why each is required** — so your org's platform team knows exactly what to grant.

**Same subscription (default — RuriSkry and your workloads are in the same sub):**

| Role | Why it's needed |
|---|---|
| `Contributor` | Creates all RuriSkry resources — Container App, Cosmos DB, Key Vault, ACR, Foundry, etc. |
| `User Access Administrator` | Terraform must assign `Reader`, `Network Contributor`, and `VM Contributor` to RuriSkry's Managed Identity. Without this role, those `azurerm_role_assignment` resources fail with AuthorizationFailed. |
| `Monitoring Contributor` | Creates the Alert Processing Rule (APR) that routes subscription alerts to RuriSkry. Without this, the APR step fails and alerts never reach RuriSkry. |

> `Owner` = `Contributor` + `User Access Administrator` + all management plane actions. If your org grants Owner, all three above are covered automatically.

**Cross-subscription (RuriSkry deployed in sub A, scanning sub B):**

| Subscription | Roles needed |
|---|---|
| Deployment sub (A) | `Contributor` + `User Access Administrator` |
| Target sub (B) | `User Access Administrator` + `Monitoring Contributor` |

The target sub needs `User Access Administrator` because Terraform creates the `Reader`, `Network Contributor`, and `VM Contributor` role assignments there, and `Monitoring Contributor` because the APR lives in the target sub.

**Service Principal / CI-CD pipeline:**
If deploying via automation rather than personal login, create a Service Principal and assign it the same roles above. Avoid using `Owner` for automated pipelines — use the granular roles.

---

### RuriSkry's Managed Identity (granted automatically by Terraform — no manual steps needed)

Once deployed, RuriSkry governs your resources using its own Managed Identity, not your personal account. Terraform creates all these role assignments automatically during `terraform apply`.

**Important: scanning vs. direct remediation**

`Reader` covers every resource type in Azure — VMs, App Services, AKS, SQL, Storage, Cosmos, Function Apps, etc. RuriSkry can scan and make governance decisions on all of them.

Direct remediation (executing the action via Azure SDK) is intentionally limited to two resource types:

| Resource type | Can scan? | Can directly remediate? |
|---|---|---|
| Virtual Machines | ✓ | ✓ start / restart via `VM Contributor` |
| Network Security Groups | ✓ | ✓ add / modify / delete rules via `Network Contributor` |
| App Services, AKS, SQL, Storage, and everything else | ✓ | ✗ — proposes action + creates Terraform PR via Execution Gateway |

For all other resource types, RuriSkry raises a governance verdict and the Execution Gateway opens a Terraform PR in your IaC repo. A human reviews and merges it — the change happens through your normal IaC pipeline, not via direct Azure API calls. This is by design: direct API access is only granted for the two operations that are fast, reversible, and well-understood (restarting a VM, patching an NSG rule).

> **Known limitation — be aware before adopting.** If your environment is **not Terraform-managed** (no IaC repo, or you manage Azure via portal / `az` CLI / scripts), you currently have no automated remediation path for App Services, AKS, SQL, Storage, Function Apps, etc. RuriSkry will still scan and propose actions for these resource types, but you will have to execute the remediations manually based on its recommendations. Direct execution coverage will expand in a future release — track [the roadmap](https://github.com/psc0des/ruriskry/issues) or open an issue if a specific resource type is blocking your adoption.

**The five roles and which part of RuriSkry uses each:**

| Role | Scope | Used by | What it enables |
|---|---|---|---|
| `Reader` | Target subscription | All agents — scans all resource types for cost, security, availability, and deployment analysis. Also covers Azure Advisor, Defender for Cloud, and Azure Policy APIs. | Discovery and analysis across your entire subscription. |
| `Network Contributor` | Target subscription | Execution Agent — directly calls `NetworkManagementClient` to create/delete NSG rules when a security remediation is approved. | Live NSG rule patching without a Terraform PR. |
| `Virtual Machine Contributor` | Target subscription | Execution Agent — directly calls `ComputeManagementClient` to `start_vm` / `restart_vm` when a monitoring remediation is approved. | Live VM start/restart without a Terraform PR. |
| `Cognitive Services OpenAI User` | Foundry account (internal) | All four governance agents — required to call `gpt-4.1-mini` for every governance decision. Without this, all LLM decisions fail with 401. | LLM-based governance decisions. |
| `AcrPull` | ACR (internal) | Container App runtime — pulls the backend image on every start and restart. | Container startup. |
| `Cognitive Services OpenAI User` | Foundry account (internal) | Call the LLM (`gpt-4.1-mini`) to make governance decisions. Every agent scan that reaches the LLM step requires this. Without it, all LLM decisions fail with 401 PermissionDenied. |
| `AcrPull` | ACR (internal) | Pull the backend Docker image from the private registry. The Container App uses this on every start and restart. |

> **Note on scope:** `Reader`, `Network Contributor`, and `VM Contributor` are granted at subscription scope — meaning RuriSkry can govern resources across all resource groups in your subscription. If you want tighter scope (e.g., only a specific resource group), change the `scope` in `infrastructure/terraform-core/main.tf` on the relevant `azurerm_role_assignment` resources before deploying.

---

### Dashboard operators (day-to-day users)

No Azure permissions needed. Dashboard operators log in with the admin credentials created on first visit. All governance actions (scans, approvals, remediations) execute under RuriSkry's Managed Identity — operator accounts have no direct Azure access.

---

## Prerequisites

- Python 3.11+
- Node.js 18+ (for dashboard)
- Azure CLI (`az login` configured)
- Terraform 1.5+
- Azure subscription(s) with sufficient quota:

  | Quota | Sub | Default on new sub | Action if missing |
  |---|---|---|---|
  | Container Apps (6 vCPU, Consumption) | Core | Usually available | Request via portal |
  | gpt-4.1-mini Standard 150K TPM | Core | Pre-allocated on all new subs | Default — no request needed |
  | gpt-5-mini GlobalStandard 30K TPM | Core | Not pre-allocated | Request via ai.azure.com/quota (upgrade option) |
  | AI Search Free tier | Core | 1 per sub | Set `search_sku = "basic"` in tfvars if taken |
  | Cosmos DB Free tier | Core | 1 per sub | Set `cosmos_free_tier = false` in tfvars if taken |
  | Standard_B2ls_v2 (4 vCPUs) | Demo | 0 on new subs | Request standardBsv2Family quota increase |

  Estimated cost: ~$70–85/month (core ~$70, demo VMs ~$10 with nightly auto-shutdown)

- Docker Desktop — required to build and push the backend image (`scripts/deploy.sh` handles the build automatically)

## Infrastructure Is Terraform-Managed

Terraform in `infrastructure/terraform-core/` deploys (two providers: `hashicorp/azurerm` ~> 4.0 and `azure/azapi` ~> 2.0):

1. Azure Resource Group (`ruriskry-core-engine-rg`) — with a CanNotDelete management lock
2. Azure AI Foundry account (`azurerm_ai_services`)
3. Foundry model deployment (`azurerm_cognitive_deployment`, default `gpt-4.1-mini` version `2025-04-14`, Standard, 150K TPM)
4. **Foundry project** — fully Terraform-managed via AzAPI (`azapi_update_resource` to enable `allowProjectManagement`, `azapi_resource` to create the project). Set `create_foundry_project = true` in `terraform.tfvars`.
5. Azure AI Search
6. Azure Cosmos DB (SQL API) — seven containers, all Terraform-managed (`azurerm_cosmosdb_sql_container`): `governance-decisions` (partition `/resource_id`), `governance-agents` (partition `/name`), `governance-scan-runs` (partition `/agent_type`), `governance-alerts` (partition `/severity`), `governance-executions` (partition `/resource_id`), `resource-inventory` (partition `/subscription_id`), `governance-checkpoints` (partition `/id` — Phase 33C, stores scan-level workflow checkpoints for resume support). Managed Identity auth; no connection string stored in tfstate.
7. Azure Key Vault — purge protection enabled, 90-day soft-delete retention
8. Azure Log Analytics
9. Azure Container Registry (ACR) — admin disabled; Container App pulls via Managed Identity (`AcrPull` role)
10. Azure Container Apps Environment + Container App (backend)
11. Azure Static Web App (dashboard)

Security notes:
- ACR `admin_enabled = false` — credentials never appear in tfstate or env vars
- Foundry `local_authentication_enabled = false` — Managed Identity only; agents use `DefaultAzureCredential` so local dev (`az login`) still works unchanged. The Container App MI is granted the `Cognitive Services OpenAI User` role via `azurerm_role_assignment.foundry_openai_user` in Terraform — without this role, all agent scans fail with 401 PermissionDenied.
- The Container App MI is granted `Reader` at subscription scope (`azurerm_role_assignment.subscription_reader`). This single role covers all Microsoft API safety nets used by all three operational agents: Azure Advisor (`Microsoft.Advisor/recommendations/read`), Microsoft Defender for Cloud (`Microsoft.Security/assessments/read`), and Azure Policy (`Microsoft.PolicyInsights/policyStates/read`). No additional role assignments are needed for these APIs.
- Cosmos DB and Key Vault accessed via Managed Identity (no API keys in tfstate)
- Slack webhook stored as a Key Vault secret, injected via Container App secret mechanism
- CORS enforced at the FastAPI application layer using `DASHBOARD_URL` env var
- `terraform destroy` automatically removes the RG lock first (the lock `depends_on` all major resources, so Terraform destroys it before anything else)

Runtime secrets are read from Key Vault by default via `DefaultAzureCredential`.
In Azure, use Managed Identity. Locally, `az login` is used by the same credential chain.

## Path A — Cloud Deployment (Azure)

Deploy the full stack to Azure in one command. No local Python or Node.js runtime needed.

### Step 1 — Clone and login
```bash
git clone https://github.com/psc0des/ruriskry.git
cd ruriskry

# Login and set the CORE subscription (where RuriSkry will be deployed)
az login --use-device-code
az account set --subscription "<core-sub-id>"
az account show --query "{sub:id, name:name}" -o table   # confirm correct sub

# Register the Container Apps provider — required on new subscriptions
az provider register --namespace Microsoft.App --wait
```

### Step 2 — Configure tfvars

```bash
cp infrastructure/terraform-core/terraform.tfvars.example \
   infrastructure/terraform-core/terraform.tfvars
```

Edit `terraform.tfvars` — at minimum set:
```hcl
subscription_id = "xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx"   # az account show --query id -o tsv
suffix          = "<suffix>"   # see suffix rules below
```

**Choosing a suffix — read this before deploying:**

The suffix is appended to every Azure resource name (ACR, Key Vault, Container App, storage accounts, etc.). Several of these resource types have **globally unique** names across all of Azure — not just within your subscription. If two users pick the same suffix, the second deploy will fail with a `name already taken` error.

Rules:
- **Lowercase letters and digits only** — no hyphens, underscores, or uppercase (storage account name restriction)
- **6–10 characters** — shorter risks collision, longer risks hitting Azure name length limits
- **Must be globally unique** — use something personal and unlikely to be taken: your initials + a few digits works well (e.g. `jd4821`, `as9302`)
- **Do not use** generic words like `test`, `demo`, `dev`, `prod`, or `ruriskry` — these are almost certainly already taken

Resource types where the suffix must be globally unique:
| Resource | Name pattern | Azure scope |
|---|---|---|
| Storage account (Terraform state) | `ruriskrytf<suffix>` | Global |
| Key Vault | `ruriskry-core-kv-<suffix>` | Global |
| Azure Container Registry | `ruriskryacr<suffix>` | Global |
| Cognitive Services (Foundry) | `ruriskry-core-foundry-<suffix>` | Global |

**Cross-subscription scanning (optional):** if you want RuriSkry to scan a *different* subscription
than the one it deploys into (hub-spoke model), also set:
```hcl
target_subscription_id = "yyyyyyyy-yyyy-yyyy-yyyy-yyyyyyyyyyyy"
```
Terraform automatically creates Reader + Network Contributor + VM Contributor on that subscription —
no manual `az role assignment create` commands needed. Leave it commented out if both
RuriSkry and your resources are in the same subscription.

> **Permission required for cross-subscription:** the identity running `terraform apply`
> (your `az login` account) must have **Owner** or **User Access Administrator** on
> `target_subscription_id`. Without it, Terraform will fail when creating the RBAC assignments.

### Step 3 — Deploy everything

```bash
# Run in Git Bash (not PowerShell) from the repo root
bash scripts/deploy.sh
# If Stage 2 fails partway, resume without rebuilding:
#   bash scripts/deploy.sh --stage2
```

`deploy.sh` fully automates the deployment. It handles:
- **Azure provider registration** (Microsoft.App, etc.) — required once on new subscriptions
- **Soft-delete purge** — purges any soft-deleted Foundry account or Key Vault from previous failed deploys
- **Remote state storage** — creates `ruriskry-tfstate-rg` + storage account + `backend.hcl` if they don't exist
- **Foundry quota check** — fails fast with clear instructions if model quota is 0
- **Terraform init → Stage 1** (ACR + Managed Identity)
- **Docker build + push** to ACR
- **Stage 2** full infra apply (Container App, Cosmos DB, Key Vault, Static Web App)
- **Dashboard build + deploy** to Azure Static Web Apps
- **Health check**
- **Alert rule wiring** — sweeps `target_subscription_id` for existing alert rules and offers to add the RuriSkry Action Group to each one

**Cleaning up / retrying a failed deploy:**
```bash
bash scripts/cleanup.sh        # delete main RG + monitor RG (target sub) + purge soft-deleted (keeps tfstate)
bash scripts/cleanup.sh --all  # full wipe including tfstate storage
bash scripts/deploy.sh         # fresh deploy after cleanup
```

When it finishes:

```
Dashboard  →  https://<app>.azurestaticapps.net
Backend    →  https://ruriskry-core-backend-<suffix>.<hash>.eastus2.azurecontainerapps.io
```

Terraform injects all environment variables into the Container App automatically — no `.env`
file is needed for cloud deployment.

### Step 4 — First login

Open the dashboard URL. Because no admin account exists yet, you will see the **one-time setup screen**:

1. Choose a username and password (minimum 8 characters)
2. Click **Create account** — you are logged in immediately
3. A two-step **onboarding guide** appears: Step 1 scans your Azure inventory, Step 2 navigates to the Agents page to run your first governance scan. You can skip it at any time by clicking X.
4. All future visits show a standard **login screen** instead

Admin credentials are stored in two durable layers:
- **Cosmos DB** (`governance-agents` container, document `_admin_auth`) — survives container restarts and revision deployments
- **`/app/data/admin_auth.json`** — local cache for fast startup reads

Sessions expire after 8 hours.

**Forgot the admin password?** Use the `--reset-admin` flag. This clears both the Cosmos record and the local file, then restarts the container so the setup screen reappears:
```bash
bash scripts/deploy.sh --reset-admin
```
Then visit the dashboard URL and create a new admin account.

No additional environment variables are needed for auth — it is always enabled once an
admin account has been created.

**Get URLs after deployment:**
```bash
terraform -chdir=infrastructure/terraform-core output backend_url
terraform -chdir=infrastructure/terraform-core output dashboard_url
```

### Step 5 — Wire your workload infrastructure to RuriSkry

RuriSkry receives Azure Monitor alerts via a webhook endpoint:

```
POST https://<your-backend>/api/alert-trigger
```

Any Azure subscription can send alerts to this endpoint — not just `terraform-demo`. Choose the method that matches how your workload infrastructure is managed:

#### Option A — Terraform (recommended, automatic)

`terraform apply` creates `azurerm_monitor_alert_processing_rule_action_group.ruriskry` — one APR scoped to the entire target subscription. The APR routes every current and future alert rule to the RuriSkry action group automatically. No per-rule or per-action-group wiring needed. The APR is owned by Terraform state and is tied to no personal identity — it survives staff changes and subscription ownership transfers.

```
All alert rules in target subscription
    └── Alert Processing Rule "apr-ruriskry-governance-fanout"
            └── Action Group "ag-ruriskry-*-alert-handler-*"  (in infra sub)
                    └── Webhook → POST /api/alert-trigger
```

If `terraform apply` fails at the APR resource (`AuthorizationFailed`), the deploying identity lacks `Monitoring Contributor` on the target subscription. Grant it once and re-apply:
```bash
terraform apply -chdir=infrastructure/terraform-core \
  -target=azurerm_monitor_alert_processing_rule_action_group.ruriskry
```

`deploy.sh` Step 9 reports APR status (present / missing) — it no longer creates it.

#### Option B — terraform-demo (test/demo environment)

To wire `terraform-demo` manually:

```bash
BACKEND_URL=$(terraform -chdir=infrastructure/terraform-core output -raw backend_url)

# Set in infrastructure/terraform-demo/terraform.tfvars:
#   alert_webhook_url = "<BACKEND_URL>/api/alert-trigger"

cd infrastructure/terraform-demo
terraform apply -target=azurerm_monitor_action_group.prod
```

#### Option C — Terraform-managed production infrastructure

Add the backend URL to your existing Terraform action group resource:

```hcl
resource "azurerm_monitor_action_group" "alerts" {
  name                = "ruriskry-alerts"
  resource_group_name = var.resource_group_name
  short_name          = "ruriskry"

  webhook_receiver {
    name                    = "ruriskry-backend"
    service_uri             = "https://<your-backend>/api/alert-trigger"
    use_common_alert_schema = false
  }
}
```

#### Option C — ARM/Bicep-managed infrastructure

```json
{
  "type": "Microsoft.Insights/actionGroups",
  "properties": {
    "webhookReceivers": [{
      "name": "ruriskry-backend",
      "serviceUri": "https://<your-backend>/api/alert-trigger",
      "useCommonAlertSchema": false
    }]
  }
}
```

#### Option D — Existing infrastructure (no IaC / click-ops)

Azure Portal → **Monitor** → **Action Groups** → **+ Create**
- Action type: **Webhook**
- URI: `https://<your-backend>/api/alert-trigger`
- Enable common alert schema: **No**

Or via CLI:
```bash
BACKEND_URL=$(terraform -chdir=infrastructure/terraform-core output -raw backend_url)

az monitor action-group create \
  --name ruriskry-alerts \
  --resource-group <your-rg> \
  --short-name ruriskry \
  --action webhook ruriskry-webhook "$BACKEND_URL/api/alert-trigger"
```

Then attach this action group to your existing alert rules:
```bash
az monitor metrics alert update \
  --name <your-alert-rule> \
  --resource-group <your-rg> \
  --add-action ruriskry-alerts
```

> **`use_common_alert_schema = false`** — RuriSkry's alert normaliser expects the richer
> non-common schema. Setting this to `true` will cause resource extraction to fail.

---

## Path B — Local Development

Run the backend and dashboard locally against your Azure infrastructure (or in mock mode without Azure).

```bash
# 1. Clone and install
git clone https://github.com/psc0des/ruriskry.git
cd ruriskry
python -m venv .venv
source .venv/bin/activate       # Linux/Mac
# .venv\Scripts\activate        # Windows
pip install -r requirements.txt

# 2. Run tests — no Azure credentials needed (mock mode)
pytest tests/ -v
# Expected: 1112 passed, 0 failed

# 3a. Mock mode (no Azure needed) — set in .env
echo "USE_LOCAL_MOCKS=true" > .env

# 3b. Live mode — pull Azure endpoints from Terraform outputs into .env
#     Requires: deploy.sh already run (Azure infra provisioned)
bash scripts/setup_env.sh
# Optional: write plaintext API keys into .env for offline use
# bash scripts/setup_env.sh --include-keys

# 4. Start the backend
uvicorn src.api.dashboard_api:app --reload
# → http://localhost:8000

# 5. Start the React dashboard (separate terminal)
cd dashboard && npm install && npm run dev
# → http://localhost:5173
#
# First visit: one-time admin setup screen (pick username + password ≥8 chars)
# Subsequent visits: standard login screen
# Logout: "Sign out" button in the top-right of the header

# 6. Optional: run as MCP server (for Claude Desktop)
python -m src.mcp_server.server

# 7. Optional: run as A2A server (agent-to-agent protocol)
uvicorn src.a2a.ruriskry_a2a_server:app --host 0.0.0.0 --port 8000

# 8. Optional: demo scripts
python examples/examples/demo.py        # direct Python pipeline — 3 governance scenarios
python examples/examples/demo_a2a.py    # A2A protocol demo — server + 3 agent clients
python examples/examples/demo_live.py   # live Azure scan (requires USE_LOCAL_MOCKS=false + Azure creds)
python examples/examples/demo_live.py --resource-group ruriskry-prod-rg  # scope to a specific RG
```

## Post-Deploy: Connect Your Real Infrastructure

After `deploy.sh` completes, RuriSkry is running and has the right permissions on your subscription. But it only receives governance triggers when Azure Monitor alerts fire. You need to configure alert rules on your existing resources — RuriSkry cannot do this for you because it doesn't know what resources you have.

### What you need to set up

**Alert rules** on each resource you want RuriSkry to govern. When these fire, the Alert Processing Rule (created by Terraform) automatically routes them to RuriSkry.

Your backend's alert webhook URL is printed at the end of `deploy.sh`. You can also get it anytime:
```bash
terraform -chdir=infrastructure/terraform-core output -raw backend_url
# Webhook: <backend_url>/api/alert-trigger
```

### Common alert rules to create

**VM stopped unexpectedly:**
```bash
az monitor activity-log alert create \
  --name "ruriskry-vm-stopped" \
  --resource-group <your-rg> \
  --condition category=Administrative operationName=Microsoft.Compute/virtualMachines/deallocate/action \
  --action-group $(az monitor action-group show -g ruriskry-monitor-rg-<suffix> -n ag-ruriskry-prod --query id -o tsv)
```

**NSG rule changed:**
```bash
az monitor activity-log alert create \
  --name "ruriskry-nsg-change" \
  --resource-group <your-rg> \
  --condition category=Administrative operationName=Microsoft.Network/networkSecurityGroups/securityRules/write \
  --action-group $(az monitor action-group show -g ruriskry-monitor-rg-<suffix> -n ag-ruriskry-prod --query id -o tsv)
```

**High CPU on a VM:**
```bash
az monitor metrics alert create \
  --name "ruriskry-high-cpu" \
  --resource-group <your-rg> \
  --scopes <vm-resource-id> \
  --condition "avg Percentage CPU > 80" \
  --window-size 5m \
  --evaluation-frequency 1m \
  --action $(az monitor action-group show -g ruriskry-monitor-rg-<suffix> -n ag-ruriskry-prod --query id -o tsv)
```

You can also create these in **Azure Portal → Monitor → Alerts → Create alert rule** — point the action group at `ag-ruriskry-prod` in resource group `ruriskry-monitor-rg-<suffix>`.

### What you need

`Monitoring Contributor` on the resource group or subscription where your resources live. This is separate from deploying RuriSkry — it's your own infrastructure that you're adding alert rules to.

### You do NOT need to configure the webhook manually

The Alert Processing Rule (APR) created by Terraform already intercepts all alerts in your subscription and forwards them to RuriSkry. You only need to point alert rules at any action group — the APR handles the rest.

---

## Optional: Deploy Demo Environment

**What this is:** `infrastructure/terraform-demo/` spins up a small set of real Azure
resources (VMs, NSG, storage, App Service) that RuriSkry governs. It lets you see the
full governance loop — scan → decision → remediation — against real infrastructure without
needing your own workloads. This is completely separate from the core deploy and has no
effect on it.

**Prerequisites:** complete the core deploy first (`bash scripts/deploy.sh`) so you have
a backend URL.

**Step 1 — get your backend URL:**
```bash
terraform -chdir=infrastructure/terraform-core output -raw backend_url
# e.g. https://ruriskry-core-backend-abc123.eastus2.azurecontainerapps.io
```

**Step 2 — configure and deploy:**
```bash
cd infrastructure/terraform-demo
cp terraform.tfvars.example terraform.tfvars
```

Edit `terraform.tfvars` and fill in:
```hcl
subscription_id   = "xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx"  # az account show --query id -o tsv
suffix            = "jd4821"          # same rules as core suffix — globally unique, 6-10 chars
vm_admin_password = "Str0ng!Pass99"   # 12+ chars, upper + lower + digit + symbol
alert_email       = "you@example.com"
alert_webhook_url = "https://<your-backend-url>/api/alert-trigger"  # from Step 1
```

Then deploy:
```bash
terraform init
terraform apply
```

**Step 3 — start the demo VMs** (auto-shutdown stops them at 22:00 UTC daily):
```bash
az vm start --resource-group ruriskry-prod-rg --name vm-dr-01
az vm start --resource-group ruriskry-prod-rg --name vm-web-01
```

**Tear down when done** (~$0.35/day while VMs are running):
```bash
terraform destroy
```

Resources created and their governance roles:

| Resource | Demo Scenario | Expected Verdict |
|---|---|---|
| `vm-dr-01` (B2ls_v2) | Cost agent proposes DELETE (idle DR VM) | DENIED — `disaster-recovery=true` policy |
| `vm-web-01` (B2ls_v2) | Monitoring agent proposes SCALE UP (CPU >80%) — cloud-init stress cron fires automatically | APPROVED — safe action |
| `payment-api-prod` (App Service F1, free) | Critical dependency of vm-web-01 | Raises blast radius score |
| `nsg-east-prod` (NSG) | Deploy agent proposes open port 8080 | ESCALATED — affects all governed workloads |
| `ruriskryprod{suffix}` (Storage) | Shared dependency of all three above | Deletion → high blast radius |

See `infrastructure/terraform-demo/README.md` for full detail including cost estimates.

---

## Optional: Scanning Multiple Subscriptions

By default RuriSkry scans one subscription (`target_subscription_id` or `subscription_id`).
For organisations with many subscriptions, three approaches are available.

### Which option to choose

| Scenario | Recommendation |
|---|---|
| 1 subscription | `target_subscription_id` in tfvars — Terraform handles it automatically |
| 2–4 subscriptions, same tenant | Option 2 — per-subscription role assignment loop |
| 5+ subscriptions, same tenant | **Option 1 — Management Group scope** (simpler even at 5 subs) |
| Cross-tenant / MSP | Option 3 — Azure Lighthouse |

---

### Option 1 — Azure Management Group *(recommended for 5+ subscriptions)*

Assign RuriSkry's Managed Identity roles at the **management group** level once.
Azure Resource Graph queries all subscriptions under that group in a single call.
New subscriptions added under the MG are covered automatically — no config changes ever.

**Which management group to target:**
Do NOT assign at the tenant root management group — that would expose Platform subscriptions
(the subscription hosting RuriSkry itself, connectivity, identity) to the scanner.
Target the **"Landing Zones"** management group (or equivalent) that contains only workload
subscriptions. This follows the [Azure Landing Zone](https://learn.microsoft.com/en-us/azure/cloud-adoption-framework/ready/landing-zone/design-area/resource-org-management-groups) least-privilege pattern.

**Roles required:**
- `Reader` — always required (resource scanning, Resource Graph, Advisor, Defender, Policy)
- `Network Contributor` + `Virtual Machine Contributor` — only needed when `EXECUTION_GATEWAY_ENABLED=true` (direct remediation). For scan-only deployments, `Reader` alone is sufficient.

**Steps:**
```bash
PRINCIPAL=$(terraform -chdir=infrastructure/terraform-core output -raw backend_container_app_principal_id)
MG_ID="mg-landing-zones"   # your workloads management group ID — NOT the tenant root

# Always needed
az role assignment create --role "Reader" \
  --assignee $PRINCIPAL \
  --scope /providers/Microsoft.Management/managementGroups/$MG_ID

# Only if EXECUTION_GATEWAY_ENABLED=true
az role assignment create --role "Network Contributor" \
  --assignee $PRINCIPAL \
  --scope /providers/Microsoft.Management/managementGroups/$MG_ID

az role assignment create --role "Virtual Machine Contributor" \
  --assignee $PRINCIPAL \
  --scope /providers/Microsoft.Management/managementGroups/$MG_ID
```

> **Wait 5–30 minutes** after creating MG-scope role assignments before running the first scan.
> Azure caches management group hierarchy for up to 30 minutes — tokens issued before the
> cache refreshes will not reflect the new assignments.

> **Limit:** 500 role assignments per management group (Azure hard limit). RuriSkry uses 3.
> This only becomes relevant for organisations with hundreds of other service principals also
> assigned at the same MG level.

**One-line code change to unlock MG scanning** — update `resource_graph.py`:
```python
# Current (single subscription)
subscriptions=[settings.azure_subscription_id]

# After (management group — set AZURE_MANAGEMENT_GROUP_ID env var)
management_groups=[settings.azure_management_group_id]
```
Also add `azure_management_group_id: str = ""` to `config.py` and set
`AZURE_MANAGEMENT_GROUP_ID` in the Container App environment variables.

**For incremental rollouts** (not all subs under the MG are ready yet), add
`options=QueryRequestOptions(allow_partial_scopes=True)` to the `ResourceGraphClient` call.
This returns results for accessible subscriptions instead of failing with 403 on the ones
not yet covered.

---

### Option 2 — Per-subscription role assignments *(2–4 subscriptions)*

For a small, stable set of subscriptions, grant assignments directly on each:

```bash
PRINCIPAL=$(terraform -chdir=infrastructure/terraform-core output -raw backend_container_app_principal_id)

for SUB_ID in "aaaa-..." "bbbb-..." "cccc-..."; do
  az role assignment create --role "Reader" \
    --assignee $PRINCIPAL --scope /subscriptions/$SUB_ID
  # Add Network Contributor + VM Contributor only if EXECUTION_GATEWAY_ENABLED=true
done
```

> Avoid querying subscriptions one-at-a-time in a loop — Azure Resource Graph throttles at
> 15 queries per 5-second window. Group all subscription IDs into a single query call instead.

---

### Option 3 — Azure Lighthouse *(cross-tenant only)*

Use Lighthouse **only** when subscriptions belong to a different Entra tenant (e.g. an MSP
governing customer environments, or subscriptions from an acquired company). Within a single
tenant, management group RBAC (Option 1) is simpler and fully supported.

Lighthouse works by deploying an ARM template into the managed tenant that delegates specific
roles to your tenant's principal. After onboarding, RuriSkry's Managed Identity sees those
subscriptions as if they were in your own tenant — no code changes to the scanning logic.
Each customer/tenant requires one ARM template deployment (one-time, per tenant).

---

## Optional: Wire Azure Monitor Alerts (real-time governance)

RuriSkry agents run periodic scans, but you can also wire Azure Monitor alerts
so any infrastructure event (VM stops, CPU spikes, disk fills) creates a
**pending alert** in the dashboard for manual investigation.  Click
**Investigate** on any pending alert row to trigger the Monitoring Agent.

**Terraform automates this**: `azurerm_monitor_alert_processing_rule_action_group.ruriskry` in `terraform-core` creates one APR scoped to the entire target subscription — all current and future alert rules route to RuriSkry automatically. `deploy.sh` Step 9 reports whether the APR is in place. If it is missing, re-apply the target:

```bash
terraform apply -chdir=infrastructure/terraform-core \
  -target=azurerm_monitor_alert_processing_rule_action_group.ruriskry
```

See [`docs/alert-wiring.md`](alert-wiring.md) for:
- What is wired automatically vs what requires manual steps
- Step-by-step: adding a new VM (AMA, DCR association, alert rules)
- Adding alerts for non-VM resources (storage, databases, Container Apps)
- Large-environment approach using Azure Policy for automatic coverage
- Troubleshooting common wiring problems

---

## Optional: Configure Slack Notifications (Phase 17)

RuriSkry can post a Slack message to a channel whenever a verdict is DENIED or ESCALATED.
Zero config required to run without it — just leave `SLACK_WEBHOOK_URL` empty.

See [`docs/slack-setup.md`](slack-setup.md) for the full step-by-step Slack app creation guide.

**Set the env var (local development):**
```bash
# .env (local only — in deployed environments the webhook URL is stored as a Key Vault secret
#        and injected via the Container App secret mechanism, not as a plain env var)
SLACK_WEBHOOK_URL=https://hooks.slack.com/services/...
SLACK_NOTIFICATIONS_ENABLED=true
DASHBOARD_URL=http://localhost:5173   # URL in the "View in Dashboard" button
```

**Test it:**
```bash
# With the API running, click the green 🔔 Slack button in the dashboard header
# — or call the endpoint directly:
curl -X POST http://localhost:8000/api/test-notification
```

This sends a realistic sample DENIED message for `vm-dr-01` (SRI 77.0, POL-DR-001 violation).
If the webhook URL is empty or wrong, the endpoint returns `{"status": "skipped"}` or
`{"status": "failed"}` — it never crashes the API.

---

## Optional: Decision Explanation Drilldown (Phase 18)

No setup required — the explanation engine runs automatically. Click any row in the
Live Activity Feed to open the 6-section drilldown:

- SRI™ dimensional bars (which dimension drove the verdict)
- Plain-English explanation generated by the LLM (or a template in mock mode)
- Counterfactual analysis — "what would change this outcome?"
- Per-governance-agent reasoning
- Collapsible raw JSON audit trail

In mock mode (`USE_LOCAL_MOCKS=true`) the LLM summary is replaced by a deterministic
template string — the rest of the drilldown (factors, counterfactuals, violations) is always
fully computed regardless of mode.

---

## Optional: Execution Gateway & HITL (Phase 21)

The Execution Gateway routes APPROVED verdicts to IaC-safe paths — generating Terraform PRs
instead of directly modifying Azure resources. This prevents IaC state drift.

**Step 1 — Add IaC tags to your Terraform resources (recommended):**

All resources in `infrastructure/terraform-demo/main.tf` benefit from:
```hcl
tags = {
  managed_by = "terraform"
  iac_repo   = "your-org/ruriskry"
  iac_path   = "infrastructure/terraform-demo"
}
```

Run `terraform apply` to push the tags to Azure. Tags are **optional** — the PR overlay
lets you select the correct repo at click-time even if tags are missing or wrong.

**Step 2 — Create a GitHub Personal Access Token:**
1. GitHub → Settings → Developer settings → Personal access tokens → Fine-grained tokens
2. Repository access: select **only your IaC repo** (e.g. `yourname/ruriskry-iac-test`) — do not grant access to the RuriSkry backend repo or any unrelated repos
3. Permissions: Contents (Read & Write), Pull requests (Read & Write)
4. Copy the token

> **Security note:** The governance engine will only show repos matching `IAC_GITHUB_REPO` in the dropdown regardless of token scope, but scoping the PAT narrowly is defence-in-depth — it prevents the engine from ever creating PRs against unintended repos even if misconfigured.

**Step 3 — Store the PAT:**

In a production (Azure) deployment, `deploy.sh` prompts for the PAT during deployment and
stores it in Key Vault automatically. The Container App reads it via Managed Identity —
it never appears in `.env` or Terraform state.

For local development only:
```bash
# .env
GITHUB_TOKEN=ghp_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
IAC_GITHUB_REPO=owner/ruriskry
IAC_TERRAFORM_PATH=infrastructure/terraform-demo
EXECUTION_GATEWAY_ENABLED=true
```

**Step 4 — Test it:**
Run a scan from the dashboard. When an APPROVED verdict is issued, click
**Create Terraform PR** in the drilldown panel. A confirmation overlay opens:
- Shows the auto-detected repo and path (from resource tags or `IAC_GITHUB_REPO` setting)
- Lets you search all repos accessible via your PAT to select a different one
- Lets you edit the Terraform path before confirming

After confirming, a PR is opened in the selected repo with the proposed Terraform change.
Check the drilldown panel for execution status and a link to the PR.

See `infrastructure/terraform-core/deploy.md` for full implementation guide.

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
Terraform from the other provisioned resources. Secrets (API keys, Slack webhook URL) are
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

The 4 governance agents run as Python coroutines in the same event loop, sharing a single
process. Since Phase 33D the default execution path is the **WorkflowBuilder graph** — a
7-executor typed pipeline (`DispatchExecutor` → parallel fan-out → `ScoringExecutor` →
`ConditionGateExecutor`) that replaces the legacy `asyncio.gather()` call. There is no
inter-service HTTP, no message broker, no service mesh. This gives:

- True parallelism without network overhead (fan-out executors run concurrently)
- Single deployment unit — one `docker build`, one `az containerapp update`
- No distributed tracing setup needed for the core evaluation path
- Structured SSE streaming per-agent as the workflow progresses (Phase 33B)

Set `USE_WORKFLOWS=false` to fall back to the legacy `asyncio.gather()` path (deprecated —
will be removed in a future release). Scale by increasing Container App CPU/memory limits
and replica count.

---

## Environment Variables

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `USE_LOCAL_MOCKS` | No | `false` | `false` = live Azure (production default). `true` = local JSON files for offline dev. Startup logs a prominent `⚠ MOCK MODE ACTIVE` warning when true. |
| `USE_LIVE_TOPOLOGY` | No | `false` | `true` = governance agents query Azure Resource Graph for real dependency topology and SKU cost (Phase 19). Only effective when `USE_LOCAL_MOCKS=false` and `AZURE_SUBSCRIPTION_ID` is set. |
| `USE_WORKFLOWS` | No | `true` | `true` (default as of Phase 33D) = pipeline uses the 7-executor WorkflowBuilder graph with ConditionGate and checkpointing. `false` = legacy `asyncio.gather()` path (deprecated — will be removed in a future release). |
| `AZURE_OPENAI_ENDPOINT` | Live only | — | Foundry endpoint URL |
| `AZURE_OPENAI_DEPLOYMENT` | Live only | `gpt-4.1-mini` | Model deployment name (set via `foundry_deployment_name` in terraform.tfvars) |
| `AZURE_SEARCH_ENDPOINT` | Live only | — | Azure AI Search endpoint |
| `AZURE_SEARCH_INDEX` | Live only | `incident-history` | Search index name |
| `COSMOS_ENDPOINT` | Live only | — | Cosmos DB endpoint |
| `COSMOS_DATABASE` | Live only | `ruriskry` | Database name |
| `COSMOS_CONTAINER_DECISIONS` | Live only | `governance-decisions` | Container for verdict audit trail |
| `COSMOS_CONTAINER_SCAN_RUNS` | Live only | `governance-scan-runs` | Container for scan-run records (Terraform-managed) |
| `COSMOS_CONTAINER_ALERTS` | Live only | `governance-alerts` | Container for alert investigation records (Terraform-managed) |
| `COSMOS_CONTAINER_EXECUTIONS` | Live only | `governance-executions` | Container for HITL execution records — survives deployments (Terraform-managed) |
| `COSMOS_CONTAINER_INVENTORY` | Live only | `resource-inventory` | Container for resource inventory snapshots — partition key `/subscription_id` (Terraform-managed) |
| `COSMOS_CONTAINER_AGENTS` | Live only | `governance-agents` | Container for agent registration records and admin auth backup (Terraform-managed) |
| `COSMOS_CONTAINER_CHECKPOINTS` | Live only | `governance-checkpoints` | Container for scan-level workflow checkpoints — enables `POST /api/scan/{id}/resume` to re-evaluate from where a scan left off (Phase 33C, Terraform-managed) |
| `INVENTORY_STALE_HOURS` | No | `24` | Hours after which an inventory snapshot is considered stale. Dashboard shows an amber warning and recommends refreshing before scans. |
| `AZURE_SUBSCRIPTION_ID_DISPLAY` | No | `""` | Optional human-friendly label shown next to the subscription ID in the Inventory page header. |
| `DEMO_MODE` | No | `false` | `true` = ops agents return hardcoded sample proposals (no Azure OpenAI needed). Full governance pipeline still runs. |
| `SLACK_WEBHOOK_URL` | No | `""` | Slack Incoming Webhook URL. Empty = notifications disabled (zero-config default). |
| `SLACK_NOTIFICATIONS_ENABLED` | No | `true` | Master on/off switch for Slack notifications. Has no effect if `SLACK_WEBHOOK_URL` is empty. |
| `SLACK_TIMEOUT` | No | `10` | HTTP timeout (seconds) for each Slack webhook POST. Covers connect + read. Increase only if your network to Slack is consistently slow. |
| `DASHBOARD_URL` | No | `http://localhost:5173` | URL embedded in the "View in Dashboard" button on Slack Block Kit messages. In production this is set automatically from Terraform output. |
| `AZURE_KEYVAULT_URL` | Live only | — | Key Vault URL for secret resolution |
| `DEFAULT_RESOURCE_GROUP` | No | `""` | Default Azure resource group for dashboard scan endpoints. Empty = scan whole subscription. Body `resource_group` overrides this. |
| `GITHUB_TOKEN` | Phase 21 | `""` | GitHub PAT with repo write access (Contents + Pull requests). Required for Terraform PR generation. Both classic PATs and fine-grained PATs are supported. For fine-grained PATs, grant **Contents** (read/write) + **Pull requests** (read/write) on the IaC repo — Metadata is implicitly included. |
| `IAC_GITHUB_REPO` | Phase 21 | `""` | GitHub repo for IaC PRs (e.g. `your-org/ruriskry`). |
| `IAC_TERRAFORM_PATH` | Phase 21 | `infrastructure/terraform-demo` | Path within the repo to the Terraform config directory. |
| `EXECUTION_GATEWAY_ENABLED` | No | `false` | Enable the Execution Gateway. When `false`, verdicts are informational only (no PRs created). |
| `API_KEY` | No | `""` | When set, all mutating POST/PATCH endpoints (except `/api/alert-trigger`) require `X-API-Key: <value>` header. GET endpoints stay open. Generate: `python -c "import secrets; print(secrets.token_hex(32))"`. |
| `ALERT_WEBHOOK_SECRET` | No | `""` | When set, `POST /api/alert-trigger` requires `Authorization: Bearer <value>` header. Set the same value in the Azure Monitor Action Group "Secure Webhook" field. |
| `PR_BRANCH_PREFIX` | No | `ruriskry/approved` | Branch name prefix for governance-approved Terraform PRs. Full branch: `<prefix>/<resource>-<action_id[:8]>`. |
| `SERVICE_NAME` | No | `ruriskry-backend` | Value returned in the `GET /` health check `service` field. Override for multi-tenant or renamed deployments. |
| `LLM_TIMEOUT` | No | `600` | Hard timeout (seconds) for any single agentic LLM call. Applied at two layers: (1) each individual HTTP request to Azure OpenAI, (2) the entire `agent.run()` agentic loop via `asyncio.wait_for`. Multi-step agent loops need >300s; 600s is the production-tested minimum. Scans that exceed this limit set `scan_error` and show a red Error badge. |
| `LLM_CONCURRENCY_LIMIT` | No | `6` | Maximum simultaneous LLM calls across all agents (3 operational + 4 governance + execution share one semaphore). `6` is safe at 150K TPM (default gpt-4.1-mini Standard allocation). Set lower only if hitting 429 errors. |
| `ORG_NAME` | No | `Contoso` | Display name for your organisation — used in triage context and future reporting. |
| `ORG_RESOURCE_COUNT` | No | `0` | Approximate total Azure resources under management. Used by risk triage for scale-aware context. |
| `ORG_COMPLIANCE_FRAMEWORKS` | No | `""` | Comma-separated compliance frameworks in scope (e.g. `HIPAA,PCI-DSS,SOC2`). Any production resource is treated as compliance-scoped when this is non-empty, routing it to Tier 3 governance. |
| `ORG_RISK_TOLERANCE` | No | `moderate` | Organisation-wide risk posture: `conservative`, `moderate`, or `aggressive`. Informs triage context; `conservative` is recommended for regulated industries. |
| `ORG_BUSINESS_CRITICAL_RGS` | No | `""` | Comma-separated resource group names that contain P0 workloads (e.g. `rg-prod-payments,rg-prod-identity`). Actions targeting these RGs are always scoped as compliance-relevant (Tier 3 minimum). |
