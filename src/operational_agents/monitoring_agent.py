"""SRE Monitoring & RCA Agent — detects anomalies and proposes remediation.

This is an operational agent (the governed subject). It proposes
infrastructure actions that SentinelLayer evaluates before execution.

Microsoft Agent Framework integration (Phase 8)
------------------------------------------------
In live mode (USE_LOCAL_MOCKS=false), this agent is driven by a
Microsoft Agent Framework ``Agent`` backed by Azure OpenAI GPT-4.1.

The LLM agent calls our deterministic ``scan_anomalies`` tool,
which applies SRE heuristics to the resource topology and returns
structured remediation proposals.  The LLM then synthesises a concise
SRE incident-analysis narrative for the operator.

In mock mode the framework is skipped — only deterministic scanning runs.

The agent scans a resource topology (loaded from ``data/seed_resources.json``)
and applies SRE heuristics to detect structural anomalies:

Detection rules
---------------
1. **Missing owner tag on critical resources** — Resources tagged
   ``criticality: critical`` with no ``owner`` tag create accountability gaps:
   if the resource fails, no team is automatically responsible. Proposes
   UPDATE_CONFIG to add ownership metadata.
2. **Circular dependency** — Resources with bidirectional dependency edges
   create split-brain risk during restarts or failovers. Proposes
   RESTART_SERVICE on the second node in the cycle, with sequencing guidance.
3. **High-cost single point of failure** — Resources tagged
   ``criticality: critical`` with monthly cost above threshold and downstream
   dependents, but no redundancy indicated. Proposes SCALE_UP to add a
   standby replica or additional node pool.
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

_AGENT_ID = "monitoring-agent"

_DEFAULT_RESOURCES_PATH = (
    Path(__file__).parent.parent.parent / "data" / "seed_resources.json"
)

# Critical resources costing more than this per month are flagged as high-cost SPOFs.
_CRITICAL_COST_THRESHOLD: float = 500.0

# System instructions for the framework agent (live mode only).
_AGENT_INSTRUCTIONS = """\
You are SentinelLayer's SRE Monitoring Agent — a specialist in cloud
infrastructure reliability, anomaly detection, and root cause analysis.

Your job:
1. Call the `scan_anomalies` tool to analyse the resource topology.
2. Receive the list of anomaly-based remediation proposals from the deterministic scan.
3. Write a concise 2-3 sentence SRE summary explaining what anomalies were detected
   and the reliability risks they represent.
   Highlight the most critical finding (e.g., unowned critical resource, circular dep).

