# RuriSkry — Use Cases by Agent

> This document shows real-world scenarios for each operational agent.
> Each example follows the same flow:
>
> **Trigger → Agent discovers issue → Governance pipeline evaluates → Verdict in dashboard → Human action (HITL)**

---

## How the Engine Works (Quick Recap)

```
┌─────────────────────────────────────────────────────────────────────┐
│  Trigger (manual scan / Azure Monitor alert)                        │
│       ↓                                                             │
│  Operational Agent (Cost / SRE / Deploy)                           │
│    - Queries Azure Resource Graph + Metrics via GPT-4.1            │
│    - Decides what action to propose                                 │
│       ↓                                                             │
│  Governance Pipeline (4 parallel agents)                           │
│    - Blast Radius Agent  → how many things break if we do this?    │
│    - Policy Agent        → does this violate any rules?            │
│    - Historical Agent    → has this gone wrong before?             │
│    - Financial Agent     → what does this cost / save?             │
│       ↓                                                             │
│  SRI™ Score (0–100) → APPROVED / ESCALATED / DENIED               │
│       ↓                                                             │
│  Execution Gateway                                                  │
│    - APPROVED + IaC tags  → Terraform PR on GitHub                 │
│    - APPROVED + no IaC    → manual_required (HITL panel)           │
│    - ESCALATED            → awaiting_review (HITL panel)           │
│    - DENIED               → blocked, logged                        │
└─────────────────────────────────────────────────────────────────────┘
```

---

## Agent 1 — Cost Optimization Agent

**What it does:** Queries Resource Graph for all VMs, AKS clusters, and other
compute resources. For each one, checks 7-day average CPU utilisation via Azure
Monitor. GPT-4.1 reasons about whether the resource is wasteful and proposes
right-sizing or deletion.

**Scan trigger:** `POST /api/scan/cost` (dashboard button or scheduled job)

---

### Use Case 1.1 — Idle VM (Low CPU → Right-size)

| Field | Detail |
|-------|--------|
| **Scenario** | `vm-dr-01` has been running for 30 days. 7-day avg CPU: **3.2%**, peak: 8%. Size: `Standard_D4as_v4` ($140/month). |
| **Agent discovers** | CPU well below 20% threshold → right-sizing candidate |
| **Proposed action** | `scale_down` to `Standard_B2ls_v2` (~$30/month) |
| **Governance verdict** | APPROVED (SRI low — small blast radius, no policy violations) |
| **Dashboard** | `manual_required` panel — "Fix using Agent" shows `az vm resize` preview |
| **Human action** | Review → click Run → SDK resizes VM via `ComputeManagementClient` |
| **Outcome** | ~$110/month saved. VM continues running, just on smaller hardware. |

---

### Use Case 1.2 — Abandoned VM (Candidate for Deletion)

| Field | Detail |
|-------|--------|
| **Scenario** | `vm-test-legacy` has 0.8% CPU over 30 days, no traffic, tagged `environment=dev`. Cost: $250/month. |
| **Agent discovers** | Near-zero utilisation + dev tag + no incoming connections → deletion candidate |
| **Proposed action** | `delete_resource` |
| **Governance verdict** | ESCALATED (SRI medium — blast radius unclear, no DR tag but high cost) |
| **Dashboard** | `awaiting_review` panel — Approve & Execute or Dismiss |
| **Human action** | Verifies no one is using it → Approves → Gateway creates Terraform PR |
| **Outcome** | VM deleted after PR merge. $250/month saved. |

---

### Use Case 1.3 — Oversized AKS Cluster

| Field | Detail |
|-------|--------|
| **Scenario** | `aks-prod-01` node pool: 6 nodes of `Standard_D8s_v3`. Avg CPU across nodes: 12%. Cluster was sized for a load test 3 months ago. |
| **Agent discovers** | AKS node count above threshold, CPU low → scale-down candidate |
| **Proposed action** | `scale_down` (reduce node count) |
| **Governance verdict** | ESCALATED (SRI medium — prod cluster, needs human sign-off) |
| **Dashboard** | `awaiting_review` — team lead reviews metrics before approving |
| **Human action** | Approves during business hours (not Friday afternoon) |
| **Outcome** | Node pool scaled from 6 → 3 nodes. ~$1,200/month saved. |

---

### Use Case 1.4 — Dev/Test VM Running 24×7 in Production Hours

