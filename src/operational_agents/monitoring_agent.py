"""SRE Monitoring & RCA Agent — detects anomalies and proposes remediation.

This is an operational agent (the governed subject). It proposes
infrastructure actions that RuriSkry evaluates before execution.

Phase 12 — Intelligent, evidence-driven agent
----------------------------------------------
The agent now operates in two modes:

**Alert mode** (``alert_payload`` provided):
  Triggered by an Azure Monitor alert webhook.  The agent investigates the
  specific alerted resource — queries real metrics to confirm the alert,
  checks what services depend on it, and proposes an evidence-backed
  remediation action (scale_up, restart_service, etc.).

**Scan mode** (no ``alert_payload``):
  Proactively scans a resource group (or the whole subscription) for
  structural reliability anomalies: unowned critical resources, circular
  dependencies, high-cost single points of failure.

Both modes use GPT-4.1 with generic Azure investigation tools to reason about
the evidence before calling ``propose_action``.

Microsoft Agent Framework tools (live mode)
--------------------------------------------
- ``query_metrics(resource_id, metric_names, timespan)`` — confirm alert data
- ``get_resource_details(resource_id)`` — check tags, dependencies, power state
- ``query_resource_graph(kusto_query)`` — discover resources for scan mode
- ``query_activity_log(resource_group, timespan)`` — recent changes and failures
- ``list_nsg_rules(nsg_resource_id)`` — inspect NSG rules for network issues
- ``get_resource_health(resource_id)`` — Azure Platform availability signal
- ``list_advisor_recommendations(scope, category)`` — Azure Advisor HA/Perf tips
- ``propose_action(...)`` — submit a validated ProposedAction

In mock mode (USE_LOCAL_MOCKS=true) the deterministic ``_scan_rules()``
fallback runs — reads ``data/seed_resources.json``, applies SRE heuristics.
"""

import json
import logging
from pathlib import Path
from typing import Any

from src.config import settings as _default_settings
from src.core.models import ActionTarget, ActionType, EvidencePayload, ProposedAction, Urgency
from src.operational_agents import is_compliant_reason

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_AGENT_ID = "monitoring-agent"

_DEFAULT_RESOURCES_PATH = (
    Path(__file__).parent.parent.parent / "data" / "seed_resources.json"
)

# Rule-based fallback threshold (unchanged from Phase 8)
_CRITICAL_COST_THRESHOLD: float = 500.0

# CPU thresholds for alert-driven remediation
_CPU_SCALE_UP_THRESHOLD: float = 80.0   # sustained above this → scale_up
_CPU_RESTART_THRESHOLD: float = 95.0    # above this → may also restart

# System instructions for alert-driven investigation.
_ALERT_INSTRUCTIONS = """\
You are a Senior SRE investigating a triggered Azure Monitor alert.
Every conclusion must be backed by evidence from real Azure API data.
Do not guess — confirm with tools before proposing action.

STEP 1 — UNDERSTAND THE ALERT
Read the alert payload: alertRule/metric name, resource_id, severity, and
description. The resource_id in the payload may be a workspace ARM ID —
extract the actual VM or resource name from the alertRule or description field.

STEP 2 — INVESTIGATE BY ALERT TYPE

A. AVAILABILITY / HEARTBEAT (VM stopped, heartbeat = 0, service unavailable):
   - Call get_resource_details on the resource. Read power state and resource type.
   - A deallocated/stopped VM WILL have no metrics — this IS confirmation,
     not ambiguity. Do NOT skip action because metrics are empty.
   - Call query_activity_log for the resource group (last 24h) to determine:
     * Was it stopped manually? By auto-shutdown policy? By an Azure incident?
     * Any failed operations before the stop?
   - For VMs (Microsoft.Compute/virtualMachines): propose restart_service (HIGH urgency).
   - For App Services (Microsoft.Web/sites) or Function Apps: also propose restart_service
     (HIGH urgency) — the Execution Agent will use restart_app_service or
     restart_function_app tools automatically based on resource_type.
   - Reason must include: confirmed power state/health, activity log findings,
     downstream dependencies.

B. CPU / MEMORY METRIC ALERTS:
   - Call query_metrics for "Percentage CPU" over P1D and P7D.
   - P7D avg > 70%: sustained overload → propose scale_up (HIGH urgency).
   - P1D spike only, P7D avg < 40%: transient → propose update_config (MEDIUM).
   - Call get_resource_details to confirm current SKU for right-sizing advice.
   - Include in reason: "P7D avg CPU: X%, P1D avg: Y%, peak: Z%".

C. DISK / STORAGE ALERTS:
   - Call query_metrics for "Disk Read Bytes", "Disk Write Bytes",
     "OS Disk Queue Depth".
   - Queue depth > 10 sustained → propose scale_up to Premium SSD (HIGH).
   - Near-full OS disk → propose update_config to expand disk (HIGH).

D. DATABASE / DATA SERVICE ALERTS:
   - Call get_resource_details to read current configuration.
   - Call query_metrics for service-relevant metrics (latency, error rate).
   - High P99 latency or elevated 5xx rate → propose update_config (HIGH/MEDIUM).
   - Configuration drift detected → propose update_config with evidence.

E. NETWORK / CONNECTIVITY ALERTS:
   - Call list_nsg_rules on any associated NSG.
   - Call query_activity_log for recent NSG rule changes.
   - Unexpected traffic block or exposure → propose modify_nsg with evidence.

STEP 3 — PROPOSE ACTION
Call propose_action with:
  - action_type matching root cause (restart_service, scale_up, update_config,
    modify_nsg, etc.)
  - reason: confirmed metric values, power state, activity log evidence
  - urgency: HIGH if service is down or SLA at risk; MEDIUM if degraded; LOW if preventive

CRITICAL: Never propose action without first confirming with real data.
CRITICAL: For availability alerts — no metrics = VM is down = restart_service.
CRITICAL: Include the resource ARM ID in your proposal, not just the name.
"""

