# Adding Terraform Feature: Execution Gateway & Human-in-the-Loop

> **Purpose:** Step-by-step implementation guide for the IaC-safe Execution Gateway.
> Feed this file + CONTEXT.md + STATUS.md + docs/ARCHITECTURE.md to your coding model.

---

## Background: Why This Feature Exists

Currently, RuriSkry **evaluates** proposed actions and produces a verdict
(APPROVED / ESCALATED / DENIED) — but it **never executes** anything.
The verdict is informational: "we would approve this."

The problem: if we add an execution layer that directly modifies Azure resources
via SDK calls, it causes **IaC state drift**:

```
monitoring-agent proposes → "Enable geo-redundancy on Cosmos DB" → APPROVED
                                        ↓
                            Direct Azure SDK change
                                        ↓
                    Terraform state: geo-redundancy = false
                    Azure reality:   geo-redundancy = true
                                        ↓
                              STATE DRIFT
                                        ↓
                    Next "terraform apply" REVERTS the fix
```

**Solution:** APPROVED verdicts don't execute directly — they generate a
**Terraform PR** against the IaC repo. A human reviews and merges the PR,
CI/CD runs `terraform apply`, and IaC state stays in sync.

---

## Architecture Overview

```
GovernanceVerdict (from pipeline.evaluate())
       │
       ▼
ExecutionGateway (new component)
  ├── Check verdict: DENIED → log + no action
  ├── Check verdict: ESCALATED → create review ticket + Slack notification
  ├── Check verdict: APPROVED →
  │     ├── Is resource IaC-managed? (check managed_by tag)
  │     │     ├── YES → Generate Terraform PR (GitOps path)
  │     │     └── NO  → Mark as "manual execution required" (HITL)
  │     └── Store execution record in Cosmos DB
  └── Update dashboard with execution status
```

### Three-Tier Execution Model

| Verdict | Action | Dashboard Status |
|---------|--------|-----------------|
| **DENIED** | Block. Log to audit trail. Slack alert. | "Blocked" (red) |
| **ESCALATED** | Create review request. Slack alert. Human decides. | "Awaiting Review" (yellow) |
| **APPROVED** | Auto-generate Terraform PR if IaC-managed. Human approves PR. | "PR Created" → "Merged" → "Applied" (green) |

---

## Implementation Steps

### Step 1: New Pydantic Models

**File:** `src/core/models.py`

Add these models after the existing `DecisionExplanation` class:

```python
class ExecutionStatus(str, Enum):
    """Lifecycle states for a verdict's execution."""
    pending = "pending"              # Verdict issued, not yet processed
    blocked = "blocked"              # DENIED — no execution
    awaiting_review = "awaiting_review"  # ESCALATED — waiting for human
    pr_created = "pr_created"        # APPROVED + IaC — PR opened
    pr_merged = "pr_merged"          # PR merged by human
    applied = "applied"              # terraform apply succeeded
    manual_required = "manual_required"  # APPROVED + not IaC-managed
    dismissed = "dismissed"          # Human chose to skip
    failed = "failed"                # PR creation or apply failed

class ExecutionRecord(BaseModel):
    """Tracks the execution lifecycle of a governance verdict."""
    execution_id: str                # UUID
    action_id: str                   # Links to GovernanceVerdict.action_id
    verdict: SRIVerdict              # approved / escalated / denied
    status: ExecutionStatus
    iac_managed: bool = False        # True if resource has managed_by tag
    iac_tool: str = ""               # "terraform" | "bicep" | "" (unknown)
    iac_repo: str = ""               # e.g. "psc0des/ruriskry"
    iac_path: str = ""               # e.g. "infrastructure/terraform-prod"
    pr_url: str = ""                 # GitHub PR URL (when created)
    pr_number: int = 0               # GitHub PR number
    reviewed_by: str = ""            # Human who approved/dismissed
    created_at: datetime
    updated_at: datetime
    notes: str = ""                  # Human-added context
```

### Step 2: IaC Detection via Azure Tags

**File:** `infrastructure/terraform-prod/main.tf`

Add standard IaC tags to ALL terraform-managed resources:

