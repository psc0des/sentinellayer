"""LLM-Driven Execution Agent — plans and executes approved governance actions.

Replaces the hardcoded switch statement in execution_gateway.py with an LLM
that reasons about HOW to implement any approved action dynamically.

Two-phase flow:
1. plan()    — LLM inspects resource state, generates structured execution plan
2. execute() — LLM follows the approved plan, calling Azure SDK write tools

Both phases use the same agent_framework pattern as all other agents in the system.
Mock mode provides deterministic paths for testing (no LLM, no Azure SDK).

Design principles:
1. Plan phase is read-only — no Azure mutations until human approves.
2. Execute phase follows the plan EXACTLY — no scope expansion.
3. Fail-stop — if any step fails, execution halts immediately.
4. Full audit trail — every tool call logged to execution_log.

Usage::

    agent = ExecutionAgent()

    # Phase 1: generate a plan the human can review
    plan = await agent.plan(proposed_action, verdict_snapshot)
    # plan = {"steps": [...], "summary": "...", "commands": [...], ...}

    # Phase 2: execute the human-approved plan
    result = await agent.execute(plan, proposed_action)
    # result = {"success": True, "steps_completed": [...], "summary": "..."}
"""

import json
import logging
import re
from datetime import datetime, timezone

from src.config import settings as _default_settings
from src.core.models import ActionType, ProposedAction, RemediationConfidence

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# System prompts
# ---------------------------------------------------------------------------

_PLAN_INSTRUCTIONS = """\
You are RuriSkry's Execution Planning Agent. An infrastructure action has been
APPROVED by the governance pipeline (SRI™ score below the auto-approve threshold).
Your job is to plan the exact Azure operations needed to implement this action.

You will receive:
- The approved action (type, target resource, reason, urgency)
- The governance verdict (SRI scores, decision rationale)

Your workflow:
1. Call get_resource_details to confirm the resource still exists and check its
   current state (SKU, tags, power state, dependencies).
2. For NSG actions, call list_nsg_rules to confirm the specific rule exists.
3. For metric-driven actions (scale_up, scale_down), call query_metrics to verify
   the current utilisation.
4. Choose the right operation using the decision tree below.
5. Call submit_execution_plan with your structured plan.

OPERATION DECISION TREE — apply in order, stop at first match:

  1. Does a SPECIFIC tool exist for this operation?
     - NSG rule delete: use delete_nsg_rule
     - NSG rule create: use create_nsg_rule
     - VM start/restart: use start_vm or restart_vm
     - VM resize: use resize_vm
     - App Service (Web/sites) restart: use restart_app_service
     - Function App (Web/sites kind=functionapp) restart: use restart_function_app
     - App Service Plan scale (SKU or worker count): use scale_app_service_plan
     - AKS nodepool scale (node count): use scale_aks_nodepool
     - Storage account key rotation: use rotate_storage_keys
     - Resource deletion: use delete_resource
     - Tag update only: use update_resource_tags
     → These are the safest, most tested paths. Use them when they fit.

  2. Is it a PROPERTY UPDATE on an existing resource?
     Storage: allowBlobPublicAccess, supportsHttpsTrafficOnly, minimumTlsVersion, networkAcls
     Key Vault: enableSoftDelete, enablePurgeProtection, publicNetworkAccess
     VM: osProfile settings, disk encryption settings
     Database: publicNetworkAccess, firewallRules, minimalTlsVersion
     App Service: httpsOnly, minTlsVersion, ftpsState
     Any other property on any resource type
     → Call fetch_azure_docs(resource_type) first to confirm the correct api_version.
     → Then use update_resource_property with the confirmed api_version and property_path.
     → property_path uses dot notation: e.g. 'properties.allowBlobPublicAccess'

  3. Does it require CREATING NEW RESOURCES or multi-step orchestration?
     Private endpoints, encryption sets, backup vaults, VNet integration, RBAC roles
     → Use operation="guided_manual" and populate the params with:
       {
         "az_cli_commands": ["az network private-endpoint create ...", "az network ..."],
         "portal_steps": [
           "Navigate to the resource in Azure Portal",
           "Go to Networking → Private endpoint connections",
           "Click + Private endpoint and follow the wizard"
         ],
         "doc_url": "https://learn.microsoft.com/en-us/azure/..."
       }
     Commands must be copy-pasteable. Include every parameter the human needs.
     Portal steps must be numbered and specific (not generic "click settings").

  4. Truly unknown?
     → Use operation="manual" and explain clearly what the human should do.

CONSTRAINTS:
- You may ONLY plan operations that implement the approved action. Do not expand scope.
- Each step must specify: operation name, target ARM ID, parameters, and reason.
- Never guess api_version — always call fetch_azure_docs first for update_resource_property.
- If the resource has already been fixed, submit a plan with empty steps[] and explain why.
- Always include a rollback_hint that captures enough before-state to AUTOMATE the reversal.
  Read the current resource state FIRST with get_resource_details or list_nsg_rules, then
  embed the values needed for reversal. Per operation type:

  * delete_nsg_rule: MANDATORY — embed the original rule as a JSON object using
    the EXACT parameter names of the create_nsg_rule tool (the rollback LLM will
    copy these values verbatim). Call list_nsg_rules BEFORE generating the delete
    step — the rule will be unrecoverable afterwards.
    Required format (these are the exact parameters create_nsg_rule accepts):
    {
      "resource_group": "<rg>",
      "nsg_name": "<nsg-name>",
      "rule_name": "<rule-name>",
      "priority": <integer>,
      "direction": "Inbound" or "Outbound",
      "access": "Allow" or "Deny",
      "protocol": "Tcp" or "Udp" or "*",
      "source_address_prefix": "<value from list_nsg_rules>",
      "destination_address_prefix": "<value from list_nsg_rules>",
      "destination_port_range": "<value from list_nsg_rules>"
    }
    The hint must start with: "Call create_nsg_rule with these exact parameters: "
    followed by the JSON. Do not paraphrase — the rollback LLM needs machine-readable values.
    NOTE: source_port_range is always "*" and is handled internally — do not include it.

  * resize_vm: embed the CURRENT SKU from get_resource_details before resizing.
    Format: "Call resize_vm with resource_group='<rg>', vm_name='<name>', new_size='<original_sku>'"

  * update_resource_property: embed the CURRENT property value before patching.
    Format: "Call update_resource_property to restore <property_path> to <original_value> on <resource_id>"

  * delete_resource: state that deletion is irreversible and reference the IaC source.
    Example: "Cannot auto-rollback — recreate from Terraform module terraform-core/main.tf"

  * start_vm / restart_vm: embed current power state.
    Format: "Call deallocate_vm with resource_group='<rg>', vm_name='<name>' to return to Deallocated state"
- Always include equivalent az CLI commands in the commands[] array (one per step).
"""

_VERIFY_INSTRUCTIONS = """\
You are RuriSkry's Execution Verification Agent. An approved fix has just been
applied to an Azure resource. Your job is to confirm that the fix took effect.

You will receive:
- action_type: what category of fix was applied
- steps_completed: the exact operations that ran, with their result messages

Use steps_completed to understand WHAT was changed, then verify it in Azure:
- delete_nsg_rule step: call list_nsg_rules and confirm the named rule is absent.
- resize_vm step: call get_resource_details and confirm the new SKU is set.
- start_vm / restart_vm step: call get_resource_details and check power state is Running.
- update_resource_property step: call get_resource_details and confirm the property changed.
- delete_resource step: call get_resource_details and confirm the resource returns empty.

IMPORTANT — Azure replication lag:
Azure Resource Graph can take 30–60 seconds to reflect changes made via the ARM API.
If a step reports success but the change is not yet visible in Resource Graph, call
submit_verification_result with confirmed=true and note "change applied, propagation pending".
Trust the step result — if the SDK tool returned success, the change is in Azure.
Only set confirmed=false if the step itself reported failure.

Then call submit_verification_result with:
- confirmed: true if the step succeeded (even if not yet visible in Resource Graph).
- message: a one-line confirmation referencing the specific resource and what changed.

Be concise. Do not propose further actions.
"""

_ROLLBACK_INSTRUCTIONS = """\
You are RuriSkry's Rollback Agent. A previously approved and applied fix now
needs to be reversed.

CRITICAL: The rollback_hint was captured at plan time — BEFORE the fix ran.
It contains the complete original resource state needed to reverse the operation.
Trust it. Do NOT try to query Azure to reconstruct the original state.

Your job:
1. Read the rollback_hint carefully — it is the authoritative source of original state.
2. Execute the inverse operation using the appropriate write tool with the values
   from the hint. Use them exactly — do not modify or look them up from Azure.
3. Call report_step_result for each step with the outcome.

OPERATION-SPECIFIC GUIDANCE:

NSG rule deleted → recreate it:
  The hint contains the original rule properties as JSON (priority, protocol,
  source_address_prefix, destination_address_prefix, destination_port_range, etc.)
  Call create_nsg_rule with those exact values.
  DO NOT call list_nsg_rules — the rule was deleted, it will NOT be there.

VM resized → resize back:
  The hint contains the original SKU. Call resize_vm with it.

VM started → deallocate:
  Call deallocate_vm with the resource_group and vm_name from the hint.

Resource property updated → restore old value:
  The hint contains the original property value. Call update_resource_property to restore it.

Irreversible (resource deleted, etc.):
  Call report_step_result with success=false and explain why in the message.

CONSTRAINTS:
- Only reverse what was applied. Do NOT change anything else.
- Do NOT query Azure to reconstruct original state — the hint has it.
- If the hint is incomplete or missing properties, call report_step_result with
  success=false and message: "Rollback failed: rollback_hint missing required properties".
"""

_EXECUTE_INSTRUCTIONS = """\
You are RuriSkry's Execution Agent. A human has reviewed and approved the
execution plan below. Execute it EXACTLY as specified.

CONSTRAINTS:
- Execute steps in the order listed. Call the appropriate tool for each step.
- After each tool call succeeds, call report_step_result with step_index and outcome.
- If ANY step fails, call report_step_result with success=false and STOP immediately.
  Do NOT continue to subsequent steps after a failure.
- Do NOT add, remove, or modify any steps from the approved plan.
- Do NOT call any investigation tools — the plan phase already confirmed resource state.
- Do NOT propose new actions or expand scope beyond what the plan specifies.
"""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_AUTO_FIX_OPS: frozenset[str] = frozenset({
    "start_vm", "restart_vm", "resize_vm",
    "delete_nsg_rule", "create_nsg_rule",
    "delete_resource", "update_resource_tags",
    # Phase 34A — Tier 1 SDK expansion
    "restart_app_service", "restart_function_app",
    "scale_app_service_plan", "scale_aks_nodepool",
    "rotate_storage_keys",
})


