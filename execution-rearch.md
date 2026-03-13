# Phase 28 — LLM-Driven Execution Agent: Complete Implementation Guide

> **For**: Claude Sonnet implementation session
> **Author**: Claude Opus (architecture + plan)
> **Date**: 2026-03-12

---

## WHY This Change

The execution layer is currently a hardcoded `if/elif` switch statement (`src/core/execution_gateway.py:76-140`) that maps 5 `ActionType` values to 5 Azure SDK calls. If the LLM proposes anything outside this fixed menu, execution falls through to "apply manually in Azure Portal."

This doesn't scale. In enterprise, there are hundreds of possible remediation actions. The investigation layer (3 ops agents) and governance layer (4 gov agents) are already LLM-driven. The execution layer is the last piece that needs intelligence.

**After this change**:
```
Investigation (LLM reasons about WHAT) → Governance (LLM scores risk) → Execution (LLM reasons about HOW)
```

All three operational agents (Monitoring, Deploy, Cost) benefit — the Execution Agent is generic.

---

## ARCHITECTURE: Two-Phase LLM Execution

```
User clicks "Fix by Agent" in dashboard
  ↓
Phase 1: PLAN (LLM call #1 — read-only tools)
  - LLM receives: approved GovernanceVerdict + resource context
  - LLM uses: get_resource_details, list_nsg_rules, query_metrics (read-only)
  - LLM outputs: structured execution plan via submit_execution_plan tool
  - Dashboard shows: step-by-step plan to human for review
  ↓
User reviews plan, clicks "Run"
  ↓
Phase 2: EXECUTE (LLM call #2 — write tools)
  - LLM receives: the approved plan (injected into prompt, NOT regenerated)
  - LLM uses: write tools (start_vm, resize_vm, delete_nsg_rule, etc.)
  - LLM executes: steps in order, reports each result
  - Dashboard shows: success/failure per step
```

### Safety Layers (MUST preserve all)
1. **Governance already approved** — SRI pipeline scored before execution starts
2. **Human reviews plan** — user sees exactly what will happen before confirming
3. **Tool scoping** — execute phase only gets tools relevant to the planned operations
4. **Step-by-step audit** — every SDK call logged to ExecutionRecord
5. **Fail-stop** — if any step fails, LLM stops immediately
6. **HITL always present** — no auto-execution, human must click "Run"

---

## EXISTING PATTERNS TO FOLLOW

All agents in this codebase follow the same pattern. You MUST follow it too.

### Agent Framework Pattern (from `monitoring_agent.py:214-420`)

```python
async def _method_with_framework(self, ...):
    from openai import AsyncAzureOpenAI
    from azure.identity import DefaultAzureCredential, get_bearer_token_provider
    import agent_framework as af
    from agent_framework.openai import OpenAIResponsesClient

    # 1. Create credential + client
    credential = DefaultAzureCredential()
    token_provider = get_bearer_token_provider(
        credential, "https://cognitiveservices.azure.com/.default"
    )
    azure_openai = AsyncAzureOpenAI(
        azure_endpoint=self._cfg.azure_openai_endpoint,
        azure_ad_token_provider=token_provider,
        api_version="2025-03-01-preview",  # CRITICAL: Responses API requires this exact version
        timeout=float(self._cfg.llm_timeout),
    )
    client = OpenAIResponsesClient(
        async_client=azure_openai,
        model_id=self._cfg.azure_openai_deployment,
    )

    # 2. Result holder — closure captures results from tool callbacks
    result_holder: list[dict] = []

    # 3. Define tools as @af.tool decorated functions
    @af.tool(name="tool_name", description="...")
    async def some_tool(param: str) -> str:
        data = await some_async_function(param)
        return json.dumps(data, default=str)  # Always return JSON string

    @af.tool(name="submit_result", description="...")
    def submit_result(result_json: str) -> str:
        result_holder.append(json.loads(result_json))  # Capture via closure
        return "Result recorded"

    # 4. Create agent
    agent = client.as_agent(
        name="agent-name",
        instructions=_INSTRUCTIONS_CONSTANT,
        tools=[some_tool, submit_result],
    )

    # 5. Run with throttle
    from src.infrastructure.llm_throttle import run_with_throttle
    await run_with_throttle(agent.run, prompt)

    # 6. Return from holder
    return result_holder[0] if result_holder else {}
```

