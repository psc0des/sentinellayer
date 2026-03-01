"""Infrastructure Deploy Agent — proposes deployment and configuration actions.

This is an operational agent (the governed subject). It proposes
infrastructure changes that SentinelLayer evaluates before execution.

Phase 12 — Intelligent, evidence-driven agent
----------------------------------------------
The agent now investigates the real Azure environment before proposing changes:

1. Discovers NSGs with ``query_resource_graph``.
2. Inspects actual security rules with ``list_nsg_rules`` — checks for
   missing deny-all inbound rules using real NSG rule data.
3. Reviews recent change activity with ``query_activity_log`` — detects
   failed writes or suspicious recent modifications.
4. Checks resource tags and configuration with ``get_resource_details``.
5. Uses GPT-4.1 to reason about security posture before proposing actions.

The agent is environment-agnostic: accepts an optional
``target_resource_group`` parameter and can scan any Azure environment.

Microsoft Agent Framework tools (live mode)
--------------------------------------------
- ``query_resource_graph(kusto_query)`` — discover NSGs and VMs
- ``list_nsg_rules(nsg_resource_id)`` — inspect actual security rules
- ``get_resource_details(resource_id)`` — check tags and configuration
- ``query_activity_log(resource_group, timespan)`` — review recent changes
- ``propose_action(...)`` — submit a validated ProposedAction

In mock mode (USE_LOCAL_MOCKS=true) the deterministic ``_scan_rules()``
fallback runs — reads ``data/seed_resources.json`` and applies heuristics.
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

_AGENT_ID = "deploy-agent"

_DEFAULT_RESOURCES_PATH = (
    Path(__file__).parent.parent.parent / "data" / "seed_resources.json"
)

# Rule-based fallback thresholds (unchanged from Phase 8)
_SPARSE_TOPOLOGY_THRESHOLD: int = 3
_NSG_RESOURCE_TYPE = "Microsoft.Network/networkSecurityGroups"
# Tags used by the rule-based (mock/CI) path to detect lifecycle metadata.
# These are organisation-specific examples from the SentinelLayer demo environment.
# In production, replace with your org's actual lifecycle tag key names.
_LIFECYCLE_TAGS: set[str] = {"backup", "disaster-recovery", "purpose"}

# System instructions for the framework agent.
_AGENT_INSTRUCTIONS = """\
You are a Senior Platform/Security Engineer conducting an infrastructure
security and configuration review.

Investigation workflow
1. Use query_resource_graph to discover NSGs and VMs.
   NSG query: "Resources | where type == 'microsoft.network/networksecuritygroups' \
| project id, name, resourceGroup, tags, properties"
   VM query: "Resources | where type == 'microsoft.compute/virtualmachines' \
| project id, name, resourceGroup, tags, sku"
2. For each NSG, call list_nsg_rules to inspect the actual security rules.
   Check: is there a deny-all inbound rule (access=Deny, port=*)? Priority 4096
   is Azure's lowest priority — typically used for deny-all.
3. Call query_activity_log for the resource group to see recent changes.
   Look for: failed write operations on NSGs, recent security rule modifications.
4. Use get_resource_details to check resource tags for lifecycle management
   indicators. Do NOT check for specific tag key names — tag schemas vary by
   organisation. Instead, flag resources that have NO lifecycle or ownership
   tags of any kind (e.g. no backup policy, no DR designation, no owner, no
   cost centre, no environment label). A resource with ANY of these categories
   populated — regardless of the exact key name — is adequately tagged.
5. For each security gap or configuration issue found, call propose_action.

Focus areas
- NSGs missing an explicit deny-all inbound rule → propose modify_nsg (MEDIUM urgency)
- Recent FAILED writes to NSGs → investigate and propose remediation (HIGH urgency)
- Production resources with zero lifecycle or ownership tags → propose update_config (LOW urgency)