```hcl
locals {
  iac_tags = {
    managed_by = "terraform"
    iac_repo   = "psc0des/ruriskry"
    iac_path   = "infrastructure/terraform-prod"
  }
}

# Then merge into every resource's tags:
resource "azurerm_linux_virtual_machine" "vm_dr_01" {
  # ... existing config ...
  tags = merge(local.common_tags, local.iac_tags, {
    "disaster-recovery" = "true"
  })
}
```

**File:** `src/core/execution_gateway.py` (NEW)

The gateway reads tags from the verdict's resource metadata to detect IaC management:

```python
def _detect_iac_management(self, resource_tags: dict[str, str]) -> tuple[bool, str, str, str]:
    """Check if a resource is IaC-managed via its Azure tags.

    Returns:
        (is_managed, iac_tool, iac_repo, iac_path)
    """
    managed_by = resource_tags.get("managed_by", "")
    if managed_by.lower() in ("terraform", "bicep", "pulumi"):
        return (
            True,
            managed_by.lower(),
            resource_tags.get("iac_repo", ""),
            resource_tags.get("iac_path", ""),
        )
    return (False, "", "", "")
```

### Step 3: Execution Gateway Core

**File:** `src/core/execution_gateway.py` (NEW)

This is the main orchestrator. It processes a `GovernanceVerdict` and routes it
through the correct execution path.

