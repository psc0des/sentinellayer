"""Cost Optimization Agent — identifies wasteful resources and proposes savings.

This is an operational agent (the governed subject). It proposes
infrastructure actions that RuriSkry evaluates before execution.

Phase 12 — Intelligent, environment-agnostic agent
----------------------------------------------------
The agent now **genuinely investigates** the Azure environment before
proposing any action.  In live mode it:

1. Queries Azure Resource Graph to **discover** VMs and clusters.
2. Queries Azure Monitor to get **actual 7-day CPU utilisation** for each
   resource — not hardcoded heuristics.
3. Only proposes action when metric evidence shows the resource is wasteful
   (avg CPU < 20 % for right-sizing; < 5 % for deletion candidates).
4. Uses GPT-4.1 to **reason about trade-offs** before calling
   ``propose_action``.

The agent is environment-agnostic: it accepts an optional
``target_resource_group`` parameter and can scan any Azure subscription.

In mock mode (USE_LOCAL_MOCKS=true) the deterministic ``_scan_rules()``
fallback runs instead — it reads ``data/seed_resources.json`` and applies
the same heuristics as Phase 8 for CI/offline compatibility.

Microsoft Agent Framework tools (live mode)
--------------------------------------------
- ``query_resource_graph(kusto_query)`` — discover VMs, disks, IPs, storage
- ``query_metrics(resource_id, metric_names, timespan)`` — actual CPU/memory data
- ``get_resource_details(resource_id)`` — full resource information, power state
- ``query_activity_log(resource_group, timespan)`` — recent changes (avoid false flags)
- ``list_nsg_rules(nsg_resource_id)`` — check security posture alongside cost
- ``get_resource_health(resource_id)`` — Azure Platform health before proposing delete
- ``list_advisor_recommendations(scope, category)`` — Azure Advisor Cost tips
- ``propose_action(...)`` — submit a validated ProposedAction
"""

import json
import logging
from pathlib import Path

from src.config import settings as _default_settings
from src.core.models import ActionTarget, ActionType, EvidencePayload, ProposedAction, Urgency
from src.operational_agents import is_compliant_reason

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_AGENT_ID = "cost-optimization-agent"

_DEFAULT_RESOURCES_PATH = (
    Path(__file__).parent.parent.parent / "data" / "seed_resources.json"
)

# Rule-based fallback thresholds (unchanged from Phase 8)
_MIN_COST_THRESHOLD: float = 200.0
_HIGH_COST_THRESHOLD: float = 500.0
_VM_DOWNSIZE_SAVINGS_RATE: float = 0.45
_AKS_SCALE_DOWN_NODE_THRESHOLD: int = 4
_AKS_SCALE_DOWN_SAVINGS_RATE: float = 0.35
_OVERSIZED_SKUS: set[str] = {
    "Standard_D8s_v3",
    "Standard_D16s_v3",
    "Standard_D32s_v3",
}
_DOWNSIZE_MAP: dict[str, str] = {
    "Standard_D8s_v3": "Standard_D4s_v3",
    "Standard_D16s_v3": "Standard_D8s_v3",
    "Standard_D32s_v3": "Standard_D16s_v3",
}