### Mock Mode Pattern (from ALL agents)

```python
if not self._use_framework or force_deterministic:
    return self._deterministic_fallback(...)
try:
    return await self._method_with_framework(...)
except Exception:
    return self._deterministic_fallback(...)
```

The `_use_framework` check:
```python
self._use_framework: bool = (
    not self._cfg.use_local_mocks
    and bool(self._cfg.azure_openai_endpoint)
)
```

### Azure SDK Async Pattern (from `azure_tools.py` and `execution_gateway.py`)

```python
from azure.identity.aio import DefaultAzureCredential
from azure.mgmt.compute.aio import ComputeManagementClient

async with DefaultAzureCredential() as credential:
    async with ComputeManagementClient(credential, settings.azure_subscription_id) as client:
        poller = await client.virtual_machines.begin_start(rg, name)
        await poller.result()
```

Both credential AND client must be used as async context managers and both must be closed.

### Read-Only Azure Tools (reuse from `azure_tools.py`)

```python
from src.infrastructure.azure_tools import (
    query_metrics_async,
    get_resource_details_async,
    query_resource_graph_async,
    query_activity_log_async,
    list_nsg_rules_async,
)
```

These are all `async def` functions that return `dict` or `list[dict]`.

### ARM ID Parser (keep from `execution_gateway.py`)

```python
def _parse_arm_id(resource_id: str) -> dict[str, str]:
    # Returns: {"resource_group": "rg", "name": "vm-web-01", "provider": "...", "resource_type": "...", "full_id": "..."}
```

This stays in `execution_gateway.py` and is imported by the execution agent.

---

## FILE-BY-FILE IMPLEMENTATION

### 1. CREATE: `src/core/execution_agent.py` (~350 lines)

This is the core new file. It contains the `ExecutionAgent` class.

```python
"""LLM-Driven Execution Agent — plans and executes approved governance actions.

Replaces the hardcoded switch statement in execution_gateway.py with an LLM
that reasons about HOW to implement any approved action dynamically.

Two-phase flow:
1. plan()   — LLM inspects resource state, generates structured execution plan
2. execute() — LLM follows the approved plan, calling Azure SDK write tools

Both phases use the same agent_framework pattern as all other agents in the system.
Mock mode provides deterministic paths for testing (no LLM, no Azure SDK).

Design principles:
1. Plan phase is read-only — no Azure mutations until human approves.
2. Execute phase follows the plan EXACTLY — no scope expansion.
3. Fail-stop — if any step fails, execution halts immediately.
4. Full audit trail — every tool call logged to execution_log.
"""
```

**Class structure:**

```python
class ExecutionAgent:
    def __init__(self, cfg=None):
        self._cfg = cfg or settings
        self._use_framework = (
            not self._cfg.use_local_mocks
            and bool(self._cfg.azure_openai_endpoint)
        )

    async def plan(self, action: ProposedAction, verdict_snapshot: dict) -> dict:
        """Generate an execution plan for an approved action.

        Returns dict with: steps, summary, estimated_impact, rollback_hint, commands
        """
        if not self._use_framework:
            return self._build_mock_plan(action)
        try:
            return await self._plan_with_framework(action, verdict_snapshot)
        except Exception as exc:
            logger.warning("ExecutionAgent: plan LLM failed (%s) — using mock plan", exc)
            return self._build_mock_plan(action)

    async def execute(self, plan: dict, action: ProposedAction) -> dict:
        """Execute a pre-approved plan.

        Returns dict with: success, steps_completed, summary
        """
        if not self._use_framework:
            return self._execute_mock(plan)
        try:
            return await self._execute_with_framework(plan, action)
        except Exception as exc:
            logger.warning("ExecutionAgent: execute LLM failed (%s)", exc)
            return {"success": False, "steps_completed": [], "summary": f"LLM error: {exc}"}
```

**`_build_mock_plan(action)` — deterministic plan (refactored from old `_build_az_commands`)**:

This method builds the same plan structure but from deterministic logic. It covers ALL 7 ActionType values. For known types, it generates specific steps. For unknown types, it returns a plan with a generic step explaining manual action is needed.