```python
"""Execution Gateway — routes governance verdicts to IaC-safe execution paths.

The gateway NEVER directly modifies Azure resources. It routes verdicts to:
- DENIED  → log + block
- ESCALATED → review request + Slack notification
- APPROVED + IaC-managed → generate Terraform PR via GitHub API
- APPROVED + not IaC-managed → mark as manual execution required
"""

import logging
import uuid
from datetime import datetime, timezone

from src.core.models import (
    ExecutionRecord,
    ExecutionStatus,
    GovernanceVerdict,
    SRIVerdict,
)

logger = logging.getLogger(__name__)


class ExecutionGateway:
    """Process governance verdicts and route to IaC-safe execution."""

    def __init__(self):
        self._records: dict[str, ExecutionRecord] = {}  # In-memory; extend to Cosmos later

    async def process_verdict(
        self,
        verdict: GovernanceVerdict,
        resource_tags: dict[str, str] | None = None,
    ) -> ExecutionRecord:
        """Route a verdict to the correct execution path.

        Args:
            verdict: The governance verdict from the pipeline.
            resource_tags: Azure resource tags (used for IaC detection).

        Returns:
            ExecutionRecord tracking the execution lifecycle.
        """
        tags = resource_tags or {}
        iac_managed, iac_tool, iac_repo, iac_path = self._detect_iac_management(tags)
        now = datetime.now(timezone.utc)

        record = ExecutionRecord(
            execution_id=str(uuid.uuid4()),
            action_id=verdict.action_id,
            verdict=verdict.decision,
            status=ExecutionStatus.pending,
            iac_managed=iac_managed,
            iac_tool=iac_tool,
            iac_repo=iac_repo,
            iac_path=iac_path,
            created_at=now,
            updated_at=now,
        )

        if verdict.decision == SRIVerdict.DENIED:
            record.status = ExecutionStatus.blocked
            logger.info("ExecutionGateway: DENIED — blocked, no execution for %s",
                        verdict.proposed_action.target.resource_id)

        elif verdict.decision == SRIVerdict.ESCALATED:
            record.status = ExecutionStatus.awaiting_review
            logger.info("ExecutionGateway: ESCALATED — awaiting human review for %s",
                        verdict.proposed_action.target.resource_id)

        elif verdict.decision == SRIVerdict.APPROVED:
            if iac_managed and iac_repo:
                # IaC-managed → generate Terraform PR
                record = await self._create_terraform_pr(record, verdict)
            else:
                # Not IaC-managed → manual execution required
                record.status = ExecutionStatus.manual_required
                logger.info("ExecutionGateway: APPROVED but not IaC-managed — manual execution for %s",
                            verdict.proposed_action.target.resource_id)

        record.updated_at = datetime.now(timezone.utc)
        self._records[record.execution_id] = record
        return record

    async def approve_execution(self, execution_id: str, reviewed_by: str) -> ExecutionRecord:
        """Human approves an ESCALATED verdict for execution.

        After approval, routes to the same IaC/manual path as APPROVED.
        """
        record = self._records.get(execution_id)
        if not record:
            raise ValueError(f"Unknown execution_id: {execution_id}")
        if record.status != ExecutionStatus.awaiting_review:
            raise ValueError(f"Cannot approve: status is {record.status.value}")

        record.reviewed_by = reviewed_by
        record.status = ExecutionStatus.pending  # Re-route through execution
        record.updated_at = datetime.now(timezone.utc)
        # TODO: Re-route to IaC PR or manual path
        logger.info("ExecutionGateway: execution %s approved by %s", execution_id, reviewed_by)
        return record

    async def dismiss_execution(self, execution_id: str, reviewed_by: str, reason: str = "") -> ExecutionRecord:
        """Human dismisses a verdict — no execution will happen."""
        record = self._records.get(execution_id)
        if not record:
            raise ValueError(f"Unknown execution_id: {execution_id}")

        record.status = ExecutionStatus.dismissed
        record.reviewed_by = reviewed_by
        record.notes = reason
        record.updated_at = datetime.now(timezone.utc)
        logger.info("ExecutionGateway: execution %s dismissed by %s", execution_id, reviewed_by)
        return record

    def get_record(self, execution_id: str) -> ExecutionRecord | None:
        return self._records.get(execution_id)

    def get_records_for_verdict(self, action_id: str) -> list[ExecutionRecord]:
        return [r for r in self._records.values() if r.action_id == action_id]

    def get_pending_reviews(self) -> list[ExecutionRecord]:
        return [r for r in self._records.values()
                if r.status == ExecutionStatus.awaiting_review]

    # --- Private helpers ---

    def _detect_iac_management(self, resource_tags: dict[str, str]) -> tuple[bool, str, str, str]:
        managed_by = resource_tags.get("managed_by", "")
        if managed_by.lower() in ("terraform", "bicep", "pulumi"):
            return (True, managed_by.lower(),
                    resource_tags.get("iac_repo", ""),
                    resource_tags.get("iac_path", ""))
        return (False, "", "", "")

    async def _create_terraform_pr(
        self, record: ExecutionRecord, verdict: GovernanceVerdict
    ) -> ExecutionRecord:
        """Generate a Terraform PR for an approved IaC-managed action.

        This is a placeholder — Step 4 implements the actual GitHub PR creation.
        """
        # TODO: Implement in Step 4
        record.status = ExecutionStatus.pr_created
        record.pr_url = ""  # Will be filled by GitHub API
        logger.info("ExecutionGateway: Terraform PR creation requested for %s (repo: %s, path: %s)",
                    verdict.proposed_action.target.resource_id,
                    record.iac_repo, record.iac_path)
        return record
```

### Step 4: Terraform PR Generator

**File:** `src/core/terraform_pr_generator.py` (NEW)

This component generates the actual Terraform diff and creates a GitHub PR.

**Prerequisites:**
- `pip install PyGithub` (add to `requirements.txt`)
- Set env var `GITHUB_TOKEN` with repo write access
- Set env var `IAC_GITHUB_REPO` (e.g. `psc0des/ruriskry`)

