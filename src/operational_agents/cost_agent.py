"""Cost Optimization Agent — identifies wasteful resources and proposes savings.

This is an operational agent (the governed subject). It proposes
infrastructure actions that SentinelLayer evaluates before execution.

Microsoft Agent Framework integration (Phase 8)
------------------------------------------------
In live mode (USE_LOCAL_MOCKS=false), this agent is driven by a
Microsoft Agent Framework ``Agent`` backed by Azure OpenAI GPT-4.1.

The LLM agent calls our deterministic ``scan_cost_opportunities`` tool,
which applies heuristic rules to the resource topology and returns
structured cost-saving proposals.  The LLM then synthesises a concise
FinOps recommendation narrative.

In mock mode the framework is skipped — only deterministic rule-based
scanning runs.  This preserves fully-offline CI/test behaviour.

The agent scans a resource topology (loaded from ``data/seed_resources.json``)
and applies heuristics to identify overprovisioned or idle resources:

Detection rules
---------------
1. **Oversized VM SKU** — VMs running on D8s_v3 or larger are candidates for
   downsizing to the next smaller SKU tier, saving ~45 % of monthly cost.
2. **Large AKS cluster** — Kubernetes clusters with ≥ 4 nodes are candidates
   for a node-count reduction, saving ~35 % of monthly cost.

Resources with monthly cost below ``_MIN_COST_THRESHOLD`` ($200) are skipped
because the operational overhead of changing them exceeds the benefit.

Resources above ``_HIGH_COST_THRESHOLD`` ($500) get MEDIUM urgency so they
rise to the top of the human review queue.
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

# Only worth analysing resources that cost more than this per month.
_MIN_COST_THRESHOLD: float = 200.0

# Resources costing more than this receive MEDIUM urgency.
_HIGH_COST_THRESHOLD: float = 500.0

# Estimated monthly savings when downsizing a VM (e.g. D8s → D4s).
# In practice the ratio is ~48 %; we use 45 % as a conservative estimate.
_VM_DOWNSIZE_SAVINGS_RATE: float = 0.45

# Propose AKS scale-down only when node count is at or above this value.
_AKS_SCALE_DOWN_NODE_THRESHOLD: int = 4

# Estimated monthly savings when reducing AKS node count by 2.
_AKS_SCALE_DOWN_SAVINGS_RATE: float = 0.35

# SKUs considered oversized for general-purpose workloads.
_OVERSIZED_SKUS: set[str] = {
    "Standard_D8s_v3",
    "Standard_D16s_v3",
    "Standard_D32s_v3",
}

# Suggested replacement SKU for each oversized tier.
_DOWNSIZE_MAP: dict[str, str] = {
    "Standard_D8s_v3": "Standard_D4s_v3",
    "Standard_D16s_v3": "Standard_D8s_v3",
    "Standard_D32s_v3": "Standard_D16s_v3",
}

# System instructions for the framework agent (live mode only).
_AGENT_INSTRUCTIONS = """\
You are SentinelLayer's FinOps Optimization Agent — a specialist in cloud
cost management and resource right-sizing.

Your job:
1. Call the `scan_cost_opportunities` tool to analyse the resource topology.
2. Receive the list of cost-saving proposals from the deterministic scan.
3. Write a brief 2-3 sentence FinOps summary explaining what opportunities
   were found and the total estimated savings potential.
   Do NOT restate individual resource names; give the overall picture.

