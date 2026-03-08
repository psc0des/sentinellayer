# HITL Action Panel Enhancement — Create PR + Agent Fix + Decline

## Context

The `manual_required` execution status currently shows 3 buttons: "Show Terraform Fix" (inline HCL), "Open in Azure Portal", "Mark as Fixed". The user wants a production-grade workflow:

1. **Create Terraform PR** — proper branch + PR with fix + comments (not just inline HCL)
2. **Open in Azure Portal** — keep as-is
3. **Fix using Agent** — two-step: preview `az` CLI command → user confirms → execute
4. **Decline / Ignore** — dismiss the finding

---

## Files to Modify

| File | Changes |
|------|---------|
| `src/core/execution_gateway.py` | Add `_parse_arm_id()`, `_build_az_commands()`, `create_pr_from_manual()`, `generate_agent_fix_commands()`, `execute_agent_fix()` |
| `src/api/dashboard_api.py` | Add 3 endpoints: `POST .../create-pr`, `GET .../agent-fix-preview`, `POST .../agent-fix-execute` |
| `dashboard/src/api.js` | Add `createPRFromManual()`, `fetchAgentFixPreview()`, `executeAgentFix()` |
| `dashboard/src/components/EvaluationDrilldown.jsx` | Replace `manual_required` panel: 4 buttons + agent-fix two-step flow |
| `tests/test_execution_gateway.py` | ~14 new tests for PR creation + agent fix |

---

## Step 1: Backend — `execution_gateway.py`

### 1a. `_parse_arm_id(resource_id) -> dict`
Module-level helper. Extracts `resource_group`, `name`, `provider`, `resource_type` from ARM ID by splitting on `/`.

### 1b. `_build_az_commands(action: ProposedAction) -> list[str]`
Module-level helper. Maps action types to `az` CLI commands:
- `modify_nsg` → `az network nsg rule delete --resource-group RG --nsg-name NSG --name RULE`
- `scale_down` / `scale_up` → `az vm resize --resource-group RG --name VM --size SKU`
- `delete_resource` → `az resource delete --ids FULL_ARM_ID`
- `restart_service` → `az vm restart --resource-group RG --name VM`
- Unknown → comment fallback

Rule name parsed from `action.reason` using same regex as `terraform_pr_generator.py`.

### 1c. `create_pr_from_manual(execution_id, reviewed_by) -> ExecutionRecord`
- Validates status == `manual_required`
- Reconstructs verdict from snapshot
- Calls existing `_create_terraform_pr()` (reuse, no duplication)
- If GitHub not configured, `TerraformPRGenerator.create_pr()` already returns `manual_required` with a note
- Saves and returns updated record

### 1d. `generate_agent_fix_commands(execution_id) -> dict`
- Returns `{execution_id, action_type, resource_id, commands: [...], warning: "..."}`
- Pure read — no side effects

### 1e. `execute_agent_fix(execution_id, reviewed_by) -> ExecutionRecord`
- Mock mode (`settings.use_local_mocks`): simulate success, set status=`applied`
- Live mode: `asyncio.create_subprocess_exec()` for each command
  - Success → status=`applied`, output in `notes`
  - Failure → status=`failed`, stderr in `notes`
  - `az` not found → status=`failed` with install URL

---

## Step 2: Backend — `dashboard_api.py`

Three new endpoints (placed near existing execution endpoints):

1. `POST /api/execution/{execution_id}/create-pr` → `gateway.create_pr_from_manual()`
2. `GET /api/execution/{execution_id}/agent-fix-preview` → `gateway.generate_agent_fix_commands()`
3. `POST /api/execution/{execution_id}/agent-fix-execute` → `gateway.execute_agent_fix()`

Error handling: `KeyError` → 404, `ValueError` → 400 (same pattern as existing endpoints).

---

## Step 3: Frontend — `api.js`

Add 3 functions: `createPRFromManual()`, `fetchAgentFixPreview()`, `executeAgentFix()`.

---

## Step 4: Frontend — `EvaluationDrilldown.jsx`

### New state
```
agentFixPreview, agentFixLoading, agentFixExpanded, agentFixExecuting, agentFixResult, createPrLoading
```

### New handlers
- `handleCreatePR(executionId)` — calls `createPRFromManual`, updates `executionStatus`
- `handleAgentFixPreview(executionId)` — lazy fetch + toggle (same pattern as `handleShowTerraform`)
- `handleAgentFixExecute(executionId)` — `window.confirm()` safety prompt, calls `executeAgentFix`, updates status

### New `manual_required` panel layout
```
[ Create Terraform PR ] [ Open in Azure Portal ] [ Fix using Agent ] [ Decline / Ignore ]

(if Fix using Agent clicked → expands below:)
┌─────────────────────────────────────────┐
│ ⚠ These commands will modify Azure...   │
│ ┌─────────────────────────────────────┐ │
│ │ $ az network nsg rule delete ...    │ │
│ └─────────────────────────────────────┘ │
│ [ ▶ Run ]  [ Cancel ]                   │
│                                         │
│ (after Run → shows output below)        │
└─────────────────────────────────────────┘
```

---

## Step 5: Tests — `tests/test_execution_gateway.py`

~14 new tests in two classes:

**TestAgentFixFlow**: command generation for each action type, ARM ID parsing, mock-mode execution, wrong-status validation

**TestCreatePRFromManual**: status transition, GitHub-not-configured fallback, missing snapshot error, unknown ID

---

## Step 6: Doc sync + Learning

- `docs/API.md` — add 3 new endpoints to summary table + detail sections
- `CONTEXT.md` — add agent-fix flow description
- `STATUS.md` — update description
- `learning/40-agent-fix-hitl.md` — document: two-step UX, `asyncio.create_subprocess_exec`, ARM ID parsing, `_build_az_commands` mapping

---

## Verification

1. `python -m pytest tests/ -x -q` — all tests pass including ~14 new ones
2. Start backend + frontend, trigger a deploy scan, get `manual_required` status
3. Click "Create Terraform PR" → verify PR appears on GitHub with real HCL
4. Click "Fix using Agent" → verify `az` command shown → click Run → verify status changes
5. Click "Decline / Ignore" → verify status changes to `dismissed`