```python
def _build_mock_plan(self, action: ProposedAction) -> dict:
    """Build a deterministic execution plan without LLM.

    Refactored from the old _build_az_commands() switch statement but now
    returns the richer plan dict structure instead of raw CLI commands.
    """
    arm = _parse_arm_id(action.target.resource_id)
    rg = arm["resource_group"] or "<RESOURCE_GROUP>"
    name = arm["name"] or action.target.resource_id
    steps = []
    commands = []

    if action.action_type == ActionType.MODIFY_NSG:
        rule_match = re.search(r"rule ['\"]([^'\"]+)['\"]", action.reason, re.IGNORECASE) \
            or re.search(r"\brule\s+([\w][\w]*[-_][\w\-_]+)", action.reason, re.IGNORECASE)
        rule_name = rule_match.group(1) if rule_match else "<RULE_NAME>"
        steps.append({
            "operation": "delete_nsg_rule",
            "target": action.target.resource_id,
            "params": {"resource_group": rg, "nsg_name": name, "rule_name": rule_name},
            "reason": f"Remove insecure NSG rule '{rule_name}'",
        })
        commands.append(f"az network nsg rule delete --resource-group {rg} --nsg-name {name} --name {rule_name}")

    elif action.action_type in (ActionType.SCALE_DOWN, ActionType.SCALE_UP):
        proposed = action.target.proposed_sku or "<NEW_SKU>"
        steps.append({
            "operation": "resize_vm",
            "target": action.target.resource_id,
            "params": {"resource_group": rg, "vm_name": name, "new_size": proposed},
            "reason": f"Resize VM to {proposed}",
        })
        commands.append(f"az vm resize --resource-group {rg} --name {name} --size {proposed}")

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
            commands.append(f"az resource delete --resource-group {rg} --name {name} --resource-type {arm['provider'] or '<PROVIDER/TYPE>'}")

    elif action.action_type == ActionType.RESTART_SERVICE:
        steps.append({
            "operation": "start_vm",
            "target": action.target.resource_id,
            "params": {"resource_group": rg, "vm_name": name},
            "reason": f"Start VM '{name}' (may be deallocated)",
        })
        commands.append(f"az vm start --resource-group {rg} --name {name}")

    elif action.action_type == ActionType.UPDATE_CONFIG:
        steps.append({
            "operation": "update_resource_tags",
            "target": action.target.resource_id,
            "params": {"resource_id": arm["full_id"], "tags_json": "{}"},
            "reason": f"Update configuration/tags on '{name}'",
        })
        commands.append(f"# Manual: update tags/config on {name} via Azure Portal")

    elif action.action_type == ActionType.CREATE_RESOURCE:
        steps.append({
            "operation": "manual",
            "target": action.target.resource_id,
            "params": {},
            "reason": f"Create resource — requires Terraform or Portal",
        })
        commands.append(f"# Manual: create resource via Terraform or Azure Portal")

    else:
        steps.append({
            "operation": "manual",
            "target": action.target.resource_id,
            "params": {},
            "reason": f"No automated path for '{action.action_type.value}' — apply manually",
        })
        commands.append(f"# No automated command for '{action.action_type.value}'. Apply manually.")

    return {
        "steps": steps,
        "summary": f"{action.action_type.value} on {name}",
        "estimated_impact": action.reason[:200],
        "rollback_hint": "Reverse the operation manually via Azure Portal if needed",
        "commands": commands,  # backward compat with existing dashboard rendering
    }
```

**`_execute_mock(plan)` — mock execution**:

```python
def _execute_mock(self, plan: dict) -> dict:
    """Simulate successful execution of all plan steps."""
    steps_completed = []
    for i, step in enumerate(plan.get("steps", [])):
        steps_completed.append({
            "step": i,
            "operation": step["operation"],
            "success": True,
            "message": f"[mock] {step.get('reason', step['operation'])} — simulated success",
        })
    return {
        "success": True,
        "steps_completed": steps_completed,
        "summary": f"[mock] All {len(steps_completed)} steps completed successfully",
    }
```

**`_plan_with_framework(action, verdict_snapshot)` — LLM plan phase**:

Follow the exact agent framework pattern. The LLM gets:
- Read-only tools from `azure_tools.py` (get_resource_details, list_nsg_rules, query_metrics)
- `submit_execution_plan` tool that captures the plan via closure

