"""Financial Impact Agent — SRI:Cost dimension.

Estimates the financial impact of a proposed action and detects
over-optimisation risk — cases where short-term cost savings would
trigger expensive recovery events that outweigh the savings.

Microsoft Agent Framework integration (Phase 8)
------------------------------------------------
In live mode (USE_LOCAL_MOCKS=false), this agent is driven by a
Microsoft Agent Framework ``Agent`` backed by Azure OpenAI GPT-4.1.

The LLM agent calls our deterministic ``evaluate_financial_rules`` tool,
which computes the cost delta, over-optimisation risk, and SRI:Cost score.
The LLM then synthesises an expert financial risk narrative.

In mock mode the framework is skipped — only deterministic evaluation runs.

Cost change is determined from these sources in priority order
--------------------------------------------------------------
1. ``action.projected_savings_monthly`` — explicit figure from the proposing
   agent.  Most accurate; treated as exact (not uncertain).
2. ``action.target.current_monthly_cost`` — cost stated in the action target
   for DELETE actions.
3. ``seed_resources.json`` — looked up by resource name; used as a fallback
   when the action does not carry cost metadata.
4. Estimated percentage of current cost:
   - SCALE_DOWN → ~30 % reduction
   - SCALE_UP   → ~50 % increase
   These are flagged as uncertain and incur a scoring penalty.
5. Zero — actions with no expected cost impact (RESTART, MODIFY_NSG, etc.).
   Returned as certain because zero *is* the correct value.

SRI:Cost score (0–100)
-----------------------
* 0–25   — minimal financial risk (auto-approve)
* 26–60  — moderate financial risk (escalate for review)
* 61–100 — significant financial risk (deny)

Score formula
--------------
``score = magnitude_score(|monthly_change|) × action_multiplier``
``      + over_optimisation_penalty   (if detected)``
``      + cost_uncertainty_penalty    (if estimated, not exact)``
Capped at 100.

Magnitude thresholds (absolute monthly change → base pts)
----------------------------------------------------------
* ≥ $1000 → 70 pts
* $600–$999  → 50 pts
* $300–$599  → 30 pts
* $100–$299  → 15 pts
* $0.01–$99  →  5 pts
* $0         →  0 pts

Action multipliers
------------------
* DELETE_RESOURCE : 1.5   (irreversible — full cost disappears; highest risk)
* SCALE_DOWN      : 1.2   (availability risk; partial cost reduction)
* UPDATE_CONFIG   : 0.8
* SCALE_UP        : 0.6   (planned spend; cost increase is controlled)
* CREATE_RESOURCE : 0.5
* RESTART_SERVICE : 0.3   (no cost change expected)
* MODIFY_NSG      : 0.3   (no cost change expected)
"""

import asyncio
import json
import logging
from pathlib import Path

from src.config import settings as _default_settings
from src.core.models import ActionType, FinancialResult, ProposedAction

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_DEFAULT_RESOURCES_PATH = (
    Path(__file__).parent.parent.parent / "data" / "seed_resources.json"
)

# Magnitude thresholds: (minimum_abs_change, score_pts).
# Evaluated from highest to lowest; first match wins.
_MAGNITUDE_THRESHOLDS: list[tuple[float, float]] = [
    (1000.0, 70.0),
    (600.0,  50.0),
    (300.0,  30.0),
    (100.0,  15.0),
    (0.01,    5.0),
]
# A change of exactly $0 yields 0 pts (implicit default).

# How much each action type amplifies the magnitude score
_ACTION_MULTIPLIER: dict[ActionType, float] = {
    ActionType.DELETE_RESOURCE: 1.5,
    ActionType.SCALE_DOWN:      1.2,
    ActionType.UPDATE_CONFIG:   0.8,
    ActionType.SCALE_UP:        0.6,
    ActionType.CREATE_RESOURCE: 0.5,
    ActionType.RESTART_SERVICE: 0.3,
    ActionType.MODIFY_NSG:      0.3,
}

_OVER_OPTIMISATION_PENALTY: float = 20.0
_COST_UNCERTAINTY_PENALTY: float = 10.0