# System instructions for proactive scanning.
_SCAN_INSTRUCTIONS = """\
You are a Senior SRE conducting an enterprise-grade proactive infrastructure
reliability review. Azure Advisor (HighAvailability/Performance), Defender for Cloud,
and Azure Policy have pre-run and found reliability issues — your job is to confirm
and enrich each finding, then look for anything they missed.

━━━ PRIMARY TASK: INVESTIGATE PRE-COMPUTED RELIABILITY FINDINGS ━━━
Reliability findings from Microsoft APIs are listed at the top of the prompt.
For EACH finding:
1. Call get_resource_details to confirm the resource state and health.
2. Call query_metrics for relevant metrics (CPU, availability, error rate, latency).
3. Assess who depends on this resource (what services fail if it goes down).
4. Call propose_action with confirmed state and blast radius in the reason.
Do NOT skip any pre-computed finding.

━━━ SECONDARY TASK: INDEPENDENT RELIABILITY DISCOVERY ━━━
After processing pre-computed findings, check the resource inventory (above)
or call query_resource_graph for reliability risks the APIs missed:

VM AVAILABILITY — CHECK EVERY VM, NO EXCEPTIONS:
  For each VM, call get_resource_details to read power state.
  STOPPED / DEALLOCATED VM (powerState != "running"):
    - HIGH urgency. IMMEDIATELY propose restart_service (HIGH).
      Do NOT wait for activity log — the stopped state IS the incident.
    - After proposing, call query_activity_log to enrich the reason (optional).
    - Exception: DR/standby VMs → still propose at MEDIUM. Note DR risk.
  RUNNING VM:
    - Call query_metrics: "Percentage CPU", "Available Memory Bytes" over P7D.
    - CPU avg > 70% → propose scale_up (HIGH).
    - CPU avg 50–70% with memory > 75% → propose scale_up (MEDIUM).

DATABASE & DATA SERVICE HEALTH:
  - No failover policy (single-region) → propose update_config (MEDIUM).
  - publicNetworkAccess = Enabled → flag (MEDIUM).
  - No backup policy → flag (MEDIUM).
  - P99 latency > 500ms → propose update_config (MEDIUM).

CONTAINER APPS & APP SERVICES:
  - Replica count < 2 in non-dev → propose update_config (MEDIUM).
  - Http5xx error rate > 1% → propose restart_service (MEDIUM); resource_type="Microsoft.Web/sites".
  - App Service Plan CpuPercentage > 70% → propose scale_up (MEDIUM);
    resource_type="Microsoft.Web/serverfarms"; put proposed SKU in proposed_sku.

AKS CLUSTERS:
  - Node CPU > 70% across nodes (P7D) → propose scale_up (MEDIUM);
    resource_type="Microsoft.ContainerService/managedClusters";
    put target node_count in config_changes as {"nodepool_name": "<name>", "node_count": "<n>"}.

MONITORING GAPS:
  - VM without AMA extension → propose update_config (MEDIUM).
  - Resources with ZERO tags → propose update_config (LOW).

ORPHANED RESOURCES:
  - Unattached disks → propose delete_resource (LOW/MEDIUM).
  - Unused public IPs → propose delete_resource (LOW).

━━━ RESOURCE DISCOVERY (use this KQL when no inventory is provided) ━━━
Resources
| project id, name, type, location, resourceGroup, tags, properties, sku
| order by type asc

Do NOT add a 'where type in (...)' filter — ALL resource types matter for reliability.

━━━ URGENCY SCALE ━━━
  HIGH:   VM not running, CPU > 70%, service completely unavailable.
  MEDIUM: Single-region DB, no monitoring agent, Container App with 1 replica,
          public network access on data services.
  LOW:    Missing tags, orphaned resources, configuration hygiene gaps.

CRITICAL: Never assume a resource is healthy because metrics are absent.
Always confirm VM health via get_resource_details power state.
NEVER skip or suppress a finding. The governance engine handles deduplication.
"""