System instructions (`_PLAN_INSTRUCTIONS`):
```
You are RuriSkry's Execution Planning Agent. An infrastructure action has been
APPROVED by the governance pipeline (SRI™ score below auto-approve threshold).
Your job is to plan the exact Azure operations needed to implement this action.

You will receive:
- The approved action (type, target resource, reason, urgency)
- The governance verdict (SRI scores, decision rationale)

Your workflow:
1. Call get_resource_details to confirm the resource still exists and check its
   current state (SKU, tags, power state, dependencies).
2. For NSG actions, call list_nsg_rules to confirm the specific rule exists.
3. For metric-driven actions, call query_metrics to verify current state.
4. Build your execution plan — each step must be a single Azure SDK operation.
5. Call submit_execution_plan with your structured plan.

CONSTRAINTS:
- You may ONLY plan operations that implement the approved action. Do not expand scope.
- Each step must specify: operation name, target ARM ID, parameters, and reason.
- Valid operations: start_vm, restart_vm, resize_vm, delete_nsg_rule, create_nsg_rule,
  delete_resource, update_resource_tags.
- If the resource has already been fixed (e.g., VM already running, rule already
  removed), submit a plan with empty steps[] and explain why no action is needed.
- If you cannot determine the correct operation, submit a plan with a single step
  where operation="manual" and explain what the human should do.
- Always include a rollback_hint — how to reverse the operation if needed.
- Always include equivalent az CLI commands in the commands[] array.
```

**`_execute_with_framework(plan, action)` — LLM execute phase**:

The LLM gets ONLY write tools + `report_step_result`. The approved plan is injected into the prompt.

System instructions (`_EXECUTE_INSTRUCTIONS`):
```
You are RuriSkry's Execution Agent. A human has reviewed and approved the
execution plan below. Execute it EXACTLY as specified.

CONSTRAINTS:
- Execute steps in the order listed. Call the appropriate tool for each step.
- After each tool call succeeds, call report_step_result with step_index and outcome.
- If ANY step fails, call report_step_result with success=false and STOP immediately.
  Do NOT continue to subsequent steps after a failure.
- Do NOT add, remove, or modify any steps from the approved plan.
- Do NOT call any investigation tools — the plan phase already confirmed resource state.
- Do NOT propose new actions or expand scope.
```

**Write tools for execute phase** (defined as `@af.tool` inside `_execute_with_framework`):

Each tool wraps a single Azure SDK operation:

```python
@af.tool(name="start_vm", description="Start a stopped/deallocated VM")
async def tool_start_vm(resource_group: str, vm_name: str) -> str:
    from azure.identity.aio import DefaultAzureCredential
    from azure.mgmt.compute.aio import ComputeManagementClient
    async with DefaultAzureCredential() as credential:
        async with ComputeManagementClient(credential, self._cfg.azure_subscription_id) as client:
            poller = await client.virtual_machines.begin_start(resource_group, vm_name)
            await poller.result()
    return json.dumps({"success": True, "message": f"Started VM '{vm_name}' in '{resource_group}'"})

@af.tool(name="restart_vm", description="Restart a running VM")
async def tool_restart_vm(resource_group: str, vm_name: str) -> str:
    # Similar pattern with begin_restart

@af.tool(name="resize_vm", description="Resize a VM to a new SKU")
async def tool_resize_vm(resource_group: str, vm_name: str, new_size: str) -> str:
    # Similar pattern with begin_update + hardware_profile

@af.tool(name="delete_nsg_rule", description="Delete a specific NSG security rule")
async def tool_delete_nsg_rule(resource_group: str, nsg_name: str, rule_name: str) -> str:
    # NetworkManagementClient.security_rules.begin_delete

@af.tool(name="create_nsg_rule", description="Create or update an NSG security rule")
async def tool_create_nsg_rule(resource_group: str, nsg_name: str, rule_name: str,
                                priority: int, direction: str, access: str,
                                protocol: str, source_address: str,
                                destination_address: str, destination_port: str) -> str:
    # NetworkManagementClient.security_rules.begin_create_or_update

@af.tool(name="delete_resource", description="Delete an Azure resource by ARM ID")
async def tool_delete_resource(resource_id: str) -> str:
    # ResourceManagementClient.resources.begin_delete_by_id

@af.tool(name="update_resource_tags", description="Update tags on an Azure resource")
async def tool_update_resource_tags(resource_id: str, tags_json: str) -> str:
    # ResourceManagementClient — merge tags

@af.tool(name="report_step_result", description="Report the outcome of an execution step")
def tool_report_step_result(step_index: int, success: bool, message: str) -> str:
    execution_log.append({"step": step_index, "success": success, "message": message})
    return "Step result recorded"
```