Always call the tool first before providing any commentary.
"""


# ---------------------------------------------------------------------------
# Agent
# ---------------------------------------------------------------------------


class MonitoringAgent:
    """Detects structural anomalies in the resource topology and proposes remediation.

    Loads resource metadata from ``data/seed_resources.json`` (mock for
    Azure Monitor + Resource Graph), applies SRE heuristics, and returns
    a list of remediation :class:`~src.core.models.ProposedAction` objects.

    In live mode the Microsoft Agent Framework drives GPT-4.1 to call the
    deterministic tool and synthesise an SRE narrative.

    Usage::

        agent = MonitoringAgent()
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
        # Directed dependency edges from the JSON
        self._edges: list[dict] = data.get("dependency_edges", [])

        self._cfg = cfg or _default_settings

        self._use_framework: bool = (
            not self._cfg.use_local_mocks
            and bool(self._cfg.azure_openai_endpoint)
        )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def scan(self) -> list[ProposedAction]:
        """Detect anomalies across the resource topology.

        Routes to the Microsoft Agent Framework agent in live mode, or to the
        deterministic rule-based scanner in mock mode.

        Returns:
            A list of :class:`~src.core.models.ProposedAction` objects,
            one per anomaly detected. Returns an empty list when the
            topology appears healthy.
        """
        if not self._use_framework:
            return self._scan_rules()

        try:
            return await self._scan_with_framework()
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "MonitoringAgent: framework call failed (%s) — falling back to rules.", exc
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
            name="scan_anomalies",
            description=(
                "Scan the infrastructure resource topology for structural anomalies "
                "using SRE heuristics. Detects unowned critical resources, circular "
                "dependencies, and high-cost single points of failure. Returns a JSON "
                "array of ProposedAction objects representing remediation recommendations."
            ),
        )
        def scan_anomalies() -> str:
            """Detect reliability anomalies and generate remediation proposals."""
            proposals = self._scan_rules()
            proposals_holder.append(proposals)
            return json.dumps([p.model_dump() for p in proposals], default=str)

        agent = client.as_agent(
            name="sre-monitoring-agent",
            instructions=_AGENT_INSTRUCTIONS,
            tools=[scan_anomalies],
        )

        await agent.run(
            "Scan the infrastructure for reliability anomalies and "
            "provide an SRE incident analysis summary."
        )

        return proposals_holder[-1] if proposals_holder else self._scan_rules()

    # ------------------------------------------------------------------
    # Deterministic rule-based scan
    # ------------------------------------------------------------------

    def _scan_rules(self) -> list[ProposedAction]:
        """Run all three detection rules in sequence and aggregate results."""
        proposals: list[ProposedAction] = []
        proposals.extend(self._detect_untagged_critical_resources())
        proposals.extend(self._detect_circular_dependencies())
        proposals.extend(self._detect_high_cost_spofs())

        logger.info(
            "MonitoringAgent: scan complete — %d anomalies detected",
            len(proposals),
        )
        return proposals

    # ------------------------------------------------------------------
    # Detection rules
    # ------------------------------------------------------------------

    def _detect_untagged_critical_resources(self) -> list[ProposedAction]:
        """Rule 1 — Critical resources missing an owner tag.

        An unowned critical resource creates an accountability gap: if it
        fails at 3 AM, no on-call engineer is automatically paged. We propose
        UPDATE_CONFIG to add the ``owner`` tag.
        """
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
            logger.info(
                "MonitoringAgent: unowned critical resource — '%s'",
                resource["name"],
            )

        return proposals

    def _detect_circular_dependencies(self) -> list[ProposedAction]:
        """Rule 2 — Bidirectional dependency edges (circular dependencies).

        A circular dependency means that if either service restarts, the
        other may fail to reconnect, potentially causing a cascading outage.
        We propose RESTART_SERVICE on the second node in the pair, with
        sequencing guidance so the operator restarts them in the right order.

        Only the first occurrence of each unique pair is flagged to avoid
        producing duplicate proposals for the same circular edge.
        """
        # Build a set of directed edges for fast reverse-lookup.
        edge_set: set[tuple[str, str]] = {(e["from"], e["to"]) for e in self._edges}

        seen_pairs: set[frozenset[str]] = set()
        proposals: list[ProposedAction] = []

        for edge in self._edges:
            a, b = edge["from"], edge["to"]
            pair = frozenset({a, b})

            if pair in seen_pairs:
                continue  # already reported this circular pair

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
                logger.info(
                    "MonitoringAgent: circular dependency — '%s' ↔ '%s'", a, b
                )

        return proposals

    def _detect_high_cost_spofs(self) -> list[ProposedAction]:
        """Rule 3 — High-cost critical resources with many dependents.

        A critical resource that is expensive and has many dependents but shows
        no sign of redundancy is a single point of failure with high blast radius.
        We propose SCALE_UP to add a standby replica or additional node pool.
        """
        proposals: list[ProposedAction] = []

        for resource in self._resources.values():
            tags = resource.get("tags", {})
            if tags.get("criticality") != "critical":
                continue

            monthly_cost = resource.get("monthly_cost")
            if monthly_cost is None or monthly_cost < _CRITICAL_COST_THRESHOLD:
                continue

            # Count downstream workloads that would be disrupted
            dependents = (
                resource.get("dependents", []) + resource.get("services_hosted", [])
            )
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
                resource["name"],
                monthly_cost,
                len(dependents),
            )

        return proposals
