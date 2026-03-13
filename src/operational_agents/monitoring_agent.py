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
from src.core.models import ActionTarget, ActionType, ProposedAction, Urgency

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
   - Call get_resource_details on the resource. Read power state.
   - A deallocated/stopped VM WILL have no metrics — this IS confirmation,
     not ambiguity. Do NOT skip action because metrics are empty.
   - Call query_activity_log for the resource group (last 24h) to determine:
     * Was it stopped manually? By auto-shutdown policy? By an Azure incident?
     * Any failed operations before the stop?
   - Propose restart_service (HIGH urgency). Reason must include:
     confirmed power state, activity log findings, downstream dependencies.

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
reliability review. Your findings drive automated governance — every proposal
you submit is reviewed before any change is executed.

━━━ STEP 1: RESOURCE DISCOVERY ━━━
Call query_resource_graph to enumerate all resources:
  Resources | project id, name, type, location, resourceGroup, tags, properties, sku
Include: virtualMachines, databaseAccounts, containerApps, storageAccounts,
         appServices, disks, publicIPAddresses, networkInterfaces.

━━━ STEP 2: VM AVAILABILITY — CHECK EVERY VM, NO EXCEPTIONS ━━━
For each VM, call get_resource_details to read power state.

STOPPED / DEALLOCATED VM (powerState != "running"):
  - HIGH urgency — a stopped VM is an availability incident.
  - A stopped VM returns NO metrics. No metrics = confirmation it is down.
  - Call query_activity_log to determine: stopped manually, by automation,
    or by an unexpected event. Include findings in the reason.
  - Propose restart_service. Exception: VMs named or tagged as DR/standby
    (name contains 'dr', 'standby', 'backup', or tag role=dr) → flag MEDIUM.

RUNNING VM — check performance:
  Call query_metrics: ["Percentage CPU", "Available Memory Bytes",
  "Disk Read Bytes", "Disk Write Bytes"] over P7D.
  - CPU avg > 70% → propose scale_up (HIGH urgency).
  - CPU avg 50–70% with memory > 75% → propose scale_up (MEDIUM urgency).
  - No metrics from a supposedly running VM → check query_activity_log
    for recent stop/start events; the VM may have AMA misconfigured.

━━━ STEP 3: DATABASE & DATA SERVICE HEALTH ━━━
For each Cosmos DB account or SQL database, call get_resource_details:
  - No failover policy / single-region only → propose update_config (MEDIUM).
    Reason: "Single-region deployment — risk of full data unavailability on
    regional outage. Enable multi-region geo-redundancy and automatic failover."
  - publicNetworkAccess = Enabled → flag (MEDIUM urgency).
  - No backup policy configured → flag (MEDIUM urgency).
  Call query_metrics for "TotalRequests", "ServerSideLatencyP99" if available.
  - P99 latency > 500ms sustained → propose update_config (MEDIUM urgency).

━━━ STEP 4: CONTAINER APPS & APP SERVICES ━━━
Call get_resource_details for each Container App and App Service:
  - Replica count < 2 in a non-dev environment → propose update_config (MEDIUM).
  - No custom domain / HTTPS not enforced on public-facing app → flag (LOW).
  Call query_metrics for "Requests", "Http5xx" where available.
  - Http5xx error rate > 1% → propose update_config (MEDIUM urgency).

━━━ STEP 5: MONITORING & OBSERVABILITY GAPS ━━━
For each VM, check get_resource_details for AMA extension presence:
  (look for AzureMonitorLinuxAgent or AzureMonitorWindowsAgent in extensions)
  - VM without AMA installed → propose update_config (MEDIUM urgency).
    Reason: "VM has no monitoring agent — metrics and logs not being collected."
Resources with no owner, environment, or criticality tags → propose
  update_config (LOW urgency) for tag hygiene.

━━━ STEP 6: ORPHANED & WASTEFUL RESOURCES ━━━
  - Disks with diskState = Unattached → propose delete_resource (LOW/MEDIUM).
  - Public IPs with no associated NIC/LB/Gateway → propose delete_resource (LOW).

━━━ URGENCY SCALE ━━━
  HIGH:   VM not running, CPU > 70%, service completely unavailable.
  MEDIUM: Single-region DB, no monitoring agent, Container App with 1 replica,
          public network access on data services.
  LOW:    Missing tags, orphaned resources, configuration hygiene gaps.

CRITICAL: Never assume a resource is healthy because metrics are absent.
Always confirm VM health via get_resource_details power state.
If uncertain about a finding, include it with your reasoning.
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
            return await self._scan_with_framework(alert_payload, target_resource_group)
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
                "urgency must be one of: low, medium, high."
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
        else:
            instructions = _SCAN_INSTRUCTIONS
            rg_scope = (
                f"in resource group '{target_resource_group}'"
                if target_resource_group
                else "across the Azure environment"
            )
            prompt = (
                f"Conduct a full 6-domain proactive reliability scan {rg_scope}. "
                "Follow ALL steps in your instructions: "
                "(1) Discover ALL resource types — VMs, databases, Container Apps, "
                "App Services, storage accounts, disks, public IPs, network interfaces. "
                "(2) For EVERY VM: call get_resource_details to check powerState FIRST — "
                "stopped/deallocated VMs are DOWN (HIGH urgency restart_service). "
                "For running VMs, query_metrics for CPU/memory stress over P7D. "
                "A VM with no metrics is DOWN, not clean. "
                "(3) For each database (Cosmos DB, SQL): check failover policy, "
                "publicNetworkAccess, backup config. Single-region = MEDIUM risk. "
                "(4) For each Container App and App Service: check replica count "
                "(< 2 in non-dev = MEDIUM), HTTPS enforcement, Http5xx error rate. "
                "(5) Check VMs for AMA extension (AzureMonitorLinuxAgent/WindowsAgent) — "
                "missing agent means no metrics or logs collected (MEDIUM). "
                "Flag resources with zero tags (LOW). "
                "(6) Flag orphaned resources: unattached disks, public IPs with no NIC/LB. "
                "Call get_resource_health and list_advisor_recommendations(category=HighAvailability) "
                "for platform-level signals. "
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

        # Empty proposals means GPT found no reliability risks — a valid outcome.
        # Falling back to seed-data rules would produce false positives in any
        # real environment that does not match the demo seed_resources.json.
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
            tags = resource.get("tags", {})
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
            tags = resource.get("tags", {})
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