---

### 2. MODIFY: `src/core/execution_gateway.py`

**Remove these functions** (replaced by `ExecutionAgent`):
- `_build_az_commands()` (lines 76-140) — DELETE ENTIRELY
- `_execute_fix_via_sdk()` (lines 143-269) — DELETE ENTIRELY

**Keep these functions**:
- `_parse_arm_id()` (lines 46-73) — still needed, import from here in execution_agent.py

**Modify `generate_agent_fix_commands()`** → rename to `generate_agent_fix_plan()`:

```python
async def generate_agent_fix_plan(self, execution_id: str) -> dict:
    """Generate an LLM-driven execution plan for this issue.

    In mock mode, returns a deterministic plan (same structure).
    In live mode, LLM inspects the resource and generates a plan.
    """
    self._ensure_loaded()
    record = self._records.get(execution_id)
    if not record:
        raise KeyError(f"Execution record not found: {execution_id!r}")

    snapshot = record.verdict_snapshot
    if not snapshot or "proposed_action" not in snapshot:
        raise ValueError(f"Cannot generate plan for {execution_id!r}: verdict snapshot missing")

    action = ProposedAction.model_validate(snapshot["proposed_action"])

    from src.core.execution_agent import ExecutionAgent
    agent = ExecutionAgent(cfg=self._cfg)
    plan = await agent.plan(action, snapshot)

    # Store the plan on the record so execute can use it
    record.execution_plan = plan
    record.updated_at = datetime.now(timezone.utc)
    self._save(record)

    # Add execution_id to the response for the API
    plan["execution_id"] = execution_id
    plan["action_type"] = action.action_type.value
    plan["resource_id"] = action.target.resource_id
    plan["warning"] = "These operations will modify your Azure environment. Review carefully before executing."
    return plan
```

NOTE: This method is now `async` (was sync before). The API endpoint must `await` it.

**Modify `execute_agent_fix()`**:

```python
async def execute_agent_fix(self, execution_id: str, reviewed_by: str) -> ExecutionRecord:
    # ... same validation as before (check record exists, check status) ...

    # Read the stored plan from the record
    plan = record.execution_plan
    if not plan or not plan.get("steps"):
        raise ValueError(f"No execution plan found for {execution_id!r}. Run preview first.")

    action = ProposedAction.model_validate(snapshot["proposed_action"])

    record.reviewed_by = reviewed_by
    record.updated_at = datetime.now(timezone.utc)

    from src.core.execution_agent import ExecutionAgent
    agent = ExecutionAgent(cfg=self._cfg)
    result = await agent.execute(plan, action)

    if result["success"]:
        record.status = ExecutionStatus.applied
        record.notes = f"Agent fix applied.\n{result['summary']}"
    else:
        record.status = ExecutionStatus.failed
        record.notes = f"Agent fix failed.\n{result['summary']}"

    record.execution_log = result.get("steps_completed", [])
    self._save(record)
    return record
```

---

### 3. MODIFY: `src/core/models.py`

Add two fields to `ExecutionRecord`:

```python
class ExecutionRecord(BaseModel):
    # ... existing fields ...
    verdict_snapshot: dict = {}
    execution_plan: Optional[dict] = None   # ADD — stored after plan phase
    execution_log: Optional[list] = None    # ADD — stored after execute phase
```

---

### 4. MODIFY: `src/api/dashboard_api.py`

Find the two agent-fix endpoints and update them.

**`GET /api/execution/{execution_id}/agent-fix-preview`**:

This endpoint currently calls `gateway.generate_agent_fix_commands(execution_id)` which is sync.
Change to `await gateway.generate_agent_fix_plan(execution_id)` (now async).

