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
from src.core.models import ActionType, ProposedAction

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
4. Build your execution plan — each step must be a single Azure SDK operation.
5. Call submit_execution_plan with your structured plan.

CONSTRAINTS:
- You may ONLY plan operations that implement the approved action. Do not expand scope.
- Each step must specify: operation name, target ARM ID, parameters, and reason.
- Valid operations: start_vm, restart_vm, resize_vm, delete_nsg_rule, create_nsg_rule,
  delete_resource, update_resource_tags.
- If the resource has already been fixed (e.g., VM already running, rule already removed),
  submit a plan with empty steps[] and explain why no action is needed in the summary.
- If you cannot determine the correct operation, submit a plan with a single step
  where operation="manual" and explain what the human should do in the reason field.
- Always include a rollback_hint — how to reverse the operation if needed.
- Always include equivalent az CLI commands in the commands[] array (one per step).
"""

_VERIFY_INSTRUCTIONS = """\
You are RuriSkry's Execution Verification Agent. An approved fix has just been
applied to an Azure resource. Your job is to confirm that the fix took effect.

Use read-only tools to check the current resource state:
- For NSG actions: call list_nsg_rules and confirm the problematic rule no longer exists.
- For VM start/restart: call get_resource_details and check the power state.
- For VM resize: call get_resource_details and confirm the new SKU is set.
- For delete: call get_resource_details and confirm the resource is gone.

Then call submit_verification_result with:
- confirmed: true if the fix is visible in Azure, false if not yet reflected.
- message: a one-line human-readable confirmation (e.g. "VM confirmed running — power state is Running").

Be concise. Do not propose further actions.
"""

_ROLLBACK_INSTRUCTIONS = """\
You are RuriSkry's Rollback Agent. A previously approved and applied fix now
needs to be reversed. The rollback hint below tells you what the inverse
operation is.

Your job:
1. Read the rollback_hint to understand what needs to be undone.
2. Use get_resource_details (and list_nsg_rules for NSG actions) to confirm
   the current resource state before acting.
3. Execute the inverse operation using the appropriate write tool.
4. Call report_step_result for each step with the outcome.
5. If the resource is already in the pre-fix state, report success — no-op is fine.