| Field | Detail |
|-------|--------|
| **Scenario** | `vm-dev-build-01` tagged `environment=dev`. Cost: $180/month. CPU: 45% during business hours, 0% nights and weekends. |
| **Agent discovers** | Dev-tagged VM with off-hours idle pattern → update_config (add auto-shutdown) |
| **Proposed action** | `update_config` — add auto-shutdown schedule |
| **Governance verdict** | APPROVED (SRI low — dev environment, limited blast radius) |
| **Dashboard** | `manual_required` — no automated SDK path for auto-shutdown config |
| **Human action** | Opens Azure Portal link → manually enables auto-shutdown at 19:00 |
| **Outcome** | ~$90/month saved (VM off 14hrs/day on weekdays + full weekends). |

---

### Use Case 1.5 — Premium App Service Plan with Minimal Traffic

| Field | Detail |
|-------|--------|
| **Scenario** | `payment-api-prod` runs on App Service P2v3 ($200/month). Requests/day: ~500. CPU: 4%. |
| **Agent discovers** | App Service significantly over-provisioned for actual traffic load |
| **Proposed action** | `scale_down` to P1v3 |
| **Governance verdict** | DENIED (SRI high — Policy Agent flags `POL-PAYMENT-001`: payment services require P2+ SLA tier) |
| **Dashboard** | `blocked` — red badge, reason shown in drilldown |
| **Human action** | None required — engine correctly blocked a dangerous cost-cut |
| **Outcome** | No change. Compliance preserved. Engineer learns why the tier is mandated. |

---

## Agent 2 — SRE Monitoring Agent

**What it does:** Operates in two modes:

- **Alert mode** — triggered by Azure Monitor webhook → investigates the specific
  alerted resource with real metric data before proposing remediation
- **Scan mode** — proactively scans for reliability risks: high CPU, SPOFs,
  unmonitored critical resources, missing owner tags

**Scan triggers:**
- `POST /api/scan/monitoring` (dashboard button)
- `POST /api/alert-trigger` (Azure Monitor Action Group webhook — automatic)

---

### Use Case 2.1 — VM Under High CPU (Alert-triggered)

| Field | Detail |
|-------|--------|
| **Scenario** | Azure Monitor fires: `vm-web-01` CPU > 80% for 15 minutes. Alert webhook hits `POST /api/alert-trigger`. |
| **Agent discovers** | Confirms 7-day avg CPU: 82.5%, peak: 100%. VM is `Standard_B2ls_v2` — undersized for load. |
| **Proposed action** | `scale_up` to `Standard_D4as_v4` |
| **Governance verdict** | APPROVED (SRI low — scale-up reduces risk, not increases it) |
| **Dashboard** | `manual_required` — "Fix using Agent" previews `begin_update()` SDK call |
| **Human action** | SRE reviews → confirms load pattern → clicks Run |
| **Outcome** | VM resized. CPU drops to 35%. No downtime. |

---

### Use Case 2.2 — Service Crash / Unresponsive VM

| Field | Detail |
|-------|--------|
| **Scenario** | Azure Monitor: heartbeat missing on `vm-dr-01` for 5 minutes. Alert fires. |
| **Agent discovers** | Heartbeat flatlined. Queries `get_resource_details` — VM state: "stopped". |
| **Proposed action** | `restart_service` |
| **Governance verdict** | ESCALATED (SRI medium — DR resource, tagged `criticality=critical`, Policy Agent flags review requirement) |
| **Dashboard** | `awaiting_review` — on-call SRE gets Slack notification (webhook), sees ESCALATED banner |
| **Human action** | Approves in dashboard → Gateway calls `virtual_machines.begin_restart()` |
| **Outcome** | VM restarted. Heartbeat restored. RCA documented in audit trail. |

---

### Use Case 2.3 — Single Point of Failure (SPOF) Detected

| Field | Detail |
|-------|--------|
| **Scenario** | Proactive scan finds `payment-api-prod` is the only App Service in the subscription. No standby/slot configured. Tagged `criticality=critical`. |
| **Agent discovers** | Critical service with no redundancy — structural reliability risk |
| **Proposed action** | `update_config` — enable deployment slots / geo-redundancy |
| **Governance verdict** | ESCALATED (SRI high — critical payment service, Historical Agent finds similar SPOF incident in 2023) |
| **Dashboard** | `awaiting_review` — architect notified via Slack |
| **Human action** | Reviews finding → escalates to architecture review, not a quick fix |
| **Outcome** | SPOF documented. Architecture task created. Not auto-fixed — correctly escalated. |

---

### Use Case 2.4 — Memory Exhaustion Pattern