# Variant of _SCAN_INSTRUCTIONS used when a pre-fetched inventory is available.
# Same as _SCAN_INSTRUCTIONS but references the inventory in the primary task.
_SCAN_INSTRUCTIONS_WITH_INVENTORY = """\
You are a Senior SRE conducting an enterprise-grade proactive infrastructure
reliability review. Microsoft Reliability APIs have pre-run and found issues —
your job is to confirm and enrich each finding, then review every resource in
the inventory for anything they missed.

━━━ PRIMARY TASK: INVESTIGATE PRE-COMPUTED RELIABILITY FINDINGS ━━━
Reliability findings from Microsoft APIs are listed at the top of the prompt.
For EACH finding:
1. Call get_resource_details to confirm the resource state and health.
2. Call query_metrics for relevant metrics (CPU, availability, error rate).
3. Assess who depends on this resource and call propose_action with full evidence.
Do NOT skip any pre-computed finding.

━━━ SECONDARY TASK: REVIEW THE RESOURCE INVENTORY ━━━
A complete resource inventory is included in the prompt above.
It contains EVERY resource with current properties, tags, and configuration.
For VMs: the pre-fetched power state is included — "VM deallocated" = confirmed DOWN.
Review EVERY resource. Do not skip any.

VM AVAILABILITY — CHECK EVERY VM IN THE INVENTORY:
  STOPPED / DEALLOCATED VM (powerState != "VM running"):
    - HIGH urgency. IMMEDIATELY propose restart_service (HIGH).
      Do NOT wait for activity log — the stopped state IS the incident.
    - After proposing, call query_activity_log to enrich (optional).
    - Exception: DR/standby VMs → propose at MEDIUM. Note DR risk.
  RUNNING VM: call query_metrics (CPU, memory) over P7D.
    - CPU avg > 70% → propose scale_up (HIGH).

DATABASE & DATA SERVICE HEALTH:
  - No failover policy (single-region) → propose update_config (MEDIUM).
  - publicNetworkAccess = Enabled → flag (MEDIUM).
  - No backup policy → flag (MEDIUM).

CONTAINER APPS & APP SERVICES:
  - Replica count < 2 in non-dev → propose update_config (MEDIUM).
  - Http5xx error rate > 1% → propose restart_service (MEDIUM); resource_type="Microsoft.Web/sites".
  - App Service Plan CpuPercentage > 70% → propose scale_up (MEDIUM);
    resource_type="Microsoft.Web/serverfarms"; put proposed SKU in proposed_sku.

AKS CLUSTERS:
  - Node CPU > 70% across nodes (P7D) → propose scale_up (MEDIUM);
    resource_type="Microsoft.ContainerService/managedClusters";
    put target node_count in config_changes as {"nodepool_name": "<name>", "node_count": "<n>"}.

MONITORING GAPS:
  - VM without AMA extension → propose update_config (MEDIUM).
  - Resources with ZERO tags → propose update_config (LOW).

ORPHANED RESOURCES:
  - Unattached disks → propose delete_resource (LOW/MEDIUM).
  - Unused public IPs → propose delete_resource (LOW).

━━━ URGENCY SCALE ━━━
  HIGH:   VM not running, CPU > 70%, service completely unavailable.
  MEDIUM: Single-region DB, no monitoring agent, Container App with 1 replica.
  LOW:    Missing tags, orphaned resources.

CRITICAL: Never assume healthy because metrics are absent.
Always confirm VM health via the pre-fetched powerState in the inventory.
NEVER skip or suppress a finding. The governance engine handles deduplication.
"""


# ---------------------------------------------------------------------------
# Agent
# ---------------------------------------------------------------------------