# System instructions for the framework agent — drives three-layer intelligence.
_AGENT_INSTRUCTIONS = """\
You are a Senior FinOps Engineer conducting an enterprise cloud cost optimisation review.
Azure Advisor (Cost) and Azure Policy have pre-run and found cost waste — your job is
to confirm each finding with actual utilisation data, then look for anything they missed.

━━━ PRIMARY TASK: INVESTIGATE PRE-COMPUTED COST FINDINGS ━━━
Cost recommendations from Azure Advisor and Azure Policy are listed at the top of
the prompt. For EACH finding:
1. Call query_metrics(resource_id, "Percentage CPU", "P7D") to confirm actual utilisation.
   For deallocated VMs: call get_resource_details to confirm power state and disk costs.
2. Include actual metric values in the proposal reason: "7-day avg CPU: X%, peak: Y%".
3. Call propose_action with confirmed evidence.
Do NOT propose scale_down if CPU > 40% — that contradicts the finding.
Do NOT skip a pre-computed finding.

━━━ SECONDARY TASK: INDEPENDENT COST DISCOVERY ━━━
After processing pre-computed findings, scan the resource inventory (above) or
call query_resource_graph for cost waste the APIs missed:

IDLE & OVERSIZED COMPUTE (virtual machines and compute resources):
  For each VM, call get_resource_details for power state:
  - DEALLOCATED: incurs storage costs → propose delete_resource (MEDIUM) or scale_down.
  - RUNNING + avg CPU < 5% (P7D): strong right-size → propose scale_down (MEDIUM).
  - RUNNING + avg CPU < 20%: right-size → propose scale_down (LOW).
  Reason MUST include "7-day avg CPU: X%, peak: Y%".
  Do NOT propose deleting running VMs. Exception: DR/standby VMs with CPU < 2%
  may be delete_resource (MEDIUM) — note the DR risk in reason.

ORPHANED RESOURCES:
  Unattached disks (diskState = 'Unattached') → propose delete_resource (MEDIUM).
  Unassociated public IPs (isnull properties.ipConfiguration) → delete_resource (LOW).

PaaS RIGHTSIZING:
  AKS avg CPU < 40% (P7D) → propose scale_down (LOW/MEDIUM).
  App Service CpuPercentage < 10% → propose scale_down (LOW).
  SQL dtu_consumption_percent < 20% → propose scale_down (LOW).
  Cosmos DB: compare provisioned RU/s against TotalRequests metric.

━━━ RESOURCE DISCOVERY (use this KQL when no inventory is provided) ━━━
Resources
| project id, name, type, location, resourceGroup, tags, sku, properties
| order by type asc

Do NOT add a 'where type in (...)' filter — ALL resource types have costs.
The Microsoft APIs already flagged the obvious waste — your job is to confirm
and find what they missed.

━━━ PROPOSAL RULES ━━━
- Reason MUST include actual metric values: "7-day avg CPU: X%, peak: Y%".
- Always call get_resource_details before proposing delete_resource.
- For VMs: prefer scale_down over delete_resource unless clearly abandoned.
- projected_savings_monthly: estimate 45% savings per VM SKU tier reduction;
  note disk cost for unattached disks (Standard HDD 128 GB ≈ $5/mo).

━━━ URGENCY SCALE ━━━
  MEDIUM: Unattached disks, deallocated VMs with no recent activity, oversized
          compute with avg CPU < 5%.
  LOW:    Right-size candidates (CPU 5–20%), unused public IPs, lightly used PaaS.

━━━ YOUR ROLE AND BOUNDARIES ━━━
Your ONLY job is to inspect and report. You are a detection tool.
NEVER skip a finding — the governance engine handles deduplication.
"""


# ---------------------------------------------------------------------------
# Agent
# ---------------------------------------------------------------------------


