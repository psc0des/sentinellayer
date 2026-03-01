"""Cost Optimization Agent — identifies wasteful resources and proposes savings.

This is an operational agent (the governed subject). It proposes
infrastructure actions that SentinelLayer evaluates before execution.

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
- ``query_resource_graph(kusto_query)`` — discover VMs and clusters
- ``query_metrics(resource_id, metric_names, timespan)`` — actual CPU data
- ``get_resource_details(resource_id)`` — full resource information
- ``propose_action(...)`` — submit a validated ProposedAction
"""

import json
import logging
from pathlib import Path

from src.config import settings as _default_settings
from src.core.models import ActionTarget, ActionType, ProposedAction, Urgency

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

# System instructions for the framework agent — drives two-layer intelligence.
_AGENT_INSTRUCTIONS = """\
You are a Senior FinOps Engineer at a cloud company with expertise in Azure cost
optimisation. Your mission: identify WASTED cloud spend by investigating ACTUAL
resource utilisation — not just resource size or SKU name.

Investigation workflow
1. Call query_resource_graph to discover ALL cost-significant resources in the
   environment.  Do NOT limit to VMs only.  Adapt your KQL to the environment:
   at minimum include VMs, AKS clusters, App Service plans, SQL databases,
   Cosmos DB accounts, and any other resource types that incur meaningful cost.
   Example starting query (expand as needed):
   "Resources | where type in (
     'microsoft.compute/virtualmachines',
     'microsoft.containerservice/managedclusters',
     'microsoft.web/serverfarms',
     'microsoft.sql/servers/databases',
     'microsoft.documentdb/databaseaccounts'
   ) | project id, name, type, location, resourceGroup, tags, sku, properties"
2. For each discovered resource, call query_metrics with the metrics appropriate
   for its type (P7D timespan for a 7-day baseline):
   - VMs / AKS nodes: "Percentage CPU", optionally "Available Memory Bytes"
   - App Service plans: "CpuPercentage", "MemoryPercentage"
   - SQL databases: "dtu_consumption_percent", "connection_successful"
   - Use your judgement for other resource types.
3. Evaluate utilisation against the resource's capacity:
   - Avg utilisation < 20% of capacity → right-sizing candidate
   - Avg utilisation < 5% → strong deletion or deep-downsize candidate
4. Call get_resource_details for each candidate to confirm SKU and cost context.
5. For each wasteful resource, call propose_action with an evidence-backed reason.

Proposal rules
- Reason MUST include actual metric values (e.g. "7-day avg CPU: 3.2%, peak 14.8%").
- Do NOT propose deleting resources that appear to serve disaster-recovery or
  backup purposes (check tags and name) unless utilisation is overwhelmingly
  low (< 2% avg) — their idleness may be intentional.
- For VMs: prefer scale_down (right-size SKU) before delete_resource.
- For AKS: propose scale_down (reduce node count) if cluster avg CPU < 40%.
- projected_savings_monthly: estimate 45% savings for one VM SKU tier reduction.
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

        self._use_framework: bool = (
            not self._cfg.use_local_mocks
            and bool(self._cfg.azure_openai_endpoint)
        )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def scan(
        self,
        target_resource_group: str | None = None,
    ) -> list[ProposedAction]:
        """Investigate the Azure environment and return cost-saving proposals.

        Args:
            target_resource_group: Optional resource group name to scope the
                investigation.  When ``None`` the agent scans the entire
                subscription visible to its credentials.

        Returns:
            List of :class:`~src.core.models.ProposedAction` objects.
        """
        if not self._use_framework:
            return self._scan_rules()

        try:
            return await self._scan_with_framework(target_resource_group)
        except Exception as exc:  # noqa: BLE001
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
        self, target_resource_group: str | None
    ) -> list[ProposedAction]:
        """Run GPT-4.1 with investigation tools to produce evidence-backed proposals."""
        from openai import AsyncAzureOpenAI
        from azure.identity import DefaultAzureCredential, get_bearer_token_provider
        import agent_framework as af
        from agent_framework.openai import OpenAIResponsesClient
        from src.infrastructure.azure_tools import (
            query_resource_graph,
            query_metrics,
            get_resource_details,
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
            name="query_resource_graph",
            description=(
                "Query Azure Resource Graph with a Kusto (KQL) query to discover "
                "resources in the Azure environment. Returns a JSON array of resource "
                "objects with id, name, type, location, resourceGroup, tags, sku."
            ),
        )
        def tool_query_resource_graph(kusto_query: str) -> str:
            """Discover Azure resources via Resource Graph KQL query."""
            results = query_resource_graph(kusto_query)
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
        def tool_query_metrics(
            resource_id: str,
            metric_names: str,
            timespan: str = "P7D",
        ) -> str:
            """Get actual utilisation metrics for a resource."""
            names = [m.strip() for m in metric_names.split(",")]
            results = query_metrics(resource_id, names, timespan)
            return json.dumps(results, default=str)

        @af.tool(
            name="get_resource_details",
            description=(
                "Get full details for a specific Azure resource by its ARM resource ID "
                "or short name. Returns SKU, tags, cost, location, and other properties."
            ),
        )
        def tool_get_resource_details(resource_id: str) -> str:
            """Retrieve full resource details including SKU and tags."""
            details = get_resource_details(resource_id)
            return json.dumps(details, default=str)

        @af.tool(
            name="propose_action",
            description=(
                "Submit a governance proposal for a resource. Call this when you have "
                "metric evidence that a resource is wasted or over-provisioned. "
                "action_type must be one of: scale_down, delete_resource, scale_up, "
                "update_config, modify_nsg, create_resource, restart_service. "
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
            projected_savings_monthly: float = 0.0,
            resource_type: str = "",
            resource_group: str = "",
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
                tool_propose_action,
            ],
        )

        rg_scope = (
            f"in resource group '{target_resource_group}'"
            if target_resource_group
            else "across the Azure environment"
        )
        await agent.run(
            f"Investigate and identify cost optimisation opportunities {rg_scope}. "
            "Use query_resource_graph to discover VMs and clusters, then check actual "
            "CPU utilisation with query_metrics before proposing any action."
        )

        # Empty proposals means GPT found no waste — that is a valid outcome.
        # Falling back to seed-data rules would produce false positives in any
        # real environment that does not match the demo seed_resources.json.
        return proposals_holder

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
        tags = resource.get("tags", {})
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