```python
"""Terraform PR Generator — creates GitHub PRs with IaC changes.

Given an approved GovernanceVerdict, generates the appropriate Terraform
configuration change and opens a PR against the IaC repository.

The PR body includes:
- The governance verdict (decision, SRI composite, reason)
- The proposed action details
- The Terraform diff
- A link to the dashboard drilldown
"""

import logging
import os
from datetime import datetime, timezone

from github import Github, GithubException  # PyGithub

from src.core.models import ExecutionRecord, ExecutionStatus, GovernanceVerdict
from src.config import settings

logger = logging.getLogger(__name__)


class TerraformPRGenerator:
    """Generate GitHub PRs with Terraform changes for approved verdicts."""

    def __init__(self):
        self._token = os.getenv("GITHUB_TOKEN", "")
        self._repo_name = os.getenv("IAC_GITHUB_REPO", settings.iac_github_repo)
        self._default_iac_path = os.getenv("IAC_TERRAFORM_PATH", "infrastructure/terraform-prod")
        self._dashboard_url = settings.dashboard_url

    async def create_pr(
        self,
        verdict: GovernanceVerdict,
        record: ExecutionRecord,
    ) -> ExecutionRecord:
        """Create a GitHub PR with the proposed Terraform change.

        Args:
            verdict: The approved governance verdict.
            record: The execution record to update.

        Returns:
            Updated ExecutionRecord with PR URL and number.
        """
        if not self._token:
            logger.warning("TerraformPRGenerator: GITHUB_TOKEN not set — cannot create PR")
            record.status = ExecutionStatus.manual_required
            record.notes = "GITHUB_TOKEN not configured — manual execution required"
            return record

        if not self._repo_name:
            logger.warning("TerraformPRGenerator: IAC_GITHUB_REPO not set — cannot create PR")
            record.status = ExecutionStatus.manual_required
            record.notes = "IAC_GITHUB_REPO not configured — manual execution required"
            return record

        try:
            gh = Github(self._token)
            repo = gh.get_repo(self._repo_name)

            # Generate branch name
            action = verdict.proposed_action
            resource_short = action.target.resource_id.split("/")[-1] if "/" in action.target.resource_id else action.target.resource_id
            branch_name = f"ruriskry/approved/{resource_short}-{verdict.action_id[:8]}"

            # Get the default branch
            default_branch = repo.default_branch
            base_ref = repo.get_git_ref(f"heads/{default_branch}")
            base_sha = base_ref.object.sha

            # Create a new branch
            repo.create_git_ref(f"refs/heads/{branch_name}", base_sha)

            # Generate the Terraform diff content
            tf_content = self._generate_terraform_change(verdict, record)
            tf_path = f"{record.iac_path or self._default_iac_path}/ruriskry_approved_{resource_short}.tf"

            # Create the file in the new branch
            commit_msg = (
                f"ruriskry: {action.action_type.value} {resource_short} "
                f"(SRI {verdict.skry_risk_index.sri_composite:.1f}, APPROVED)\n\n"
                f"Auto-generated by RuriSkry Governance Engine.\n"
                f"Action ID: {verdict.action_id}\n"
                f"Agent: {action.agent_id}\n"
                f"Reason: {action.reason}"
            )

            repo.create_file(
                path=tf_path,
                message=commit_msg,
                content=tf_content,
                branch=branch_name,
            )

            # Create the PR
            pr_body = self._build_pr_body(verdict, record)
            pr = repo.create_pull(
                title=f"[RuriSkry] {action.action_type.value.replace('_', ' ').title()}: {resource_short}",
                body=pr_body,
                head=branch_name,
                base=default_branch,
            )

            # Add labels
            try:
                pr.add_to_labels("ruriskry", "auto-generated", "governance-approved")
            except GithubException:
                pass  # Labels may not exist yet

            record.status = ExecutionStatus.pr_created
            record.pr_url = pr.html_url
            record.pr_number = pr.number
            record.updated_at = datetime.now(timezone.utc)

            logger.info(
                "TerraformPRGenerator: PR #%d created — %s",
                pr.number, pr.html_url,
            )

        except GithubException as exc:
            logger.error("TerraformPRGenerator: GitHub API error — %s", exc)
            record.status = ExecutionStatus.failed
            record.notes = f"GitHub API error: {exc}"

        except Exception as exc:
            logger.error("TerraformPRGenerator: unexpected error — %s", exc)
            record.status = ExecutionStatus.failed
            record.notes = f"Error: {exc}"

        return record

    def _generate_terraform_change(
        self, verdict: GovernanceVerdict, record: ExecutionRecord
    ) -> str:
        """Generate Terraform HCL for the proposed action.

        This creates a Terraform configuration snippet that implements the
        approved action. The actual content depends on the action type.

        NOTE: This is a starting point. For production, you would:
        1. Parse the existing Terraform state to find the resource block
        2. Generate a targeted diff (not a new file)
        3. Use terraform-cdk or hcl2 libraries for proper AST manipulation
        """
        action = verdict.proposed_action
        resource_id = action.target.resource_id
        resource_short = resource_id.split("/")[-1] if "/" in resource_id else resource_id
        action_type = action.action_type.value
        sri = verdict.skry_risk_index

        header = f"""# =============================================================================
# RuriSkry Governance — Approved Action
# =============================================================================
# Action:    {action_type}
# Resource:  {resource_short}
# Agent:     {action.agent_id}
# SRI:       {sri.sri_composite:.1f} (infra={sri.sri_infrastructure:.1f}, policy={sri.sri_policy:.1f}, hist={sri.sri_historical:.1f}, cost={sri.sri_cost:.1f})
# Verdict:   APPROVED
# Reason:    {action.reason}
# Action ID: {verdict.action_id}
# Generated: {datetime.now(timezone.utc).isoformat()}
# =============================================================================
#
# INSTRUCTIONS FOR REVIEWER:
# 1. Review this change against your infrastructure standards
# 2. Verify the resource ID matches the intended target
# 3. Run "terraform plan" to preview the actual changes
# 4. Merge this PR to apply via CI/CD
# =============================================================================

"""
        # Generate action-specific Terraform snippets
        # These are illustrative — real implementations would read existing .tf files
        if action_type == "scale_up" and action.target.proposed_sku:
            return header + f"""# Scale up {resource_short}: {action.target.current_sku or '?'} -> {action.target.proposed_sku}
# TODO: Update the vm_size variable or resource block for {resource_short}
# Example:
# variable "vm_size" {{
#   default = "{action.target.proposed_sku}"
# }}
"""
        elif action_type == "update_config":
            return header + f"""# Configuration update for {resource_short}
# Agent reason: {action.reason}
# TODO: Apply the specific configuration change described above
"""
        elif action_type == "modify_nsg":
            return header + f"""# NSG modification for {resource_short}
# Agent reason: {action.reason}
# TODO: Add/modify the NSG rule as described
"""
        elif action_type == "delete_resource":
            return header + f"""# Resource deletion: {resource_short}
# WARNING: This removes the resource from Terraform state.
# Agent reason: {action.reason}
# TODO: Comment out or remove the resource block for {resource_short}
"""
        else:
            return header + f"""# Action: {action_type} on {resource_short}
# Agent reason: {action.reason}
# TODO: Implement the change described above
"""

    def _build_pr_body(self, verdict: GovernanceVerdict, record: ExecutionRecord) -> str:
        """Build the PR description with full governance context."""
        action = verdict.proposed_action
        sri = verdict.skry_risk_index
        resource_short = action.target.resource_id.split("/")[-1] if "/" in action.target.resource_id else action.target.resource_id
        dashboard_link = f"{self._dashboard_url}" if self._dashboard_url else ""

        return f"""## RuriSkry Governance — Approved Action

**This PR was auto-generated by the RuriSkry Governance Engine.**
A human must review and merge it to execute the change safely via Terraform.

### Governance Verdict

| Field | Value |
|-------|-------|
| Decision | **APPROVED** |
| SRI Composite | {sri.sri_composite:.1f} / 100 |
| SRI Infrastructure | {sri.sri_infrastructure:.1f} |
| SRI Policy | {sri.sri_policy:.1f} |
| SRI Historical | {sri.sri_historical:.1f} |
| SRI Cost | {sri.sri_cost:.1f} |

### Proposed Action

| Field | Value |
|-------|-------|
| Resource | `{resource_short}` |
| Action | {action.action_type.value.replace('_', ' ')} |
| Proposing Agent | {action.agent_id} |
| Reason | {action.reason} |
| Urgency | {action.urgency.value} |

### IaC Context

| Field | Value |
|-------|-------|
| IaC Tool | {record.iac_tool} |
| IaC Path | `{record.iac_path}` |
| Action ID | `{verdict.action_id}` |

### Reviewer Checklist

- [ ] Verify the resource ID matches the intended target
- [ ] Review the Terraform change for correctness
- [ ] Run `terraform plan` in CI to preview changes
- [ ] Confirm no unrelated resources are affected
- [ ] Merge to apply

{f'[View in RuriSkry Dashboard]({dashboard_link})' if dashboard_link else ''}

---
*Auto-generated by [RuriSkry Governance Engine](https://github.com/{record.iac_repo})*
"""
```