class CostOptimizationAgent:
    """Scans the Azure environment and proposes cost-saving actions.

    In live mode (USE_LOCAL_MOCKS=false) the Microsoft Agent Framework drives
    GPT-4.1 to investigate real utilisation data via generic Azure tools before
    submitting evidence-backed proposals.

    In mock mode only the deterministic ``_scan_rules()`` runs — seed data,
    heuristics, no network calls.  This is the safe offline/CI path.

    Usage::

        agent = CostOptimizationAgent()
        proposals: list[ProposedAction] = await agent.scan()
        for p in proposals:
            print(p.action_type.value, p.target.resource_id)

        # Target a specific resource group in live mode:
        proposals = await agent.scan(target_resource_group="my-rg")
    """

    def __init__(
        self,
        resources_path: str | Path | None = None,
        cfg=None,
    ) -> None:
        path = Path(resources_path) if resources_path else _DEFAULT_RESOURCES_PATH
        with open(path, encoding="utf-8") as fh:
            data: dict = json.load(fh)

        self._resources: list[dict] = data.get("resources", [])
        self._cfg = cfg or _default_settings
        self._use_framework: bool = bool(self._cfg.azure_openai_endpoint)
        self.scan_error: str | None = None  # populated if framework call fails

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def scan(
        self,
        target_resource_group: str | None = None,
        inventory: list[dict] | None = None,
    ) -> list[ProposedAction]:
        """Investigate the Azure environment and return cost-saving proposals.

        Args:
            target_resource_group: Optional resource group name to scope the
                investigation.  When ``None`` the agent scans the entire
                subscription visible to its credentials.
            inventory: Optional pre-fetched resource list.  When provided,
                injected into the LLM prompt so the agent can review all
                resources without relying on non-deterministic discovery.

        Returns:
            List of :class:`~src.core.models.ProposedAction` objects.
        """
        if self._cfg.demo_mode:
            logger.info(
                "CostOptimizationAgent: DEMO_MODE enabled — returning sample proposals "
                "for pipeline testing (set DEMO_MODE=false for real Azure scanning)."
            )
            return self._demo_proposals()

        if not self._use_framework:
            logger.info(
                "CostOptimizationAgent: no Azure OpenAI endpoint configured — "
                "returning no proposals (set AZURE_OPENAI_ENDPOINT to enable live scanning)."
            )
            return []

        self.scan_error = None
        try:
            return await self._scan_with_framework(target_resource_group, inventory)
        except Exception as exc:  # noqa: BLE001
            self.scan_error = str(exc)
            logger.warning(
                "CostOptimizationAgent: framework call failed (%s) — returning no proposals "
                "(live-mode fallback to seed data would generate false positives).",
                exc,
            )
            return []

    # ------------------------------------------------------------------
    # Microsoft Agent Framework path (live mode)
    # ------------------------------------------------------------------

    async def _scan_with_framework(
        self, target_resource_group: str | None, inventory: list[dict] | None = None
    ) -> list[ProposedAction]:
        """Run GPT-4.1 with investigation tools to produce evidence-backed proposals."""
        from openai import AsyncAzureOpenAI
        from azure.identity import DefaultAzureCredential, get_bearer_token_provider
        import agent_framework as af
        from agent_framework.openai import OpenAIResponsesClient
        from src.infrastructure.azure_tools import (
            query_resource_graph_async,
            query_metrics_async,
            get_resource_details_async,
            query_activity_log_async,
            list_nsg_rules_async,
            get_resource_health_async,
            list_advisor_recommendations_async,
            list_policy_violations_async,
        )

        credential = DefaultAzureCredential()
        token_provider = get_bearer_token_provider(
            credential, "https://cognitiveservices.azure.com/.default"
        )
        azure_openai = AsyncAzureOpenAI(
            azure_endpoint=self._cfg.azure_openai_endpoint,
            azure_ad_token_provider=token_provider,
            api_version="2025-03-01-preview",  # Responses API requires >=2025-03-01-preview
            timeout=float(self._cfg.llm_timeout),
        )
        client = OpenAIResponsesClient(
            async_client=azure_openai,
            model_id=self._cfg.azure_openai_deployment,
        )

        proposals_holder: list[ProposedAction] = []
        scan_notes: list[str] = []

        # ── Pre-scan: Microsoft APIs detect first ─────────────────────────────
        # Run Advisor (Cost) and Policy BEFORE the LLM scan so the agent
        # receives confirmed findings as context and confirms with real metrics.
        raw_findings: list[dict] = []

        try:
            advisor_recs = await list_advisor_recommendations_async(
                scope=target_resource_group or None, category="Cost"
            )
            advisor_high = [r for r in advisor_recs if str(r.get("impact", "")).lower() == "high"]
            for rec in advisor_high:
                short_desc = (
                    rec.get("shortDescription", {}).get("problem", "")
                    if isinstance(rec.get("shortDescription"), dict)
                    else str(rec.get("shortDescription", ""))
                ) or rec.get("description", "")
                if not short_desc:
                    continue  # skip recommendations with no description
                raw_findings.append({
                    "source": "ADVISOR-HIGH",
                    "severity": "HIGH",
                    "resource_name": rec.get("impactedValue", ""),
                    "description": short_desc,
                    "rec_id": rec.get("id", ""),
                    "resource_type": rec.get("impactedField", ""),
                })
            scan_notes.append(
                f"Pre-scan: Azure Advisor Cost — {len(advisor_high)} HIGH recommendation(s)"
            )
        except Exception as exc:
            logger.warning("CostAgent: pre-scan Advisor failed: %s", exc)
            scan_notes.append(f"Pre-scan: Azure Advisor unavailable ({exc})")

        try:
            policy_violations = await list_policy_violations_async(
                scope=target_resource_group or None
            )
            for v in policy_violations:
                if not v.get("resourceId") or not v.get("policyDefinitionName"):
                    continue  # skip incomplete violation records
                assignment = v.get("policyAssignmentName", "")
                raw_findings.append({
                    "source": "POLICY-NONCOMPLIANT",
                    "severity": "MEDIUM",
                    "resource_id": v.get("resourceId", ""),
                    "resource_name": v.get("resourceName", ""),
                    "description": (
                        f"Policy: {v.get('policyDefinitionName', '')}"
                        + (f" (assignment: {assignment})" if assignment else "")
                    ),
                    "resource_type": "unknown",
                })
            scan_notes.append(
                f"Pre-scan: Azure Policy — {len(policy_violations)} non-compliant resource(s)"
            )
        except Exception as exc:
            logger.warning("CostAgent: pre-scan Policy failed: %s", exc)
            scan_notes.append(f"Pre-scan: Azure Policy unavailable ({exc})")

        # Build findings summary to inject into the LLM prompt.
        if raw_findings:
            findings_lines = [
                f"=== PRE-COMPUTED FINDINGS: {len(raw_findings)} cost issue(s) from Microsoft APIs ===",
                "Each finding below was detected deterministically. For each one:",
                "1. Confirm with query_metrics (CPU/utilisation) or get_resource_details (power state).",
                "2. Include actual metric values in the proposal reason.",
                "3. Call propose_action with full evidence.",
                "",
            ]
            for i, f in enumerate(raw_findings, 1):
                r_name = f.get("resource_name") or f.get("resource_id", "?")
                line = f"[{i}] [{f.get('source', '?')}] Resource: {r_name} — {f.get('description', '?')}"
                findings_lines.append(line)
            findings_lines.append("")
            findings_text = "\n".join(findings_lines)
        else:
            findings_text = (
                "=== PRE-COMPUTED FINDINGS: No high-impact cost issues detected by "
                "Microsoft APIs ===\n"
            )

        @af.tool(
            name="query_resource_graph",
            description=(
                "Query Azure Resource Graph with a Kusto (KQL) query to discover "
                "resources in the Azure environment. Returns a JSON array of resource "
                "objects with id, name, type, location, resourceGroup, tags, sku."
            ),
        )
        async def tool_query_resource_graph(kusto_query: str) -> str:
            """Discover Azure resources via Resource Graph KQL query."""
            results = await query_resource_graph_async(kusto_query)
            return json.dumps(results, default=str)

        @af.tool(
            name="query_metrics",
            description=(
                "Query Azure Monitor metrics for a resource. Returns average, max, and "
                "min values for the requested metrics over the specified timespan. "
                "metric_names is a comma-separated list (e.g. 'Percentage CPU,Network In'). "
                "timespan uses ISO 8601 duration format (e.g. 'P7D' for 7 days)."
            ),
        )
        async def tool_query_metrics(
            resource_id: str,
            metric_names: str,
            timespan: str = "P7D",
        ) -> str:
            """Get actual utilisation metrics for a resource."""
            names = [m.strip() for m in metric_names.split(",")]
            results = await query_metrics_async(resource_id, names, timespan)
            return json.dumps(results, default=str)

        @af.tool(
            name="get_resource_details",
            description=(
                "Get full details for a specific Azure resource by its ARM resource ID "
                "or short name. Returns SKU, tags, cost, location, and other properties."
            ),
        )
        async def tool_get_resource_details(resource_id: str) -> str:
            """Retrieve full resource details including SKU and tags."""
            details = await get_resource_details_async(resource_id)
            return json.dumps(details, default=str)

        @af.tool(
            name="query_activity_log",
            description=(
                "Query Azure Monitor activity logs for a resource group. "
                "Returns recent operations with timestamp, operation name, status "
                "(Succeeded/Failed), caller, and resource type. "
                "Use this to check whether a resource was recently created or modified — "
                "newly created resources should not be flagged as waste. "
                "timespan uses ISO 8601 format (e.g. 'P7D' for last 7 days)."
            ),
        )
        async def tool_query_activity_log(resource_group: str, timespan: str = "P7D") -> str:
            """Check recent resource changes before flagging waste."""
            entries = await query_activity_log_async(resource_group, timespan)
            return json.dumps(entries, default=str)

        @af.tool(
            name="list_nsg_rules",
            description=(
                "List the security rules for an Azure Network Security Group. "
                "Returns JSON array of rules with name, port, access (Allow/Deny), "
                "priority, and direction. Use this to check security posture alongside cost."
            ),
        )
        async def tool_list_nsg_rules(nsg_resource_id: str) -> str:
            """Inspect NSG security rules when reviewing network-related cost items."""
            rules = await list_nsg_rules_async(nsg_resource_id)
            return json.dumps(rules, default=str)

        @af.tool(
            name="get_resource_health",
            description=(
                "Get Azure Resource Health status for a specific resource. "
                "Returns availabilityState (Available/Unavailable/Degraded/Unknown), "
                "a human-readable summary, reasonType, and timestamps. "
                "Use this to verify a deallocated or stopped resource is genuinely idle "
                "before proposing deletion — a platform-degraded resource should not be deleted."
            ),
        )
        async def tool_get_resource_health(resource_id: str) -> str:
            """Check Azure Platform health signal for a resource."""
            health = await get_resource_health_async(resource_id)
            return json.dumps(health, default=str)

        @af.tool(
            name="list_advisor_recommendations",
            description=(
                "List Azure Advisor recommendations for the subscription or a scoped resource group. "
                "Returns recommendations with category (Cost/Security/HighAvailability/Performance), "
                "impact (High/Medium/Low), impactedValue (resource name), shortDescription, "
                "and remediation guidance. "
                "scope (optional): filter by resource group name. "
                "category (optional): filter by one category — e.g. 'Cost' or 'HighAvailability'."
            ),
        )
        async def tool_list_advisor_recommendations(
            scope: str = "", category: str = ""
        ) -> str:
            """Retrieve pre-computed Microsoft Advisor recommendations."""
            recs = await list_advisor_recommendations_async(
                scope=scope or None, category=category or None
            )
            return json.dumps(recs, default=str)

        @af.tool(
            name="propose_action",
            description=(
                "Submit a governance proposal for a resource. Call this when you have "
                "metric evidence that a resource is wasted or over-provisioned. "
                "action_type must be one of: scale_down, delete_resource, scale_up, "
                "update_config, modify_nsg, create_resource, restart_service. "
                "urgency must be one of: low, medium, high. "
                "evidence_json: JSON string with observed data — metrics dict with "
                "avg_cpu_7d, peak_cpu_14d, etc. Pass {} if no structured evidence."
            ),
        )
        def tool_propose_action(
            resource_id: str,
            action_type: str,
            reason: str,
            urgency: str = "medium",
            current_sku: str = "",
            proposed_sku: str = "",
            projected_savings_monthly: float = 0.0,
            resource_type: str = "",
            resource_group: str = "",
            evidence_json: str = "{}",
        ) -> str:
            """Validate parameters and record a ProposedAction."""
            try:
                action_type_enum = ActionType(action_type.lower())
            except ValueError:
                valid = [e.value for e in ActionType]
                return f"ERROR: Invalid action_type '{action_type}'. Valid: {valid}"
            try:
                urgency_enum = Urgency(urgency.lower())
            except ValueError:
                urgency_enum = Urgency.MEDIUM

            # Parse resource_group and resource_type from the ARM resource ID.
            if not resource_group and "/" in resource_id:
                parts = resource_id.split("/")
                if len(parts) > 4 and parts[3].lower() == "resourcegroups":
                    resource_group = parts[4]
            if not resource_type and "/" in resource_id:
                parts = resource_id.split("/")
                if len(parts) > 7:
                    resource_type = f"{parts[6]}/{parts[7]}"

            # Deterministic gate: block proposals where the reason signals compliance.
            # This cannot be overridden by LLM non-determinism or instruction drift.
            if is_compliant_reason(reason):
                name = resource_id.split("/")[-1]
                logger.info(
                    "CostAgent: blocked compliant-resource proposal — %s on %s",
                    action_type, name,
                )
                return (
                    f"Proposal rejected: reason indicates resource is already compliant "
                    f"— no governance action needed for {name}"
                )

            evidence: EvidencePayload | None = None
            try:
                ev_dict = json.loads(evidence_json) if evidence_json else {}
                if ev_dict:
                    evidence = EvidencePayload(**ev_dict)
            except Exception:
                pass  # malformed evidence JSON — drop it, don't block the proposal

            proposal = ProposedAction(
                agent_id=_AGENT_ID,
                action_type=action_type_enum,
                target=ActionTarget(
                    resource_id=resource_id,
                    resource_type=resource_type or "Microsoft.Resources/unknown",
                    resource_group=resource_group or None,
                    current_sku=current_sku or None,
                    proposed_sku=proposed_sku or None,
                ),
                reason=reason,
                urgency=urgency_enum,
                projected_savings_monthly=(
                    projected_savings_monthly if projected_savings_monthly > 0 else None
                ),
                evidence=evidence,
            )
            proposals_holder.append(proposal)
            name = resource_id.split("/")[-1]
            logger.info("CostAgent: proposal submitted — %s on %s", action_type, name)
            return f"Proposal submitted: {action_type} on {name}"

        agent = client.as_agent(
            name="cost-optimizer",
            instructions=_AGENT_INSTRUCTIONS,
            tools=[
                tool_query_resource_graph,
                tool_query_metrics,
                tool_get_resource_details,
                tool_query_activity_log,
                tool_list_nsg_rules,
                tool_get_resource_health,
                tool_list_advisor_recommendations,
                tool_propose_action,
            ],
        )

        rg_scope = (
            f"in resource group '{target_resource_group}'"
            if target_resource_group
            else "across the Azure environment"
        )

        # Build prompt — inject pre-computed findings + inventory (if provided).
        if inventory is not None:
            from src.infrastructure.inventory_formatter import format_inventory_for_prompt  # noqa: PLC0415
            inventory_text = format_inventory_for_prompt({
                "resources": inventory,
                "resource_count": len(inventory),
                "refreshed_at": "pre-fetched",
            })
            scan_prompt = (
                f"{findings_text}\n\n"
                f"{inventory_text}\n\n"
                f"Conduct a full cost optimisation audit {rg_scope}. "
                "FIRST: Investigate EVERY finding listed above — confirm with actual metrics "
                "(query_metrics for CPU/utilisation, get_resource_details for power state), "
                "then propose action with real data in the reason. "
                "THEN: Review the resource inventory for additional waste the APIs missed: "
                "deallocated VMs (disk storage costs), running VMs with avg CPU < 20% (P7D), "
                "unattached disks (diskState=Unattached), unassociated public IPs, "
                "oversized AKS/App Service/SQL/Cosmos DB, large unused storage accounts. "
                "Include projected_savings_monthly where possible."
            )
        else:
            scan_prompt = (
                f"{findings_text}\n\n"
                f"Conduct a full cost optimisation audit {rg_scope}. "
                "FIRST: Investigate EVERY finding listed above — confirm with actual metrics "
                "(query_metrics for CPU/utilisation, get_resource_details for power state). "
                "THEN: Query ALL resource types for additional cost waste the APIs missed — "
                "do NOT filter by type. Use the open-ended KQL from your instructions. "
                "Check: deallocated VMs, unattached disks, unused public IPs, "
                "running VMs with avg CPU < 20% (P7D), oversized PaaS resources. "
                "Include projected_savings_monthly where possible."
            )

        from src.infrastructure.llm_throttle import run_with_throttle
        await run_with_throttle(agent.run, scan_prompt)

        # ── Post-scan safety net: auto-propose raw_findings LLM missed ────
        # APIs ran pre-scan (no new calls here). If the LLM skipped any
        # pre-computed finding, we auto-propose it as a belt-and-suspenders.
        pre_auto_count = len(proposals_holder)
        for finding in raw_findings:
            resource_id = finding.get("resource_id", "")
            if not resource_id:
                rec_id = finding.get("rec_id", "")
                if rec_id and "/providers/" in rec_id:
                    idx = rec_id.find("/providers/Microsoft.Advisor")
                    if idx > 0:
                        resource_id = rec_id[:idx]
            if not resource_id:
                resource_id = finding.get("resource_name", "")
            if not resource_id:
                continue

            resource_name = finding.get("resource_name", "")
            already = any(
                p.target.resource_id == resource_id
                or (resource_name and resource_name in (p.target.resource_id or ""))
                for p in proposals_holder
            )
            if already:
                continue

            src = finding.get("source", "UNKNOWN")
            desc = finding.get("description", "")
            sev = finding.get("severity", "MEDIUM")
            urgency_enum = Urgency.HIGH if sev == "HIGH" else Urgency.MEDIUM
            action_type = (
                ActionType.SCALE_DOWN if src == "ADVISOR-HIGH" else ActionType.UPDATE_CONFIG
            )
            proposals_holder.append(ProposedAction(
                agent_id=_AGENT_ID,
                action_type=action_type,
                target=ActionTarget(
                    resource_id=resource_id,
                    resource_type=finding.get("resource_type", "unknown"),
                ),
                reason=f"{src}: {desc}",
                urgency=urgency_enum,
            ))

        auto_missed = len(proposals_holder) - pre_auto_count
        if auto_missed:
            scan_notes.append(
                f"Post-scan safety net: {auto_missed} pre-computed finding(s) "
                "auto-proposed (LLM did not cover them)"
            )

        # Post-scan log — counts per detection path.
        auto_count = sum(
            1 for p in proposals_holder
            if p.reason.startswith(("ADVISOR-HIGH:", "POLICY-NONCOMPLIANT:"))
        )
        llm_count = len(proposals_holder) - auto_count
        if proposals_holder:
            scan_notes.append(
                f"Scan complete — {len(proposals_holder)} proposal(s): "
                f"{auto_count} deterministic, {llm_count} LLM-originated"
            )
        else:
            scan_notes.append(
                "Scan complete — no cost waste found in Azure environment."
            )

        self.scan_notes: list[str] = scan_notes
        return proposals_holder

    # ------------------------------------------------------------------
    # Demo mode — realistic sample proposals for pipeline testing
    # ------------------------------------------------------------------

    def _demo_proposals(self) -> list[ProposedAction]:
        """Return 2 realistic sample proposals for DEMO_MODE=true.

        These proposals flow through the full RuriSkry governance pipeline
        (SRI scoring, governance engine, audit trail) — they just skip the
        real Azure investigation step.  Useful for verifying the pipeline
        end-to-end without Azure OpenAI credentials.
        """
        return [
            ProposedAction(
                agent_id=_AGENT_ID,
                action_type=ActionType.DELETE_RESOURCE,
                target=ActionTarget(
                    resource_id="vm-idle-demo-01",
                    resource_type="Microsoft.Compute/virtualMachines",
                    current_monthly_cost=234.50,
                ),
                reason=(
                    "[DEMO] VM shows 1.2% avg CPU over 30 days — idle resource. "
                    "No activity log entries for 4 weeks. Estimated savings $234/month."
                ),
                urgency=Urgency.MEDIUM,
                projected_savings_monthly=234.50,
            ),
            ProposedAction(
                agent_id=_AGENT_ID,
                action_type=ActionType.SCALE_DOWN,
                target=ActionTarget(
                    resource_id="aks-demo-prod",
                    resource_type="Microsoft.ContainerService/managedClusters",
                    current_monthly_cost=890.0,
                    current_sku="Standard_D4s_v3 (6 nodes)",
                    proposed_sku="Standard_D4s_v3 (3 nodes)",
                ),
                reason=(
                    "[DEMO] AKS cluster using 18% avg CPU across 6 nodes. "
                    "Reducing to 3 nodes saves ~$320/month with adequate headroom."
                ),
                urgency=Urgency.LOW,
                projected_savings_monthly=320.0,
            ),
        ]

    # ------------------------------------------------------------------
    # Deterministic rule-based scan (fallback / mock mode)
    # ------------------------------------------------------------------

    def _scan_rules(self) -> list[ProposedAction]:
        """Run all detection rules across the resource topology."""
        proposals: list[ProposedAction] = []
        for resource in self._resources:
            proposal = self._analyze_resource(resource)
            if proposal is not None:
                proposals.append(proposal)
                logger.info(
                    "CostOptimizationAgent: flagged '%s' (%s) — savings $%.0f/month",
                    resource["name"],
                    proposal.action_type.value,
                    proposal.projected_savings_monthly or 0,
                )
        return proposals

    # ------------------------------------------------------------------
    # Private helpers (rule-based path)
    # ------------------------------------------------------------------

    def _analyze_resource(self, resource: dict) -> ProposedAction | None:
        monthly_cost: float | None = resource.get("monthly_cost")
        if monthly_cost is None or monthly_cost < _MIN_COST_THRESHOLD:
            return None

        resource_type: str = resource.get("type", "")
        if "virtualMachines" in resource_type:
            return self._propose_vm_scale_down(resource, monthly_cost)
        if "managedClusters" in resource_type:
            return self._propose_aks_scale_down(resource, monthly_cost)
        return None

    def _propose_vm_scale_down(
        self, resource: dict, monthly_cost: float
    ) -> ProposedAction | None:
        sku: str = resource.get("sku", "")
        if sku not in _OVERSIZED_SKUS:
            return None
        proposed_sku = _DOWNSIZE_MAP[sku]
        savings = round(monthly_cost * _VM_DOWNSIZE_SAVINGS_RATE, 2)
        tags = resource.get("tags") or {}
        is_idle = tags.get("purpose") == "disaster-recovery"
        reason = f"VM '{resource['name']}' is running SKU {sku} at ${monthly_cost:.0f}/month. "
        if is_idle:
            reason += "Tagged as disaster-recovery — expected to be idle most of the time. "
        reason += f"Downsizing to {proposed_sku} is estimated to save ${savings:.0f}/month."
        urgency = Urgency.MEDIUM if monthly_cost >= _HIGH_COST_THRESHOLD else Urgency.LOW
        return ProposedAction(
            agent_id=_AGENT_ID,
            action_type=ActionType.SCALE_DOWN,
            target=ActionTarget(
                resource_id=resource["id"],
                resource_type=resource["type"],
                resource_group=resource.get("resource_group"),
                current_sku=sku,
                proposed_sku=proposed_sku,
                current_monthly_cost=monthly_cost,
            ),
            reason=reason,
            urgency=urgency,
            projected_savings_monthly=savings,
        )

    def _propose_aks_scale_down(
        self, resource: dict, monthly_cost: float
    ) -> ProposedAction | None:
        node_count: int = resource.get("node_count", 0)
        if node_count < _AKS_SCALE_DOWN_NODE_THRESHOLD:
            return None
        proposed_nodes = node_count - 2
        savings = round(monthly_cost * _AKS_SCALE_DOWN_SAVINGS_RATE, 2)
        reason = (
            f"AKS cluster '{resource['name']}' is running {node_count} nodes "
            f"at ${monthly_cost:.0f}/month. Reducing to {proposed_nodes} nodes "
            f"is estimated to save ${savings:.0f}/month."
        )
        return ProposedAction(
            agent_id=_AGENT_ID,
            action_type=ActionType.SCALE_DOWN,
            target=ActionTarget(
                resource_id=resource["id"],
                resource_type=resource["type"],
                resource_group=resource.get("resource_group"),
                current_sku=f"{node_count} nodes",
                proposed_sku=f"{proposed_nodes} nodes",
                current_monthly_cost=monthly_cost,
            ),
            reason=reason,
            urgency=Urgency.MEDIUM,
            projected_savings_monthly=savings,
        )