# Assumed cost reduction / increase for scale operations when no explicit data
_SCALE_DOWN_ESTIMATE: float = 0.30  # 30 % monthly cost reduction
_SCALE_UP_ESTIMATE:   float = 0.50  # 50 % monthly cost increase

# Estimated recovery cost per dependent service if over-optimisation occurs
_RECOVERY_COST_PER_SERVICE: float = 10_000.0

# Minimum number of dependents required to trigger over-optimisation detection
_OVER_OPT_THRESHOLD: int = 1

# System instructions for the framework agent (live mode only).
_AGENT_INSTRUCTIONS = """\
You are RuriSkry's Financial Governance Agent — an expert in cloud FinOps with
the authority to ADJUST financial risk scores.

## Your role
You receive a baseline financial risk score and cost analysis. You reason about
whether the financial risk is accurately captured given the full context.

## Process
1. Call `evaluate_financial_rules` to get the baseline score and cost analysis.
2. Reason about the true financial risk:
   - Is the cost estimate based on real data or uncertain heuristics?
   - Is the over-optimisation risk real, or are the "dependent services" actually inactive?
   - For cost-saving actions: does the saving justify the operational risk?
   - For cost-increasing actions: is the spend justified by security or reliability needs?
   - Is the ops agent performing a remediation that incidentally increases cost? (lower risk)
3. Call `submit_governance_decision` with your adjusted score and justification.

## Adjustment rules
- You may adjust the baseline score by at most +/-30 points
- Security remediations that increase cost should receive reduced financial risk scores
- Cost savings with uncertain estimates should receive slightly increased scores
- Over-optimisation risk on truly critical services should be increased
- Provide a specific reason for each adjustment
"""


# ---------------------------------------------------------------------------
# Agent
# ---------------------------------------------------------------------------