Do not propose changes that are already correctly configured. Only flag genuine gaps.
"""


# ---------------------------------------------------------------------------
# Agent
# ---------------------------------------------------------------------------


class DeployAgent:
    """Proposes infrastructure deployment and configuration actions.

    In live mode (USE_LOCAL_MOCKS=false) GPT-4.1 inspects real NSG rules,
    activity logs, and resource configuration before proposing changes.

    In mock mode only the deterministic ``_scan_rules()`` runs.

    Usage::

        agent = DeployAgent()
        proposals: list[ProposedAction] = await agent.scan()

        # Scope to a specific resource group:
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

        self._resources: dict[str, dict] = {
            r["name"]: r for r in data.get("resources", [])
        }
        self._all_resources: list[dict] = data.get("resources", [])

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
        """Investigate the Azure environment and return configuration proposals.

        Args:
            target_resource_group: Optional resource group to scope the scan.
                When ``None`` the agent scans across the subscription.

        Returns:
            List of :class:`~src.core.models.ProposedAction` objects.
        """
        if not self._use_framework:
            return self._scan_rules()

        try:
            return await self._scan_with_framework(target_resource_group)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "DeployAgent: framework call failed (%s) — returning no proposals "
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
        """Run GPT-4.1 with security investigation tools."""
        from openai import AsyncAzureOpenAI
        from azure.identity import DefaultAzureCredential, get_bearer_token_provider
        import agent_framework as af
        from agent_framework.openai import OpenAIResponsesClient
        from src.infrastructure.azure_tools import (
            query_resource_graph,
            list_nsg_rules,
            get_resource_details,
            query_activity_log,
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
                "resources. Returns JSON array with id, name, type, resourceGroup, tags."
            ),
        )
        def tool_query_resource_graph(kusto_query: str) -> str:
            """Discover NSGs, VMs, and other resources."""
            results = query_resource_graph(kusto_query)
            return json.dumps(results, default=str)

        @af.tool(
            name="list_nsg_rules",
            description=(
                "List the security rules for an Azure Network Security Group. "
                "Returns JSON array of rules with name, port, access (Allow/Deny), "
                "priority, and direction fields."
            ),
        )
        def tool_list_nsg_rules(nsg_resource_id: str) -> str:
            """Inspect actual NSG security rules to check for deny-all rules."""
            rules = list_nsg_rules(nsg_resource_id)
            return json.dumps(rules, default=str)

        @af.tool(
            name="get_resource_details",
            description=(
                "Get full details for an Azure resource by its ARM resource ID or "
                "short name. Returns SKU, tags, location, and other properties."
            ),
        )
        def tool_get_resource_details(resource_id: str) -> str:
            """Check resource tags and configuration."""
            details = get_resource_details(resource_id)
            return json.dumps(details, default=str)

        @af.tool(
            name="query_activity_log",
            description=(
                "Query Azure Monitor activity logs for a resource group. "
                "Returns recent operations with timestamp, operation name, status "
                "(Succeeded/Failed), caller, and resource. "
                "timespan uses ISO 8601 format (e.g. 'P7D' for last 7 days)."
            ),
        )
        def tool_query_activity_log(
            resource_group: str,
            timespan: str = "P7D",
        ) -> str:
            """Review recent changes and look for failed operations."""
            entries = query_activity_log(resource_group, timespan)
            return json.dumps(entries, default=str)

        @af.tool(
            name="propose_action",
            description=(
                "Submit a security or configuration governance proposal. "
                "action_type must be one of: modify_nsg, update_config, create_resource, "
                "scale_up, scale_down, delete_resource, restart_service. "
                "urgency must be one of: low, medium, high."
            ),
        )
        def tool_propose_action(
            resource_id: str,
            action_type: str,
            reason: str,
            urgency: str = "medium",
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
                    resource_type=resource_type or _NSG_RESOURCE_TYPE,
                    resource_group=resource_group or None,
                ),
                reason=reason,
                urgency=urgency_enum,
            )
            proposals_holder.append(proposal)
            name = resource_id.split("/")[-1]
            logger.info("DeployAgent: proposal submitted — %s on %s", action_type, name)
            return f"Proposal submitted: {action_type} on {name}"

        agent = client.as_agent(
            name="deploy-agent",
            instructions=_AGENT_INSTRUCTIONS,
            tools=[
                tool_query_resource_graph,
                tool_list_nsg_rules,
                tool_get_resource_details,
                tool_query_activity_log,
                tool_propose_action,
            ],
        )

        rg_scope = (
            f"in resource group '{target_resource_group}'"
            if target_resource_group
            else "across the Azure environment"
        )
        await agent.run(
            f"Conduct a security and configuration review {rg_scope}. "
            "Discover NSGs and check their security rules, review recent activity "
            "logs for failed operations, and identify any configuration gaps."
        )

        # Empty proposals means GPT found no security gaps — a valid outcome.
        # Falling back to seed-data rules would produce false positives in any
        # real environment that does not match the demo seed_resources.json.
        return proposals_holder

    # ------------------------------------------------------------------
    # Deterministic rule-based scan (fallback / mock mode)
    # ------------------------------------------------------------------

    def _scan_rules(self) -> list[ProposedAction]:
        """Run all detection rules and aggregate deployment proposals."""
        proposals: list[ProposedAction] = []
        proposals.extend(self._detect_nsg_without_deny_all())
        proposals.extend(self._detect_missing_lifecycle_tags())
        proposals.extend(self._detect_sparse_topology())
        logger.info(
            "DeployAgent: scan complete — %d deployment proposals generated", len(proposals)
        )
        return proposals

    # ------------------------------------------------------------------
    # Detection rules (rule-based path)
    # ------------------------------------------------------------------

    def _detect_nsg_without_deny_all(self) -> list[ProposedAction]:
        proposals: list[ProposedAction] = []
        for resource in self._resources.values():
            if resource.get("type") != _NSG_RESOURCE_TYPE:
                continue
            tags = resource.get("tags", {})
            has_deny_all = tags.get("deny_all_inbound") == "true"
            if has_deny_all:
                continue
            reason = (
                f"NSG '{resource['name']}' does not have an explicit deny-all "
                "inbound rule documented in its tags. "
                "Best practice requires a priority-4096 deny-all rule to ensure "
                "the security posture is intentional and auditable. "
                "Propose adding the deny-all rule via NSG modification."
            )
            proposals.append(
                ProposedAction(
                    agent_id=_AGENT_ID,
                    action_type=ActionType.MODIFY_NSG,
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
            logger.info("DeployAgent: NSG without deny-all — '%s'", resource["name"])
        return proposals

    def _detect_missing_lifecycle_tags(self) -> list[ProposedAction]:
        proposals: list[ProposedAction] = []
        for resource in self._resources.values():
            tags = resource.get("tags", {})
            if tags.get("environment") != "production":
                continue
            if any(k in tags for k in _LIFECYCLE_TAGS):
                continue
            reason = (
                f"Production resource '{resource['name']}' has no lifecycle "
                "management tags (backup, disaster-recovery, or purpose). "
                "Without these tags, automated backup policies and DR plans "
                "cannot be applied by the governance engine. "
                "Propose adding lifecycle metadata via config update."
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
                    urgency=Urgency.LOW,
                )
            )
            logger.info("DeployAgent: missing lifecycle tags — '%s'", resource["name"])
        return proposals

    def _detect_sparse_topology(self) -> list[ProposedAction]:
        proposals: list[ProposedAction] = []
        if len(self._all_resources) >= _SPARSE_TOPOLOGY_THRESHOLD:
            return proposals
        reason = (
            f"Resource topology contains only {len(self._all_resources)} resource(s), "
            f"below the observability threshold ({_SPARSE_TOPOLOGY_THRESHOLD}). "
            "No monitoring or observability resource is likely deployed. "
            "Propose creating a Log Analytics workspace to provide infrastructure "
            "visibility and enable alert-driven governance."
        )
        proposals.append(
            ProposedAction(
                agent_id=_AGENT_ID,
                action_type=ActionType.CREATE_RESOURCE,
                target=ActionTarget(
                    resource_id="new-log-analytics-workspace",
                    resource_type="Microsoft.OperationalInsights/workspaces",
                ),
                reason=reason,
                urgency=Urgency.LOW,
            )
        )
        logger.info("DeployAgent: sparse topology — proposing observability resource")
        return proposals