| Field | Detail |
|-------|--------|
| **Scenario** | `vm-web-01` CPU: 40% (normal), but available memory has been < 5% for 3 days. No Azure Monitor alert configured for memory. |
| **Agent discovers** | Proactive scan → query_metrics includes `Available Memory Bytes` → near-zero detected |
| **Proposed action** | `scale_up` to memory-optimised SKU (`Standard_E4as_v4`) |
| **Governance verdict** | APPROVED (SRI low — memory upgrade, low blast radius) |
| **Dashboard** | `manual_required` panel — HITL required because no IaC tags on resource |
| **Human action** | Confirms memory pattern → clicks Run → SDK resizes during maintenance window |
| **Outcome** | OOM errors stop. Application stable. |

---

### Use Case 2.5 — Critical Resource with No Owner Tag

| Field | Detail |
|-------|--------|
| **Scenario** | `nsg-east-prod` tagged `criticality=critical` but has no `owner` or `team` tag. If something goes wrong, no one knows who to call. |
| **Agent discovers** | Governance gap — critical resource without ownership metadata |
| **Proposed action** | `update_config` — add owner tag |
| **Governance verdict** | APPROVED (SRI very low — tag change, zero blast radius) |
| **Dashboard** | `manual_required` — `update_config` has no automated SDK path |
| **Human action** | Opens Azure Portal → adds `owner=platform-team@company.com` tag |
| **Outcome** | Tag added. Next scan won't flag this resource. Governance posture improved. |

---

### Use Case 2.6 — Cascading Dependency Risk

| Field | Detail |
|-------|--------|
| **Scenario** | Proactive scan finds `vm-dr-01` has 4 downstream resources depending on it (discovered via Resource Graph topology). VM has no availability zone configured. |
| **Agent discovers** | High fan-out dependency + no AZ redundancy → cascading failure risk |
| **Proposed action** | `update_config` — migrate to availability zone |
| **Governance verdict** | ESCALATED (SRI high — Blast Radius Agent calculates 4 dependent services affected) |
| **Dashboard** | `awaiting_review` — infrastructure lead review required |
| **Human action** | Assesses migration complexity → creates a project to address it |
| **Outcome** | Not auto-fixed. Correctly surfaced for planned remediation. |

---

## Agent 3 — Deploy / Security Review Agent

**What it does:** Scans NSG rules for dangerous configurations — port 22 (SSH),
3389 (RDP), or other sensitive ports open to `*` (any source). Uses KQL queries
against Resource Graph to find NSGs, then inspects each rule set. GPT-4.1
decides whether a rule is genuinely dangerous before proposing removal.

**Scan trigger:** `POST /api/scan/deploy` (dashboard button)

---

### Use Case 3.1 — RDP Wide Open to the Internet

| Field | Detail |
|-------|--------|
| **Scenario** | `nsg-east-prod` has rule `AllowRDP_Inbound`: port 3389, `sourceAddressPrefix=*`, priority 100. |
| **Agent discovers** | Port 3389 open to any source — critical security exposure |
| **Proposed action** | `modify_nsg` — delete rule `AllowRDP_Inbound` |
| **Governance verdict** | DENIED (SRI very high — Policy Agent: `POL-SEC-001` prohibits any-source RDP. Historical Agent: similar rule caused breach in 2022.) |
| **Dashboard** | `blocked` — red badge. Drilldown shows policy violation + historical incident |
| **Human action** | Engineer sees the block → goes to Azure Portal to fix immediately |
| **Outcome** | Rule removed manually. "Decline/Ignore" is NOT clicked — rule stays flagged until fixed. |

---

### Use Case 3.2 — SSH Open to Any Source

| Field | Detail |
|-------|--------|
| **Scenario** | `nsg-dev-01` has rule `Allow-SSH`: port 22, `sourceAddressPrefix=Internet`, priority 200. |
| **Agent discovers** | Port 22 reachable from the internet — brute-force attack surface |
| **Proposed action** | `modify_nsg` — delete rule `Allow-SSH` |
| **Governance verdict** | ESCALATED (SRI medium — dev NSG, lower criticality than prod, but still a real risk) |
| **Dashboard** | `awaiting_review` — security lead notified via Slack |
| **Human action** | Approves → Gateway calls `NetworkManagementClient.security_rules.begin_delete()` |
| **Outcome** | Rule deleted. SSH access restricted to VPN/Bastion only. |

---

### Use Case 3.3 — Wildcard Allow-All Inbound Rule

