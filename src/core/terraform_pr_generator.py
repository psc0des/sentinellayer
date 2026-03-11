"""Terraform PR Generator — creates GitHub PRs with IaC changes.

Given an approved GovernanceVerdict, generates the appropriate Terraform
configuration change and opens a PR against the IaC repository.

The PR body includes:
- The governance verdict (decision, SRI composite, reason)
- The proposed action details
- A Terraform change stub with reviewer instructions
- A link to the dashboard drilldown

This module requires PyGithub: pip install PyGithub>=2.1.0
Env vars: GITHUB_TOKEN, IAC_GITHUB_REPO (or settings equivalents).
"""

import asyncio
import logging
import re
from datetime import datetime, timezone

from src.config import settings
from src.core.models import ActionType, ExecutionRecord, ExecutionStatus, GovernanceVerdict

logger = logging.getLogger(__name__)


class TerraformPRGenerator:
    """Generate GitHub PRs with Terraform changes for approved verdicts.

    All GitHub API calls are synchronous (PyGithub does not have an async
    client).  They are run via ``asyncio.to_thread()`` so the event loop
    is not blocked.
    """

    def __init__(self) -> None:
        self._token = settings.github_token
        self._repo_name = settings.iac_github_repo
        self._default_iac_path = settings.iac_terraform_path
        self._dashboard_url = settings.dashboard_url

    async def create_pr(
        self,
        verdict: GovernanceVerdict,
        record: ExecutionRecord,
    ) -> ExecutionRecord:
        """Create a GitHub PR with the proposed Terraform change.

        Runs the blocking GitHub API calls in a thread pool via
        ``asyncio.to_thread()``.

        Args:
            verdict: The approved governance verdict.
            record: The ExecutionRecord to update with PR URL / status.

        Returns:
            Updated ExecutionRecord.
        """
        if not self._token:
            logger.warning(
                "TerraformPRGenerator: GITHUB_TOKEN not set — cannot create PR"
            )
            record.status = ExecutionStatus.manual_required
            record.notes = "GITHUB_TOKEN not configured — manual execution required"
            return record

        if not self._repo_name:
            logger.warning(
                "TerraformPRGenerator: IAC_GITHUB_REPO not set — cannot create PR"
            )
            record.status = ExecutionStatus.manual_required
            record.notes = "IAC_GITHUB_REPO not configured — manual execution required"
            return record

        return await asyncio.to_thread(self._create_pr_sync, verdict, record)

    # ------------------------------------------------------------------
    # Private helpers (sync, run via asyncio.to_thread)
    # ------------------------------------------------------------------

    def _create_pr_sync(
        self,
        verdict: GovernanceVerdict,
        record: ExecutionRecord,
    ) -> ExecutionRecord:
        """Blocking GitHub API calls — called from asyncio.to_thread."""
        from github import Github, GithubException  # noqa: PLC0415 (lazy import — PyGithub optional)

        action = verdict.proposed_action
        resource_id = action.target.resource_id
        resource_short = resource_id.split("/")[-1] if "/" in resource_id else resource_id

        try:
            gh = Github(self._token)
            repo = gh.get_repo(self._repo_name)

            # Build branch name: ruriskry/approved/{resource}-{short-action-id}
            branch_name = (
                f"ruriskry/approved/{resource_short}-{verdict.action_id[:8]}"
            )

            # Get default branch SHA for the new branch base
            default_branch = repo.default_branch
            base_ref = repo.get_git_ref(f"heads/{default_branch}")
            base_sha = base_ref.object.sha

            # Create branch
            repo.create_git_ref(f"refs/heads/{branch_name}", base_sha)

            iac_path = record.iac_path or self._default_iac_path

            # --- Attempt 1: find & patch an existing .tf file (production-grade) ---
            patch = self._find_and_patch_tf_file(repo, iac_path, action)
            if patch:
                commit_message = (
                    f"fix({resource_short}): {patch['summary']}\n\n"
                    f"RuriSkry governance action: {action.action_type.value}\n"
                    f"SRI composite: {verdict.skry_risk_index.sri_composite:.1f} — APPROVED\n"
                    f"Action ID: {verdict.action_id}\n"
                    f"Proposed by: {action.agent_id}\n"
                    f"Reason: {action.reason}"
                )
                repo.update_file(
                    path=patch["path"],
                    message=commit_message,
                    content=patch["new_content"].encode(),
                    sha=patch["sha"],
                    branch=branch_name,
                )
                logger.info(
                    "TerraformPRGenerator: patched existing file '%s' "
                    "(rule '%s' → Deny)",
                    patch["path"], patch.get("rule_name", "?"),
                )
            else:
                # --- Attempt 2: create a stub file (fallback when existing resource
                #     can't be located in the IaC repo) ---
                tf_content = self._generate_terraform_stub(verdict, record)
                tf_file_path = (
                    f"{iac_path}/ruriskry_fix_{resource_short}_{verdict.action_id[:8]}.tf"
                )
                commit_message = (
                    f"fix({resource_short}): governance-approved {action.action_type.value} stub\n\n"
                    f"RuriSkry could not locate the existing resource block for "
                    f"'{resource_short}' in {iac_path}.\n"
                    f"This stub documents the required change — apply it manually,\n"
                    f"then delete the stub file.\n\n"
                    f"Action ID: {verdict.action_id}\n"
                    f"Proposed by: {action.agent_id}\n"
                    f"Reason: {action.reason}"
                )
                repo.create_file(
                    path=tf_file_path,
                    message=commit_message,
                    content=tf_content.encode(),
                    branch=branch_name,
                )
                logger.info(
                    "TerraformPRGenerator: no existing .tf resource found for '%s' "
                    "— created stub file '%s'",
                    resource_short, tf_file_path,
                )

            # Build PR
            pr_title = (
                f"[RuriSkry] "
                f"{action.action_type.value.replace('_', ' ').title()}: "
                f"{resource_short}"
            )
            pr_body = self._build_pr_body(verdict, record)

            pr = repo.create_pull(
                title=pr_title,
                body=pr_body,
                head=branch_name,
                base=default_branch,
            )

            # Add labels (best-effort — labels may not exist in the repo yet)
            try:
                pr.add_to_labels("ruriskry", "auto-generated", "governance-approved")
            except GithubException:
                pass

            record.status = ExecutionStatus.pr_created
            record.pr_url = pr.html_url
            record.pr_number = pr.number
            record.updated_at = datetime.now(timezone.utc)

            logger.info(
                "TerraformPRGenerator: PR #%d created — %s",
                pr.number,
                pr.html_url,
            )

        except GithubException as exc:
            logger.error("TerraformPRGenerator: GitHub API error — %s", exc)
            record.status = ExecutionStatus.failed
            record.notes = f"GitHub API error: {exc}"
            record.updated_at = datetime.now(timezone.utc)

        except Exception as exc:  # noqa: BLE001
            logger.error("TerraformPRGenerator: unexpected error — %s", exc)
            record.status = ExecutionStatus.failed
            record.notes = f"Error creating PR: {exc}"
            record.updated_at = datetime.now(timezone.utc)

        return record

    def _apply_nsg_fix_to_content(self, content: str, rule_name: str) -> str | None:
        """Find an NSG rule block for rule_name and change Allow→Deny.

        Handles two Terraform patterns:
        1. Standalone resource "azurerm_network_security_rule" block
        2. Inline security_rule {} block inside resource "azurerm_network_security_group"

        Uses a line-by-line brace-counting walk (no HCL parser needed).
        Returns modified file content, or None if rule not found / already Deny.
        """
        lines = content.split("\n")

        # --- Pass 1: standalone azurerm_network_security_rule resource ---
        resource_start: int | None = None
        rule_found = False
        brace_depth = 0

        for i, line in enumerate(lines):
            stripped = line.strip()

            if re.match(r'resource\s+"azurerm_network_security_rule"', stripped):
                resource_start = i
                rule_found = False
                brace_depth = 0

            if resource_start is not None:
                brace_depth += stripped.count("{") - stripped.count("}")

                if re.search(
                    r'name\s*=\s*["\']' + re.escape(rule_name) + r'["\']', line
                ):
                    rule_found = True

                if brace_depth <= 0 and i > resource_start:
                    if rule_found:
                        logger.info(
                            "TerraformPRGenerator: found rule '%s' as standalone resource at line %d",
                            rule_name, resource_start + 1,
                        )
                        return self._patch_block(lines, resource_start, i)
                    resource_start = None
                    rule_found = False

        # --- Pass 2: inline security_rule {} inside azurerm_network_security_group ---
        sr_start: int | None = None
        sr_brace_depth = 0
        sr_rule_found = False

        for i, line in enumerate(lines):
            stripped = line.strip()

            # Match both "security_rule {" (same line) and "security_rule" (next line brace)
            if sr_start is None and re.match(r'security_rule\b', stripped):
                sr_start = i
                sr_brace_depth = stripped.count("{") - stripped.count("}")
                sr_rule_found = False
                if "{" in stripped:
                    continue  # brace already counted; skip name check on opening line

            if sr_start is not None and i > sr_start:
                sr_brace_depth += stripped.count("{") - stripped.count("}")

                if re.search(
                    r'name\s*=\s*["\']' + re.escape(rule_name) + r'["\']', line
                ):
                    sr_rule_found = True

                if sr_brace_depth <= 0:
                    if sr_rule_found:
                        logger.info(
                            "TerraformPRGenerator: found rule '%s' as inline security_rule at line %d",
                            rule_name, sr_start + 1,
                        )
                        return self._patch_block(lines, sr_start, i)
                    sr_start = None
                    sr_rule_found = False

        logger.debug(
            "TerraformPRGenerator: rule '%s' not found in content (%d lines)",
            rule_name, len(lines),
        )
        return None

    def _patch_block(self, lines: list[str], start: int, end: int) -> str | None:
        """Change access = "Allow" → "Deny" within lines[start:end+1].

        Returns the full modified file content as a string, or None if no change
        was made (rule already set to Deny).
        """
        new_lines = list(lines)
        changed = False
        for j in range(start, end + 1):
            patched = re.sub(
                r'(\baccess\s*=\s*)"Allow"', r'\1"Deny"',
                new_lines[j], flags=re.IGNORECASE,
            )
            patched = re.sub(
                r"(\baccess\s*=\s*)'Allow'", r"\1'Deny'",
                patched, flags=re.IGNORECASE,
            )
            if patched != new_lines[j]:
                changed = True
            new_lines[j] = patched
        return "\n".join(new_lines) if changed else None

    def _find_and_patch_tf_file(
        self,
        repo,  # github.Repository.Repository
        iac_path: str,
        action,  # ProposedAction
    ) -> dict | None:
        """Search .tf files in iac_path for the target resource and apply the fix.

        Handles MODIFY_NSG for two Terraform patterns:
        - Standalone ``resource "azurerm_network_security_rule"`` blocks
        - Inline ``security_rule {}`` blocks inside ``azurerm_network_security_group``

        Returns a dict ``{path, new_content, sha, summary}`` on success, or None
        when no matching resource was found in any .tf file.
        """
        if action.action_type != ActionType.MODIFY_NSG:
            return None

        # Quoted: rule 'name' or rule "name" — or unquoted Azure-style identifier
        rule_match = re.search(
            r"rule ['\"]([^'\"]+)['\"]", action.reason, re.IGNORECASE
        ) or re.search(r"\brule\s+([\w][\w]*[-_][\w\-_]+)", action.reason, re.IGNORECASE)
        if not rule_match:
            logger.warning(
                "TerraformPRGenerator: cannot extract rule name from reason — "
                "falling back to stub. Reason: %.120s", action.reason
            )
            return None
        rule_name = rule_match.group(1)
        logger.info("TerraformPRGenerator: extracted rule_name='%s' from reason", rule_name)

        try:
            items = repo.get_contents(iac_path)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "TerraformPRGenerator: get_contents('%s') failed — %s", iac_path, exc
            )
            return None

        # Collect .tf files one level deep (including one layer of subdirectories)
        tf_files = []
        for item in items:
            if item.name.endswith(".tf"):
                tf_files.append(item)
            elif item.type == "dir":
                try:
                    sub_items = repo.get_contents(item.path)
                    tf_files.extend(f for f in sub_items if f.name.endswith(".tf"))
                except Exception:  # noqa: BLE001
                    pass

        logger.info(
            "TerraformPRGenerator: scanning %d .tf file(s) in '%s' for rule '%s'",
            len(tf_files), iac_path, rule_name,
        )

        for tf_file in tf_files:
            try:
                original = tf_file.decoded_content.decode("utf-8")
            except Exception:  # noqa: BLE001
                continue
            new_content = self._apply_nsg_fix_to_content(original, rule_name)
            if new_content is not None:
                return {
                    "path": tf_file.path,
                    "new_content": new_content,
                    "sha": tf_file.sha,
                    "rule_name": rule_name,
                    "summary": (
                        f"Changed access = \"Allow\" → \"Deny\" for rule "
                        f"'{rule_name}' in {tf_file.name}"
                    ),
                }
            logger.debug(
                "TerraformPRGenerator: rule '%s' not found (or already Deny) in '%s'",
                rule_name, tf_file.path,
            )

        logger.warning(
            "TerraformPRGenerator: rule '%s' not found in any .tf file under '%s' "
            "— falling back to stub",
            rule_name, iac_path,
        )
        return None

    def _generate_terraform_stub(
        self,
        verdict: GovernanceVerdict,
        record: ExecutionRecord,
    ) -> str:
        """Generate a Terraform HCL stub documenting the approved change.

        This creates a self-documenting stub file that tells the reviewer
        exactly what change to implement.  For a production system you would:
        - Parse the existing .tf files (use the ``hcl2`` library)
        - Generate a targeted diff against the existing resource block
        - Run terraform-cdk for typed configuration changes

        The stub approach is safe: it creates no actual Terraform resources
        but documents the change clearly with a reviewer checklist.
        """
        action = verdict.proposed_action
        resource_id = action.target.resource_id
        resource_short = resource_id.split("/")[-1] if "/" in resource_id else resource_id
        action_type = action.action_type.value
        sri = verdict.skry_risk_index

        header = f"""# =============================================================================
# RuriSkry Governance — Approved Action Stub
# =============================================================================
# Action:       {action_type}
# Resource:     {resource_short}
# Full ID:      {resource_id}
# Proposing Agent: {action.agent_id}
# SRI Composite: {sri.sri_composite:.1f} / 100
#   Infrastructure: {sri.sri_infrastructure:.1f}
#   Policy:         {sri.sri_policy:.1f}
#   Historical:     {sri.sri_historical:.1f}
#   Cost:           {sri.sri_cost:.1f}
# Verdict:      APPROVED
# Reason:       {action.reason}
# Action ID:    {verdict.action_id}
# IaC Tool:     {record.iac_tool}
# IaC Path:     {record.iac_path}
# Generated:    {datetime.now(timezone.utc).isoformat()}
# =============================================================================
#
# REVIEWER INSTRUCTIONS:
# 1. Run `terraform plan` in CI to preview the actual changes.
# 2. Locate the existing resource block for "{resource_short}" in this directory.
# 3. Apply the change described below, then DELETE this stub file.
# 4. The governance verdict is logged in Cosmos DB (Action ID above).
# =============================================================================

"""
        # Action-specific guidance
        if action_type == "scale_up":
            proposed_sku = action.target.proposed_sku or "<new_vm_size>"
            current_sku = action.target.current_sku or "<current_vm_size>"
            return header + f"""# CHANGE REQUIRED: Scale up {resource_short}
# Current SKU:  {current_sku}
# Proposed SKU: {proposed_sku}
#
# In the existing resource block for {resource_short}, update:
#   size = "{proposed_sku}"
# Then delete this file.
"""

        elif action_type == "scale_down":
            proposed_sku = action.target.proposed_sku or "<smaller_vm_size>"
            current_sku = action.target.current_sku or "<current_vm_size>"
            return header + f"""# CHANGE REQUIRED: Scale down {resource_short}
# Current SKU:  {current_sku}
# Proposed SKU: {proposed_sku}
#
# In the existing resource block for {resource_short}, update:
#   size = "{proposed_sku}"
# Then delete this file.
"""

        elif action_type == "delete_resource":
            return header + f"""# CHANGE REQUIRED: Delete {resource_short}
# Agent reason: {action.reason}
#
# WARNING: Removal is IRREVERSIBLE.  Before proceeding:
# - Confirm no live dependencies exist
# - Back up any critical data
#
# In the existing resource block for {resource_short}:
# - Comment out or remove the entire resource block
# - Run `terraform plan` to confirm only this resource is removed
# Then delete this file.
"""

        elif action_type == "modify_nsg":
            return self._generate_nsg_fix(header, action, resource_short, resource_id)

        elif action_type == "restart_service":
            return header + f"""# CHANGE REQUIRED: Restart service on {resource_short}
# Agent reason: {action.reason}
#
# NOTE: Service restarts are typically not managed via Terraform.
# Consider:
# - Using az vm restart or az webapp restart directly
# - Adding a null_resource with a remote-exec provisioner
# Then delete this file.
"""

        elif action_type == "update_config":
            return header + f"""# CHANGE REQUIRED: Configuration update for {resource_short}
# Agent reason: {action.reason}
#
# Apply the configuration change described above to the
# resource block for {resource_short} in this Terraform directory.
# Then delete this file.
"""

        else:
            return header + f"""# CHANGE REQUIRED: {action_type} on {resource_short}
# Agent reason: {action.reason}
#
# Apply the change described above.  Then delete this file.
"""

    def _generate_nsg_fix(
        self,
        header: str,
        action,
        resource_short: str,
        resource_id: str,
    ) -> str:
        """Generate real, actionable Terraform for an NSG security rule fix.

        Parses the agent's reason string to extract rule name, port, and source,
        then generates three concrete remediation options the reviewer can choose from.
        """
        import re  # noqa: PLC0415

        # Extract resource group from ARM ID
        # /subscriptions/.../resourceGroups/ruriskry-prod-rg/providers/.../nsg-east-prod
        rg = action.target.resource_group or ""
        if not rg and "/" in resource_id:
            parts = resource_id.split("/")
            for i, part in enumerate(parts):
                if part.lower() == "resourcegroups" and i + 1 < len(parts):
                    rg = parts[i + 1]
                    break
        rg = rg or "<YOUR_RESOURCE_GROUP>"

        # Parse agent reason for specific rule details
        reason = action.reason
        rule_match = re.search(r"rule ['\"]([^'\"]+)['\"]", reason, re.IGNORECASE)
        rule_name = rule_match.group(1) if rule_match else None

        port_match = re.search(r"destinationPortRange=['\"]?(\d+|\*)['\"]?", reason) or \
                     re.search(r"port (\d+)", reason, re.IGNORECASE)
        port = port_match.group(1) if port_match else "*"

        tf_rule_name = rule_name.replace("-", "_") if rule_name else "offending_rule"

        if rule_name:
            # We know the specific rule — generate targeted fix
            return header + f"""# ===========================================================================
# REAL TERRAFORM FIX — Choose ONE option below, then delete the others.
# After applying, run: terraform plan → verify → terraform apply
# ===========================================================================

# ---------------------------------------------------------------------------
# OPTION A (Recommended): Remove the insecure rule entirely
# ---------------------------------------------------------------------------
# 1. Find the azurerm_network_security_rule resource named "{rule_name}"
#    OR the inline security_rule block inside azurerm_network_security_group.
# 2. Delete that resource block (or the inline block).
# 3. Run: terraform plan   ← confirm only that rule is removed
# 4. Merge this PR to apply via CI/CD.
#
# If defined as a standalone resource, it looks like:
#
# resource "azurerm_network_security_rule" "{tf_rule_name}" {{  ← DELETE THIS BLOCK
#   name  = "{rule_name}"
#   ...
# }}

# ---------------------------------------------------------------------------
# OPTION B: Restrict source to your specific IP (keeps SSH access for you)
# ---------------------------------------------------------------------------
resource "azurerm_network_security_rule" "{tf_rule_name}" {{
  name                        = "{rule_name}"
  priority                    = 140        # Keep existing priority
  direction                   = "Inbound"
  access                      = "Allow"
  protocol                    = "Tcp"
  source_port_range           = "*"
  destination_port_range      = "{port}"
  source_address_prefix       = "YOUR_IP_ADDRESS/32"  # ← Replace with your IP
  destination_address_prefix  = "*"
  resource_group_name         = "{rg}"
  network_security_group_name = "{resource_short}"
}}

# ---------------------------------------------------------------------------
# OPTION C: Add a higher-priority deny rule to block internet access
#           (keeps the existing rule, blocks it for internet sources)
# ---------------------------------------------------------------------------
resource "azurerm_network_security_rule" "deny_{tf_rule_name}_internet" {{
  name                        = "deny-{rule_name}-internet"
  priority                    = 100        # Higher priority than {rule_name} (140)
  direction                   = "Inbound"
  access                      = "Deny"
  protocol                    = "Tcp"
  source_port_range           = "*"
  destination_port_range      = "{port}"
  source_address_prefix       = "Internet"
  destination_address_prefix  = "*"
  resource_group_name         = "{rg}"
  network_security_group_name = "{resource_short}"
}}
"""
        else:
            # Fallback when rule name can't be parsed — still generate real HCL
            return header + f"""# ===========================================================================
# REAL TERRAFORM FIX — Add deny rule to block dangerous inbound access
# ===========================================================================
resource "azurerm_network_security_rule" "deny_dangerous_inbound" {{
  name                        = "deny-dangerous-inbound"
  priority                    = 100
  direction                   = "Inbound"
  access                      = "Deny"
  protocol                    = "Tcp"
  source_port_range           = "*"
  destination_port_range      = "{port}"
  source_address_prefix       = "Internet"
  destination_address_prefix  = "*"
  resource_group_name         = "{rg}"
  network_security_group_name = "{resource_short}"
}}
# Agent reason: {action.reason}
"""

    def _build_pr_body(
        self,
        verdict: GovernanceVerdict,
        record: ExecutionRecord,
    ) -> str:
        """Build the full GitHub PR description with governance context."""
        action = verdict.proposed_action
        sri = verdict.skry_risk_index
        resource_id = action.target.resource_id
        resource_short = resource_id.split("/")[-1] if "/" in resource_id else resource_id
        dashboard_link = self._dashboard_url or ""

        return f"""## RuriSkry Governance — Approved Action

**This PR was auto-generated by the RuriSkry Governance Engine.**
A human must review and merge it to execute the change safely via Terraform.
No resources are modified until a human merges this PR.

---

### Governance Verdict

| Field | Value |
|-------|-------|
| Decision | **APPROVED** |
| SRI™ Composite | **{sri.sri_composite:.1f}** / 100 |
| SRI Infrastructure | {sri.sri_infrastructure:.1f} |
| SRI Policy | {sri.sri_policy:.1f} |
| SRI Historical | {sri.sri_historical:.1f} |
| SRI Financial | {sri.sri_cost:.1f} |
| Verdict Reason | {verdict.reason} |

---

### Proposed Action

| Field | Value |
|-------|-------|
| Resource | `{resource_short}` |
| Full Resource ID | `{resource_id}` |
| Action Type | {action.action_type.value.replace('_', ' ')} |
| Proposing Agent | `{action.agent_id}` |
| Reason | {action.reason} |
| Urgency | {action.urgency.value} |

---

### IaC Context

| Field | Value |
|-------|-------|
| IaC Tool | {record.iac_tool} |
| IaC Path | `{record.iac_path}` |
| Execution ID | `{record.execution_id}` |
| Action ID | `{verdict.action_id}` |

---

### Reviewer Checklist

- [ ] Verify the resource ID matches the intended target
- [ ] Review the Terraform change stub in this PR
- [ ] Run `terraform plan` in CI to preview the actual changes
- [ ] Confirm no unrelated resources are affected
- [ ] Apply the change to the existing .tf file and delete the stub
- [ ] Merge to apply via CI/CD

{f'[View in RuriSkry Dashboard]({dashboard_link})' if dashboard_link else ''}

---
*Auto-generated by [RuriSkry Governance Engine](https://github.com/{record.iac_repo})*
"""