Always call the tool first before providing any commentary.
"""


# ---------------------------------------------------------------------------
# Agent
# ---------------------------------------------------------------------------


class CostOptimizationAgent:
    """Scans a resource topology and proposes cost-saving actions.

    Loads resource metadata from ``data/seed_resources.json`` (mock for
    Azure Cost Management + Resource Graph), then applies heuristic rules
    to identify overprovisioned or idle resources.

    In live mode the Microsoft Agent Framework drives GPT-4.1 to call the
    deterministic tool and synthesise a FinOps commentary.

    Usage::

        agent = CostOptimizationAgent()
        proposals: list[ProposedAction] = agent.scan()
        for p in proposals:
            print(p.action_type.value, p.target.resource_id, p.projected_savings_monthly)
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

    async def scan(self) -> list[ProposedAction]:
        """Scan all resources and return cost-optimisation proposals.

        Routes to the Microsoft Agent Framework agent in live mode, or to the
        deterministic rule-based scanner in mock mode.

        Returns:
            A list of :class:`~src.core.models.ProposedAction` objects,
            one per resource that triggered a detection rule.
            Returns an empty list if no optimisation opportunities are found.
        """
        if not self._use_framework:
            return self._scan_rules()

        try:
            return await self._scan_with_framework()
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "CostOptimizationAgent: framework call failed (%s) — falling back to rules.", exc
            )
            return self._scan_rules()

    # ------------------------------------------------------------------
    # Microsoft Agent Framework path (live mode)
    # ------------------------------------------------------------------

    async def _scan_with_framework(self) -> list[ProposedAction]:
        """Run the framework agent with GPT-4.1 driving the tool call."""
        from openai import AsyncAzureOpenAI
        from azure.identity import DefaultAzureCredential, get_bearer_token_provider
        import agent_framework as af
        from agent_framework.openai import OpenAIResponsesClient

        credential = DefaultAzureCredential()
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
            name="scan_cost_opportunities",
            description=(
                "Scan the infrastructure resource topology for cost optimisation "
                "opportunities. Applies rule-based heuristics (oversized VMs, "
                "over-provisioned AKS clusters) and returns a JSON array of "
                "ProposedAction objects representing cost-saving recommendations."
            ),
        )
        def scan_cost_opportunities() -> str:
            """Identify idle or overprovisioned resources that can be cost-optimised."""
            proposals = self._scan_rules()
            proposals_holder.append(proposals)
            return json.dumps([p.model_dump() for p in proposals], default=str)

        agent = client.as_agent(
            name="cost-optimizer",
            instructions=_AGENT_INSTRUCTIONS,
            tools=[scan_cost_opportunities],
        )

        await agent.run(
            "Scan the infrastructure for cost optimisation opportunities and "
            "provide a FinOps summary."
        )

        return proposals_holder[-1] if proposals_holder else self._scan_rules()

    # ------------------------------------------------------------------
    # Deterministic rule-based scan
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
    # Private helpers
    # ------------------------------------------------------------------

    def _analyze_resource(self, resource: dict) -> ProposedAction | None:
        """Apply detection rules to a single resource.

        Returns a :class:`~src.core.models.ProposedAction` if the resource
        triggers a rule, or ``None`` if no optimisation is warranted.
        """
        monthly_cost: float | None = resource.get("monthly_cost")

        # Skip free or cheap resources — not worth the operational overhead.
        if monthly_cost is None or monthly_cost < _MIN_COST_THRESHOLD:
            return None

        resource_type: str = resource.get("type", "")

        # Rule 1 — Oversized VM SKU
        if "virtualMachines" in resource_type:
            return self._propose_vm_scale_down(resource, monthly_cost)

        # Rule 2 — Large AKS cluster
        if "managedClusters" in resource_type:
            return self._propose_aks_scale_down(resource, monthly_cost)

        return None

    def _propose_vm_scale_down(
        self, resource: dict, monthly_cost: float
    ) -> ProposedAction | None:
        """Propose a scale-down for an oversized VM.

        Only triggers when the current SKU is in ``_OVERSIZED_SKUS``.
        Returns ``None`` if the VM is already on a smaller SKU tier.
        """
        sku: str = resource.get("sku", "")
        if sku not in _OVERSIZED_SKUS:
            return None

        proposed_sku = _DOWNSIZE_MAP[sku]
        savings = round(monthly_cost * _VM_DOWNSIZE_SAVINGS_RATE, 2)
        tags = resource.get("tags", {})
        is_idle = tags.get("purpose") == "disaster-recovery"

        reason = (
            f"VM '{resource['name']}' is running SKU {sku} at ${monthly_cost:.0f}/month. "
        )
        if is_idle:
            reason += (
                "Tagged as disaster-recovery — expected to be idle most of the time. "
            )
        reason += (
            f"Downsizing to {proposed_sku} is estimated to save ${savings:.0f}/month."
        )

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
        """Propose a node-count reduction for an over-provisioned AKS cluster.

        Only triggers when node count is at or above ``_AKS_SCALE_DOWN_NODE_THRESHOLD``.
        """
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
