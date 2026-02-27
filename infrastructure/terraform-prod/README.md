# Mini Production Environment — Terraform

This folder creates a **real Azure environment** that SentinelLayer governs in live demos.
Every resource here has a specific role in the governance story.

---

## What Gets Deployed and Why

| Resource | Type | Purpose | Expected Verdict |
|---|---|---|---|
| `vm-dr-01` | Linux VM B1s | Idle disaster-recovery standby | **DENIED** (DR policy + high blast radius) |
| `vm-web-01` | Linux VM B1s | Active web server under CPU load | **APPROVED** (safe scale-up, no policy violations) |
| `payment-api-prod-<suffix>` | App Service F1 | Payment microservice | Critical dependency (raises blast radius of vm-web-01 actions) |
| `nsg-east-prod` | Network Security Group | Subnet gateway for both VMs | **ESCALATED** (opening port 8080 affects all governed workloads) |
| `sentinelprod<suffix>` | Storage Account LRS | Shared dependency for all three | Deletion would cascade to all three resources |
| Auto-shutdown | Dev/Test Schedule | Shutdown VMs at 22:00 UTC | Saves ~$1/day while not demoing |
| CPU alert | Monitor Metric Alert | Fires when vm-web-01 CPU > 80% | Triggers monitoring agent → scale-up proposal |
| Heartbeat alert | Scheduled Query Alert | Fires when vm-dr-01 goes silent | Triggers cost agent → deletion proposal |

---

## Governance Demo Scenarios

### Scenario 1 — DENIED: Cost Agent Tries to Delete vm-dr-01

```
Cost agent: "vm-dr-01 has been idle for 30+ days. Proposing deletion. Savings: $15/month."
SentinelLayer evaluates:
  SRI:Policy      = 90  ← disaster-recovery=true tag → protected resource violation
  SRI:Blast Radius= 75  ← dr-failover-service and backup-coordinator depend on it
  SRI:Historical  = 80  ← similar DR deletions caused 2h outages
  SRI:Cost        = 10  ← saving money is good, but not at this risk
  SRI Composite   = 78  ← DENIED (threshold: >60)
```

### Scenario 2 — APPROVED: SRE Agent Scales Up vm-web-01

```
Monitoring agent: "vm-web-01 CPU at 87% for 15 minutes. Proposing scale-up to Standard_B2s."
SentinelLayer evaluates:
  SRI:Policy      = 10  ← no policy violations (not a protected resource)
  SRI:Blast Radius= 15  ← vm-web-01 has no critical downstream services
  SRI:Historical  = 5   ← web VM scaling has zero incident history
  SRI:Cost        = 20  ← slight cost increase, acceptable for availability
  SRI Composite   = 12  ← APPROVED (threshold: ≤25)
```

### Scenario 3 — ESCALATED: Deploy Agent Opens Port 8080 on nsg-east-prod

```
Deploy agent: "Opening port 8080 on nsg-east-prod for new microservice."
SentinelLayer evaluates:
  SRI:Policy      = 55  ← NSG changes need human review (managed-by=platform-team)
  SRI:Blast Radius= 60  ← nsg-east-prod governs both vm-dr-01 and vm-web-01
  SRI:Historical  = 40  ← similar NSG changes caused brief connectivity issues
  SRI:Cost        = 5   ← no cost impact
  SRI Composite   = 43  ← ESCALATED (threshold: 26-60, human review required)
```

---

## Prerequisites

- Terraform 1.5+
- Azure CLI — run `az login` first
- Azure subscription with enough quota for 2× Standard_B1s VMs
- About $2–5 of Azure credits (VMs auto-shutdown nightly)

---

## Deploy

```bash
# 1. Go to this folder
cd infrastructure/terraform-prod

# 2. Copy and fill in your variables
cp terraform.tfvars.example terraform.tfvars
# Edit terraform.tfvars with your subscription ID, suffix, password, email

# 3. Initialize Terraform (downloads the Azure provider)
terraform init

# 4. Preview what will be created
terraform plan

# 5. Create the resources (takes ~5 minutes)
terraform apply

# 6. Copy real resource IDs into seed_resources.json
terraform output seed_resources_ids
```

---

## After Deployment — Update seed_resources.json

After `terraform apply`, run:

```bash
terraform output seed_resources_ids
```

Copy the real Azure resource IDs into `data/seed_resources.json`. This makes SentinelLayer's
mock Azure Resource Graph point to the actual resources, so the dashboard shows real IDs.

---

## Destroy (When You're Done)

```bash
# From infrastructure/terraform-prod/
terraform destroy
```

This removes all resources and stops all charges. Always run this after the demo.

---

## Cost Estimate

| Resource | Cost while running | With auto-shutdown (8h/day) |
|---|---|---|
| vm-dr-01 (B1s) | $0.021/hour | ~$0.17/day |
| vm-web-01 (B1s) | $0.021/hour | ~$0.17/day |
| App Service F1 | FREE | FREE |
| Storage LRS 1GB | ~$0.002/day | ~$0.002/day |
| Log Analytics | pay-per-GB | minimal for demo |
| **Total** | — | **~$0.35/day** |

Auto-shutdown is configured at 22:00 UTC. Remember to start VMs manually before a demo:
```bash
az vm start --resource-group sentinel-prod-rg --name vm-dr-01
az vm start --resource-group sentinel-prod-rg --name vm-web-01
```

---

## File Map

```
infrastructure/terraform-prod/
├── main.tf                   ← All resources (VMs, NSG, storage, alerts, App Service)
├── variables.tf              ← Input variable definitions
├── outputs.tf                ← Exports all resource IDs, names, tags, URLs
├── terraform.tfvars.example  ← Template for your terraform.tfvars (gitignored)
└── README.md                 ← This file
```

---

## Notes

- **Heartbeat alerts** require the Azure Monitor Agent to be installed on each VM.
  Without it, the heartbeat alert will always be in "No data" state.
  To install: `az vm extension set --name AzureMonitorLinuxAgent ...`
  (not included in Terraform to keep the demo lightweight)

- **App Service name** includes your suffix (`payment-api-prod-<suffix>`) because
  App Service names must be globally unique across all Azure customers.
  In `seed_resources.json` we refer to it as `payment-api-prod` for readability.

- **Storage account name** similarly includes your suffix (`sentinelprod<suffix>`).

- All resources use **Standard_LRS** or **Free tier** — no zone redundancy, no SLA.
  This is intentional: this is a demo environment, not production-grade.