Find the route (search for `agent-fix-preview`) and update:
```python
# OLD:
preview = _get_execution_gateway().generate_agent_fix_commands(execution_id)
# NEW:
preview = await _get_execution_gateway().generate_agent_fix_plan(execution_id)
```

The response shape now includes `steps`, `summary`, `estimated_impact`, `rollback_hint` in addition to the existing `commands`, `warning`, `execution_id`, `action_type`, `resource_id`.

**`POST /api/execution/{execution_id}/agent-fix-execute`**:

No signature change needed. The gateway's `execute_agent_fix` already reads the plan from the record.

---

### 5. MODIFY: Dashboard UI

**`dashboard/src/components/EvaluationDrilldown.jsx`** and **`dashboard/src/pages/Alerts.jsx`**:

Both files have the same preview rendering pattern. Currently they show:

```jsx
<pre>
    {agentFixPreview?.commands?.map(cmd => `$ ${cmd}`).join('\n')}
</pre>
```

Replace with a richer plan display that shows BOTH the structured steps AND the CLI commands:

```jsx
{/* Plan summary */}
{agentFixPreview?.summary && (
    <p className="text-xs text-slate-300 font-medium mb-2">
        {agentFixPreview.summary}
    </p>
)}

{/* Step-by-step table */}
{agentFixPreview?.steps?.length > 0 && (
    <div className="space-y-1 mb-2">
        {agentFixPreview.steps.map((step, i) => (
            <div key={i} className="flex items-start gap-2 text-xs">
                <span className="text-slate-500 font-mono w-4 shrink-0">{i + 1}.</span>
                <span className="text-teal-400 font-mono shrink-0">{step.operation}</span>
                <span className="text-slate-400">{step.reason}</span>
            </div>
        ))}
    </div>
)}

{/* Equivalent CLI commands (collapsible, for power users) */}
{agentFixPreview?.commands?.length > 0 && (
    <pre className="text-xs text-slate-300 bg-slate-900 rounded p-2 overflow-x-auto border border-slate-700 whitespace-pre-wrap">
        {agentFixPreview.commands.map(cmd => `$ ${cmd}`).join('\n')}
    </pre>
)}

{/* Impact + rollback */}
{agentFixPreview?.estimated_impact && (
    <p className="text-xs text-amber-400/80 mt-1">
        Impact: {agentFixPreview.estimated_impact}
    </p>
)}
{agentFixPreview?.rollback_hint && (
    <p className="text-xs text-slate-500 mt-1">
        Rollback: {agentFixPreview.rollback_hint}
    </p>
)}
```

This rendering appears in 4 places total (2 in EvaluationDrilldown.jsx, 2 in Alerts.jsx — `manual_required` panel and `pr_created` panel each). Update all 4 places.

The error fallback also changes:
```jsx
// OLD:
setAgentFixPreview({ commands: [`# Error: ${err.message}`], warning: '' })
// NEW:
setAgentFixPreview({ commands: [`# Error: ${err.message}`], steps: [], warning: '', summary: `Error: ${err.message}` })
```

**`dashboard/src/api.js`**: Update the JSDoc comment for `fetchAgentFixPreview` to reflect the new return shape. No code change needed.

---

### 6. CREATE: `tests/test_execution_agent.py`

Follow the exact test patterns from `tests/test_execution_gateway.py`.

```python
"""Tests for the LLM-Driven Execution Agent (Phase 28)."""

import pytest
from src.core.execution_agent import ExecutionAgent
from src.core.models import ActionTarget, ActionType, ProposedAction, Urgency


def _make_action(action_type=ActionType.RESTART_SERVICE, resource_id=None, **kwargs):
    """Helper to create test ProposedActions."""
    rid = resource_id or "/subscriptions/sub/resourceGroups/rg/providers/Microsoft.Compute/virtualMachines/vm-web-01"
    return ProposedAction(
        agent_id="test-agent",
        action_type=action_type,
        target=ActionTarget(resource_id=rid, resource_type="Microsoft.Compute/virtualMachines", **kwargs),
        reason=kwargs.get("reason", "Test reason"),
        urgency=Urgency.HIGH,
    )