def _compute_confidence(steps: list[dict]) -> str:
    """Return the RemediationConfidence value for a plan based on its operations.

    Priority order (worst confidence wins — determines badge shown to user):
      manual > guided_manual > generic_fix > auto_fix
    This ensures a mixed plan (e.g. one auto step + one manual step) is honestly
    labelled at the lowest confidence level present.
    """
    if not steps:
        return RemediationConfidence.MANUAL.value

    confidence = RemediationConfidence.AUTO_FIX  # start optimistic
    for step in steps:
        op = step.get("operation", "")
        if op == "manual":
            return RemediationConfidence.MANUAL.value   # can't get worse
        elif op == "guided_manual":
            confidence = RemediationConfidence.GUIDED_MANUAL
        elif op == "update_resource_property":
            if confidence not in (RemediationConfidence.GUIDED_MANUAL,):
                confidence = RemediationConfidence.GENERIC_FIX
        elif op not in _AUTO_FIX_OPS:
            # Unknown op — treat as guided_manual conservatively
            if confidence == RemediationConfidence.AUTO_FIX:
                confidence = RemediationConfidence.GUIDED_MANUAL
    return confidence.value


def _build_rollback_commands(
    steps: list[dict],
    captured_nsg_rules: dict[str, list[dict]],
    captured_resource_details: dict[str, dict],
) -> list[dict]:
    """Build pre-computed rollback commands from before-state captured at plan time.

    Returns a list of dicts: [{operation, step_index, params, az_cli?}]
    Operations: create_nsg_rule, resize_vm, deallocate_vm,
                update_resource_property, irreversible, manual.

    These are stored in plan["rollback_commands"] and executed directly at
    rollback time — no LLM reasoning required.
    """
    commands: list[dict] = []

    for i, step in enumerate(steps):
        op = step.get("operation", "")
        params = step.get("params", {})
        target = step.get("target", "")

        if op == "delete_nsg_rule":
            rule_name = params.get("rule_name", "")
            rg = params.get("resource_group", "")
            nsg_name = params.get("nsg_name", "")

            # Search captured NSG data for the original rule properties.
            # Azure live format: rule["properties"][...] or flat rule[...].
            original: dict | None = None
            for rules in captured_nsg_rules.values():
                for r in rules:
                    r_name = r.get("name") or r.get("properties", {}).get("name", "")
                    if r_name.lower() == rule_name.lower():
                        original = r
                        break
                if original:
                    break

            if original:
                # Live Azure uses nested "properties"; seed data is flat.
                props = original.get("properties") or original
                dest_port = (
                    props.get("destinationPortRange")
                    or props.get("destination_port_range")
                    or str(props.get("port", "*"))
                )
                src_addr = (
                    props.get("sourceAddressPrefix")
                    or props.get("source_address_prefix", "*")
                )
                dst_addr = (
                    props.get("destinationAddressPrefix")
                    or props.get("destination_address_prefix", "*")
                )
                priority = props.get("priority", 100)
                direction = props.get("direction", "Inbound")
                access = props.get("access", "Allow")
                protocol = props.get("protocol", "*")
                commands.append({
                    "operation": "create_nsg_rule",
                    "step_index": i,
                    "params": {
                        "resource_group": rg,
                        "nsg_name": nsg_name,
                        "rule_name": rule_name,
                        "priority": priority,
                        "direction": direction,
                        "access": access,
                        "protocol": protocol,
                        "source_address_prefix": src_addr,
                        "destination_address_prefix": dst_addr,
                        "destination_port_range": dest_port,
                    },
                    "az_cli": (
                        f"az network nsg rule create"
                        f" --resource-group {rg} --nsg-name {nsg_name}"
                        f" --name {rule_name} --priority {priority}"
                        f" --direction {direction} --access {access}"
                        f" --protocol {protocol}"
                        f" --source-address-prefixes '{src_addr}'"
                        f" --destination-address-prefixes '{dst_addr}'"
                        f" --destination-port-ranges '{dest_port}'"
                    ),
                })
            else:
                commands.append({
                    "operation": "irreversible",
                    "step_index": i,
                    "reason": (
                        f"Rule '{rule_name}' properties were not captured at plan time "
                        "— auto-rollback unavailable. Restore via Azure Portal or Activity Log."
                    ),
                })

        elif op == "resize_vm":
            rg = params.get("resource_group", "")
            vm_name = params.get("vm_name", "")
            # Find original SKU in captured resource details
            original_sku: str | None = None
            for rid, details in captured_resource_details.items():
                if vm_name.lower() in rid.lower():
                    hw = details.get("properties", {}).get("hardwareProfile", {})
                    original_sku = (
                        hw.get("vmSize")
                        or details.get("sku", {}).get("name")
                        or details.get("current_sku")
                    )
                    break
            if original_sku:
                commands.append({
                    "operation": "resize_vm",
                    "step_index": i,
                    "params": {"resource_group": rg, "vm_name": vm_name, "new_size": original_sku},
                    "az_cli": (
                        f"az vm resize --resource-group {rg}"
                        f" --name {vm_name} --size {original_sku}"
                    ),
                })
            else:
                commands.append({
                    "operation": "manual",
                    "step_index": i,
                    "reason": f"Original SKU for '{vm_name}' not found — resize back manually.",
                })

        elif op in ("start_vm", "restart_vm"):
            rg = params.get("resource_group", "")
            vm_name = params.get("vm_name", "")
            commands.append({
                "operation": "deallocate_vm",
                "step_index": i,
                "params": {"resource_group": rg, "vm_name": vm_name},
                "az_cli": f"az vm deallocate --resource-group {rg} --name {vm_name}",
            })

        elif op == "update_resource_property":
            resource_id = params.get("resource_id", target)
            property_path = params.get("property_path", "")
            api_version = params.get("api_version", "")
            # Navigate the dot-notation path in captured details to find original value
            details = captured_resource_details.get(resource_id, {})
            original_value = None
            if details and property_path:
                obj: object = details
                for part in property_path.split("."):
                    obj = obj.get(part) if isinstance(obj, dict) else None  # type: ignore[union-attr]
                original_value = obj
            if original_value is not None:
                commands.append({
                    "operation": "update_resource_property",
                    "step_index": i,
                    "params": {
                        "resource_id": resource_id,
                        "property_path": property_path,
                        "new_value": original_value,
                        "api_version": api_version,
                    },
                })
            else:
                commands.append({
                    "operation": "manual",
                    "step_index": i,
                    "reason": (
                        f"Original value of '{property_path}' on '{resource_id}' not captured "
                        "— restore manually."
                    ),
                })

        elif op == "delete_resource":
            commands.append({
                "operation": "irreversible",
                "step_index": i,
                "reason": "Resource deletion cannot be auto-rolled back — recreate from IaC.",
            })

        else:
            # guided_manual, manual, unknown
            commands.append({
                "operation": "manual",
                "step_index": i,
                "reason": f"Operation '{op}' has no pre-computed rollback — reverse manually.",
            })

    return commands


# ---------------------------------------------------------------------------
# ExecutionAgent
# ---------------------------------------------------------------------------