class MonitoringAgent:
    """Detects anomalies and proposes remediation.

    In live mode (USE_LOCAL_MOCKS=false) GPT-4.1 investigates real metric
    data via Azure tools before proposing any action.  Supports both
    alert-driven investigation and proactive scanning.

    In mock mode only the deterministic ``_scan_rules()`` runs.

    Usage::

        agent = MonitoringAgent()

        # Respond to an Azure Monitor alert:
        alert = {"resource_id": "vm-web-01", "metric": "Percentage CPU",
                 "value": 95.0, "threshold": 80.0}
        proposals = await agent.scan(alert_payload=alert)

        # Proactive scan of a resource group:
        proposals = await agent.scan(target_resource_group="ruriskry-prod-rg")
    """

    def __init__(
        self,
        resources_path: str | Path | None = None,
        cfg=None,
    ) -> None:
        path = Path(resources_path) if resources_path else _DEFAULT_RESOURCES_PATH
        with open(path, encoding="utf-8") as fh:
            data: dict = json.load(fh)

        # Fast lookup: resource name → resource dict (for _scan_rules fallback)
        self._resources: dict[str, dict] = {
            r["name"]: r for r in data.get("resources", [])
        }
        self._edges: list[dict] = data.get("dependency_edges", [])

        self._cfg = cfg or _default_settings
        self._use_framework: bool = bool(self._cfg.azure_openai_endpoint)
        self.scan_error: str | None = None  # populated if framework call fails

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def scan(
        self,
        alert_payload: dict[str, Any] | None = None,
        target_resource_group: str | None = None,
        inventory: list[dict] | None = None,
    ) -> list[ProposedAction]:
        """Investigate the environment and return remediation proposals.

        Args:
            alert_payload: Optional Azure Monitor alert dict.  When provided
                the agent investigates the specific alerted resource.
                Expected keys: ``resource_id``, ``metric``, ``value``,
                ``threshold``, ``resource_group`` (all optional except
                ``resource_id``).
            target_resource_group: Optional resource group to scope a
                proactive scan.  Ignored when ``alert_payload`` is provided.
            inventory: Optional pre-fetched resource list from the Resource
                Inventory feature.  When provided, injected into the LLM
                prompt so the agent reviews ALL resources without relying
                on non-deterministic tool-call discovery.

        Returns:
            List of :class:`~src.core.models.ProposedAction` objects.
        """
        if self._cfg.demo_mode:
            logger.info(
                "MonitoringAgent: DEMO_MODE enabled — returning sample proposals "
                "for pipeline testing (set DEMO_MODE=false for real Azure scanning)."
            )
            return self._demo_proposals()

        if not self._use_framework:
            logger.info(
                "MonitoringAgent: no Azure OpenAI endpoint configured — "
                "returning no proposals (set AZURE_OPENAI_ENDPOINT to enable live scanning)."
            )
            return []

        self.scan_error = None
        try:
            return await self._scan_with_framework(alert_payload, target_resource_group, inventory)
        except Exception as exc:  # noqa: BLE001
            self.scan_error = str(exc)
            logger.warning(
                "MonitoringAgent: framework call failed (%s) — returning no proposals "
                "(live-mode fallback to seed data would generate false positives).",
                exc,
            )
            return []

    # ------------------------------------------------------------------
    # Microsoft Agent Framework path (live mode)
    # ------------------------------------------------------------------

    async def _scan_with_framework(
        self,
        alert_payload: dict[str, Any] | None,
        target_resource_group: str | None,
        inventory: list[dict] | None = None,
    ) -> list[ProposedAction]:
        """Run GPT-4.1 with Azure investigation tools."""
        from openai import AsyncAzureOpenAI
        from azure.identity import DefaultAzureCredential, get_bearer_token_provider
        import agent_framework as af
        from agent_framework.openai import OpenAIResponsesClient
        from src.infrastructure.azure_tools import (
            query_metrics_async,
            get_resource_details_async,
            query_resource_graph_async,
            query_activity_log_async,
            list_nsg_rules_async,
            get_resource_health_async,
            list_advisor_recommendations_async,
            list_defender_assessments_async,
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

        # ── Pre-scan: Microsoft APIs detect first (scan mode only) ────────────
        # Alert mode is a targeted investigation — mixing subscription-wide
        # reliability findings would be noise. Pre-scan runs for scan mode only.
        raw_findings: list[dict] = []

        if not alert_payload:
            for advisor_category in ("HighAvailability", "Performance"):
                try:
                    advisor_recs = await list_advisor_recommendations_async(
                        scope=target_resource_group or None, category=advisor_category
                    )
                    advisor_high = [
                        r for r in advisor_recs if str(r.get("impact", "")).lower() == "high"
                    ]
                    for rec in advisor_high:
                        short_desc = (
                            rec.get("shortDescription", {}).get("problem", "")
                            if isinstance(rec.get("shortDescription"), dict)
                            else str(rec.get("shortDescription", ""))
                        ) or rec.get("description", "")
                        raw_findings.append({
                            "source": f"ADVISOR-HIGH",
                            "severity": "HIGH",
                            "resource_name": rec.get("impactedValue", ""),
                            "description": short_desc,
                            "rec_id": rec.get("id", ""),
                            "resource_type": rec.get("impactedField", ""),
                        })
                    scan_notes.append(
                        f"Pre-scan: Azure Advisor {advisor_category} — "
                        f"{len(advisor_high)} HIGH recommendation(s)"
                    )
                except Exception as exc:
                    logger.warning("MonitoringAgent: pre-scan Advisor (%s) failed: %s", advisor_category, exc)
                    scan_notes.append(f"Pre-scan: Azure Advisor {advisor_category} unavailable ({exc})")

            try:
                defender_assessments = await list_defender_assessments_async(
                    scope=target_resource_group or None
                )
                defender_high = [
                    a for a in defender_assessments if str(a.get("severity", "")).lower() == "high"
                ]
                for a in defender_high:
                    raw_findings.append({
                        "source": "DEFENDER-HIGH",
                        "severity": "HIGH",
                        "resource_id": a.get("resourceId", ""),
                        "resource_name": a.get("resourceName", ""),
                        "description": a.get("assessmentName", ""),
                        "remediation": a.get("remediation", ""),
                        "resource_type": "unknown",
                    })
                scan_notes.append(
                    f"Pre-scan: Defender for Cloud — {len(defender_high)} HIGH assessment(s)"
                )
            except Exception as exc:
                logger.warning("MonitoringAgent: pre-scan Defender failed: %s", exc)
                scan_notes.append(f"Pre-scan: Defender for Cloud unavailable ({exc})")

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
                logger.warning("MonitoringAgent: pre-scan Policy failed: %s", exc)
                scan_notes.append(f"Pre-scan: Azure Policy unavailable ({exc})")

            # Build findings summary to inject into the LLM prompt.
            if raw_findings:
                findings_lines = [
                    f"=== PRE-COMPUTED FINDINGS: {len(raw_findings)} reliability issue(s) from Microsoft APIs ===",
                    "Each finding below was detected deterministically. For each one:",
                    "1. Call get_resource_details to confirm the issue is current.",
                    "2. Call query_metrics for health metrics to enrich the reason.",
                    "3. Assess blast radius and call propose_action with full evidence.",
                    "",
                ]
                for i, f in enumerate(raw_findings, 1):
                    r_name = f.get("resource_name") or f.get("resource_id", "?")
                    line = f"[{i}] [{f.get('source', '?')}] Resource: {r_name} — {f.get('description', '?')}"
                    if f.get("remediation"):
                        line += f" | Hint: {f['remediation']}"
                    findings_lines.append(line)
                findings_lines.append("")
                findings_text = "\n".join(findings_lines)
            else:
                findings_text = (
                    "=== PRE-COMPUTED FINDINGS: No high-severity reliability issues detected "
                    "by Microsoft APIs ===\n"
                )
        else:
            findings_text = ""

        @af.tool(
            name="query_metrics",
            description=(
                "Query Azure Monitor metrics for a resource. Returns average, max, and "
                "min values over the specified timespan. "
                "metric_names is a comma-separated list (e.g. 'Percentage CPU'). "
                "timespan uses ISO 8601 format (e.g. 'P7D' for 7 days, 'PT24H' for 24 hours)."
            ),
        )
        async def tool_query_metrics(
            resource_id: str,
            metric_names: str,
            timespan: str = "P7D",
        ) -> str:
            """Confirm alert metrics or check resource health."""
            names = [m.strip() for m in metric_names.split(",")]
            results = await query_metrics_async(resource_id, names, timespan)
            return json.dumps(results, default=str)

        @af.tool(
            name="get_resource_details",
            description=(
                "Get full details for a specific Azure resource by its ARM resource ID "
                "or short name. Returns SKU, tags, dependents, cost, and other properties. "
                "For Virtual Machines, also returns 'powerState' (e.g. 'VM running', "
                "'VM deallocated', 'VM stopped') — use this to detect availability issues. "
                "A powerState of 'VM deallocated' or 'VM stopped' means the VM is DOWN."
            ),
        )
        async def tool_get_resource_details(resource_id: str) -> str:
            """Retrieve resource details to understand impact and dependencies."""
            details = await get_resource_details_async(resource_id)
            return json.dumps(details, default=str)

        @af.tool(
            name="query_resource_graph",
            description=(
                "Query Azure Resource Graph with a Kusto (KQL) query to discover resources. "
                "Returns a JSON array with id, name, type, location, resourceGroup, tags."
            ),
        )
        async def tool_query_resource_graph(kusto_query: str) -> str:
            """Discover resources for proactive scanning."""
            results = await query_resource_graph_async(kusto_query)
            return json.dumps(results, default=str)

        @af.tool(
            name="query_activity_log",
            description=(
                "Query Azure Monitor activity logs for a resource group. "
                "Returns recent operations with timestamp, operation name, status "
                "(Succeeded/Failed), caller, and resource type. "
                "Use this to check for recent failed operations or suspicious changes "
                "that may explain an active alert. "
                "timespan uses ISO 8601 format (e.g. 'P7D' for last 7 days)."
            ),
        )
        async def tool_query_activity_log(resource_group: str, timespan: str = "P7D") -> str:
            """Check recent changes that may correlate with reliability issues."""
            entries = await query_activity_log_async(resource_group, timespan)
            return json.dumps(entries, default=str)

        @af.tool(
            name="list_nsg_rules",
            description=(
                "List the security rules for an Azure Network Security Group. "
                "Returns JSON array of rules with name, port, access (Allow/Deny), "
                "priority, and direction. Use this to check if NSG misconfigurations "
                "may be contributing to network-related reliability issues."
            ),
        )
        async def tool_list_nsg_rules(nsg_resource_id: str) -> str:
            """Inspect NSG rules when diagnosing network-layer incidents."""
            rules = await list_nsg_rules_async(nsg_resource_id)
            return json.dumps(rules, default=str)

        @af.tool(
            name="get_resource_health",
            description=(
                "Get Azure Resource Health status for a specific resource. "
                "Returns availabilityState (Available/Unavailable/Degraded/Unknown), "
                "a human-readable summary, reasonType, and timestamps. "
                "Use this as authoritative confirmation of resource availability — "
                "Azure Platform reports this directly, independent of metrics. "
                "A state of 'Unavailable' or 'Degraded' means the platform has detected an issue."
            ),
        )
        async def tool_get_resource_health(resource_id: str) -> str:
            """Check Azure Platform health signal for a resource."""
            health = await get_resource_health_async(resource_id)
            return json.dumps(health, default=str)

        @af.tool(
            name="list_advisor_recommendations",
            description=(
                "List Azure Advisor recommendations for the subscription or a specific scope. "
                "Returns recommendations with category (Cost/Security/HighAvailability/Performance), "
                "impact (High/Medium/Low), impactedValue (resource name), shortDescription, "
                "and remediation guidance. "
                "scope (optional): filter by resource group name. "
                "category (optional): filter by one category."
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
                "Submit a governance proposal for a resource after confirming with metrics. "
                "action_type must be one of: scale_up, scale_down, delete_resource, "
                "restart_service, modify_nsg, create_resource, update_config. "
                "urgency must be one of: low, medium, high. "
                "evidence_json: JSON string with observed data — metrics (dict), "
                "logs (list), alerts (list), duration_minutes (int), severity "
                "(low|medium|high|critical). Pass {} if no structured evidence available."
            ),
        )
        def tool_propose_action(
            resource_id: str,
            action_type: str,
            reason: str,
            urgency: str = "medium",
            current_sku: str = "",
            proposed_sku: str = "",
            resource_type: str = "",
            resource_group: str = "",
            evidence_json: str = "{}",
        ) -> str:
            """Validate and record a ProposedAction."""
            try:
                action_type_enum = ActionType(action_type.lower())
            except ValueError:
                valid = [e.value for e in ActionType]
                return f"ERROR: Invalid action_type '{action_type}'. Valid: {valid}"
            try:
                urgency_enum = Urgency(urgency.lower())
            except ValueError:
                urgency_enum = Urgency.MEDIUM

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
                    "MonitoringAgent: blocked compliant-resource proposal — %s on %s",
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
                evidence=evidence,
            )
            proposals_holder.append(proposal)
            name = resource_id.split("/")[-1]
            logger.info("MonitoringAgent: proposal submitted — %s on %s", action_type, name)
            return f"Proposal submitted: {action_type} on {name}"

        # Choose instructions and prompt based on mode.
        if alert_payload:
            instructions = _ALERT_INSTRUCTIONS
            alert_summary = json.dumps(alert_payload, indent=2)
            metric = alert_payload.get("metric", "")
            is_heartbeat = any(k in metric.lower() for k in ("heartbeat", "availab", "stopped", "down"))
            alert_type_hint = (
                "This is a HEARTBEAT / AVAILABILITY alert — the resource is likely stopped "
                "or deallocated. "
                "CRITICAL: empty metrics confirm the VM is down — do NOT treat empty metrics "
                "as 'nothing to do'. "
                "Call get_resource_details first to read powerState, then query_activity_log "
                "to find why it stopped, then propose restart_service (HIGH urgency)."
                if is_heartbeat else
                "Identify the alert type from the metric name and follow the matching step "
                "(A–E) in your instructions. Confirm all findings with real Azure API data "
                "before calling propose_action."
            )
            prompt = (
                f"An Azure Monitor alert has fired:\n\n{alert_summary}\n\n"
                f"{alert_type_hint}\n\n"
                "Follow ALL steps in your instructions: "
                "(1) Read the alert payload and identify resource_id and alert type. "
                "(2) Call get_resource_details on the alerted resource — check powerState, "
                "current configuration, and health. "
                "(3) Call query_metrics or query_activity_log based on alert type. "
                "(4) Call propose_action with confirmed evidence — include power state, "
                "metric values, and activity log findings in the reason."
            )
        elif inventory is not None:
            # Inventory-assisted scan — inject pre-computed findings + formatted resource list.
            from src.infrastructure.inventory_formatter import format_inventory_for_prompt  # noqa: PLC0415
            inventory_text = format_inventory_for_prompt({
                "resources": inventory,
                "resource_count": len(inventory),
                "refreshed_at": "pre-fetched",
            })
            instructions = _SCAN_INSTRUCTIONS_WITH_INVENTORY
            rg_scope = (
                f"in resource group '{target_resource_group}'"
                if target_resource_group
                else "across the Azure subscription"
            )
            prompt = (
                f"{findings_text}\n\n"
                f"{inventory_text}\n\n"
                f"Conduct a full reliability scan {rg_scope}. "
                "FIRST: Investigate EVERY finding listed above — confirm with real data, "
                "assess blast radius, and propose action. "
                "THEN: Review the resource inventory for additional reliability risks the APIs missed: "
                "stopped/deallocated VMs (restart_service HIGH), running VMs with high CPU (scale_up), "
                "single-region databases, App Services with 1 replica, missing monitoring agents, "
                "resources with zero tags, orphaned disks/IPs. "
                "Propose remediation for EVERY reliability risk found."
            )
        else:
            instructions = _SCAN_INSTRUCTIONS
            rg_scope = (
                f"in resource group '{target_resource_group}'"
                if target_resource_group
                else "across the Azure environment"
            )
            prompt = (
                f"{findings_text}\n\n"
                f"Conduct a full proactive reliability scan {rg_scope}. "
                "FIRST: Investigate EVERY finding listed above — confirm with real data and propose action. "
                "THEN: Query ALL resource types for additional reliability risks the APIs missed — "
                "do NOT filter by type. Use the open-ended KQL from your instructions. "
                "Check: VM power states (stopped = HIGH restart_service), running VM CPU (P7D), "
                "database failover/backup config, Container App replica counts, "
                "AMA monitoring gaps, orphaned resources, tagging gaps. "
                "Propose remediation for EVERY reliability risk found."
            )

        agent = client.as_agent(
            name="sre-monitoring-agent",
            instructions=instructions,
            tools=[
                tool_query_metrics,
                tool_get_resource_details,
                tool_query_resource_graph,
                tool_query_activity_log,
                tool_list_nsg_rules,
                tool_get_resource_health,
                tool_list_advisor_recommendations,
                tool_propose_action,
            ],
        )

        from src.infrastructure.llm_throttle import run_with_throttle
        await run_with_throttle(agent.run, prompt)

        # ── Post-scan safety net (scan mode only) ────────────────────────────
        # APIs ran pre-scan (no new calls here). If the LLM skipped any
        # pre-computed finding, we auto-propose it as a belt-and-suspenders.
        # Alert mode has no raw_findings so this is a no-op there.
        if not alert_payload:
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
                rem = finding.get("remediation", "")
                reason = f"{src}: {desc}"
                if rem:
                    reason += f". Remediation: {rem}"

                proposals_holder.append(ProposedAction(
                    agent_id=_AGENT_ID,
                    action_type=ActionType.UPDATE_CONFIG,
                    target=ActionTarget(
                        resource_id=resource_id,
                        resource_type=finding.get("resource_type", "unknown"),
                    ),
                    reason=reason,
                    urgency=urgency_enum,
                ))

            auto_missed = len(proposals_holder) - pre_auto_count
            if auto_missed:
                scan_notes.append(
                    f"Post-scan safety net: {auto_missed} pre-computed finding(s) "
                    "auto-proposed (LLM did not cover them)"
                )

            # Post-scan integrity log.
            auto_count = sum(
                1 for p in proposals_holder
                if p.reason.startswith(("ADVISOR-HIGH:", "DEFENDER-HIGH:", "POLICY-NONCOMPLIANT:"))
            )
            llm_count = len(proposals_holder) - auto_count
            if proposals_holder:
                scan_notes.append(
                    f"Scan complete — {len(proposals_holder)} proposal(s): "
                    f"{auto_count} deterministic, {llm_count} LLM-originated"
                )
            else:
                scan_notes.append(
                    "Scan complete — no reliability risks found in Azure environment."
                )

        self.scan_notes: list[str] = scan_notes
        return proposals_holder

    # ------------------------------------------------------------------
    # ------------------------------------------------------------------
    # Demo mode — realistic sample proposals for pipeline testing
    # ------------------------------------------------------------------

    def _demo_proposals(self) -> list[ProposedAction]:
        """Return 1 realistic sample proposal for DEMO_MODE=true."""
        return [
            ProposedAction(
                agent_id=_AGENT_ID,
                action_type=ActionType.SCALE_UP,
                target=ActionTarget(
                    resource_id="vm-web-demo-01",
                    resource_type="Microsoft.Compute/virtualMachines",
                    current_sku="Standard_B2ms",
                    proposed_sku="Standard_B4ms",
                ),
                reason=(
                    "[DEMO] CPU sustained at 87% avg for 25 minutes on vm-web-demo-01. "
                    "Peak CPU 100%. Scale-up to Standard_B4ms recommended to restore headroom."
                ),
                urgency=Urgency.HIGH,
            ),
        ]

    # Deterministic rule-based scan (fallback / mock mode)
    # ------------------------------------------------------------------

    def _scan_rules(self) -> list[ProposedAction]:
        """Run all three detection rules and aggregate results."""
        proposals: list[ProposedAction] = []
        proposals.extend(self._detect_untagged_critical_resources())
        proposals.extend(self._detect_circular_dependencies())
        proposals.extend(self._detect_high_cost_spofs())
        logger.info(
            "MonitoringAgent: scan complete — %d anomalies detected", len(proposals)
        )
        return proposals

    # ------------------------------------------------------------------
    # Detection rules (rule-based path)
    # ------------------------------------------------------------------

    def _detect_untagged_critical_resources(self) -> list[ProposedAction]:
        proposals: list[ProposedAction] = []
        for resource in self._resources.values():
            tags = resource.get("tags") or {}
            if tags.get("criticality") != "critical":
                continue
            if "owner" in tags:
                continue
            reason = (
                f"Resource '{resource['name']}' is tagged criticality=critical "
                "but has no 'owner' tag. Unowned critical resources create "
                "accountability gaps during incidents — no team is automatically "
                "responsible when the resource fails. Add an owner tag."
            )
            proposals.append(
                ProposedAction(
                    agent_id=_AGENT_ID,
                    action_type=ActionType.UPDATE_CONFIG,
                    target=ActionTarget(
                        resource_id=resource["id"],
                        resource_type=resource["type"],
                        resource_group=resource.get("resource_group"),
                        current_monthly_cost=resource.get("monthly_cost"),
                    ),
                    reason=reason,
                    urgency=Urgency.MEDIUM,
                )
            )
            logger.info("MonitoringAgent: unowned critical resource — '%s'", resource["name"])
        return proposals

    def _detect_circular_dependencies(self) -> list[ProposedAction]:
        edge_set: set[tuple[str, str]] = {(e["from"], e["to"]) for e in self._edges}
        seen_pairs: set[frozenset[str]] = set()
        proposals: list[ProposedAction] = []
        for edge in self._edges:
            a, b = edge["from"], edge["to"]
            pair = frozenset({a, b})
            if pair in seen_pairs:
                continue
            if (b, a) in edge_set:
                seen_pairs.add(pair)
                reason = (
                    f"Circular dependency detected between '{a}' and '{b}'. "
                    "Bidirectional dependencies create split-brain risk: if "
                    f"either service restarts, '{b}' may fail to reconnect to "
                    f"'{a}', causing a cascading outage. Recommend restarting "
                    f"'{b}' first (with health checks) before restarting '{a}'."
                )
                proposals.append(
                    ProposedAction(
                        agent_id=_AGENT_ID,
                        action_type=ActionType.RESTART_SERVICE,
                        target=ActionTarget(
                            resource_id=b,
                            resource_type="unknown",
                        ),
                        reason=reason,
                        urgency=Urgency.HIGH,
                    )
                )
                logger.info("MonitoringAgent: circular dependency — '%s' ↔ '%s'", a, b)
        return proposals

    def _detect_high_cost_spofs(self) -> list[ProposedAction]:
        proposals: list[ProposedAction] = []
        for resource in self._resources.values():
            tags = resource.get("tags") or {}
            if tags.get("criticality") != "critical":
                continue
            monthly_cost = resource.get("monthly_cost")
            if monthly_cost is None or monthly_cost < _CRITICAL_COST_THRESHOLD:
                continue
            dependents = resource.get("dependents", []) + resource.get("services_hosted", [])
            if not dependents:
                continue
            preview = ", ".join(dependents[:3])
            ellipsis = "..." if len(dependents) > 3 else ""
            reason = (
                f"Critical resource '{resource['name']}' costs ${monthly_cost:.0f}/month "
                f"and has {len(dependents)} dependent(s): {preview}{ellipsis}. "
                "No redundancy configuration detected. A failure here would cause "
                "a wide blast-radius outage. Recommend scaling up to add a standby "
                "replica or additional node pool."
            )
            proposals.append(
                ProposedAction(
                    agent_id=_AGENT_ID,
                    action_type=ActionType.SCALE_UP,
                    target=ActionTarget(
                        resource_id=resource["id"],
                        resource_type=resource["type"],
                        resource_group=resource.get("resource_group"),
                        current_sku=resource.get("sku"),
                        current_monthly_cost=monthly_cost,
                    ),
                    reason=reason,
                    urgency=Urgency.HIGH,
                )
            )
            logger.info(
                "MonitoringAgent: high-cost SPOF — '%s' $%.0f/month %d dependents",
                resource["name"], monthly_cost, len(dependents),
            )
        return proposals