class FinancialImpactAgent:
    """Estimates the financial impact and over-optimisation risk of a proposed action.

    Loads resource cost data from ``seed_resources.json`` (mock for Azure Cost
    Management), then computes an SRI:Cost score (0–100), a monthly cost delta,
    a 90-day projection, and optionally an over-optimisation risk assessment.

    In live mode the Microsoft Agent Framework drives GPT-4.1 to call the
    deterministic tool and synthesise a financial risk narrative.

    Usage::

        agent = FinancialImpactAgent()
        result: FinancialResult = agent.evaluate(action)
        print(result.sri_cost, result.immediate_monthly_change)
    """

    def __init__(
        self,
        resources_path: str | Path | None = None,
        cfg=None,
    ) -> None:
        self._cfg = cfg or _default_settings

        self._use_framework: bool = (
            not self._cfg.use_local_mocks
            and bool(self._cfg.azure_openai_endpoint)
        )

        _live = (
            not self._cfg.use_local_mocks
            and bool(self._cfg.azure_subscription_id)
            and getattr(self._cfg, "use_live_topology", False)
        )
        if not _live:
            # Mock / JSON mode: load seed_resources.json — all tests pass unchanged.
            path = Path(resources_path) if resources_path else _DEFAULT_RESOURCES_PATH
            with open(path, encoding="utf-8") as fh:
                data: dict = json.load(fh)
            self._resources: dict[str, dict] = {
                r["name"]: r for r in data.get("resources", [])
            }
            self._rg_client = None
        else:
            # Live topology mode (USE_LIVE_TOPOLOGY=true): lazy Azure queries.
            # monthly_cost is populated by ResourceGraphClient._azure_enrich_topology()
            # via cost_lookup.
            from src.infrastructure.resource_graph import ResourceGraphClient
            self._rg_client = ResourceGraphClient(cfg=self._cfg)
            self._resources = {}  # not used in live mode

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def aclose(self) -> None:
        """Release async Azure SDK resources (aiohttp connection pool)."""
        if self._rg_client is not None:
            await self._rg_client.aclose()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def evaluate(self, action: ProposedAction, force_deterministic: bool = False) -> FinancialResult:
        """Evaluate the financial impact of a proposed infrastructure action.

        Routes to the Microsoft Agent Framework agent in live mode, or to the
        deterministic rule-based engine in mock mode.

        Args:
            action: The proposed action from an operational agent.

        Returns:
            :class:`~src.core.models.FinancialResult` containing:

            * ``sri_cost`` — 0–100 financial risk score
            * ``immediate_monthly_change`` — estimated USD change per month
              (negative = savings, positive = additional spend)
            * ``projection_90_day`` — 3-month cost forecast dict
            * ``over_optimization_risk`` — risk assessment dict if detected,
              else None
            * ``reasoning`` — human-readable explanation
        """
        if not self._use_framework or force_deterministic:
            if self._rg_client is not None:
                # Live topology: use the fully async path so Azure SDK calls
                # don't block the event loop (Phase 20 — async end-to-end).
                return await self._evaluate_rules_async(action)
            return self._evaluate_rules(action)  # mock: pure in-memory, no IO

        try:
            return await self._evaluate_with_framework(action)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "FinancialImpactAgent: framework call failed (%s) — falling back to rules.", exc
            )
            if self._rg_client is not None:
                return await self._evaluate_rules_async(action)
            return self._evaluate_rules(action)

    # ------------------------------------------------------------------
    # Microsoft Agent Framework path (live mode)
    # ------------------------------------------------------------------

    async def _evaluate_with_framework(self, action: ProposedAction) -> FinancialResult:
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
            timeout=float(self._cfg.llm_timeout),
        )
        client = OpenAIResponsesClient(
            async_client=azure_openai,
            model_id=self._cfg.azure_openai_deployment,
        )

        result_holder: list[FinancialResult] = []
        llm_decision_holder: list[dict] = []

        @af.tool(
            name="evaluate_financial_rules",
            description=(
                "Compute the financial impact of a proposed action using deterministic "
                "rule-based analysis. Returns a JSON object with sri_cost, "
                "immediate_monthly_change, projection_90_day, over_optimization_risk, "
                "and reasoning."
            ),
        )
        async def evaluate_financial_rules(action_json: str) -> str:
            """Calculate financial impact and over-optimisation risk."""
            try:
                a = ProposedAction.model_validate_json(action_json)
            except Exception:
                a = action
            r = await self._evaluate_rules_async(a)
            result_holder.append(r)
            return r.model_dump_json()

        @af.tool(
            name="submit_governance_decision",
            description=(
                "Submit your final governance decision after reviewing the baseline score. "
                "adjusted_score must be within +/-30 of the baseline score. "
                "Provide an adjustment entry for each score change with a clear reason."
            ),
        )
        async def submit_governance_decision(
            adjusted_score: float,
            adjustments_json: str = "[]",
            reasoning: str = "",
            confidence: float = 0.8,
        ) -> str:
            """Record the LLM's governance decision with justification."""
            import json as _json
            try:
                adjustments = _json.loads(adjustments_json)
            except Exception:
                adjustments = []
            llm_decision_holder.append({
                "adjusted_score": adjusted_score,
                "adjustments": adjustments,
                "reasoning": reasoning,
                "confidence": confidence,
            })
            return "Decision recorded."

        agent = client.as_agent(
            name="financial-impact-assessor",
            instructions=_AGENT_INSTRUCTIONS,
            tools=[evaluate_financial_rules, submit_governance_decision],
        )

        from src.infrastructure.llm_throttle import run_with_throttle
        from src.governance_agents._llm_governance import parse_llm_decision

        evidence_section = ""
        if action.evidence:
            evidence_section = f"\n## Observed Evidence\n{action.evidence.model_dump_json()}\n"

        prompt = (
            f"## Proposed Action\n{action.model_dump_json()}\n\n"
            f"## Ops Agent's Reasoning\n{action.reason}\n"
            f"{evidence_section}\n"
            "INSTRUCTIONS: First call evaluate_financial_rules to get the baseline score "
            "and cost analysis. Reason about whether the financial risk accurately reflects "
            "the business impact given the ops agent's intent. "
            "If evidence shows high/critical severity on a production resource, the cost of "
            "inaction outweighs the action cost — consider reducing the score. "
            "If a scale-down has peak_cpu_14d > 50% in evidence metrics, the resize may be "
            "dangerous — consider increasing the score. "
            "Then call submit_governance_decision with your adjusted score and justification."
        )
        await run_with_throttle(agent.run, prompt)

        if result_holder:
            base = result_holder[-1]
            adjusted_score, adjustment_text, _ = parse_llm_decision(
                llm_decision_holder, base.sri_cost
            )
            return FinancialResult(
                sri_cost=adjusted_score,
                immediate_monthly_change=base.immediate_monthly_change,
                projection_90_day=base.projection_90_day,
                over_optimization_risk=base.over_optimization_risk,
                reasoning=base.reasoning + adjustment_text,
            )

        # Tool was never called — return plain rule-based result (async to avoid blocking)
        return await self._evaluate_rules_async(action)

    # ------------------------------------------------------------------
    # Async rule-based evaluation (Phase 20 — used when rg_client is set)
    # ------------------------------------------------------------------

    async def _evaluate_rules_async(self, action: ProposedAction) -> FinancialResult:
        """Async variant of :meth:`_evaluate_rules` — non-blocking Azure calls.

        Only ``_find_resource`` is async (it queries Azure Resource Graph).
        All other helpers are pure computation and remain synchronous.
        """
        resource = await self._find_resource_async(action.target.resource_id)
        monthly_change, cost_uncertain = self._estimate_cost_change(action, resource)
        over_opt = self._detect_over_optimisation(action, resource, monthly_change)
        projection = self._build_projection(monthly_change)
        score = self._calculate_score(action, monthly_change, cost_uncertain, over_opt)

        logger.info(
            "FinancialImpactAgent(async): action=%s change=%.2f uncertain=%s score=%.1f",
            action.action_type.value,
            monthly_change,
            cost_uncertain,
            score,
        )

        reasoning = self._build_reasoning(action, monthly_change, cost_uncertain, over_opt, score)

        return FinancialResult(
            sri_cost=score,
            immediate_monthly_change=monthly_change,
            projection_90_day=projection,
            over_optimization_risk=over_opt,
            reasoning=reasoning,
        )

    async def _find_resource_async(self, resource_id: str) -> dict | None:
        """Async resource lookup — uses async ResourceGraphClient in live mode."""
        if self._rg_client is not None:
            return await self._rg_client.get_resource_async(resource_id)
        # Mock mode: in-memory dict lookup, no I/O
        if resource_id in self._resources:
            return self._resources[resource_id]
        name = resource_id.split("/")[-1]
        return self._resources.get(name)

    # ------------------------------------------------------------------
    # Deterministic rule-based evaluation
    # ------------------------------------------------------------------

    def _evaluate_rules(self, action: ProposedAction) -> FinancialResult:
        """Run the full deterministic financial impact analysis."""
        resource = self._find_resource(action.target.resource_id)
        monthly_change, cost_uncertain = self._estimate_cost_change(action, resource)
        over_opt = self._detect_over_optimisation(action, resource, monthly_change)
        projection = self._build_projection(monthly_change)
        score = self._calculate_score(action, monthly_change, cost_uncertain, over_opt)

        logger.info(
            "FinancialImpactAgent: action=%s change=%.2f uncertain=%s score=%.1f",
            action.action_type.value,
            monthly_change,
            cost_uncertain,
            score,
        )

        reasoning = self._build_reasoning(action, monthly_change, cost_uncertain, over_opt, score)

        return FinancialResult(
            sri_cost=score,
            immediate_monthly_change=monthly_change,
            projection_90_day=projection,
            over_optimization_risk=over_opt,
            reasoning=reasoning,
        )

    # ------------------------------------------------------------------
    # Cost estimation
    # ------------------------------------------------------------------

    def _find_resource(self, resource_id: str) -> dict | None:
        """Look up a resource by name or the last segment of its Azure resource ID.

        In live mode, queries Azure Resource Graph (with cost data populated
        via ``cost_lookup`` inside ``ResourceGraphClient._azure_enrich_topology``).
        In mock mode, looks up the in-memory dict loaded from seed JSON.
        """
        if self._rg_client is not None:
            # Live mode: resource dict includes monthly_cost from Retail Prices API.
            return self._rg_client.get_resource(resource_id)
        # Mock mode: existing in-memory lookup.
        if resource_id in self._resources:
            return self._resources[resource_id]
        name = resource_id.split("/")[-1]
        return self._resources.get(name)

    def _estimate_cost_change(
        self, action: ProposedAction, resource: dict | None
    ) -> tuple[float, bool]:
        """Return ``(monthly_change_usd, cost_uncertain)`` for the action.

        ``monthly_change_usd`` — negative for savings, positive for new spend.
        ``cost_uncertain``     — True when the figure is an estimate, not exact.

        Resolution order:
        1. ``projected_savings_monthly`` — exact, agent-supplied.
        2. DELETE: full current cost from target metadata or resource graph.
        3. SCALE_DOWN: 30 % of current cost (estimated).
        4. SCALE_UP: 50 % of current cost (estimated).
        5. RESTART / MODIFY_NSG / UPDATE_CONFIG / CREATE — zero cost change
           (certain by design; these operations do not alter billing).
        6. Fallback: 0.0, uncertain (cost-impacting action with no data).
        """
        # 1. Proposing agent supplied an explicit savings figure
        if action.projected_savings_monthly is not None:
            return (-action.projected_savings_monthly, False)

        # Helper — resolve current monthly cost from target or resource graph
        current_cost: float | None = (
            action.target.current_monthly_cost
            if action.target.current_monthly_cost is not None
            else (resource.get("monthly_cost") if resource else None)
        )

        # 2. DELETE removes the resource's full monthly cost
        if action.action_type == ActionType.DELETE_RESOURCE:
            if current_cost is not None:
                return (-current_cost, False)
            return (0.0, True)  # cost cannot be determined

        # 3. SCALE_DOWN — estimated 30 % reduction
        if action.action_type == ActionType.SCALE_DOWN:
            if current_cost is not None:
                return (-round(current_cost * _SCALE_DOWN_ESTIMATE, 2), True)
            return (0.0, True)

        # 4. SCALE_UP — estimated 50 % increase
        if action.action_type == ActionType.SCALE_UP:
            if current_cost is not None:
                return (round(current_cost * _SCALE_UP_ESTIMATE, 2), True)
            return (0.0, True)

        # 5. All other action types — no meaningful cost change
        return (0.0, False)

    # ------------------------------------------------------------------
    # Over-optimisation detection
    # ------------------------------------------------------------------

    def _detect_over_optimisation(
        self,
        action: ProposedAction,
        resource: dict | None,
        monthly_change: float,
    ) -> dict | None:
        """Detect over-optimisation risk — saving money but risking a bigger loss.

        Triggered when:

        1. The action is ``DELETE_RESOURCE`` or ``SCALE_DOWN`` (cost-reducing).
        2. The resource has at least one dependent, consumer, or hosted service.

        Returns a risk dict if detected, else ``None``.
        """
        if action.action_type not in (
            ActionType.DELETE_RESOURCE,
            ActionType.SCALE_DOWN,
        ):
            return None

        if resource is None:
            return None

        # Collect all services/resources that depend on this one
        dependents: list[str] = (
            resource.get("dependents", [])
            + resource.get("consumers", [])
            + resource.get("services_hosted", [])
        )

        count = len(dependents)
        if count < _OVER_OPT_THRESHOLD:
            return None

        monthly_savings = abs(monthly_change)
        recovery_cost = count * _RECOVERY_COST_PER_SERVICE
        preview = dependents[:3]
        ellipsis = "..." if count > 3 else ""

        return {
            "detected": True,
            "affected_services": dependents,
            "affected_count": count,
            "monthly_savings": round(monthly_savings, 2),
            "estimated_recovery_cost": recovery_cost,
            "reason": (
                f"'{resource['name']}' has {count} dependent service(s): "
                f"{', '.join(preview)}{ellipsis}. "
                f"Saving ${monthly_savings:,.0f}/month risks "
                f"${recovery_cost:,.0f} in unplanned recovery costs."
            ),
        }

    # ------------------------------------------------------------------
    # 90-day projection
    # ------------------------------------------------------------------

    @staticmethod
    def _build_projection(monthly_change: float) -> dict:
        """Build a simple linear 90-day cost projection."""
        return {
            "month_1": round(monthly_change, 2),
            "month_2": round(monthly_change, 2),
            "month_3": round(monthly_change, 2),
            "total_90_day": round(monthly_change * 3, 2),
            "annualized": round(monthly_change * 12, 2),
            "note": (
                "Linear projection — does not account for usage growth, "
                "scaling events, or variable workloads."
            ),
        }

    # ------------------------------------------------------------------
    # Scoring
    # ------------------------------------------------------------------

    def _calculate_score(
        self,
        action: ProposedAction,
        monthly_change: float,
        cost_uncertain: bool,
        over_opt: dict | None,
    ) -> float:
        """Compute SRI:Cost score (0–100).

        Formula::

            score = magnitude_score(|monthly_change|) × action_multiplier
                  + over_optimisation_penalty   (if detected)
                  + cost_uncertainty_penalty    (if uncertain)
                  + evidence_adjustment         (Phase 32)
        """
        magnitude = self._magnitude_score(abs(monthly_change))
        multiplier = _ACTION_MULTIPLIER.get(action.action_type, 1.0)
        score = magnitude * multiplier

        if over_opt:
            score += _OVER_OPTIMISATION_PENALTY

        if cost_uncertain:
            score += _COST_UNCERTAINTY_PENALTY

        score = self._apply_evidence_adjustment(score, action)

        return round(min(score, 100.0), 2)

    @staticmethod
    def _apply_evidence_adjustment(score: float, action: "ProposedAction") -> float:
        """Adjust SRI:Cost based on attached evidence (Phase 32).

        - Incident-driven restart on a production resource: cost-of-inaction
          outweighs cost-of-action → reduce score by 10.
        - Scale-down with peak CPU evidence above safe threshold: dangerous
          resize the average hides → increase score by 10.
        """
        ev = action.evidence
        if ev is None:
            return score

        resource_id = action.target.resource_id.lower()
        is_production = "prod" in resource_id or "production" in resource_id

        if (
            action.action_type in (ActionType.RESTART_SERVICE,)
            and ev.severity in ("high", "critical")
            and is_production
        ):
            return max(score - 10.0, 0.0)

        if action.action_type == ActionType.SCALE_DOWN:
            peak_cpu = ev.metrics.get("peak_cpu_14d", 0.0)
            if peak_cpu > 50.0:
                return min(score + 10.0, 100.0)

        return score

    @staticmethod
    def _magnitude_score(abs_change: float) -> float:
        """Map an absolute monthly cost change to a base magnitude score."""
        for threshold, pts in _MAGNITUDE_THRESHOLDS:
            if abs_change >= threshold:
                return pts
        return 0.0

    # ------------------------------------------------------------------
    # Reasoning
    # ------------------------------------------------------------------

    def _build_reasoning(
        self,
        action: ProposedAction,
        monthly_change: float,
        cost_uncertain: bool,
        over_opt: dict | None,
        score: float,
    ) -> str:
        """Build a human-readable explanation of the financial risk assessment."""
        abs_change = abs(monthly_change)
        if monthly_change < 0:
            direction = "reduction"
        elif monthly_change > 0:
            direction = "increase"
        else:
            direction = "no change"

        estimate_tag = " (estimated)" if cost_uncertain else ""

        lines = [
            f"Financial analysis for '{action.action_type.value}': "
            f"${abs_change:,.2f}/month {direction}{estimate_tag}.",
            f"90-day outlook: ${monthly_change * 3:,.2f}  |  "
            f"Annualised: ${monthly_change * 12:,.2f}.",
        ]

        if over_opt:
            lines.append(f"Over-optimisation risk detected: {over_opt['reason']}")

        lines.append(f"SRI:Cost score: {score:.1f}/100.")
        return "\n".join(lines)
