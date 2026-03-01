"""SRE Monitoring & RCA Agent — detects anomalies and proposes remediation.

This is an operational agent (the governed subject). It proposes
infrastructure actions that SentinelLayer evaluates before execution.

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
- ``get_resource_details(resource_id)`` — check tags, dependencies, criticality
- ``query_resource_graph(kusto_query)`` — discover resources for scan mode
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
You are a Senior SRE (Site Reliability Engineer) investigating a triggered
Azure Monitor alert. You have been given the alert details below.

Your job:
1. Call query_metrics for the alerted resource to CONFIRM the alert with
   real metric data (do not trust the alert value alone — verify it).
2. Call get_resource_details to understand the resource: SKU, tags, what
   other services depend on it.
3. Based on the confirmed metrics, determine the right remediation:
   - Sustained CPU > 80% → propose scale_up to the next larger SKU
   - Memory exhaustion → propose scale_up
   - Service crash/unavailable → propose restart_service
   - Configuration drift → propose update_config
4. Call propose_action with your evidence-based recommendation.

CRITICAL: Your proposal reason MUST include the actual confirmed metric values
(e.g., "Confirmed 7-day avg CPU: 82.5%, peak 100% — sustained high load").
Do not propose action based solely on the alert — confirm with metrics first.
"""

# System instructions for proactive scanning.
_SCAN_INSTRUCTIONS = """\
You are a Senior SRE conducting a proactive infrastructure reliability review.

Your job:
1. Call query_resource_graph to discover all resources in the environment.
   Focus on: VMs, AKS clusters, databases, and critical services.
2. For each critical resource (tagged criticality=critical or cost > $500/month),
   call query_metrics to check if CPU or memory is near exhaustion.
3. Look for high-cost resources with no redundancy (single points of failure).
4. For each reliability risk found, call propose_action.

Proposal priorities:
- HIGH urgency: critical resources with metrics showing stress (CPU > 70%)
- MEDIUM urgency: unmonitored critical resources or missing owner tags
- LOW urgency: configuration gaps (missing tags, missing deny-all rules)
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
        proposals = await agent.scan(target_resource_group="sentinel-prod-rg")
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

        self._use_framework: bool = (
            not self._cfg.use_local_mocks
            and bool(self._cfg.azure_openai_endpoint)
        )

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
        if not self._use_framework:
            return self._scan_rules()

        try:
            return await self._scan_with_framework(alert_payload, target_resource_group)
        except Exception as exc:  # noqa: BLE001
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
            query_metrics,
            get_resource_details,
            query_resource_graph,
        )

        credential = DefaultAzureCredential()
        token_provider = get_bearer_token_provider(
            credential, "https://cognitiveservices.azure.com/.default"
        )
        azure_openai = AsyncAzureOpenAI(
            azure_endpoint=self._cfg.azure_openai_endpoint,
            azure_ad_token_provider=token_provider,
            api_version="2025-03-01-preview",
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
        def tool_query_metrics(
            resource_id: str,
            metric_names: str,
            timespan: str = "P7D",
        ) -> str:
            """Confirm alert metrics or check resource health."""
            names = [m.strip() for m in metric_names.split(",")]
            results = query_metrics(resource_id, names, timespan)
            return json.dumps(results, default=str)

        @af.tool(
            name="get_resource_details",
            description=(
                "Get full details for a specific Azure resource by its ARM resource ID "
                "or short name. Returns SKU, tags, dependents, cost, and other properties."
            ),
        )
        def tool_get_resource_details(resource_id: str) -> str:
            """Retrieve resource details to understand impact and dependencies."""
            details = get_resource_details(resource_id)
            return json.dumps(details, default=str)

        @af.tool(
            name="query_resource_graph",
            description=(
                "Query Azure Resource Graph with a Kusto (KQL) query to discover resources. "
                "Returns a JSON array with id, name, type, location, resourceGroup, tags."
            ),
        )
        def tool_query_resource_graph(kusto_query: str) -> str:
            """Discover resources for proactive scanning."""
            results = query_resource_graph(kusto_query)
            return json.dumps(results, default=str)

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
            prompt = (
                f"An Azure Monitor alert has fired:\n{alert_summary}\n\n"
                "Please investigate this alert: confirm the metrics, understand "
                "the resource and its dependents, then propose the appropriate remediation."
            )
        else:
            instructions = _SCAN_INSTRUCTIONS
            rg_scope = (
                f"in resource group '{target_resource_group}'"
                if target_resource_group
                else "across the Azure environment"
            )
            prompt = (
                f"Conduct a proactive reliability scan {rg_scope}. "
                "Discover resources, check metrics for stressed or critical resources, "
                "and propose remediation for any reliability risks found."
            )

        agent = client.as_agent(
            name="sre-monitoring-agent",
            instructions=instructions,
            tools=[
                tool_query_metrics,
                tool_get_resource_details,
                tool_query_resource_graph,
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