### Step 5: Wire Gateway into Pipeline

**File:** `src/core/pipeline.py`

After the existing `DecisionTracker.record(verdict)` call, add the execution gateway:

```python
# At the top — add import
from src.core.execution_gateway import ExecutionGateway

# In RuriSkryPipeline.__init__():
self._execution_gateway = ExecutionGateway()

# In evaluate(), after DecisionTracker.record(verdict):
# Process through execution gateway
try:
    resource_tags = resource.get("tags", {}) if resource else {}
    execution_record = await self._execution_gateway.process_verdict(verdict, resource_tags)
    logger.info(
        "Pipeline: execution status=%s for %s",
        execution_record.status.value,
        action.target.resource_id,
    )
except Exception as exc:
    logger.warning("Pipeline: execution gateway failed — %s (verdict still valid)", exc)
```

**IMPORTANT:** The execution gateway must NOT block or fail the verdict. Wrap in
`try/except` — the governance verdict is the primary output; execution routing
is secondary. If the gateway fails, the verdict is still valid and stored.

### Step 6: Config Updates

**File:** `src/config.py`

Add new settings:

```python
# Execution Gateway (Phase 21)
iac_github_repo: str = ""           # e.g. "psc0des/ruriskry"
iac_terraform_path: str = "infrastructure/terraform-prod"
execution_gateway_enabled: bool = False  # Opt-in; default off for safety
```

