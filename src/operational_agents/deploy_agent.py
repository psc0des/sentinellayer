"""Infrastructure Deploy Agent — proposes deployment and configuration actions.

This is an operational agent (the governed subject). It proposes
infrastructure changes — NSG rule updates, new resource deployments, and
configuration updates — that SentinelLayer evaluates before execution.

Microsoft Agent Framework integration (Phase 8)
------------------------------------------------
In live mode (USE_LOCAL_MOCKS=false), this agent is driven by a
Microsoft Agent Framework ``Agent`` backed by Azure OpenAI GPT-4.1.

The LLM agent calls our deterministic ``scan_deploy_opportunities`` tool,
which analyses the resource topology for deployment needs.  The LLM then
synthesises an infrastructure change narrative explaining the proposals.

In mock mode the framework is skipped — only deterministic scanning runs.

Detection rules
---------------
1. **NSG with no inbound deny-all rule** — Network Security Groups that allow
   all inbound traffic are a security risk.  Proposes MODIFY_NSG to add an
   explicit deny-all rule at the lowest priority.
2. **Resource with no backup or DR tag** — Production resources without
   ``backup`` or ``disaster-recovery`` tags may be unprotected.  Proposes
   UPDATE_CONFIG to add lifecycle-management metadata.
3. **No CREATE_RESOURCE if topology is sparse** — If the topology has fewer
   than 3 resources, proposes creating a monitoring resource to improve
   observability.

All proposals go through the SentinelLayer governance pipeline before
any deployment action is taken.
"""

import asyncio
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

# Minimum number of resources before we suggest adding observability tooling.
_SPARSE_TOPOLOGY_THRESHOLD: int = 3

# NSG resource type identifier.
_NSG_RESOURCE_TYPE = "Microsoft.Network/networkSecurityGroups"

# Tags that indicate lifecycle management is already in place.
_LIFECYCLE_TAGS: set[str] = {"backup", "disaster-recovery", "purpose"}

# System instructions for the framework agent (live mode only).
_AGENT_INSTRUCTIONS = """\
You are SentinelLayer's Infrastructure Deploy Agent — a specialist in
cloud infrastructure change management, NSG security, and deployment planning.

Your job:
1. Call the `scan_deploy_opportunities` tool to analyse the resource topology.
2. Receive the list of proposed infrastructure actions from the deterministic scan.
3. Write a concise 2-3 sentence summary explaining what deployment or configuration
   changes are recommended and why they improve security or operational maturity.
   Do NOT list individual resource IDs; describe the category of improvement.

Always call the tool first before providing any commentary.
"""


# ---------------------------------------------------------------------------
# Agent
# ---------------------------------------------------------------------------