class ExecutionAgent:
    """Plans and executes approved governance actions via GPT-4.1.

    In live mode (USE_LOCAL_MOCKS=false + AZURE_OPENAI_ENDPOINT set):
    - plan()    → LLM inspects resource state, generates structured plan
    - execute() → LLM calls Azure SDK write tools step by step

    In mock mode: deterministic paths — no LLM, no Azure SDK calls.
    """

    def __init__(self, cfg=None) -> None:
        self._cfg = cfg or _default_settings
        self._use_framework: bool = (
            not self._cfg.use_local_mocks
            and bool(self._cfg.azure_openai_endpoint)
        )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def plan(self, action: ProposedAction, verdict_snapshot: dict) -> dict:
        """Generate an execution plan for an approved action.

        Returns a dict with:
            steps           — list of {operation, target, params, reason}
            summary         — human-readable description of the plan
            estimated_impact — what will change
            rollback_hint   — how to reverse
            commands        — equivalent az CLI commands (backward compat)
        """
        if not self._use_framework:
            logger.info(
                "ExecutionAgent: mock/no-framework mode — returning deterministic plan "
                "for action '%s' on '%s'",
                action.action_type.value, action.target.resource_id,
            )
            return self._build_mock_plan(action)
        try:
            return await self._plan_with_framework(action, verdict_snapshot)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "ExecutionAgent: plan LLM failed (%s) — falling back to deterministic plan",
                exc,
            )
            return self._build_mock_plan(action)

    async def execute(self, plan: dict, action: ProposedAction, dry_run: bool = False) -> dict:
        """Execute a pre-approved plan.

        Args:
            plan:     Plan dict previously returned by plan().
            action:   The original ProposedAction (for context).
            dry_run:  If True, resolve and validate all SDK calls but skip mutating
                      operations. Returns a structured "would have run" result.
                      Audit record is written with mode="dry_run" in both cases.

        Returns a dict with:
            success         — True if all steps completed
            steps_completed — list of {step, operation, success, message}
            summary         — human-readable result
            mode            — "live" | "dry_run"
        """
        if not self._use_framework:
            logger.info(
                "ExecutionAgent: mock/no-framework mode — simulating execution of %d step(s) "
                "(dry_run=%s)",
                len(plan.get("steps", [])), dry_run,
            )
            return self._execute_mock(plan, dry_run=dry_run)
        try:
            return await self._execute_with_framework(plan, action, dry_run=dry_run)
        except Exception as exc:  # noqa: BLE001
            logger.warning("ExecutionAgent: execute LLM failed (%s)", exc)
            return {
                "success": False,
                "steps_completed": [],
                "summary": f"LLM execution error: {exc}",
                "mode": "dry_run" if dry_run else "live",
            }

    async def verify(self, action: ProposedAction, execute_result: dict) -> dict:
        """Confirm that the execution fix actually took effect on the resource.

        Returns a dict with:
            confirmed   — True if the fix is visible in Azure
            message     — human-readable confirmation string
            checked_at  — ISO 8601 timestamp of the check
        """
        if not self._use_framework:
            return self._verify_mock(action)
        try:
            return await self._verify_with_framework(action, execute_result)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "ExecutionAgent: verify LLM failed (%s) — using deterministic fallback", exc
            )
            return self._verify_mock(action)

    async def rollback(self, action: ProposedAction, plan: dict) -> dict:
        """Reverse a previously applied fix.

        Returns same shape as execute():
            success        — True if rollback completed
            steps_completed — list of {step, operation, success, message}
            summary        — human-readable outcome
        """
        if not self._use_framework:
            return self._rollback_mock(action, plan)
        try:
            return await self._rollback_with_framework(action, plan)
        except Exception as exc:  # noqa: BLE001
            logger.warning("ExecutionAgent: rollback LLM failed (%s)", exc)
            return {
                "success": False,
                "steps_completed": [],
                "summary": f"Rollback failed — LLM agent error: {exc}",
            }

    # ------------------------------------------------------------------
    # Mock / deterministic paths (no LLM, no Azure SDK)
    # ------------------------------------------------------------------

    def _verify_mock(self, action: ProposedAction) -> dict:
        """Return a deterministic verification result (no LLM, no Azure SDK)."""
        proposed_sku = action.target.proposed_sku or "<new_sku>"
        messages = {
            ActionType.MODIFY_NSG:      "NSG rule confirmed removed — no matching rule found.",
            ActionType.RESTART_SERVICE: "VM confirmed running — power state is Running.",
            ActionType.SCALE_UP:        f"VM confirmed resized to {proposed_sku}.",
            ActionType.SCALE_DOWN:      f"VM confirmed resized to {proposed_sku}.",
            ActionType.DELETE_RESOURCE: "Resource confirmed deleted — not found in resource graph.",
        }
        return {
            "confirmed": True,
            "message": messages.get(
                action.action_type, "Configuration change confirmed applied."
            ),
            "checked_at": datetime.now(timezone.utc).isoformat(),
        }

    def _rollback_mock(self, action: ProposedAction, plan: dict) -> dict:
        """Return deterministic rollback result — no LLM, no Azure SDK."""
        current_sku = action.target.current_sku or "<original_sku>"
        resource_name = action.target.resource_id.split("/")[-1] or action.target.resource_id

        op_messages = {
            ActionType.RESTART_SERVICE: (
                "deallocate_vm",
                f"VM '{resource_name}' deallocated — returned to pre-fix state.",
                True,
            ),
            ActionType.SCALE_UP: (
                "resize_vm",
                f"VM '{resource_name}' resized back to {current_sku}.",
                True,
            ),
            ActionType.SCALE_DOWN: (
                "resize_vm",
                f"VM '{resource_name}' resized back to {current_sku}.",
                True,
            ),
            ActionType.MODIFY_NSG: (
                "create_nsg_rule",
                f"NSG rule restored on '{resource_name}' — rule recreated with original settings.",
                True,
            ),
            ActionType.DELETE_RESOURCE: (
                "restore_resource",
                f"Cannot auto-rollback deleted resource '{resource_name}' — manual restoration required.",
                False,
            ),
        }
        operation, message, success = op_messages.get(
            action.action_type,
            ("manual_rollback", "Manual rollback required — operation type not auto-reversible.", False),
        )
        steps_completed = [{
            "step": 0,
            "operation": operation,
            "success": success,
            "message": message,
        }]
        return {
            "success": success,
            "steps_completed": steps_completed,
            "summary": message,
        }

    def _build_mock_plan(self, action: ProposedAction) -> dict:
        """Build a deterministic execution plan without LLM.

        Refactored from the old _build_az_commands() switch statement but now
        returns the richer plan dict structure instead of raw CLI commands.
        Covers ALL 7 ActionType values.
        """
        # Import _parse_arm_id from the gateway (stays there as a utility)
        from src.core.execution_gateway import _parse_arm_id  # noqa: PLC0415

        arm = _parse_arm_id(action.target.resource_id)
        rg = arm["resource_group"] or "<RESOURCE_GROUP>"
        name = arm["name"] or action.target.resource_id
        steps: list[dict] = []
        commands: list[str] = []
        rollback_hint: str = "Reverse the operation manually via Azure Portal if needed"

        if action.action_type == ActionType.MODIFY_NSG:
            # Use structured rule names stored by the deploy agent (primary source).
            # Fall back to regex-parsed reason string only when nsg_rule_names is absent
            # (e.g. older records created before this field was added).
            if action.nsg_rule_names:
                rule_names_list = action.nsg_rule_names
            else:
                rule_match = re.search(
                    r"rule ['\"]([^'\"]+)['\"]", action.reason, re.IGNORECASE
                ) or re.search(
                    r"\brule\s+([\w][\w]*[-_][\w\-_]+)", action.reason, re.IGNORECASE
                )
                rule_names_list = [rule_match.group(1) if rule_match else "<RULE_NAME>"]
            for rule_name in rule_names_list:
                steps.append({
                    "operation": "delete_nsg_rule",
                    "target": action.target.resource_id,
                    "params": {"resource_group": rg, "nsg_name": name, "rule_name": rule_name},
                    "reason": f"Remove insecure NSG rule '{rule_name}'",
                })
                commands.append(
                    f"az network nsg rule delete"
                    f" --resource-group {rg}"
                    f" --nsg-name {name}"
                    f" --name {rule_name}"
                )
            # Build one JSON block per rule. In mock mode the real properties
            # are unknown (no Azure connection), so placeholders are embedded.
            # The rollback LLM reads this hint and calls create_nsg_rule directly —
            # it must NOT call list_nsg_rules (the rule has been deleted).
            rule_blocks = "\n".join(
                json.dumps({
                    "resource_group": rg,
                    "nsg_name": name,
                    "rule_name": r,
                    "priority": "<retrieve-from-activity-log>",
                    "direction": "Inbound",
                    "access": "Allow",
                    "protocol": "Tcp",
                    "source_address_prefix": "*",
                    "destination_address_prefix": "*",
                    "destination_port_range": "<retrieve-from-activity-log>",
                }, indent=2)
                for r in rule_names_list
            )
            quoted = ", ".join(f"'{r}'" for r in rule_names_list)
            rollback_hint = (
                f"Call create_nsg_rule with these exact parameters for rule(s) {quoted}:\n"
                f"{rule_blocks}\n"
                "NOTE: priority and destination_port_range values marked "
                "<retrieve-from-activity-log> must be retrieved from the Azure Activity Log "
                "using get_resource_details before calling create_nsg_rule. "
                "Do NOT call list_nsg_rules — the rule was deleted and will not be found.\n"
                "Manual fallback (az CLI):\n"
                + "\n".join(
                    f"az network nsg rule create"
                    f" --resource-group {rg}"
                    f" --nsg-name {name}"
                    f" --name {r}"
                    f" --priority <original-priority>"
                    f" --direction Inbound --access Allow --protocol Tcp"
                    f" --source-address-prefixes '*'"
                    f" --destination-address-prefixes '*'"
                    f" --destination-port-ranges <original-port>"
                    for r in rule_names_list
                )
            )

        elif action.action_type in (ActionType.SCALE_DOWN, ActionType.SCALE_UP):
            proposed = action.target.proposed_sku or "<NEW_SKU>"
            original = action.target.current_sku or "<ORIGINAL_SKU>"
            resource_type = action.target.resource_type or ""

            if "Web/serverfarms" in resource_type:
                # App Service Plan vertical or horizontal scale
                steps.append({
                    "operation": "scale_app_service_plan",
                    "target": action.target.resource_id,
                    "params": {
                        "resource_group": rg,
                        "plan_name": name,
                        "new_sku_name": proposed,
                        "worker_count": 0,
                    },
                    "reason": f"Scale App Service Plan '{name}' to SKU {proposed}",
                })
                commands.append(
                    f"az appservice plan update"
                    f" --resource-group {rg}"
                    f" --name {name}"
                    f" --sku {proposed}"
                )
                rollback_hint = (
                    f"Scale App Service Plan '{name}' back to {original} using "
                    f"scale_app_service_plan (resource_group: {rg}, plan_name: {name}, "
                    f"new_sku_name: {original})."
                )

            elif "ContainerService" in resource_type:
                # AKS nodepool scale
                config = action.config_changes or {}
                nodepool_name = config.get("nodepool_name", "agentpool")
                try:
                    node_count = int(proposed)
                except (ValueError, TypeError):
                    node_count = 3
                steps.append({
                    "operation": "scale_aks_nodepool",
                    "target": action.target.resource_id,
                    "params": {
                        "resource_group": rg,
                        "cluster_name": name,
                        "nodepool_name": nodepool_name,
                        "node_count": node_count,
                    },
                    "reason": f"Scale AKS nodepool '{nodepool_name}' on '{name}' to {node_count} nodes",
                })
                commands.append(
                    f"az aks nodepool scale"
                    f" --resource-group {rg}"
                    f" --cluster-name {name}"
                    f" --name {nodepool_name}"
                    f" --node-count {node_count}"
                )
                rollback_hint = (
                    f"Scale AKS nodepool '{nodepool_name}' back to its original count using "
                    f"scale_aks_nodepool (resource_group: {rg}, cluster_name: {name}, "
                    f"nodepool_name: {nodepool_name}, node_count: {original})."
                )

            else:
                # VM resize (default)
                steps.append({
                    "operation": "resize_vm",
                    "target": action.target.resource_id,
                    "params": {"resource_group": rg, "vm_name": name, "new_size": proposed},
                    "reason": f"Resize VM from {original} to {proposed}",
                })
                commands.append(
                    f"az vm resize"
                    f" --resource-group {rg}"
                    f" --name {name}"
                    f" --size {proposed}"
                )
                rollback_hint = (
                    f"Resize VM '{name}' back to {original} using resize_vm "
                    f"(resource group: {rg}, vm_name: {name}, new_size: {original})."
                )

        elif action.action_type == ActionType.DELETE_RESOURCE:
            steps.append({
                "operation": "delete_resource",
                "target": action.target.resource_id,
                "params": {"resource_id": arm["full_id"]},
                "reason": f"Delete unused resource '{name}'",
            })
            if arm["full_id"].startswith("/"):
                commands.append(f"az resource delete --ids {arm['full_id']}")
            else:
                commands.append(
                    f"az resource delete"
                    f" --resource-group {rg}"
                    f" --name {name}"
                    f" --resource-type {arm['provider'] or '<PROVIDER/TYPE>'}"
                )
            rollback_hint = (
                f"Resource deletion cannot be automatically reversed. "
                f"Manually restore '{name}' via Azure Portal or re-run the IaC that provisioned it."
            )

        elif action.action_type == ActionType.RESTART_SERVICE:
            resource_type = action.target.resource_type or ""

            if "Web/sites" in resource_type:
                # App Service or Function App restart
                is_function = (
                    "functionapp" in resource_type.lower()
                    or "function" in action.reason.lower()
                    or "function" in name.lower()
                )
                op = "restart_function_app" if is_function else "restart_app_service"
                cmd_verb = "functionapp" if is_function else "webapp"
                steps.append({
                    "operation": op,
                    "target": action.target.resource_id,
                    "params": {"resource_group": rg, "app_name": name},
                    "reason": f"Restart {'Function App' if is_function else 'App Service'} '{name}'",
                })
                commands.append(
                    f"az {cmd_verb} restart"
                    f" --resource-group {rg}"
                    f" --name {name}"
                )
                rollback_hint = (
                    f"{'Function App' if is_function else 'App Service'} restart is idempotent — "
                    f"no rollback required. To stop '{name}': "
                    f"az {cmd_verb} stop --resource-group {rg} --name {name}"
                )

            else:
                # VM (default)
                steps.append({
                    "operation": "start_vm",
                    "target": action.target.resource_id,
                    "params": {"resource_group": rg, "vm_name": name},
                    "reason": f"Start VM '{name}' (may be deallocated or stopped)",
                })
                commands.append(
                    f"az vm start"
                    f" --resource-group {rg}"
                    f" --name {name}"
                )
                rollback_hint = (
                    f"Deallocate VM '{name}' to return it to stopped state using deallocate_vm "
                    f"(resource group: {rg}, vm_name: {name})."
                )

        elif action.action_type == ActionType.ROTATE_STORAGE_KEY:
            config = action.config_changes or {}
            key_name = config.get("key_name", "key1")
            steps.append({
                "operation": "rotate_storage_keys",
                "target": action.target.resource_id,
                "params": {
                    "resource_group": rg,
                    "account_name": name,
                    "key_name": key_name,
                },
                "reason": f"Rotate storage account key '{key_name}' on '{name}'",
            })
            commands.append(
                f"az storage account keys renew"
                f" --resource-group {rg}"
                f" --account-name {name}"
                f" --key {key_name}"
            )
            rollback_hint = (
                f"Key rotation cannot be automatically reversed — the old key value is gone. "
                f"Update all dependents (connection strings, SAS tokens) that used the rotated key. "
                f"To rotate the other key: az storage account keys renew"
                f" --resource-group {rg} --account-name {name} --key key2"
            )

        elif action.action_type == ActionType.UPDATE_CONFIG:
            # Attempt to infer property_path + value from the reason string so
            # mock mode returns a realistic update_resource_property plan rather
            # than "manual".  Falls back to manual when the pattern is unknown.
            prop_map = [
                # Storage
                ("allowBlobPublicAccess", r"allowBlobPublicAccess", "properties.allowBlobPublicAccess", "false"),
                ("supportsHttpsTrafficOnly", r"supportsHttpsTrafficOnly|HTTP traffic allowed", "properties.supportsHttpsTrafficOnly", "true"),
                ("minimumTlsVersion", r"minimumTlsVersion|TLS", "properties.minimumTlsVersion", '"TLS1_2"'),
                # Key Vault
                ("enableSoftDelete", r"soft.delete|enableSoftDelete", "properties.enableSoftDelete", "true"),
                ("enablePurgeProtection", r"purge.protect|enablePurgeProtection", "properties.enablePurgeProtection", "true"),
                # DB / general
                ("publicNetworkAccess", r"publicNetworkAccess|public.network", "properties.publicNetworkAccess", '"Disabled"'),
                # App Service
                ("httpsOnly", r"httpsOnly|HTTPS.only", "properties.httpsOnly", "true"),
            ]
            inferred_prop = None
            inferred_val = None
            for _, pattern, prop, val in prop_map:
                if re.search(pattern, action.reason, re.IGNORECASE):
                    inferred_prop = prop
                    inferred_val = val
                    break

            if inferred_prop:
                steps.append({
                    "operation": "update_resource_property",
                    "target": action.target.resource_id,
                    "params": {
                        "resource_id": arm["full_id"],
                        "api_version": "2023-01-01",  # placeholder — LLM uses fetch_azure_docs
                        "property_path": inferred_prop,
                        "new_value": inferred_val,
                    },
                    "reason": f"Set {inferred_prop}={inferred_val} on '{name}'",
                })
                commands.append(
                    f"az resource patch --ids {arm['full_id']} "
                    f"--api-version 2023-01-01 "
                    f"--properties '{{\" {inferred_prop.split('.')[-1]}\": {inferred_val}}}'"
                )
                prop_short = inferred_prop.split(".")[-1]
                rollback_hint = (
                    f"Revert '{prop_short}' on '{name}' to its previous value using "
                    f"update_resource_property (resource_id: {arm['full_id']}, "
                    f"property_path: {inferred_prop}). "
                    f"Run 'az resource show --ids {arm['full_id']} --query properties' "
                    "BEFORE executing this plan to capture the current value for rollback."
                )
            else:
                steps.append({
                    "operation": "guided_manual",
                    "target": action.target.resource_id,
                    "params": {
                        "az_cli_commands": [
                            f"# Review current resource state:",
                            f"az resource show --ids {arm['full_id']} --query 'properties' -o json",
                            f"# Then apply the configuration change: {action.reason[:100]}",
                        ],
                        "portal_steps": [
                            f"Navigate to '{name}' in the Azure Portal",
                            "Open the relevant settings blade (Configuration / Networking / Security)",
                            f"Apply the change: {action.reason[:120]}",
                            "Save and verify the change took effect",
                        ],
                        "doc_url": "https://learn.microsoft.com/en-us/azure/azure-resource-manager/management/manage-resources-portal",
                    },
                    "reason": (
                        f"Configuration change on '{name}' — "
                        f"follow the guided steps below: {action.reason[:100]}"
                    ),
                })
                commands.append(
                    f"# Guided: {action.reason[:200]}"
                )

        elif action.action_type == ActionType.CREATE_RESOURCE:
            steps.append({
                "operation": "guided_manual",
                "target": action.target.resource_id,
                "params": {
                    "az_cli_commands": [
                        f"# Review and complete the appropriate az CLI command for: {action.reason[:100]}",
                        f"az resource create --resource-group {rg} --name <resource-name> --resource-type <type> --properties '{{}}'"
                    ],
                    "portal_steps": [
                        f"Navigate to resource group '{rg}' in the Azure Portal",
                        "Click + Create and select the appropriate resource type",
                        "Configure all required properties as described in the action reason",
                        "Review and create"
                    ],
                    "doc_url": "https://learn.microsoft.com/en-us/azure/azure-resource-manager/management/manage-resources-portal",
                },
                "reason": f"Create resource — use az CLI commands or Portal steps below ({action.reason[:100]})",
            })
            commands.append(
                f"# Guided: create resource in resource group '{rg}' — see guided_manual steps"
            )

        else:
            steps.append({
                "operation": "guided_manual",
                "target": action.target.resource_id,
                "params": {
                    "az_cli_commands": [
                        f"# Apply action '{action.action_type.value}' on resource '{name}'",
                        f"# Reason: {action.reason[:150]}",
                        f"az resource show --ids {arm['full_id']} --query 'properties' -o json",
                    ],
                    "portal_steps": [
                        f"Search for '{name}' in the Azure Portal search bar",
                        "Open the resource and navigate to the relevant settings blade",
                        f"Apply the change described: {action.reason[:100]}",
                    ],
                    "doc_url": "https://learn.microsoft.com/en-us/azure/azure-resource-manager/",
                },
                "reason": (
                    f"Action '{action.action_type.value}' on '{name}' — "
                    "follow the guided steps below"
                ),
            })
            commands.append(
                f"# Guided: '{action.action_type.value}' on '{name}' — see guided_manual steps"
            )

        return {
            "steps": steps,
            "summary": f"{action.action_type.value} on {name}",
            "estimated_impact": action.reason[:200],
            "rollback_hint": rollback_hint,
            "commands": commands,  # backward compat with existing dashboard rendering
            "remediation_confidence": _compute_confidence(steps),
        }

    def _execute_mock(self, plan: dict, dry_run: bool = False) -> dict:
        """Simulate successful execution of all plan steps (no Azure SDK).

        When dry_run=True, annotates results with [dry_run] prefix and sets mode="dry_run".
        """
        steps_completed = []
        prefix = "[dry_run]" if dry_run else "[mock]"
        for i, step in enumerate(plan.get("steps", [])):
            steps_completed.append({
                "step": i,
                "operation": step.get("operation", "unknown"),
                "success": True,
                "message": (
                    f"{prefix} {step.get('reason', step.get('operation', 'step'))} "
                    "— simulated success"
                ),
            })
        n = len(steps_completed)
        mode_label = "dry-run" if dry_run else "mock"
        return {
            "success": True,
            "steps_completed": steps_completed,
            "summary": f"[{mode_label}] All {n} step{'s' if n != 1 else ''} completed successfully",
            "mode": "dry_run" if dry_run else "live",
        }

    # ------------------------------------------------------------------
    # Live LLM paths
    # ------------------------------------------------------------------

    async def _plan_with_framework(
        self, action: ProposedAction, verdict_snapshot: dict
    ) -> dict:
        """Use GPT-4.1 to inspect resource state and generate an execution plan."""
        from openai import AsyncAzureOpenAI  # noqa: PLC0415
        from azure.identity import DefaultAzureCredential, get_bearer_token_provider  # noqa: PLC0415
        import agent_framework as af  # noqa: PLC0415
        from agent_framework.openai import OpenAIResponsesClient  # noqa: PLC0415
        from src.infrastructure.azure_tools import (  # noqa: PLC0415
            get_resource_details_async,
            list_nsg_rules_async,
            query_metrics_async,
            fetch_resource_type_metadata_async,
        )
        from src.infrastructure.llm_throttle import run_with_throttle  # noqa: PLC0415

        credential = DefaultAzureCredential()
        token_provider = get_bearer_token_provider(
            credential, "https://cognitiveservices.azure.com/.default"
        )
        azure_openai = AsyncAzureOpenAI(
            azure_endpoint=self._cfg.azure_openai_endpoint,
            azure_ad_token_provider=token_provider,
            api_version="2025-03-01-preview",  # Responses API requires this exact version
            timeout=float(self._cfg.llm_timeout),
        )
        client = OpenAIResponsesClient(
            async_client=azure_openai,
            model_id=self._cfg.azure_openai_deployment,
        )

        # Result holder — captured via closure from submit_execution_plan
        plan_holder: list[dict] = []

        # Before-state capture — populated by read tools during planning.
        # Used after planning to build rollback_commands deterministically.
        captured_nsg_rules: dict[str, list[dict]] = {}
        captured_resource_details: dict[str, dict] = {}

        # Proactively capture before-state NOW, before the LLM runs.
        # This guarantees rollback_commands are built even when the LLM
        # omits the read tool call during planning (e.g. skips list_nsg_rules).
        if action.action_type == ActionType.MODIFY_NSG:
            try:
                _pre_rules = await list_nsg_rules_async(action.target.resource_id)
                captured_nsg_rules[action.target.resource_id] = _pre_rules
                logger.info(
                    "ExecutionAgent: pre-captured %d NSG rules from '%s' for rollback",
                    len(_pre_rules), action.target.resource_id,
                )
            except Exception as _exc:  # noqa: BLE001
                logger.warning(
                    "ExecutionAgent: proactive NSG rule capture failed (non-fatal): %s", _exc
                )
        elif action.action_type in (ActionType.SCALE_UP, ActionType.SCALE_DOWN, ActionType.RESTART_SERVICE):
            try:
                _pre_details = await get_resource_details_async(action.target.resource_id)
                captured_resource_details[action.target.resource_id] = _pre_details
                logger.info(
                    "ExecutionAgent: pre-captured resource details for '%s' for rollback",
                    action.target.resource_id,
                )
            except Exception as _exc:  # noqa: BLE001
                logger.warning(
                    "ExecutionAgent: proactive resource details capture failed (non-fatal): %s", _exc
                )

        @af.tool(
            name="get_resource_details",
            description=(
                "Get full details for an Azure resource by ARM ID or short name. "
                "Returns SKU, tags, power state, and dependencies."
            ),
        )
        async def tool_get_resource_details(resource_id: str) -> str:
            details = await get_resource_details_async(resource_id)
            captured_resource_details[resource_id] = details  # capture before-state
            return json.dumps(details, default=str)

        @af.tool(
            name="list_nsg_rules",
            description=(
                "List the security rules for an Azure NSG. "
                "Returns rules with name, port, access (Allow/Deny), priority, direction."
            ),
        )
        async def tool_list_nsg_rules(nsg_resource_id: str) -> str:
            rules = await list_nsg_rules_async(nsg_resource_id)
            captured_nsg_rules[nsg_resource_id] = rules  # capture before-state
            return json.dumps(rules, default=str)

        @af.tool(
            name="query_metrics",
            description=(
                "Query Azure Monitor metrics for a resource. "
                "metric_names is comma-separated. timespan uses ISO 8601 (e.g. 'P7D')."
            ),
        )
        async def tool_query_metrics(
            resource_id: str, metric_names: str, timespan: str = "P7D"
        ) -> str:
            names = [m.strip() for m in metric_names.split(",")]
            results = await query_metrics_async(resource_id, names, timespan)
            return json.dumps(results, default=str)

        @af.tool(
            name="fetch_azure_docs",
            description=(
                "Get the correct ARM API version for an Azure resource type before calling "
                "update_resource_property. Uses the Azure ARM provider metadata API. "
                "resource_type: ARM resource type e.g. 'Microsoft.Storage/storageAccounts', "
                "'Microsoft.KeyVault/vaults', 'Microsoft.Compute/virtualMachines'. "
                "Returns: latest_stable_api_version, all_api_versions. "
                "Always call this BEFORE planning an update_resource_property step — never guess api_version."
            ),
        )
        async def tool_fetch_azure_docs(resource_type: str) -> str:
            metadata = await fetch_resource_type_metadata_async(resource_type)
            return json.dumps(metadata, default=str)

        @af.tool(
            name="submit_execution_plan",
            description=(
                "Submit the final execution plan. Call this once after inspecting "
                "the resource and determining the required operations. "
                "steps is a JSON array of {operation, target, params, reason}. "
                "commands is a JSON array of equivalent az CLI commands."
            ),
        )
        def tool_submit_execution_plan(
            steps_json: str,
            summary: str,
            estimated_impact: str,
            rollback_hint: str,
            commands_json: str = "[]",
        ) -> str:
            try:
                steps = json.loads(steps_json)
            except json.JSONDecodeError:
                steps = []
            try:
                commands = json.loads(commands_json)
            except json.JSONDecodeError:
                commands = []
            plan_holder.append({
                "steps": steps,
                "summary": summary,
                "estimated_impact": estimated_impact,
                "rollback_hint": rollback_hint,
                "commands": commands,
            })
            return f"Plan submitted with {len(steps)} step(s)"

        agent = client.as_agent(
            name="execution-planning-agent",
            instructions=_PLAN_INSTRUCTIONS,
            tools=[
                tool_get_resource_details,
                tool_list_nsg_rules,
                tool_query_metrics,
                tool_fetch_azure_docs,
                tool_submit_execution_plan,
            ],
        )

        action_summary = json.dumps({
            "action_type": action.action_type.value,
            "resource_id": action.target.resource_id,
            "resource_type": action.target.resource_type,
            "resource_group": action.target.resource_group,
            "current_sku": action.target.current_sku,
            "proposed_sku": action.target.proposed_sku,
            "reason": action.reason,
            "urgency": action.urgency.value,
            "nsg_rule_names": action.nsg_rule_names or [],
        }, indent=2)

        verdict_summary = json.dumps({
            "decision": verdict_snapshot.get("decision", "approved"),
            "reason": verdict_snapshot.get("reason", ""),
            "skry_risk_index": verdict_snapshot.get("skry_risk_index", {}),
        }, indent=2)

        prompt = (
            f"An infrastructure action has been APPROVED by the governance pipeline.\n\n"
            f"Approved action:\n{action_summary}\n\n"
            f"Governance verdict:\n{verdict_summary}\n\n"
            "Please inspect the resource, confirm its current state, "
            "then call submit_execution_plan with your plan."
        )

        await run_with_throttle(agent.run, prompt)

        if plan_holder:
            plan = plan_holder[0]
            plan.setdefault("remediation_confidence", _compute_confidence(plan.get("steps", [])))
            # Build pre-computed rollback commands from before-state captured above.
            # These are stored in the execution record and executed directly at rollback
            # time — no LLM reasoning needed, no dependency on Activity Log availability.
            plan["rollback_commands"] = _build_rollback_commands(
                plan.get("steps", []),
                captured_nsg_rules,
                captured_resource_details,
            )
            logger.info(
                "ExecutionAgent: built %d rollback command(s) from captured before-state",
                len(plan["rollback_commands"]),
            )
            return plan

        # LLM didn't call submit_execution_plan — fall back to mock plan
        logger.warning(
            "ExecutionAgent: LLM plan phase returned no plan — using deterministic fallback"
        )
        return self._build_mock_plan(action)

    async def _execute_with_framework(
        self, plan: dict, action: ProposedAction, dry_run: bool = False
    ) -> dict:
        """Use GPT-4.1 to execute the approved plan using Azure SDK write tools."""
        from openai import AsyncAzureOpenAI  # noqa: PLC0415
        from azure.identity import DefaultAzureCredential, get_bearer_token_provider  # noqa: PLC0415
        import agent_framework as af  # noqa: PLC0415
        from agent_framework.openai import OpenAIResponsesClient  # noqa: PLC0415
        from src.infrastructure.llm_throttle import run_with_throttle  # noqa: PLC0415
        from src.infrastructure.azure_tools import fetch_resource_type_metadata_async  # noqa: PLC0415

        credential = DefaultAzureCredential()
        token_provider = get_bearer_token_provider(
            credential, "https://cognitiveservices.azure.com/.default"
        )
        azure_openai = AsyncAzureOpenAI(
            azure_endpoint=self._cfg.azure_openai_endpoint,
            azure_ad_token_provider=token_provider,
            api_version="2025-03-01-preview",  # Responses API requires this exact version
            timeout=float(self._cfg.llm_timeout),
        )
        client = OpenAIResponsesClient(
            async_client=azure_openai,
            model_id=self._cfg.azure_openai_deployment,
        )

        # Execution log — captured via closure from report_step_result
        execution_log: list[dict] = []
        cfg = self._cfg  # Capture for use in nested tool closures

        # ------------------------------------------------------------------
        # Write tools — each wraps one Azure SDK operation
        # ------------------------------------------------------------------

        @af.tool(
            name="start_vm",
            description="Start a stopped or deallocated Azure VM.",
        )
        async def tool_start_vm(resource_group: str, vm_name: str) -> str:
            from azure.identity.aio import DefaultAzureCredential as AioCredential  # noqa: PLC0415
            from azure.mgmt.compute.aio import ComputeManagementClient  # noqa: PLC0415
            try:
                async with AioCredential() as cred:
                    async with ComputeManagementClient(cred, cfg.azure_subscription_id) as client_:
                        poller = await client_.virtual_machines.begin_start(resource_group, vm_name)
                        await poller.result()
                return json.dumps({
                    "success": True,
                    "message": f"Started VM '{vm_name}' in '{resource_group}'",
                })
            except Exception as exc:  # noqa: BLE001
                return json.dumps({"success": False, "error": str(exc)})

        @af.tool(
            name="restart_vm",
            description="Restart a running Azure VM.",
        )
        async def tool_restart_vm(resource_group: str, vm_name: str) -> str:
            from azure.identity.aio import DefaultAzureCredential as AioCredential  # noqa: PLC0415
            from azure.mgmt.compute.aio import ComputeManagementClient  # noqa: PLC0415
            try:
                async with AioCredential() as cred:
                    async with ComputeManagementClient(cred, cfg.azure_subscription_id) as client_:
                        poller = await client_.virtual_machines.begin_restart(resource_group, vm_name)
                        await poller.result()
                return json.dumps({
                    "success": True,
                    "message": f"Restarted VM '{vm_name}' in '{resource_group}'",
                })
            except Exception as exc:  # noqa: BLE001
                return json.dumps({"success": False, "error": str(exc)})

        @af.tool(
            name="resize_vm",
            description="Resize an Azure VM to a new SKU (e.g. Standard_D4s_v3).",
        )
        async def tool_resize_vm(resource_group: str, vm_name: str, new_size: str) -> str:
            from azure.identity.aio import DefaultAzureCredential as AioCredential  # noqa: PLC0415
            from azure.mgmt.compute.aio import ComputeManagementClient  # noqa: PLC0415
            try:
                async with AioCredential() as cred:
                    async with ComputeManagementClient(cred, cfg.azure_subscription_id) as client_:
                        poller = await client_.virtual_machines.begin_update(
                            resource_group, vm_name,
                            {"hardware_profile": {"vm_size": new_size}},
                        )
                        await poller.result()
                return json.dumps({
                    "success": True,
                    "message": f"Resized VM '{vm_name}' to '{new_size}' in '{resource_group}'",
                })
            except Exception as exc:  # noqa: BLE001
                return json.dumps({"success": False, "error": str(exc)})

        @af.tool(
            name="delete_nsg_rule",
            description="Delete a specific security rule from an Azure NSG.",
        )
        async def tool_delete_nsg_rule(
            resource_group: str, nsg_name: str, rule_name: str
        ) -> str:
            from azure.identity.aio import DefaultAzureCredential as AioCredential  # noqa: PLC0415
            from azure.mgmt.network.aio import NetworkManagementClient  # noqa: PLC0415
            try:
                async with AioCredential() as cred:
                    async with NetworkManagementClient(cred, cfg.azure_subscription_id) as client_:
                        poller = await client_.security_rules.begin_delete(
                            resource_group, nsg_name, rule_name
                        )
                        await poller.result()
                return json.dumps({
                    "success": True,
                    "message": f"Deleted NSG rule '{rule_name}' from '{nsg_name}' in '{resource_group}'",
                })
            except Exception as exc:  # noqa: BLE001
                return json.dumps({"success": False, "error": str(exc)})

        @af.tool(
            name="create_nsg_rule",
            description=(
                "Create or update an Azure NSG security rule. "
                "direction must be 'Inbound' or 'Outbound'. "
                "access must be 'Allow' or 'Deny'. "
                "protocol must be 'Tcp', 'Udp', 'Icmp', or '*'. "
                "Use '*' for source_address_prefix or destination_address_prefix to mean 'Any'. "
                "source_port_range is always '*' (Azure ignores source port for security rules)."
            ),
        )
        async def tool_create_nsg_rule(
            resource_group: str,
            nsg_name: str,
            rule_name: str,
            priority: int,
            direction: str,
            access: str,
            protocol: str,
            source_address_prefix: str,
            destination_address_prefix: str,
            destination_port_range: str,
        ) -> str:
            from azure.identity.aio import DefaultAzureCredential as AioCredential  # noqa: PLC0415
            from azure.mgmt.network.aio import NetworkManagementClient  # noqa: PLC0415
            rule_params = {
                "priority": priority,
                "direction": direction,
                "access": access,
                "protocol": protocol,
                "source_address_prefix": source_address_prefix,
                "destination_address_prefix": destination_address_prefix,
                "destination_port_range": destination_port_range,
                "source_port_range": "*",
            }
            try:
                async with AioCredential() as cred:
                    async with NetworkManagementClient(cred, cfg.azure_subscription_id) as client_:
                        poller = await client_.security_rules.begin_create_or_update(
                            resource_group, nsg_name, rule_name, rule_params
                        )
                        await poller.result()
                return json.dumps({
                    "success": True,
                    "message": (
                        f"Created/updated NSG rule '{rule_name}' on '{nsg_name}' "
                        f"in '{resource_group}'"
                    ),
                })
            except Exception as exc:  # noqa: BLE001
                return json.dumps({"success": False, "error": str(exc)})

        @af.tool(
            name="delete_resource",
            description="Delete an Azure resource by its full ARM ID.",
        )
        async def tool_delete_resource(resource_id: str) -> str:
            from azure.identity.aio import DefaultAzureCredential as AioCredential  # noqa: PLC0415
            from azure.mgmt.resource.resources.aio import ResourceManagementClient  # noqa: PLC0415
            try:
                async with AioCredential() as cred:
                    async with ResourceManagementClient(cred, cfg.azure_subscription_id) as client_:
                        poller = await client_.resources.begin_delete_by_id(
                            resource_id, api_version="2023-07-01"
                        )
                        await poller.result()
                name = resource_id.split("/")[-1]
                return json.dumps({
                    "success": True,
                    "message": f"Deleted resource '{name}' (ARM ID: {resource_id})",
                })
            except Exception as exc:  # noqa: BLE001
                return json.dumps({"success": False, "error": str(exc)})

        @af.tool(
            name="update_resource_tags",
            description=(
                "Update or merge tags on an Azure resource. "
                "tags_json must be a JSON object e.g. '{\"owner\": \"team-sre\", \"env\": \"prod\"}'."
            ),
        )
        async def tool_update_resource_tags(resource_id: str, tags_json: str) -> str:
            from azure.identity.aio import DefaultAzureCredential as AioCredential  # noqa: PLC0415
            from azure.mgmt.resource.resources.aio import ResourceManagementClient  # noqa: PLC0415
            try:
                new_tags = json.loads(tags_json)
            except json.JSONDecodeError:
                new_tags = {}
            try:
                # Get current resource to merge tags
                async with AioCredential() as cred:
                    async with ResourceManagementClient(cred, cfg.azure_subscription_id) as client_:
                        resource = await client_.resources.get_by_id(
                            resource_id, api_version="2023-07-01"
                        )
                        existing_tags = resource.tags or {}
                        merged_tags = {**existing_tags, **new_tags}
                        poller = await client_.resources.begin_update_by_id(
                            resource_id,
                            api_version="2023-07-01",
                            parameters={"tags": merged_tags},
                        )
                        await poller.result()
                name = resource_id.split("/")[-1]
                return json.dumps({
                    "success": True,
                    "message": f"Updated tags on '{name}': {merged_tags}",
                })
            except Exception as exc:  # noqa: BLE001
                return json.dumps({"success": False, "error": str(exc)})

        @af.tool(
            name="fetch_azure_docs",
            description=(
                "Get the correct ARM API version for an Azure resource type. "
                "Call this BEFORE update_resource_property to confirm api_version. "
                "resource_type: ARM type e.g. 'Microsoft.Storage/storageAccounts'."
            ),
        )
        async def tool_fetch_azure_docs_execute(resource_type: str) -> str:
            metadata = await fetch_resource_type_metadata_async(resource_type)
            return json.dumps(metadata, default=str)

        @af.tool(
            name="update_resource_property",
            description=(
                "Update a single property on any Azure resource using the ARM generic PATCH API. "
                "Use this for any configuration property change that does not have a specific tool. "
                "resource_id: full ARM ID of the resource. "
                "api_version: correct API version for this resource type "
                "(call fetch_azure_docs first — never guess). "
                "property_path: dot-separated path within the resource body, e.g. "
                "'properties.allowBlobPublicAccess', 'properties.enableSoftDelete', "
                "'properties.publicNetworkAccess', 'properties.minimumTlsVersion'. "
                "new_value: JSON-serialised value, e.g. 'false', 'true', '\"Disabled\"', '\"TLS1_2\"'. "
                "CANNOT create new child resources or perform operational commands (start/stop/restart). "
                "Use for property updates only."
            ),
        )
        async def tool_update_resource_property(
            resource_id: str, api_version: str, property_path: str, new_value: str
        ) -> str:
            from azure.identity.aio import DefaultAzureCredential as AioCredential  # noqa: PLC0415
            from azure.mgmt.resource.resources.aio import ResourceManagementClient  # noqa: PLC0415
            try:
                # Parse new_value as JSON (handles bool, int, string)
                try:
                    parsed_value = json.loads(new_value)
                except json.JSONDecodeError:
                    parsed_value = new_value  # treat as raw string if not valid JSON

                async with AioCredential() as cred:
                    async with ResourceManagementClient(cred, cfg.azure_subscription_id) as client_:
                        # Read-before-write: GET current resource to confirm it exists
                        resource = await client_.resources.get_by_id(resource_id, api_version)
                        resource_dict = resource.as_dict() if hasattr(resource, "as_dict") else {}

                        # Navigate to parent path to confirm it exists
                        path_parts = property_path.split(".")
                        parent = resource_dict
                        for part in path_parts[:-1]:
                            if not isinstance(parent, dict):
                                return json.dumps({
                                    "success": False,
                                    "error": f"Property path '{property_path}' — parent '{part}' not navigable.",
                                })
                            parent = parent.get(part, {})

                        # Build minimal PATCH body — only the property that changes
                        patch_body: dict = {}
                        target = patch_body
                        for part in path_parts[:-1]:
                            target[part] = {}
                            target = target[part]
                        target[path_parts[-1]] = parsed_value

                        poller = await client_.resources.begin_update_by_id(
                            resource_id, api_version, patch_body
                        )
                        await poller.result()

                name = resource_id.split("/")[-1]
                return json.dumps({
                    "success": True,
                    "message": (
                        f"Updated '{property_path}' to {new_value} on '{name}' "
                        f"(api_version={api_version})"
                    ),
                })
            except Exception as exc:  # noqa: BLE001
                return json.dumps({"success": False, "error": str(exc)})

        # ------------------------------------------------------------------
        # Phase 34A — Tier 1 SDK expansion tools
        # Each accepts dry_run: bool — when True, validates the call but
        # skips the mutating SDK operation and returns a "would have run" result.
        # The outer-scope _dry_run captures the user's intent; the parameter
        # allows the LLM to see and pass it explicitly.
        # ------------------------------------------------------------------
        _dry_run = dry_run

        @af.tool(
            name="restart_app_service",
            description=(
                "Restart an Azure App Service web app. "
                "dry_run=True validates the call without restarting."
            ),
        )
        async def tool_restart_app_service(
            resource_group: str, app_name: str, dry_run: bool = False
        ) -> str:
            if _dry_run or dry_run:
                return json.dumps({
                    "success": True,
                    "mode": "dry_run",
                    "message": (
                        f"[dry_run] Would restart App Service '{app_name}' "
                        f"in '{resource_group}'"
                    ),
                })
            from azure.identity.aio import DefaultAzureCredential as AioCredential  # noqa: PLC0415
            from azure.mgmt.web.aio import WebSiteManagementClient  # noqa: PLC0415
            try:
                async with AioCredential() as cred:
                    async with WebSiteManagementClient(cred, cfg.azure_subscription_id) as client_:
                        await client_.web_apps.restart(resource_group, app_name)
                return json.dumps({
                    "success": True,
                    "message": f"Restarted App Service '{app_name}' in '{resource_group}'",
                })
            except Exception as exc:  # noqa: BLE001
                return json.dumps({"success": False, "error": str(exc)})

        @af.tool(
            name="restart_function_app",
            description=(
                "Restart an Azure Function App. "
                "dry_run=True validates the call without restarting."
            ),
        )
        async def tool_restart_function_app(
            resource_group: str, app_name: str, dry_run: bool = False
        ) -> str:
            if _dry_run or dry_run:
                return json.dumps({
                    "success": True,
                    "mode": "dry_run",
                    "message": (
                        f"[dry_run] Would restart Function App '{app_name}' "
                        f"in '{resource_group}'"
                    ),
                })
            from azure.identity.aio import DefaultAzureCredential as AioCredential  # noqa: PLC0415
            from azure.mgmt.web.aio import WebSiteManagementClient  # noqa: PLC0415
            try:
                async with AioCredential() as cred:
                    async with WebSiteManagementClient(cred, cfg.azure_subscription_id) as client_:
                        await client_.web_apps.restart(resource_group, app_name)
                return json.dumps({
                    "success": True,
                    "message": f"Restarted Function App '{app_name}' in '{resource_group}'",
                })
            except Exception as exc:  # noqa: BLE001
                return json.dumps({"success": False, "error": str(exc)})

        @af.tool(
            name="scale_app_service_plan",
            description=(
                "Scale an Azure App Service Plan to a new SKU tier or worker count. "
                "new_sku_name: e.g. 'P2v2', 'S2', 'B3'. "
                "worker_count: number of workers (0 = leave unchanged). "
                "dry_run=True validates the call without scaling."
            ),
        )
        async def tool_scale_app_service_plan(
            resource_group: str,
            plan_name: str,
            new_sku_name: str,
            worker_count: int = 0,
            dry_run: bool = False,
        ) -> str:
            if _dry_run or dry_run:
                return json.dumps({
                    "success": True,
                    "mode": "dry_run",
                    "message": (
                        f"[dry_run] Would scale App Service Plan '{plan_name}' "
                        f"to SKU '{new_sku_name}'"
                        + (f", {worker_count} workers" if worker_count else "")
                        + f" in '{resource_group}'"
                    ),
                })
            from azure.identity.aio import DefaultAzureCredential as AioCredential  # noqa: PLC0415
            from azure.mgmt.web.aio import WebSiteManagementClient  # noqa: PLC0415
            try:
                async with AioCredential() as cred:
                    async with WebSiteManagementClient(cred, cfg.azure_subscription_id) as client_:
                        existing = await client_.app_service_plans.get(resource_group, plan_name)
                        sku = existing.sku
                        sku.name = new_sku_name
                        if worker_count > 0:
                            sku.capacity = worker_count
                        poller = await client_.app_service_plans.begin_create_or_update(
                            resource_group, plan_name,
                            {"location": existing.location, "sku": sku},
                        )
                        await poller.result()
                return json.dumps({
                    "success": True,
                    "message": (
                        f"Scaled App Service Plan '{plan_name}' to SKU '{new_sku_name}' "
                        f"in '{resource_group}'"
                    ),
                })
            except Exception as exc:  # noqa: BLE001
                return json.dumps({"success": False, "error": str(exc)})

        @af.tool(
            name="scale_aks_nodepool",
            description=(
                "Scale an AKS node pool to a new node count. "
                "dry_run=True validates the call without scaling."
            ),
        )
        async def tool_scale_aks_nodepool(
            resource_group: str,
            cluster_name: str,
            nodepool_name: str,
            node_count: int,
            dry_run: bool = False,
        ) -> str:
            if _dry_run or dry_run:
                return json.dumps({
                    "success": True,
                    "mode": "dry_run",
                    "message": (
                        f"[dry_run] Would scale AKS nodepool '{nodepool_name}' on "
                        f"'{cluster_name}' to {node_count} nodes in '{resource_group}'"
                    ),
                })
            from azure.identity.aio import DefaultAzureCredential as AioCredential  # noqa: PLC0415
            from azure.mgmt.containerservice.aio import ContainerServiceClient  # noqa: PLC0415
            try:
                async with AioCredential() as cred:
                    async with ContainerServiceClient(cred, cfg.azure_subscription_id) as client_:
                        existing = await client_.agent_pools.get(
                            resource_group, cluster_name, nodepool_name
                        )
                        existing.count = node_count
                        poller = await client_.agent_pools.begin_create_or_update(
                            resource_group, cluster_name, nodepool_name, existing
                        )
                        await poller.result()
                return json.dumps({
                    "success": True,
                    "message": (
                        f"Scaled AKS nodepool '{nodepool_name}' to {node_count} nodes "
                        f"on cluster '{cluster_name}' in '{resource_group}'"
                    ),
                })
            except Exception as exc:  # noqa: BLE001
                return json.dumps({"success": False, "error": str(exc)})

        @af.tool(
            name="rotate_storage_keys",
            description=(
                "Rotate (regenerate) an Azure Storage Account key. "
                "key_name: 'key1' or 'key2'. "
                "IMPORTANT: Requires 'Storage Account Key Operator Service Role' "
                "scoped to the specific storage account — not subscription-wide. "
                "dry_run=True validates the call without rotating."
            ),
        )
        async def tool_rotate_storage_keys(
            resource_group: str,
            account_name: str,
            key_name: str = "key1",
            dry_run: bool = False,
        ) -> str:
            if _dry_run or dry_run:
                return json.dumps({
                    "success": True,
                    "mode": "dry_run",
                    "message": (
                        f"[dry_run] Would rotate storage account key '{key_name}' "
                        f"on '{account_name}' in '{resource_group}'"
                    ),
                })
            from azure.identity.aio import DefaultAzureCredential as AioCredential  # noqa: PLC0415
            from azure.mgmt.storage.aio import StorageManagementClient  # noqa: PLC0415
            try:
                async with AioCredential() as cred:
                    async with StorageManagementClient(cred, cfg.azure_subscription_id) as client_:
                        result = await client_.storage_accounts.regenerate_key(
                            resource_group,
                            account_name,
                            {"key_name": key_name},
                        )
                return json.dumps({
                    "success": True,
                    "message": (
                        f"Rotated storage key '{key_name}' on '{account_name}' "
                        f"in '{resource_group}' — update all dependents with the new key"
                    ),
                })
            except Exception as exc:  # noqa: BLE001
                return json.dumps({"success": False, "error": str(exc)})

        @af.tool(
            name="report_step_result",
            description=(
                "Report the outcome of an execution step. "
                "Call this after every tool call. "
                "If success=false, the agent must STOP after this call."
            ),
        )
        def tool_report_step_result(
            step_index: int, success: bool, message: str
        ) -> str:
            execution_log.append({
                "step": step_index,
                "success": success,
                "message": message,
            })
            status = "SUCCESS" if success else "FAILURE — stopping"
            return f"Step {step_index} recorded: {status}"

        agent = client.as_agent(
            name="execution-agent",
            instructions=_EXECUTE_INSTRUCTIONS,
            tools=[
                tool_start_vm,
                tool_restart_vm,
                tool_resize_vm,
                tool_delete_nsg_rule,
                tool_create_nsg_rule,
                tool_delete_resource,
                tool_update_resource_tags,
                tool_fetch_azure_docs_execute,
                tool_update_resource_property,
                # Phase 34A — Tier 1 SDK expansion
                tool_restart_app_service,
                tool_restart_function_app,
                tool_scale_app_service_plan,
                tool_scale_aks_nodepool,
                tool_rotate_storage_keys,
                tool_report_step_result,
            ],
        )

        plan_json = json.dumps(plan, indent=2, default=str)
        dry_run_clause = (
            " Pass dry_run=True to every action tool call — this is a dry run."
            if dry_run else ""
        )
        prompt = (
            f"Execute the following pre-approved plan:\n\n{plan_json}\n\n"
            f"For each step, call the appropriate tool then call report_step_result. "
            f"Stop immediately if any step fails.{dry_run_clause}"
        )

        await run_with_throttle(agent.run, prompt)

        # Determine overall success from log — patch step operations from plan
        for i, log_entry in enumerate(execution_log):
            if "operation" not in log_entry and i < len(plan.get("steps", [])):
                log_entry["operation"] = plan["steps"][i].get("operation", "unknown")

        if not execution_log:
            return {
                "success": False,
                "steps_completed": [],
                "summary": "No steps were executed — LLM did not call any tools",
                "mode": "dry_run" if dry_run else "live",
            }

        all_ok = all(s["success"] for s in execution_log)
        n_ok = sum(1 for s in execution_log if s["success"])
        n_total = len(plan.get("steps", []))

        return {
            "success": all_ok,
            "steps_completed": execution_log,
            "summary": (
                f"All {n_ok} steps completed successfully"
                if all_ok
                else f"{n_ok}/{n_total} steps completed — execution stopped on failure"
            ),
            "mode": "dry_run" if dry_run else "live",
        }

    async def _verify_with_framework(
        self, action: ProposedAction, execute_result: dict
    ) -> dict:
        """Use GPT-4.1 to confirm the fix took effect on the resource."""
        from openai import AsyncAzureOpenAI  # noqa: PLC0415
        from azure.identity import DefaultAzureCredential, get_bearer_token_provider  # noqa: PLC0415
        import agent_framework as af  # noqa: PLC0415
        from agent_framework.openai import OpenAIResponsesClient  # noqa: PLC0415
        from src.infrastructure.azure_tools import (  # noqa: PLC0415
            get_resource_details_async,
            list_nsg_rules_async,
            query_metrics_async,
        )
        from src.infrastructure.llm_throttle import run_with_throttle  # noqa: PLC0415

        credential = DefaultAzureCredential()
        token_provider = get_bearer_token_provider(
            credential, "https://cognitiveservices.azure.com/.default"
        )
        azure_openai = AsyncAzureOpenAI(
            azure_endpoint=self._cfg.azure_openai_endpoint,
            azure_ad_token_provider=token_provider,
            api_version="2025-03-01-preview",
            timeout=float(self._cfg.llm_timeout),
        )
        client = OpenAIResponsesClient(
            async_client=azure_openai,
            model_id=self._cfg.azure_openai_deployment,
        )

        result_holder: list[dict] = []

        @af.tool(name="get_resource_details", description="Get current Azure resource state by ARM ID.")
        async def tool_get_resource_details(resource_id: str) -> str:
            details = await get_resource_details_async(resource_id)
            return json.dumps(details, default=str)

        @af.tool(name="list_nsg_rules", description="List security rules for an Azure NSG.")
        async def tool_list_nsg_rules(nsg_resource_id: str) -> str:
            rules = await list_nsg_rules_async(nsg_resource_id)
            return json.dumps(rules, default=str)

        @af.tool(name="query_metrics", description="Query Azure Monitor metrics for a resource.")
        async def tool_query_metrics(
            resource_id: str, metric_names: str, timespan: str = "PT1H"
        ) -> str:
            names = [m.strip() for m in metric_names.split(",")]
            results = await query_metrics_async(resource_id, names, timespan)
            return json.dumps(results, default=str)

        @af.tool(
            name="submit_verification_result",
            description=(
                "Submit the verification outcome after checking resource state. "
                "confirmed=true means the fix is visible. confirmed=false means it is not yet reflected."
            ),
        )
        def tool_submit_verification_result(confirmed: bool, message: str) -> str:
            result_holder.append({"confirmed": confirmed, "message": message})
            return "Verification recorded."

        agent = client.as_agent(
            name="execution-verification-agent",
            instructions=_VERIFY_INSTRUCTIONS,
            tools=[
                tool_get_resource_details,
                tool_list_nsg_rules,
                tool_query_metrics,
                tool_submit_verification_result,
            ],
        )

        action_summary = json.dumps({
            "action_type": action.action_type.value,
            "resource_id": action.target.resource_id,
            "reason": action.reason,
            "execute_success": execute_result.get("success", False),
            "steps_completed": execute_result.get("steps_completed", []),
        }, indent=2)

        prompt = (
            f"The following fix was applied:\n\n{action_summary}\n\n"
            "Please verify the fix is reflected in Azure. "
            "Use steps_completed to understand exactly what was changed and what to check. "
            "For each step, confirm the expected outcome is now visible in the resource state. "
            "Call submit_verification_result when done."
        )

        await run_with_throttle(agent.run, prompt)

        if result_holder:
            result_holder[0]["checked_at"] = datetime.now(timezone.utc).isoformat()
            return result_holder[0]

        logger.warning("ExecutionAgent: verify LLM returned no result — using mock fallback")
        return self._verify_mock(action)

    async def _rollback_with_framework(
        self, action: ProposedAction, plan: dict
    ) -> dict:
        """Use GPT-4.1 to reverse the applied fix using the stored rollback_hint."""
        from openai import AsyncAzureOpenAI  # noqa: PLC0415
        from azure.identity import DefaultAzureCredential, get_bearer_token_provider  # noqa: PLC0415
        import agent_framework as af  # noqa: PLC0415
        from agent_framework.openai import OpenAIResponsesClient  # noqa: PLC0415
        from src.infrastructure.azure_tools import (  # noqa: PLC0415
            get_resource_details_async,
            list_nsg_rules_async,
        )
        from src.infrastructure.llm_throttle import run_with_throttle  # noqa: PLC0415

        credential = DefaultAzureCredential()
        token_provider = get_bearer_token_provider(
            credential, "https://cognitiveservices.azure.com/.default"
        )
        azure_openai = AsyncAzureOpenAI(
            azure_endpoint=self._cfg.azure_openai_endpoint,
            azure_ad_token_provider=token_provider,
            api_version="2025-03-01-preview",
            timeout=float(self._cfg.llm_timeout),
        )
        client = OpenAIResponsesClient(
            async_client=azure_openai,
            model_id=self._cfg.azure_openai_deployment,
        )

        rollback_log: list[dict] = []

        @af.tool(name="get_resource_details", description="Get current Azure resource state by ARM ID.")
        async def tool_get_resource_details(resource_id: str) -> str:
            details = await get_resource_details_async(resource_id)
            return json.dumps(details, default=str)

        @af.tool(name="list_nsg_rules", description="List security rules for an Azure NSG.")
        async def tool_list_nsg_rules(nsg_resource_id: str) -> str:
            rules = await list_nsg_rules_async(nsg_resource_id)
            return json.dumps(rules, default=str)

        # Reuse same write tools from execute phase
        from src.core.execution_gateway import _parse_arm_id  # noqa: PLC0415

        @af.tool(name="start_vm", description="Start a stopped/deallocated Azure VM.")
        async def tool_start_vm(resource_group: str, vm_name: str) -> str:
            from azure.identity.aio import DefaultAzureCredential as AioCredential  # noqa: PLC0415
            from azure.mgmt.compute.aio import ComputeManagementClient  # noqa: PLC0415
            try:
                async with AioCredential() as cred:
                    async with ComputeManagementClient(cred, self._cfg.azure_subscription_id) as cc:
                        poller = await cc.virtual_machines.begin_start(resource_group, vm_name)
                        await poller.result()
                return json.dumps({"status": "started", "vm": vm_name})
            except Exception as exc:  # noqa: BLE001
                return json.dumps({"status": "failed", "vm": vm_name, "error": str(exc)})

        @af.tool(name="deallocate_vm", description="Deallocate (stop) an Azure VM.")
        async def tool_deallocate_vm(resource_group: str, vm_name: str) -> str:
            from azure.identity.aio import DefaultAzureCredential as AioCredential  # noqa: PLC0415
            from azure.mgmt.compute.aio import ComputeManagementClient  # noqa: PLC0415
            try:
                async with AioCredential() as cred:
                    async with ComputeManagementClient(cred, self._cfg.azure_subscription_id) as cc:
                        poller = await cc.virtual_machines.begin_deallocate(resource_group, vm_name)
                        await poller.result()
                return json.dumps({"status": "deallocated", "vm": vm_name})
            except Exception as exc:  # noqa: BLE001
                return json.dumps({"status": "failed", "vm": vm_name, "error": str(exc)})

        @af.tool(name="resize_vm", description="Resize an Azure VM to a different SKU.")
        async def tool_resize_vm(resource_group: str, vm_name: str, new_size: str) -> str:
            from azure.identity.aio import DefaultAzureCredential as AioCredential  # noqa: PLC0415
            from azure.mgmt.compute.aio import ComputeManagementClient  # noqa: PLC0415
            from azure.mgmt.compute.models import VirtualMachineUpdate  # noqa: PLC0415
            try:
                async with AioCredential() as cred:
                    async with ComputeManagementClient(cred, self._cfg.azure_subscription_id) as cc:
                        poller = await cc.virtual_machines.begin_update(
                            resource_group, vm_name,
                            VirtualMachineUpdate(hardware_profile={"vm_size": new_size}),
                        )
                        await poller.result()
                return json.dumps({"status": "resized", "vm": vm_name, "size": new_size})
            except Exception as exc:  # noqa: BLE001
                return json.dumps({"status": "failed", "vm": vm_name, "error": str(exc)})

        @af.tool(name="create_nsg_rule", description="Create or restore an NSG security rule.")
        async def tool_create_nsg_rule(
            resource_group: str, nsg_name: str, rule_name: str,
            priority: int, direction: str, access: str, protocol: str,
            source_address_prefix: str, destination_address_prefix: str,
            destination_port_range: str,
        ) -> str:
            from azure.identity.aio import DefaultAzureCredential as AioCredential  # noqa: PLC0415
            from azure.mgmt.network.aio import NetworkManagementClient  # noqa: PLC0415
            from azure.mgmt.network.models import SecurityRule  # noqa: PLC0415
            try:
                async with AioCredential() as cred:
                    async with NetworkManagementClient(cred, self._cfg.azure_subscription_id) as nc:
                        poller = await nc.security_rules.begin_create_or_update(
                            resource_group, nsg_name, rule_name,
                            SecurityRule(
                                priority=priority, direction=direction, access=access,
                                protocol=protocol,
                                source_address_prefix=source_address_prefix,
                                destination_address_prefix=destination_address_prefix,
                                destination_port_range=destination_port_range,
                            ),
                        )
                        await poller.result()
                return json.dumps({"status": "created", "rule": rule_name})
            except Exception as exc:  # noqa: BLE001
                return json.dumps({"status": "failed", "rule": rule_name, "error": str(exc)})

        # ------------------------------------------------------------------
        # Path A: pre-computed rollback commands (deterministic, no LLM)
        # ------------------------------------------------------------------
        rollback_commands: list[dict] = plan.get("rollback_commands", [])

        if rollback_commands:
            logger.info(
                "ExecutionAgent: executing %d pre-computed rollback command(s) — no LLM needed",
                len(rollback_commands),
            )
            for cmd in rollback_commands:
                op = cmd.get("operation", "")
                params = cmd.get("params", {})
                idx = cmd.get("step_index", len(rollback_log))

                try:
                    if op == "create_nsg_rule":
                        raw = await tool_create_nsg_rule(**params)
                        result = json.loads(raw)
                        ok = result.get("status") == "created"
                        rollback_log.append({
                            "step": idx, "operation": op, "success": ok, "message": raw,
                        })
                    elif op == "resize_vm":
                        raw = await tool_resize_vm(**params)
                        result = json.loads(raw)
                        ok = result.get("status") == "resized"
                        rollback_log.append({
                            "step": idx, "operation": op, "success": ok, "message": raw,
                        })
                    elif op == "deallocate_vm":
                        raw = await tool_deallocate_vm(**params)
                        result = json.loads(raw)
                        ok = result.get("status") == "deallocated"
                        rollback_log.append({
                            "step": idx, "operation": op, "success": ok, "message": raw,
                        })
                    elif op == "start_vm":
                        raw = await tool_start_vm(**params)
                        result = json.loads(raw)
                        ok = result.get("status") == "started"
                        rollback_log.append({
                            "step": idx, "operation": op, "success": ok, "message": raw,
                        })
                    elif op in ("irreversible", "manual"):
                        reason = cmd.get("reason", f"Operation '{op}' — manual rollback required")
                        rollback_log.append({
                            "step": idx, "operation": op, "success": False, "message": reason,
                        })
                    else:
                        rollback_log.append({
                            "step": idx, "operation": op, "success": False,
                            "message": f"Unknown rollback operation '{op}'",
                        })
                except Exception as exc:  # noqa: BLE001
                    rollback_log.append({
                        "step": idx, "operation": op, "success": False,
                        "message": f"Rollback step failed: {exc}",
                    })

        else:
            # ------------------------------------------------------------------
            # Path B: deterministic reconstruction (fallback when no pre-computed
            # commands — typically execution records created before the proactive-
            # capture fix was deployed).
            #
            # For delete_nsg_rule: query Activity Log for the original rule
            # properties, then issue create_nsg_rule directly.
            # For all other operations: fail with a clear, actionable message.
            # No LLM is used — LLM rollback is inherently unreliable because the
            # deleted resource is gone and activity log KQL does not surface the
            # full rule properties to the model.
            # ------------------------------------------------------------------
            logger.info(
                "ExecutionAgent: no pre-computed rollback_commands — "
                "attempting deterministic reconstruction from plan steps + Activity Log"
            )
            from src.infrastructure.azure_tools import (  # noqa: PLC0415
                get_nsg_rule_properties_from_activity_log,
            )

            for i, step in enumerate(plan.get("steps", [])):
                op = step.get("operation", "")
                params = step.get("params", {})

                if op == "delete_nsg_rule":
                    rule_name = params.get("rule_name", "")
                    rg = params.get("resource_group", "")
                    nsg_name = params.get("nsg_name", "")

                    if not (rule_name and rg and nsg_name):
                        rollback_log.append({
                            "step": i, "operation": op, "success": False,
                            "message": (
                                "delete_nsg_rule step has incomplete params "
                                "(rule_name / resource_group / nsg_name missing) — cannot auto-rollback."
                            ),
                        })
                        continue

                    rule_data = await get_nsg_rule_properties_from_activity_log(rg, nsg_name, rule_name)
                    if rule_data:
                        logger.info(
                            "ExecutionAgent: reconstructed rule '%s' from Activity Log — calling create_nsg_rule",
                            rule_name,
                        )
                        raw = await tool_create_nsg_rule(
                            resource_group=rg, nsg_name=nsg_name, rule_name=rule_name,
                            priority=rule_data["priority"],
                            direction=rule_data["direction"],
                            access=rule_data["access"],
                            protocol=rule_data["protocol"],
                            source_address_prefix=rule_data["source_address_prefix"],
                            destination_address_prefix=rule_data["destination_address_prefix"],
                            destination_port_range=rule_data["destination_port_range"],
                        )
                        result = json.loads(raw)
                        ok = result.get("status") == "created"
                        rollback_log.append({
                            "step": i, "operation": "create_nsg_rule", "success": ok, "message": raw,
                        })
                    else:
                        rollback_log.append({
                            "step": i, "operation": "irreversible", "success": False,
                            "message": (
                                f"Original properties for rule '{rule_name}' not found in Activity Log "
                                f"(searched last 30 days of Log Analytics). "
                                f"To restore manually: Azure Portal → NSG '{nsg_name}' "
                                f"→ Inbound security rules → Add '{rule_name}'. "
                                "Check Activity Log to retrieve the original priority and destination port."
                            ),
                        })

                elif op in ("start_vm", "restart_vm"):
                    rg = params.get("resource_group", "")
                    vm_name = params.get("vm_name", "")
                    raw = await tool_deallocate_vm(resource_group=rg, vm_name=vm_name)
                    result = json.loads(raw)
                    ok = result.get("status") == "deallocated"
                    rollback_log.append({
                        "step": i, "operation": "deallocate_vm", "success": ok, "message": raw,
                    })

                else:
                    rollback_log.append({
                        "step": i, "operation": "manual", "success": False,
                        "message": (
                            f"No pre-computed rollback for operation '{op}'. "
                            "The original resource state was not captured — restore manually."
                        ),
                    })

            if not rollback_log:
                rollback_log.append({
                    "step": 0, "operation": "none", "success": False,
                    "message": "No steps found in stored plan — nothing to roll back.",
                })

        if not rollback_log:
            return {
                "success": False,
                "steps_completed": [],
                "summary": "No rollback steps were executed",
            }

        all_ok = all(s["success"] for s in rollback_log)
        return {
            "success": all_ok,
            "steps_completed": rollback_log,
            "summary": (
                "Rollback completed successfully"
                if all_ok
                else f"Rollback failed — {sum(1 for s in rollback_log if not s['success'])} step(s) failed"
            ),
        }