**File:** `.env`

Add:

```bash
# Execution Gateway (Phase 21)
GITHUB_TOKEN=                        # GitHub PAT with repo write access
IAC_GITHUB_REPO=psc0des/ruriskry     # GitHub repo for IaC PRs
IAC_TERRAFORM_PATH=infrastructure/terraform-prod
EXECUTION_GATEWAY_ENABLED=false      # Set to true to enable PR generation
```

### Step 7: Dashboard API Endpoints

**File:** `src/api/dashboard_api.py`

Add these new endpoints:

```python
# ── Execution Gateway endpoints (Phase 21) ──

@app.get("/api/execution/{action_id}")
async def get_execution_status(action_id: str):
    """Get execution status for a governance verdict."""
    gateway = _get_execution_gateway()
    records = gateway.get_records_for_verdict(action_id)
    if not records:
        return {"status": "no_execution", "action_id": action_id}
    return {"action_id": action_id, "executions": [r.model_dump(mode="json") for r in records]}

@app.get("/api/execution/pending-reviews")
async def get_pending_reviews():
    """List all ESCALATED verdicts awaiting human review."""
    gateway = _get_execution_gateway()
    pending = gateway.get_pending_reviews()
    return {"count": len(pending), "reviews": [r.model_dump(mode="json") for r in pending]}

@app.post("/api/execution/{execution_id}/approve")
async def approve_execution(execution_id: str, body: dict):
    """Human approves an escalated verdict for execution."""
    gateway = _get_execution_gateway()
    reviewed_by = body.get("reviewed_by", "dashboard-user")
    try:
        record = await gateway.approve_execution(execution_id, reviewed_by)
        return record.model_dump(mode="json")
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

@app.post("/api/execution/{execution_id}/dismiss")
async def dismiss_execution(execution_id: str, body: dict):
    """Human dismisses a verdict — no execution happens."""
    gateway = _get_execution_gateway()
    reviewed_by = body.get("reviewed_by", "dashboard-user")
    reason = body.get("reason", "")
    try:
        record = await gateway.dismiss_execution(execution_id, reviewed_by, reason)
        return record.model_dump(mode="json")
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
```

**Lazy singleton pattern (same as other singletons in dashboard_api.py):**

```python
_execution_gateway: ExecutionGateway | None = None

def _get_execution_gateway() -> ExecutionGateway:
    global _execution_gateway
    if _execution_gateway is None:
        _execution_gateway = ExecutionGateway()
    return _execution_gateway
```

### Step 8: Dashboard Frontend — Execution Status Panel