class TestPlanMockMode:
    """ExecutionAgent.plan() in mock mode (no LLM, no Azure)."""

    @pytest.fixture
    def agent(self, mock_settings):
        mock_settings.use_local_mocks = True
        mock_settings.azure_openai_endpoint = ""
        return ExecutionAgent(cfg=mock_settings)

    @pytest.mark.asyncio
    async def test_plan_restart_service(self, agent):
        action = _make_action(ActionType.RESTART_SERVICE)
        plan = await agent.plan(action, {})
        assert len(plan["steps"]) == 1
        assert plan["steps"][0]["operation"] == "start_vm"
        assert "vm-web-01" in plan["steps"][0]["params"]["vm_name"]
        assert len(plan["commands"]) == 1
        assert "vm start" in plan["commands"][0]

    @pytest.mark.asyncio
    async def test_plan_modify_nsg(self, agent):
        action = _make_action(
            ActionType.MODIFY_NSG,
            resource_id="/subscriptions/sub/resourceGroups/rg/providers/Microsoft.Network/networkSecurityGroups/nsg-prod",
            reason="Insecure rule 'AllowSSH-Any' allows SSH from 0.0.0.0",
        )
        plan = await agent.plan(action, {})
        assert plan["steps"][0]["operation"] == "delete_nsg_rule"
        assert plan["steps"][0]["params"]["rule_name"] == "AllowSSH-Any"
        assert "nsg rule delete" in plan["commands"][0]

    @pytest.mark.asyncio
    async def test_plan_scale_down(self, agent):
        action = _make_action(ActionType.SCALE_DOWN, proposed_sku="Standard_B2ms")
        plan = await agent.plan(action, {})
        assert plan["steps"][0]["operation"] == "resize_vm"
        assert plan["steps"][0]["params"]["new_size"] == "Standard_B2ms"

    @pytest.mark.asyncio
    async def test_plan_delete_resource(self, agent):
        action = _make_action(ActionType.DELETE_RESOURCE)
        plan = await agent.plan(action, {})
        assert plan["steps"][0]["operation"] == "delete_resource"

    @pytest.mark.asyncio
    async def test_plan_update_config(self, agent):
        action = _make_action(ActionType.UPDATE_CONFIG)
        plan = await agent.plan(action, {})
        assert plan["steps"][0]["operation"] == "update_resource_tags"

    @pytest.mark.asyncio
    async def test_plan_create_resource_is_manual(self, agent):
        action = _make_action(ActionType.CREATE_RESOURCE)
        plan = await agent.plan(action, {})
        assert plan["steps"][0]["operation"] == "manual"

    @pytest.mark.asyncio
    async def test_plan_has_backward_compat_commands(self, agent):
        action = _make_action(ActionType.RESTART_SERVICE)
        plan = await agent.plan(action, {})
        assert "commands" in plan
        assert isinstance(plan["commands"], list)

    @pytest.mark.asyncio
    async def test_plan_has_summary_and_impact(self, agent):
        action = _make_action(ActionType.RESTART_SERVICE)
        plan = await agent.plan(action, {})
        assert "summary" in plan
        assert "estimated_impact" in plan
        assert "rollback_hint" in plan


class TestExecuteMockMode:
    """ExecutionAgent.execute() in mock mode."""

    @pytest.fixture
    def agent(self, mock_settings):
        mock_settings.use_local_mocks = True
        mock_settings.azure_openai_endpoint = ""
        return ExecutionAgent(cfg=mock_settings)

    @pytest.mark.asyncio
    async def test_execute_mock_success(self, agent):
        plan = {"steps": [
            {"operation": "start_vm", "target": "vm-01", "params": {"resource_group": "rg", "vm_name": "vm-01"}, "reason": "Start VM"},
        ]}
        result = await agent.execute(plan, _make_action())
        assert result["success"] is True
        assert len(result["steps_completed"]) == 1
        assert result["steps_completed"][0]["success"] is True
        assert "[mock]" in result["steps_completed"][0]["message"]

    @pytest.mark.asyncio
    async def test_execute_mock_empty_plan(self, agent):
        plan = {"steps": []}
        result = await agent.execute(plan, _make_action())
        assert result["success"] is True
        assert len(result["steps_completed"]) == 0

    @pytest.mark.asyncio
    async def test_execute_mock_multi_step(self, agent):
        plan = {"steps": [
            {"operation": "delete_nsg_rule", "target": "nsg-01", "params": {}, "reason": "Step 1"},
            {"operation": "create_nsg_rule", "target": "nsg-01", "params": {}, "reason": "Step 2"},
        ]}
        result = await agent.execute(plan, _make_action())
        assert result["success"] is True
        assert len(result["steps_completed"]) == 2