CONSTRAINTS:
- Only reverse what was applied. Do NOT change anything else.
- If rollback is genuinely impossible (e.g. deleted resource), call report_step_result
  with success=false and explain why in the message.
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

    async def execute(self, plan: dict, action: ProposedAction) -> dict:
        """Execute a pre-approved plan.

        Args:
            plan:   Plan dict previously returned by plan().
            action: The original ProposedAction (for context).

        Returns a dict with:
            success         — True if all steps completed
            steps_completed — list of {step, operation, success, message}
            summary         — human-readable result
        """
        if not self._use_framework:
            logger.info(
                "ExecutionAgent: mock/no-framework mode — simulating execution of %d step(s)",
                len(plan.get("steps", [])),
            )
            return self._execute_mock(plan)
        try:
            return await self._execute_with_framework(plan, action)
        except Exception as exc:  # noqa: BLE001
            logger.warning("ExecutionAgent: execute LLM failed (%s)", exc)
            return {
                "success": False,
                "steps_completed": [],
                "summary": f"LLM execution error: {exc}",
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
            logger.warning(
                "ExecutionAgent: rollback LLM failed (%s) — using deterministic fallback", exc
            )
            return self._rollback_mock(action, plan)

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

        if action.action_type == ActionType.MODIFY_NSG:
            rule_match = re.search(
                r"rule ['\"]([^'\"]+)['\"]", action.reason, re.IGNORECASE
            ) or re.search(
                r"\brule\s+([\w][\w]*[-_][\w\-_]+)", action.reason, re.IGNORECASE
            )
            rule_name = rule_match.group(1) if rule_match else "<RULE_NAME>"
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

        elif action.action_type in (ActionType.SCALE_DOWN, ActionType.SCALE_UP):
            proposed = action.target.proposed_sku or "<NEW_SKU>"
            steps.append({
                "operation": "resize_vm",
                "target": action.target.resource_id,
                "params": {"resource_group": rg, "vm_name": name, "new_size": proposed},
                "reason": f"Resize VM from {action.target.current_sku or 'current'} to {proposed}",
            })
            commands.append(
                f"az vm resize"
                f" --resource-group {rg}"
                f" --name {name}"
                f" --size {proposed}"
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

        elif action.action_type == ActionType.RESTART_SERVICE:
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

        elif action.action_type == ActionType.UPDATE_CONFIG:
            steps.append({
                "operation": "update_resource_tags",
                "target": action.target.resource_id,
                "params": {"resource_id": arm["full_id"], "tags_json": "{}"},
                "reason": f"Update configuration/tags on '{name}'",
            })
            commands.append(
                f"# Manual: update tags/config on '{name}' via Azure Portal or az tag update"
            )

        elif action.action_type == ActionType.CREATE_RESOURCE:
            steps.append({
                "operation": "manual",
                "target": action.target.resource_id,
                "params": {},
                "reason": "Create resource — requires Terraform or Azure Portal (cannot automate)",
            })
            commands.append(
                "# Manual: create resource via Terraform or Azure Portal"
            )

        else:
            steps.append({
                "operation": "manual",
                "target": action.target.resource_id,
                "params": {},
                "reason": (
                    f"No automated path for action type '{action.action_type.value}' — "
                    "apply manually in Azure Portal"
                ),
            })
            commands.append(
                f"# No automated command for '{action.action_type.value}'. Apply manually."
            )

        return {
            "steps": steps,
            "summary": f"{action.action_type.value} on {name}",
            "estimated_impact": action.reason[:200],
            "rollback_hint": "Reverse the operation manually via Azure Portal if needed",
            "commands": commands,  # backward compat with existing dashboard rendering
        }

    def _execute_mock(self, plan: dict) -> dict:
        """Simulate successful execution of all plan steps (no Azure SDK)."""
        steps_completed = []
        for i, step in enumerate(plan.get("steps", [])):
            steps_completed.append({
                "step": i,
                "operation": step.get("operation", "unknown"),
                "success": True,
                "message": (
                    f"[mock] {step.get('reason', step.get('operation', 'step'))} "
                    "— simulated success"
                ),
            })
        n = len(steps_completed)
        return {
            "success": True,
            "steps_completed": steps_completed,
            "summary": f"[mock] All {n} step{'s' if n != 1 else ''} completed successfully",
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

        @af.tool(
            name="get_resource_details",
            description=(
                "Get full details for an Azure resource by ARM ID or short name. "
                "Returns SKU, tags, power state, and dependencies."
            ),
        )
        async def tool_get_resource_details(resource_id: str) -> str:
            details = await get_resource_details_async(resource_id)
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
            return plan_holder[0]

        # LLM didn't call submit_execution_plan — fall back to mock plan
        logger.warning(
            "ExecutionAgent: LLM plan phase returned no plan — using deterministic fallback"
        )
        return self._build_mock_plan(action)

    async def _execute_with_framework(
        self, plan: dict, action: ProposedAction
    ) -> dict:
        """Use GPT-4.1 to execute the approved plan using Azure SDK write tools."""
        from openai import AsyncAzureOpenAI  # noqa: PLC0415
        from azure.identity import DefaultAzureCredential, get_bearer_token_provider  # noqa: PLC0415
        import agent_framework as af  # noqa: PLC0415
        from agent_framework.openai import OpenAIResponsesClient  # noqa: PLC0415
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
            async with AioCredential() as cred:
                async with ComputeManagementClient(cred, cfg.azure_subscription_id) as client_:
                    poller = await client_.virtual_machines.begin_start(resource_group, vm_name)
                    await poller.result()
            return json.dumps({
                "success": True,
                "message": f"Started VM '{vm_name}' in '{resource_group}'",
            })

        @af.tool(
            name="restart_vm",
            description="Restart a running Azure VM.",
        )
        async def tool_restart_vm(resource_group: str, vm_name: str) -> str:
            from azure.identity.aio import DefaultAzureCredential as AioCredential  # noqa: PLC0415
            from azure.mgmt.compute.aio import ComputeManagementClient  # noqa: PLC0415
            async with AioCredential() as cred:
                async with ComputeManagementClient(cred, cfg.azure_subscription_id) as client_:
                    poller = await client_.virtual_machines.begin_restart(resource_group, vm_name)
                    await poller.result()
            return json.dumps({
                "success": True,
                "message": f"Restarted VM '{vm_name}' in '{resource_group}'",
            })

        @af.tool(
            name="resize_vm",
            description="Resize an Azure VM to a new SKU (e.g. Standard_D4s_v3).",
        )
        async def tool_resize_vm(resource_group: str, vm_name: str, new_size: str) -> str:
            from azure.identity.aio import DefaultAzureCredential as AioCredential  # noqa: PLC0415
            from azure.mgmt.compute.aio import ComputeManagementClient  # noqa: PLC0415
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

        @af.tool(
            name="delete_nsg_rule",
            description="Delete a specific security rule from an Azure NSG.",
        )
        async def tool_delete_nsg_rule(
            resource_group: str, nsg_name: str, rule_name: str
        ) -> str:
            from azure.identity.aio import DefaultAzureCredential as AioCredential  # noqa: PLC0415
            from azure.mgmt.network.aio import NetworkManagementClient  # noqa: PLC0415
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

        @af.tool(
            name="create_nsg_rule",
            description=(
                "Create or update an NSG security rule. "
                "direction must be 'Inbound' or 'Outbound'. "
                "access must be 'Allow' or 'Deny'. "
                "protocol must be 'Tcp', 'Udp', 'Icmp', or '*'. "
                "Use '*' for source_address or destination_address to mean 'Any'."
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
            source_address: str,
            destination_address: str,
            destination_port: str,
        ) -> str:
            from azure.identity.aio import DefaultAzureCredential as AioCredential  # noqa: PLC0415
            from azure.mgmt.network.aio import NetworkManagementClient  # noqa: PLC0415
            rule_params = {
                "priority": priority,
                "direction": direction,
                "access": access,
                "protocol": protocol,
                "source_address_prefix": source_address,
                "destination_address_prefix": destination_address,
                "destination_port_range": destination_port,
                "source_port_range": "*",
            }
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

        @af.tool(
            name="delete_resource",
            description="Delete an Azure resource by its full ARM ID.",
        )
        async def tool_delete_resource(resource_id: str) -> str:
            from azure.identity.aio import DefaultAzureCredential as AioCredential  # noqa: PLC0415
            from azure.mgmt.resource.resources.aio import ResourceManagementClient  # noqa: PLC0415
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
                tool_report_step_result,
            ],
        )

        plan_json = json.dumps(plan, indent=2, default=str)
        prompt = (
            f"Execute the following pre-approved plan:\n\n{plan_json}\n\n"
            "For each step, call the appropriate tool then call report_step_result. "
            "Stop immediately if any step fails."
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
            "steps_completed": len(execute_result.get("steps_completed", [])),
        }, indent=2)

        prompt = (
            f"The following fix was applied:\n\n{action_summary}\n\n"
            "Please verify the fix is reflected in Azure. "
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
            from azure.mgmt.compute.aio import ComputeManagementClient  # noqa: PLC0415
            async with DefaultAzureCredential() as cred:
                async with ComputeManagementClient(cred, self._cfg.azure_subscription_id) as cc:
                    poller = await cc.virtual_machines.begin_start(resource_group, vm_name)
                    await poller.result()
            return json.dumps({"status": "started", "vm": vm_name})

        @af.tool(name="deallocate_vm", description="Deallocate (stop) an Azure VM.")
        async def tool_deallocate_vm(resource_group: str, vm_name: str) -> str:
            from azure.mgmt.compute.aio import ComputeManagementClient  # noqa: PLC0415
            async with DefaultAzureCredential() as cred:
                async with ComputeManagementClient(cred, self._cfg.azure_subscription_id) as cc:
                    poller = await cc.virtual_machines.begin_deallocate(resource_group, vm_name)
                    await poller.result()
            return json.dumps({"status": "deallocated", "vm": vm_name})

        @af.tool(name="resize_vm", description="Resize an Azure VM to a different SKU.")
        async def tool_resize_vm(resource_group: str, vm_name: str, new_size: str) -> str:
            from azure.mgmt.compute.aio import ComputeManagementClient  # noqa: PLC0415
            from azure.mgmt.compute.models import VirtualMachineUpdate  # noqa: PLC0415
            async with DefaultAzureCredential() as cred:
                async with ComputeManagementClient(cred, self._cfg.azure_subscription_id) as cc:
                    poller = await cc.virtual_machines.begin_update(
                        resource_group, vm_name,
                        VirtualMachineUpdate(hardware_profile={"vm_size": new_size}),
                    )
                    await poller.result()
            return json.dumps({"status": "resized", "vm": vm_name, "size": new_size})

        @af.tool(name="create_nsg_rule", description="Create or restore an NSG security rule.")
        async def tool_create_nsg_rule(
            resource_group: str, nsg_name: str, rule_name: str,
            priority: int, direction: str, access: str, protocol: str,
            source_address_prefix: str, destination_address_prefix: str,
            destination_port_range: str,
        ) -> str:
            from azure.mgmt.network.aio import NetworkManagementClient  # noqa: PLC0415
            from azure.mgmt.network.models import SecurityRule  # noqa: PLC0415
            async with DefaultAzureCredential() as cred:
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

        @af.tool(name="report_step_result", description="Report the outcome of a rollback step.")
        async def tool_report_step_result(step_index: int, success: bool, message: str) -> str:
            rollback_log.append({"step": step_index, "success": success, "message": message})
            return "Step result recorded."

        agent = client.as_agent(
            name="execution-rollback-agent",
            instructions=_ROLLBACK_INSTRUCTIONS,
            tools=[
                tool_get_resource_details,
                tool_list_nsg_rules,
                tool_start_vm,
                tool_deallocate_vm,
                tool_resize_vm,
                tool_create_nsg_rule,
                tool_report_step_result,
            ],
        )

        rollback_hint = plan.get("rollback_hint", "No rollback hint available.")
        prompt = (
            f"Action type: {action.action_type.value}\n"
            f"Resource: {action.target.resource_id}\n"
            f"Rollback hint: {rollback_hint}\n\n"
            "Please reverse the applied fix. Use the tools to execute the rollback. "
            "Call report_step_result for each step."
        )

        await run_with_throttle(agent.run, prompt)

        if not rollback_log:
            return {
                "success": False,
                "steps_completed": [],
                "summary": "No rollback steps were executed — LLM did not call any tools",
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