**File:** `dashboard/src/components/EvaluationDrilldown.jsx`

Add a new Section 7 (after Audit Trail) showing the execution status:

```jsx
{/* Section 7 — Execution Status */}
<div className="bg-slate-800 rounded-xl border border-slate-700 p-6">
    <h2 className="text-xs font-semibold text-slate-400 uppercase tracking-widest mb-4">
        Execution Status
    </h2>

    {executionStatus ? (
        <div className="space-y-3">
            <div className="flex items-center gap-3">
                <StatusBadge status={executionStatus.status} />
                {executionStatus.iac_managed && (
                    <span className="text-xs text-blue-400 bg-blue-500/10 border border-blue-500/30 px-2 py-0.5 rounded-full">
                        IaC: {executionStatus.iac_tool}
                    </span>
                )}
            </div>

            {executionStatus.pr_url && (
                <a href={executionStatus.pr_url} target="_blank"
                   className="text-sm text-blue-400 hover:text-blue-300 underline">
                    View PR #{executionStatus.pr_number} on GitHub
                </a>
            )}

            {executionStatus.status === 'awaiting_review' && (
                <div className="flex gap-2 mt-3">
                    <button onClick={() => handleApprove(executionStatus.execution_id)}
                            className="px-4 py-2 bg-green-600 hover:bg-green-500 text-white rounded-lg text-sm">
                        Approve & Execute
                    </button>
                    <button onClick={() => handleDismiss(executionStatus.execution_id)}
                            className="px-4 py-2 bg-slate-600 hover:bg-slate-500 text-white rounded-lg text-sm">
                        Dismiss
                    </button>
                </div>
            )}
        </div>
    ) : (
        <p className="text-sm text-slate-500">No execution record — verdict is informational only.</p>
    )}
</div>
```

**File:** `dashboard/src/api.js`

Add API functions:

```javascript
export async function fetchExecutionStatus(actionId) {
    const res = await fetch(`${API}/execution/${actionId}`)
    return res.json()
}

export async function approveExecution(executionId, reviewedBy = 'dashboard-user') {
    const res = await fetch(`${API}/execution/${executionId}/approve`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ reviewed_by: reviewedBy }),
    })
    return res.json()
}

export async function dismissExecution(executionId, reviewedBy = 'dashboard-user', reason = '') {
    const res = await fetch(`${API}/execution/${executionId}/dismiss`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ reviewed_by: reviewedBy, reason }),
    })
    return res.json()
}
```

### Step 9: Add `managed_by` Tags to Existing Terraform Resources

**File:** `infrastructure/terraform-prod/main.tf`

Ensure ALL resources have the IaC detection tags. Example for the existing resources:

```hcl
locals {
  iac_tags = {
    managed_by = "terraform"
    iac_repo   = "psc0des/ruriskry"
    iac_path   = "infrastructure/terraform-prod"
  }

  common_tags = merge(var.common_tags, local.iac_tags)
}
```

Then run `terraform apply` to push the new tags to Azure.

### Step 10: Tests

**File:** `tests/test_execution_gateway.py` (NEW)

Test categories:

1. **IaC detection** — resource with `managed_by=terraform` tag detected correctly;
   resource without tag → not IaC-managed
2. **Verdict routing** — DENIED → blocked; ESCALATED → awaiting_review;
   APPROVED + IaC → pr_created; APPROVED + no IaC → manual_required
3. **Approval flow** — approve_execution changes status; dismiss sets dismissed
4. **PR generation** — mock GitHub API; verify branch name, commit message, PR body
5. **Gateway in pipeline** — verify gateway is called after DecisionTracker.record();
   verify gateway failure doesn't break the verdict
6. **Edge cases** — unknown execution_id raises ValueError;
   approving a non-ESCALATED record raises ValueError