```

NOTE: The `mock_settings` fixture is already defined in `conftest.py`. Check the existing test files for the exact fixture name. If it doesn't exist, create a simple one that returns a settings-like object with `use_local_mocks=True` and `azure_openai_endpoint=""`.

---

### 7. MODIFY: `tests/test_execution_gateway.py`

**Tests that reference `_build_az_commands`**: These import `_build_az_commands` directly. After the refactor, `_build_az_commands` no longer exists. You have two options:

**Option A** (recommended): Keep `_build_az_commands` as a private function in `execution_gateway.py` (don't delete it — it's used by `_build_mock_plan` in `execution_agent.py`). Actually, the mock plan logic moves to `execution_agent.py`, so the tests should test `ExecutionAgent._build_mock_plan()` instead.

**Option B**: Refactor the tests to test the new `generate_agent_fix_plan()` method.

For simplicity, Option A is better — keep the unit tests testing the mock plan logic directly via `ExecutionAgent`. Update the imports:

```python
# OLD:
from src.core.execution_gateway import _build_az_commands
# NEW:
# Test _build_mock_plan via ExecutionAgent.plan() instead — see test_execution_agent.py
```

The `TestAgentFixFlow` class tests in `test_execution_gateway.py` that call `generate_agent_fix_commands()` need updating:

```python
# OLD:
preview = gateway.generate_agent_fix_commands(record.execution_id)
# NEW:
preview = await gateway.generate_agent_fix_plan(record.execution_id)
```

And the mock mode execution test:
```python
# The test_mock_mode_execution_sets_applied test should still work
# because execute_agent_fix still sets applied on success.
# But it now goes through ExecutionAgent internally.
```

---

### 8. DOC SYNC (after all code changes)

**Update these files wherever relevant**:
- `CONTEXT.md` — update execution gateway description to mention LLM-driven execution
- `STATUS.md` — add Phase 28 entry with test count
- `docs/ARCHITECTURE.md` — update execution flow diagram to show two-phase LLM execution
- `docs/API.md` — update `/api/execution/{id}/agent-fix-preview` response shape
- `learning/56-execution-agent.md` — teach the two-phase LLM execution pattern

---

## IMPLEMENTATION ORDER

1. **`src/core/models.py`** — add `execution_plan` and `execution_log` fields (2 lines)
2. **`src/core/execution_agent.py`** — create the new file with mock paths first
3. **`tests/test_execution_agent.py`** — create tests, run `pytest` to verify mock mode works
4. **`src/core/execution_gateway.py`** — refactor to delegate to ExecutionAgent
5. **`tests/test_execution_gateway.py`** — update tests for new method signatures
6. **`src/api/dashboard_api.py`** — update endpoints (async preview, read stored plan)
7. **`dashboard/src/components/EvaluationDrilldown.jsx`** — update plan rendering (4 places)
8. **`dashboard/src/pages/Alerts.jsx`** — update plan rendering (4 places)
9. **Run full test suite** — `python -m pytest` — all tests must pass
10. **Add live LLM paths** — `_plan_with_framework` and `_execute_with_framework`
11. **Doc sync** — CONTEXT.md, STATUS.md, ARCHITECTURE.md, API.md, learning/

---

## CRITICAL REMINDERS

- `api_version="2025-03-01-preview"` — this is the ONLY correct version for Responses API. Do NOT change it.
- Both `DefaultAzureCredential` AND the management client MUST be used as `async with` context managers.
- `_build_mock_plan` must include the `commands` field for backward compatibility.
- The agent-fix-preview endpoint becomes `async` — make sure to `await` the gateway call.
- `generate_agent_fix_plan` stores the plan on the ExecutionRecord so `execute_agent_fix` can read it.
- The `_parse_arm_id` function stays in `execution_gateway.py` — import it in `execution_agent.py`.
- After every code change, run `python -m pytest` and verify all tests pass.
- Follow CLAUDE.md rules: explain what you wrote, teach Python concepts, create learning file.