class DeployAgent:
    """Proposes infrastructure deployment and configuration actions.

    Analyses a resource topology for security gaps, missing lifecycle metadata,
    and observability deficits, then generates :class:`~src.core.models.ProposedAction`
    objects representing recommended infrastructure changes.

    Each proposal is passed through SentinelLayer's governance pipeline for
    SRI scoring and approval before execution.

    In live mode the Microsoft Agent Framework drives GPT-4.1 to call the
    deterministic tool and synthesise a deployment narrative.

    Usage::

        agent = DeployAgent()
        proposals: list[ProposedAction] = agent.scan()
        for p in proposals:
            print(p.action_type.value, p.target.resource_id, p.reason)
    """

    def __init__(
        self,
        resources_path: str | Path | None = None,
        cfg=None,
    ) -> None:
        path = Path(resources_path) if resources_path else _DEFAULT_RESOURCES_PATH
        with open(path, encoding="utf-8") as fh:
            data: dict = json.load(fh)

        # Fast lookup: resource name → resource dict
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

    def scan(self) -> list[ProposedAction]:
        """Scan the topology and return infrastructure deployment proposals.

        Routes to the Microsoft Agent Framework agent in live mode, or to the
        deterministic rule-based scanner in mock mode.

        Returns:
            A list of :class:`~src.core.models.ProposedAction` objects
            representing recommended deployment or configuration changes.
        """
        if not self._use_framework:
            return self._scan_rules()

        try:
            return asyncio.run(self._scan_with_framework())
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "DeployAgent: framework call failed (%s) — falling back to rules.", exc
            )
            return self._scan_rules()

    # ------------------------------------------------------------------
    # Microsoft Agent Framework path (live mode)
    # ------------------------------------------------------------------

    async def _scan_with_framework(self) -> list[ProposedAction]:
        """Run the framework agent with GPT-4.1 driving the tool call."""
        from openai import AsyncAzureOpenAI
        from azure.identity import AzureCliCredential, get_bearer_token_provider
        import agent_framework as af
        from agent_framework.openai import OpenAIResponsesClient

        credential = AzureCliCredential()
        token_provider = get_bearer_token_provider(
            credential, "https://cognitiveservices.azure.com/.default"
        )
        azure_openai = AsyncAzureOpenAI(
            azure_endpoint=self._cfg.azure_openai_endpoint,
            azure_ad_token_provider=token_provider,
            api_version="2025-03-01-preview",  # Responses API requires >=2025-03-01-preview
        )
        client = OpenAIResponsesClient(
            async_client=azure_openai,
            model_id=self._cfg.azure_openai_deployment,
        )

        proposals_holder: list[list[ProposedAction]] = []

        @af.tool(
            name="scan_deploy_opportunities",
            description=(
                "Scan the infrastructure resource topology for deployment and "
                "configuration improvement opportunities. Detects NSGs without "
                "deny-all rules, resources missing lifecycle tags, and sparse "
                "topologies lacking observability. Returns a JSON array of "
                "ProposedAction objects representing recommended changes."
            ),
        )
        def scan_deploy_opportunities() -> str:
            """Identify infrastructure deployment and configuration improvement proposals."""
            proposals = self._scan_rules()
            proposals_holder.append(proposals)
            return json.dumps([p.model_dump() for p in proposals], default=str)

        agent = client.as_agent(
            name="deploy-agent",
            instructions=_AGENT_INSTRUCTIONS,
            tools=[scan_deploy_opportunities],
        )

        await agent.run(
            "Scan the infrastructure for deployment and configuration improvement "
            "opportunities and provide a change management summary."
        )

        return proposals_holder[-1] if proposals_holder else self._scan_rules()

    # ------------------------------------------------------------------
    # Deterministic rule-based scan
    # ------------------------------------------------------------------

    def _scan_rules(self) -> list[ProposedAction]:
        """Run all detection rules and aggregate deployment proposals."""
        proposals: list[ProposedAction] = []
        proposals.extend(self._detect_nsg_without_deny_all())
        proposals.extend(self._detect_missing_lifecycle_tags())
        proposals.extend(self._detect_sparse_topology())

        logger.info(
            "DeployAgent: scan complete — %d deployment proposals generated",
            len(proposals),
        )
        return proposals

    # ------------------------------------------------------------------
    # Detection rules
    # ------------------------------------------------------------------

    def _detect_nsg_without_deny_all(self) -> list[ProposedAction]:
        """Rule 1 — NSGs that lack an explicit deny-all inbound rule.

        An NSG without a deny-all fallback rule implicitly allows traffic
        that does not match any explicit rule, depending on default Azure
        behaviour.  Best practice is to add an explicit priority-4096
        deny-all rule so the security posture is documented and intentional.
        """
        proposals: list[ProposedAction] = []

        for resource in self._resources.values():
            if resource.get("type") != _NSG_RESOURCE_TYPE:
                continue

            # Check for a deny-all marker in tags or properties
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
            logger.info(
                "DeployAgent: NSG without deny-all — '%s'", resource["name"]
            )

        return proposals

    def _detect_missing_lifecycle_tags(self) -> list[ProposedAction]:
        """Rule 2 — Resources without lifecycle management tags.

        Production resources without any of the lifecycle-management tag keys
        (``backup``, ``disaster-recovery``, ``purpose``) may have no
        automated backup or DR policy.  We propose UPDATE_CONFIG to add
        the missing lifecycle metadata so the resource is governed correctly.
        """
        proposals: list[ProposedAction] = []

        for resource in self._resources.values():
            tags = resource.get("tags", {})
            environment = tags.get("environment", "")

            # Only flag production resources
            if environment != "production":
                continue

            # Skip if any lifecycle tag is present
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
            logger.info(
                "DeployAgent: missing lifecycle tags — '%s'", resource["name"]
            )

        return proposals

    def _detect_sparse_topology(self) -> list[ProposedAction]:
        """Rule 3 — Topology with fewer resources than the observability threshold.

        If the resource topology has fewer than ``_SPARSE_TOPOLOGY_THRESHOLD``
        resources, there may be no dedicated monitoring resource (Log Analytics
        workspace, Application Insights, etc.).  We propose CREATE_RESOURCE
        to add an observability tier.
        """
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
        logger.info(
            "DeployAgent: sparse topology — proposing observability resource"
        )

        return proposals