```python
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from src.core.execution_gateway import ExecutionGateway
from src.core.models import (
    ActionTarget, ActionType, ExecutionStatus, GovernanceVerdict,
    ProposedAction, SRIBreakdown, SRIVerdict, Urgency,
)


@pytest.fixture
def gateway():
    return ExecutionGateway()


@pytest.fixture
def sample_approved_verdict():
    return GovernanceVerdict(
        action_id="test-001",
        timestamp=...,
        proposed_action=ProposedAction(
            agent_id="monitoring-agent",
            action_type=ActionType.UPDATE_CONFIG,
            target=ActionTarget(resource_id="vm-web-01", resource_type="Microsoft.Compute/virtualMachines"),
            reason="Enable geo-redundancy",
            urgency=Urgency.MEDIUM,
        ),
        skry_risk_index=SRIBreakdown(sri_infrastructure=10, sri_policy=0, sri_historical=0, sri_cost=0, sri_composite=3.0),
        decision=SRIVerdict.APPROVED,
        reason="APPROVED — SRI 3.0",
    )


class TestIaCDetection:
    def test_terraform_tag_detected(self, gateway):
        tags = {"managed_by": "terraform", "iac_repo": "psc0des/ruriskry", "iac_path": "infrastructure/terraform-prod"}
        managed, tool, repo, path = gateway._detect_iac_management(tags)
        assert managed is True
        assert tool == "terraform"

    def test_no_tag_not_managed(self, gateway):
        managed, *_ = gateway._detect_iac_management({})
        assert managed is False


class TestVerdictRouting:
    @pytest.mark.asyncio
    async def test_denied_becomes_blocked(self, gateway):
        # Build a DENIED verdict and verify status is blocked
        ...

    @pytest.mark.asyncio
    async def test_escalated_becomes_awaiting_review(self, gateway):
        ...

    @pytest.mark.asyncio
    async def test_approved_iac_creates_pr(self, gateway, sample_approved_verdict):
        tags = {"managed_by": "terraform", "iac_repo": "psc0des/ruriskry"}
        record = await gateway.process_verdict(sample_approved_verdict, tags)
        assert record.status == ExecutionStatus.pr_created
        assert record.iac_managed is True

    @pytest.mark.asyncio
    async def test_approved_no_iac_manual(self, gateway, sample_approved_verdict):
        record = await gateway.process_verdict(sample_approved_verdict, {})
        assert record.status == ExecutionStatus.manual_required
        assert record.iac_managed is False
```

### Step 11: Update `requirements.txt`

Add:
```
PyGithub>=2.1.0
```

---

## Implementation Order (Priority)

1. **Models** (Step 1) — foundational; everything depends on these
2. **ExecutionGateway** (Step 3) — core routing logic (no external deps)
3. **Config** (Step 6) — env vars for GitHub token and repo
4. **Wire into Pipeline** (Step 5) — make it live
5. **API Endpoints** (Step 7) — expose execution status
6. **Tests** (Step 10) — validate routing logic
7. **Terraform Tags** (Step 2 + Step 9) — IaC detection foundation
8. **TerraformPRGenerator** (Step 4) — GitHub PR creation (needs `PyGithub`)
9. **Dashboard Frontend** (Step 8) — execution status panel
10. **requirements.txt** (Step 11) — add `PyGithub`

---

## Design Principles

1. **Gateway never executes directly** — it only creates PRs or marks for manual review
2. **Gateway failure never breaks the verdict** — wrapped in `try/except`; verdict is primary output
3. **Opt-in by default** — `EXECUTION_GATEWAY_ENABLED=false`; set to `true` explicitly
4. **HITL always exists** — even APPROVED actions require a human to merge the PR
5. **IaC state is sacred** — all changes flow through `terraform apply`, never direct SDK
6. **Audit trail complete** — `ExecutionRecord` tracks the full lifecycle from verdict to execution

---

## Future Enhancements (Not in Scope for Phase 21)

- **Cosmos DB persistence for ExecutionRecords** — currently in-memory; add durable store
- **Terraform plan preview** — run `terraform plan` in CI and post the output as a PR comment
- **Auto-merge for very low risk** — SRI < 5 with no policy flags → auto-merge PR (configurable)
- **Webhook on PR merge** — GitHub webhook notifies RuriSkry when a PR is merged; update status to `applied`
- **Multi-IaC support** — Bicep, Pulumi, CloudFormation detection and PR generation
- **Terraform AST manipulation** — use `hcl2` library to modify existing `.tf` files instead of creating new ones