| Field | Detail |
|-------|--------|
| **Scenario** | `nsg-staging` has rule `AllowAll_Inbound`: port `*`, protocol `*`, source `*`. Someone added this during debugging and forgot to remove it. |
| **Agent discovers** | Wildcard allow-all rule — entire VM surface exposed |
| **Proposed action** | `modify_nsg` — delete rule `AllowAll_Inbound` |
| **Governance verdict** | DENIED (SRI 95 — catastrophic exposure. All four governance agents flag this.) |
| **Dashboard** | `blocked` — dashboard shows SRI breakdown: all four bars in red |
| **Human action** | Immediately removes rule. Clicks "Decline/Ignore" only after it's confirmed removed. |
| **Outcome** | Rule removed. "Flag-until-fixed" governance pattern keeps re-surfacing it on every scan until dismissed. |

---

### Use Case 3.4 — Non-standard Port Exposed (Database Port)

| Field | Detail |
|-------|--------|
| **Scenario** | `nsg-data-01` has rule `AllowSQL`: port 1433 (SQL Server), `sourceAddressPrefix=*`. Directly accessible from internet. |
| **Agent discovers** | Database port exposed publicly — SQL injection and brute-force risk |
| **Proposed action** | `modify_nsg` — delete rule `AllowSQL` |
| **Governance verdict** | DENIED (SRI 88 — Policy Agent: `POL-DATA-001` prohibits public database access) |
| **Dashboard** | `blocked` — policy violation displayed in drilldown |
| **Human action** | Removes public rule, adds private endpoint instead |
| **Outcome** | Database accessible only via VNet private endpoint. Compliance restored. |

---

### Use Case 3.5 — Resource Missing Lifecycle Tags

| Field | Detail |
|-------|--------|
| **Scenario** | `vm-build-agent-03` has no `environment`, `criticality`, or `owner` tags. The Deploy Agent can't determine if it's prod or dev, who owns it, or if it's IaC-managed. |
| **Agent discovers** | Resource with zero governance tags — unclassified infrastructure |
| **Proposed action** | `update_config` — add lifecycle tags |
| **Governance verdict** | APPROVED (SRI very low — tag change only) |
| **Dashboard** | `manual_required` — `update_config` has no automated SDK path |
| **Human action** | Opens Azure Portal → adds `environment=dev`, `owner=build-team`, `managed_by=terraform` |
| **Outcome** | Resource now classified. Future scans can correctly route IaC-managed changes to Terraform PRs. |

---

### Use Case 3.6 — IaC-managed Resource with Security Issue

| Field | Detail |
|-------|--------|
| **Scenario** | `nsg-east-prod` is tagged `managed_by=terraform`, `iac_repo=psc0des/ruriskry`. It has a dangerous rule `AllowRDP_Any`. |
| **Agent discovers** | Dangerous NSG rule on an IaC-managed resource |
| **Proposed action** | `modify_nsg` — delete rule |
| **Governance verdict** | ESCALATED (SRI medium — prod resource, human sign-off needed) |
| **Dashboard** | `awaiting_review` → human approves → Gateway detects `managed_by=terraform` tag |
| **Human action** | Approves in dashboard |
| **Outcome** | `TerraformPRGenerator` opens GitHub PR with HCL that removes the rule. Platform team merges it. Change flows through `terraform apply` — IaC state preserved. |

---

## Governance Verdict Decision Matrix

| SRI Score | Verdict | What the engine does |
|-----------|---------|---------------------|
| 0–25 | **APPROVED** | Routes to Terraform PR (IaC) or `manual_required` (no IaC) |
| 26–60 | **ESCALATED** | `awaiting_review` — HITL buttons in dashboard, Slack notification sent |
| 61–100 | **DENIED** | `blocked` — logged to audit trail, no execution path opened |

---

## HITL Action Panel (manual_required / awaiting_review)

When a verdict lands in the dashboard requiring human action, the operator has four choices:

| Button | What it does | When to use |
|--------|-------------|-------------|
| **Create Terraform PR** | Opens GitHub PR with HCL fix | Resource is IaC-managed, want proper code review |
| **Open in Azure Portal** | Direct link to the resource | Quick manual fix, no IaC workflow needed |
| **Fix using Agent** | Preview Azure SDK call → confirm → execute | Want automated fix, resource has full ARM ID |
| **Decline / Ignore** | Dismisses the finding (`dismissed` status) | False positive, or fix already applied externally |

> **"Flag-until-fixed" guarantee:** `manual_required` records re-appear on every subsequent scan until the human either fixes the issue and clicks Dismiss, or the agent naturally stops flagging it (underlying config changed). No finding silently disappears.
