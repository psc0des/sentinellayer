# Plan: Ops Agent Overhaul — From Checklist to Generic Detection + Execution

**Date**: 2026-04-12
**Goal**: Make all 3 ops agents (Deploy, Cost, Monitoring) genuinely generic — detect any issue on any Azure resource type, and fix as many as possible without manual fallback.

**Current state**: Detection is a hardcoded checklist in each agent's system prompt (6-9 resource types, 10-19 specific checks). Execution has 7 write tools covering 4 resource types. Everything else falls to "manual".

**Target state**: Microsoft APIs detect first (hundreds of checks, all resource types). LLM investigates and enriches second. Execution uses a generic PATCH tool for property updates, a web doc tool to learn correct API calls, and only falls to guided manual as a last resort.

---

## Phase 1: Unified Detection Architecture (All 3 Agents)

**Principle**: Microsoft APIs do broad detection (they maintain checks for 200+ resource types). LLM does investigation and reasoning (it's better at context, dependencies, blast radius). Stop using the LLM as a checklist runner.

### 1A: Add deterministic safety nets to Cost and Monitoring agents

**Why**: Deploy already has 3 post-scan safety nets (Advisor, Defender, Policy) that run deterministically after the LLM scan. Cost and Monitoring don't — they only give the LLM an Advisor tool it may or may not call.

**Files to modify**:
- `src/operational_agents/cost_agent.py` — add post-scan safety net block after `await run_with_throttle(agent.run, scan_prompt)`
- `src/operational_agents/monitoring_agent.py` — add post-scan safety net block (scan mode only, not alert mode)

**What to add to each agent after the LLM scan completes**:

```
# Cost Agent safety nets:
1. Azure Advisor (category=Cost) — deterministic, auto-propose for HIGH-impact recommendations
2. Azure Policy — auto-propose for non-compliant resources related to cost governance
# No Defender — Defender is security-focused, not relevant for cost

# Monitoring Agent safety nets (scan mode):
1. Azure Advisor (category=HighAvailability,Performance) — deterministic
2. Defender for Cloud — HIGH-severity availability/reliability assessments
3. Azure Policy — non-compliant reliability policies
```

**Deduplication**: Same pattern Deploy uses — before adding a safety-net proposal, check if `(resource_id, action_type)` already exists in `proposals_holder`. Use substring match on short resource name to catch near-duplicates.

**Tests**: Add tests for each safety net:
- Mock the Advisor/Defender/Policy async functions to return known findings
- Verify dedup: if LLM already proposed for a resource, safety net skips it
- Verify addition: if LLM missed a resource, safety net adds it
- File: `tests/test_cost_agent.py`, `tests/test_monitoring_agent.py`

### 1B: Flip detection architecture — Microsoft APIs detect first, LLM investigates second

**Why**: Right now the LLM discovers resources and checks a hardcoded list. If a resource type isn't in the prompt, it's invisible. Flipping the order means Microsoft APIs (which cover ALL resource types) do discovery, and the LLM adds reasoning.

**New scan flow for all 3 agents**:

```
Step 1: Run Microsoft APIs first (deterministic, no LLM)
  - Advisor recommendations (category relevant to agent domain)
  - Defender assessments (HIGH severity)
  - Policy violations (non-compliant)
  → Produces: raw_findings[] — list of {resource_id, resource_type, finding_source, description, severity, remediation}

Step 2: Discover full resource inventory
  - query_resource_graph: Resources | project id, name, type, resourceGroup, tags, sku, properties
  - No hardcoded type filter — discover ALL resource types in the subscription/RG
  → Produces: resource_inventory[]

Step 3: Feed findings + inventory to LLM
  - System prompt now says: "Here are the findings from Microsoft security/cost/reliability APIs.
    For each finding, investigate the resource, confirm the issue, assess blast radius,
    and propose an action with full context."
  - LLM also instructed: "After processing the API findings, scan the inventory for any
    issues the automated checks missed. Use your knowledge of Azure best practices."
  → Produces: proposals[] — fully enriched ProposedAction objects

Step 4: Dedup across all sources
  - Same (resource_id, action_type) dedup as today
```

**Domain-specific LLM investigation instructions**:

```
Deploy Agent:
  "For each security finding, investigate the resource configuration, confirm the
   vulnerability is real (not a false positive from tags/intent), and assess who
   else is affected. For network findings, check NSG rules. For storage/DB findings,
   check public access settings and network ACLs."

Cost Agent:
  "For each cost finding, investigate actual utilisation — call query_metrics for
   7-day CPU/memory/DTU. A resource flagged as oversized by Advisor may actually
   be needed for burst workloads. Include actual metric values in every reason."

Monitoring Agent (scan mode):
  "For each reliability finding, investigate the resource health, replica count,
   failover configuration, and monitoring coverage. Check if the resource has
   dependencies that would be affected by an outage."

Monitoring Agent (alert mode):
  — No change. Alert mode is already generic — it reacts to whatever alert arrives.
```

**Files to modify**:
- `src/operational_agents/deploy_agent.py` — rewrite `_scan_with_framework()` and `_AGENT_INSTRUCTIONS`
- `src/operational_agents/cost_agent.py` — rewrite `_scan_with_framework()` and `_AGENT_INSTRUCTIONS`
- `src/operational_agents/monitoring_agent.py` — rewrite `_scan_with_framework()` and `_SCAN_INSTRUCTIONS` (NOT `_ALERT_INSTRUCTIONS`)

**Key design decision**: The safety nets from Phase 1A become redundant once the detection flip is done (Microsoft APIs now run FIRST, not after). Keep Phase 1A anyway as it's a smaller, lower-risk change that delivers value immediately. Phase 1B replaces the safety-net pattern with the primary-detection pattern. The safety-net code from 1A can be removed or kept as a belt-and-suspenders.

**Backward compatibility**: ProposedAction model is unchanged. ActionType enum is unchanged. Existing Cosmos records still work. The only change is WHERE proposals come from — not what they look like.

**Tests**:
- Mock Microsoft API functions to return known findings
- Mock LLM to return proposals based on those findings
- Verify that proposals include findings from Microsoft APIs even when LLM "misses" them
- Verify LLM enriches findings with metric data and context
- File: existing test files + new `tests/test_detection_architecture.py`

### 1C: Remove hardcoded resource type lists from discovery

**Why**: Step 1 queries in current prompts list specific resource types (`where type in (...)`). This blindspot means any resource type not listed is never discovered.

**Change**: Replace hardcoded type lists with:
```kql
Resources
| where resourceGroup == '{target_rg}'
| project id, name, type, location, resourceGroup, tags, sku, properties
| order by type asc
```

Or when scanning the full subscription:
```kql
Resources
| project id, name, type, location, resourceGroup, tags, sku, properties
| order by type asc
| take 500
```

The LLM receives ALL resources and uses its training knowledge about Azure resource types to identify issues — not a checklist.

**Risk**: Large subscriptions may have thousands of resources. Mitigate by:
- Using `take 500` in the KQL (Resource Graph returns max 1000 anyway)
- Grouping by type and summarising counts first, then drilling into specific types
- The Microsoft API findings from Step 1 already prioritise which resources to look at

---

## Phase 2: Generic Execution Layer

**Principle**: The execution agent should be able to fix any property-level issue on any Azure resource, not just the 7 hardcoded operations. Web doc lookup fills knowledge gaps before falling to manual.

### 2A: Generic PATCH tool — `update_resource_property`

**Why**: The current execution agent has 7 write tools (start_vm, restart_vm, resize_vm, delete_nsg_rule, create_nsg_rule, delete_resource, update_resource_tags). Any issue that doesn't map to one of these falls to manual. A generic PATCH tool using `begin_update_by_id` can update ANY property on ANY resource type.

**New tool to add to `src/core/execution_agent.py`**:

```python
@af.tool(
    name="update_resource_property",
    description=(
        "Update a property on any Azure resource using the ARM generic PATCH API. "
        "resource_id: full ARM ID of the resource. "
        "api_version: the correct API version for this resource type "
        "(e.g. '2023-01-01' for storage accounts — use fetch_azure_docs to look this up). "
        "property_path: dot-separated path to the property within the resource body "
        "(e.g. 'properties.allowBlobPublicAccess'). "
        "new_value: the new value to set (string, bool, or number — will be JSON-parsed). "
        "This tool CANNOT create new child resources or perform operational commands "
        "(start/stop/restart). Use the specific tools for those."
    ),
)
async def tool_update_resource_property(
    resource_id: str, api_version: str, property_path: str, new_value: str
) -> str:
    # 1. Parse new_value as JSON (handles bool, int, string)
    # 2. GET current resource via resources.get_by_id(resource_id, api_version)
    # 3. Navigate property_path to confirm the property exists (safety check)
    # 4. Build PATCH body with only the changed property
    # 5. Call resources.begin_update_by_id(resource_id, api_version, patch_body)
    # 6. Return structured JSON result
```

**Safety guardrails**:
- Read-before-write: always GET the current resource first, confirm the property exists
- Minimal PATCH body: only send the changed property, not the full resource
- Property-path validation: reject paths that don't exist on the current resource
- No creates: this tool only PATCHes — if the property_path points to something that doesn't exist, reject with a clear error

**Update `_PLAN_INSTRUCTIONS`** to include:
```
- update_resource_property(resource_id, api_version, property_path, new_value):
  Use this for ANY configuration property change that doesn't have a specific tool.
  Examples: storage allowBlobPublicAccess, Key Vault enableSoftDelete, VM osProfile settings.
  You MUST know the correct api_version — call fetch_azure_docs first if unsure.
  CANNOT create new resources or child resources. Use for property updates only.
```

**Update `_build_mock_plan`** for `UPDATE_CONFIG`:
- Instead of `operation: "manual"`, return `operation: "update_resource_property"` with a best-guess property_path based on the action reason
- This makes mock mode more realistic

**Files to modify**:
- `src/core/execution_agent.py` — add `update_resource_property` tool to execute phase, update plan/execute instructions, update mock plan for UPDATE_CONFIG
- `tests/test_execution_agent.py` — add tests for generic PATCH in mock mode

### 2B: Web documentation tool — `fetch_azure_docs`

**Why**: The LLM needs to know the correct `api_version` and `property_path` for the generic PATCH tool. Its training data covers most Azure resource types, but API versions change. A web doc lookup tool lets the LLM fetch the current REST API reference from Microsoft Learn before making the PATCH call.

**New tool to add to `src/core/execution_agent.py`** (plan phase AND execute phase):

```python
@af.tool(
    name="fetch_azure_docs",
    description=(
        "Fetch Azure REST API documentation from Microsoft Learn for a specific "
        "resource type. Returns the current API version, resource schema, and "
        "property definitions. Use this BEFORE calling update_resource_property "
        "to confirm the correct api_version and property_path. "
        "resource_type: the ARM resource type (e.g. 'Microsoft.Storage/storageAccounts', "
        "'Microsoft.KeyVault/vaults', 'Microsoft.Compute/virtualMachines')."
    ),
)
async def tool_fetch_azure_docs(resource_type: str) -> str:
    # 1. Normalise resource_type to URL slug
    #    "Microsoft.Storage/storageAccounts" → "microsoft.storage/storageaccounts"
    # 2. Construct Microsoft Learn REST API URL:
    #    https://learn.microsoft.com/en-us/azure/templates/{provider}/{resource_type}
    #    OR the ARM resource schema:
    #    https://learn.microsoft.com/en-us/rest/api/{service}/...
    # 3. Fetch via httpx (with timeout, error handling)
    # 4. Extract: latest stable api_version, updateable properties list, property types
    # 5. Return structured summary (not full HTML — just the relevant schema info)
    # 6. Cache results in-memory for the session (same resource type won't be fetched twice)
```

**Implementation options** (pick one):
- **Option A: Microsoft Learn HTML scrape** — fetch the Terraform or ARM template reference page, extract property table. Reliable but fragile if page layout changes.
- **Option B: Azure REST API specs from GitHub** — fetch the OpenAPI spec from `https://raw.githubusercontent.com/Azure/azure-rest-api-specs/main/specification/{service}/...`. Structured JSON, always up to date. More complex to parse.
- **Option C: Azure Resource Manager metadata API** — call `GET https://management.azure.com/providers/{provider}?api-version=2021-04-01` to get supported API versions and resource types. Then use the api-version in a `GET` on the resource to see its full schema. No web fetch needed — pure Azure SDK.

**Recommendation: Option C** — no external web dependency, uses Azure APIs the agent already has credentials for. The provider metadata API returns all supported API versions and resource types. Combined with a `GET` on the resource itself (which the agent already does via `get_resource_details`), the LLM has everything it needs.

If Option C is insufficient (property schema not visible from GET), fall back to Option A (Microsoft Learn scrape).

**Update `_PLAN_INSTRUCTIONS`**:
```
- When planning an update_resource_property operation:
  1. First call fetch_azure_docs to confirm the correct api_version and property_path
  2. Then call get_resource_details to confirm the current property value
  3. Then include the confirmed api_version and property_path in the plan step
  Never guess api_version — always confirm via fetch_azure_docs first.
```

**Files to modify**:
- `src/core/execution_agent.py` — add `fetch_azure_docs` tool to both plan and execute phases
- `src/infrastructure/azure_tools.py` — add `fetch_resource_type_metadata_async()` helper
- `tests/test_execution_agent.py` — add tests for doc fetch in mock mode

### 2C: Improve manual fallback — guided remediation

**Why**: When the generic PATCH can't fix an issue (e.g., needs new resource creation, multi-resource orchestration), the current fallback is `operation: "manual"` with no guidance. We should provide specific az CLI commands, Azure Portal steps, and links to docs.

**Change in execution agent plan phase**:
- When the LLM determines a fix requires new resource creation or multi-step orchestration, it should still call `submit_execution_plan` with `operation: "guided_manual"` instead of just `"manual"`
- The plan step should include:
  ```json
  {
    "operation": "guided_manual",
    "target": "<resource_id>",
    "params": {},
    "reason": "Create private endpoint for database — requires new resource creation",
    "az_cli_commands": [
      "az network private-endpoint create --name pe-mydb --resource-group myrg ...",
      "az network private-dns zone create ..."
    ],
    "portal_steps": [
      "Navigate to the resource in Azure Portal",
      "Go to Networking → Private endpoint connections",
      "Click + Private endpoint"
    ],
    "doc_url": "https://learn.microsoft.com/en-us/azure/..."
  }
  ```

**Update `_PLAN_INSTRUCTIONS`**:
```
- If the fix requires creating new resources (private endpoints, encryption sets, backup vaults)
  or multi-step orchestration that cannot be done with a single API call:
  Use operation="guided_manual" and include:
  - az_cli_commands: the exact az CLI commands the human should run (in order)
  - portal_steps: step-by-step Azure Portal navigation (for non-CLI users)
  - doc_url: link to the relevant Microsoft Learn documentation page
  The human will execute these steps. Make the commands copy-pasteable.
```

**Dashboard UI update** (optional, lower priority):
- `EvaluationDrilldown.jsx` — render `guided_manual` steps as a numbered checklist with copy buttons for az CLI commands
- This is a UX improvement, not a functional requirement

**Files to modify**:
- `src/core/execution_agent.py` — update plan instructions, add `guided_manual` handling in mock plan
- `dashboard/src/components/EvaluationDrilldown.jsx` — render guided steps (optional)

### 2D: Update `_PLAN_INSTRUCTIONS` and `_EXECUTE_INSTRUCTIONS` for new tool awareness

**Why**: The LLM needs to know about the new tools and when to use each one.

**New execution decision tree in plan instructions**:
```
When planning the fix for an approved action, follow this decision tree:

1. Does a SPECIFIC tool exist for this operation?
   - NSG rule: use delete_nsg_rule or create_nsg_rule
   - VM power: use start_vm or restart_vm
   - VM size: use resize_vm
   - Resource deletion: use delete_resource
   → Use the specific tool. These are the safest, most tested paths.

2. Is it a PROPERTY UPDATE on an existing resource?
   - Storage: allowBlobPublicAccess, supportsHttpsTrafficOnly, minimumTlsVersion, networkAcls
   - Key Vault: enableSoftDelete, enablePurgeProtection, publicNetworkAccess
   - VM: osProfile settings, disk encryption settings
   - Database: publicNetworkAccess, firewallRules
   - App Service: httpsOnly, minTlsVersion, ftpsState
   - Any other property on any resource type
   → Call fetch_azure_docs to confirm api_version and property_path.
   → Use update_resource_property.

3. Does it require CREATING NEW RESOURCES or MULTI-STEP ORCHESTRATION?
   - Private endpoints, encryption sets, backup vaults, VNet integration, RBAC roles
   → Use operation="guided_manual" with az CLI commands, Portal steps, and doc link.

4. Truly unknown / cannot determine?
   → Use operation="manual" with the best explanation you can give.
```

---

## Phase 3: Execution Confidence Levels (Dashboard UX)

**Why**: End users should know what the system can actually do vs what needs their hands.

### 3A: Add `remediation_confidence` to ExecutionRecord

**New field on ProposedAction or ExecutionRecord**:
```python
class RemediationConfidence(str, Enum):
    AUTO_FIX = "auto_fix"           # Specific tool exists (NSG, VM start, resize)
    GENERIC_FIX = "generic_fix"     # Property PATCH — likely works, verify after
    GUIDED_MANUAL = "guided_manual" # We know the fix, human runs the commands
    MANUAL = "manual"               # Investigation needed, no automated path
```

**Set during plan phase**: The execution agent assigns confidence based on which tool it chose:
- Specific tools (start_vm, delete_nsg_rule, etc.) → `auto_fix`
- `update_resource_property` → `generic_fix`
- `guided_manual` with az CLI commands → `guided_manual`
- `manual` → `manual`

**Dashboard rendering**:
- `auto_fix`: green badge — "Automated fix available"
- `generic_fix`: blue badge — "Generic fix available — verify after execution"
- `guided_manual`: amber badge — "Step-by-step guide available"
- `manual`: grey badge — "Manual investigation required"

**Files to modify**:
- `src/core/models.py` — add `RemediationConfidence` enum
- `src/core/execution_agent.py` — set confidence in plan output
- `dashboard/src/components/EvaluationDrilldown.jsx` — render confidence badge

---

## Implementation Order

```
Phase 1A  →  Add safety nets to Cost & Monitoring         (small, low risk, immediate value)
Phase 2A  →  Generic PATCH tool                            (high value — most UPDATE_CONFIG issues fixed)
Phase 2B  →  Web doc / metadata tool                       (makes PATCH reliable — correct api_version)
Phase 1B  →  Flip detection architecture                   (biggest change — rewrite agent instructions)
Phase 1C  →  Remove hardcoded resource type lists          (part of 1B, listed separately for clarity)
Phase 2C  →  Guided manual fallback                        (UX improvement — az CLI commands in plan)
Phase 2D  →  Update LLM instructions for new tools         (part of 2A/2B, listed separately for clarity)
Phase 3A  →  Remediation confidence badges                 (UX — honest about what we can/can't fix)
```

**Rationale for this order**:
1. **1A first** — smallest change, adds safety nets with copy-paste from Deploy agent. Immediate coverage boost.
2. **2A+2B before 1B** — fix execution before detection. No point detecting more issues if we can't fix them. Users see execution quality when they click buttons.
3. **1B after 2A** — once execution is generic, we can safely detect more issues without creating a wall of "manual required" cards.
4. **3A last** — polish. The confidence badges make sense only after the fix tiers exist.

---

## Test Strategy

Each phase must pass `python -m pytest tests/ -q` before commit.

**Phase 1A tests**:
- Mock Advisor/Defender/Policy returns → verify proposals added
- Mock LLM + Advisor returning same resource → verify dedup
- Mock Advisor returning empty → verify no crash

**Phase 2A tests**:
- Mock `update_resource_property` → verify PATCH body structure
- Test property_path navigation (nested paths like `properties.allowBlobPublicAccess`)
- Test safety check: reject non-existent property paths
- Test mock plan for UPDATE_CONFIG now returns `update_resource_property` instead of `manual`

**Phase 2B tests**:
- Mock provider metadata API → verify api_version extraction
- Test cache: same resource type fetched twice → only one API call
- Test fallback: metadata API fails → return "unknown, consult docs"

**Phase 1B tests**:
- Full integration: mock Microsoft APIs + mock LLM → verify proposal enrichment
- Verify LLM receives findings in prompt
- Verify open-ended discovery still works (LLM finds issues not in API findings)

---

## Backward Compatibility

- `ActionType` enum: unchanged. No new values needed.
- `ProposedAction` model: unchanged. `config_changes` field already exists for attribute:value pairs.
- `ExecutionRecord` model: add optional `remediation_confidence` field (Phase 3A only).
- Existing Cosmos records: still valid. Old records without confidence field default to `None` (rendered as "unknown" in dashboard).
- Old execution plans (stored before Phase 2): still work. The new tools are additive — old plans reference old tool names which still exist.

---

## What This Does NOT Cover (Honest Gaps That Remain)

1. **Resource creation** — creating private endpoints, encryption sets, backup vaults. These require full ARM templates. Guided manual is the best we can do safely.
2. **Multi-resource orchestration** — VNet integration, RBAC across resources, disk encryption (3 resources). Same: guided manual.
3. **Non-ARM operations** — Azure AD / Entra ID, DNS at registrar level, certificate management. Out of scope for ARM-based tooling.
4. **Cost forecasting accuracy** — we surface Advisor cost recommendations but don't validate projected savings against actual Azure pricing APIs. The numbers are Advisor's estimates.
5. **Custom policy detection** — we catch built-in Policy violations but not org-specific custom policies unless they're assigned in Azure Policy. Orgs with custom governance rules would need to define Azure Policies first.
