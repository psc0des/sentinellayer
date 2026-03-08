"""Historical Pattern Agent — SRI:Historical dimension.

Matches a proposed action against past incident history.

Microsoft Agent Framework integration (Phase 8)
------------------------------------------------
In live mode (USE_LOCAL_MOCKS=false), this agent is driven by a
Microsoft Agent Framework ``Agent`` backed by Azure OpenAI GPT-4.1.

The LLM agent calls our deterministic ``evaluate_historical_rules`` tool,
which searches Azure AI Search (BM25) or local JSON for past incidents
and scores similarity.  The LLM then synthesises a narrative explaining
what history tells us about the safety of this action.

In mock mode the framework is skipped — only deterministic evaluation runs.

Backend selection
-----------------
* **Live mode** (USE_LOCAL_MOCKS=false + Azure Search credentials available):
  Uses Azure AI Search full-text search on the ``incident-history`` index.
  Results are ranked by Azure Search BM25 relevance and normalised to a
  [0, 0.8] similarity band.

* **Mock mode** (USE_LOCAL_MOCKS=true or credentials missing):
  Loads incidents from ``data/seed_incidents.json`` and performs keyword +
  field-filter matching in Python — identical to the original implementation.

Similarity dimensions in mock mode (weights sum to 1.0)
---------------------------------------------------------
1. **Action type match** (0.40) — incident action prefix equals proposed type.
2. **Resource type match** (0.30) — Azure resource category matches.
3. **Resource name match** (0.20) — specific resource name in incident text.
4. **Tag relevance** (0.10) — action-type keyword appears in incident tags.

Only incidents with similarity ≥ 0.30 are surfaced as similar.

SRI:Historical score (0–100)
------------------------------
* 0       — no historical precedent found
* 1–25    — precedent exists but past outcome was low severity (safe signal)
* 26–60   — moderate severity precedents warrant human review
* 61–100  — critical/high severity precedents with high similarity → block

Severity weights
-----------------
* critical : 100
* high     :  75
* medium   :  40
* low      :  10

Score formula
--------------
``score = best.similarity × severity_weight[best.severity]``
``      + Σ (inc.similarity × severity_weight[inc.severity] × 0.20)``
``            for each additional similar incident``
Capped at 100.
"""

import json
import logging
from pathlib import Path

from src.config import settings as _default_settings
from src.core.models import (
    ActionType,
    HistoricalResult,
    ProposedAction,
    SimilarIncident,
)
from src.infrastructure.search_client import AzureSearchClient

# Points added per recent ESCALATED decision for the same action_type.
# Capped so governance history can raise but not dominate the score.
_GOV_BOOST_PER_ESCALATED = 25
_GOV_BOOST_PER_APPROVED = 5
_GOV_BOOST_CAP = 60
_GOV_HISTORY_LOOKBACK = 50  # how many recent decisions to scan

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_DEFAULT_INCIDENTS_PATH = (
    Path(__file__).parent.parent.parent / "data" / "seed_incidents.json"
)

# Minimum similarity for an incident to be considered relevant
_SIMILARITY_THRESHOLD: float = 0.30

# Severity label → score weight used in SRI calculation
_SEVERITY_WEIGHT: dict[str, float] = {
    "critical": 100.0,
    "high": 75.0,
    "medium": 40.0,
    "low": 10.0,
}

# Tags found in the incident dataset that signal a specific action type occurred.
_ACTION_TYPE_TAGS: dict[ActionType, set[str]] = {
    ActionType.RESTART_SERVICE: {"restart"},
    ActionType.DELETE_RESOURCE: {"deletion", "delete"},
    ActionType.MODIFY_NSG: {"nsg-change"},
    ActionType.SCALE_DOWN: {"scale-down"},
    ActionType.SCALE_UP: {"scale-up"},
    ActionType.UPDATE_CONFIG: {"config-change"},
    ActionType.CREATE_RESOURCE: set(),
}

# Dimension weights for mock-mode keyword similarity (must sum to 1.0)
_W_ACTION: float = 0.40
_W_RESOURCE_TYPE: float = 0.30
_W_RESOURCE_NAME: float = 0.20
_W_TAGS: float = 0.10

# Each incident beyond the first contributes this fraction of its weighted score
_SECONDARY_WEIGHT: float = 0.20

# System instructions for the framework agent (live mode only).
_AGENT_INSTRUCTIONS = """\
You are RuriSkry's Historical Pattern Governance Agent — an expert in incident
forensics with the authority to ADJUST historical risk scores.

## Your role
You receive a baseline score from incident similarity matching. You reason about
whether past incidents are truly relevant to this specific proposed action.

## Process
1. Call `evaluate_historical_rules` to get the baseline score and similar incidents.
2. Reason about the true relevance of each matched incident:
   - Is the matched incident about the SAME failure mode, or just superficially similar?
   - Has infrastructure changed since the incident (reducing relevance)?
   - Is the ops agent specifically trying to PREVENT a recurrence of the matched incident?
   - Is the similarity score inflated by keyword overlap without semantic relevance?
   - Would this action have AVOIDED the matched incident, making it a positive signal?
3. Call `submit_governance_decision` with your adjusted score and justification.

## Adjustment rules
- You may adjust the baseline score by at most +/-30 points
- If the ops agent is remediating the EXACT issue from a past incident, reduce score
- If past incidents are only superficially similar, reduce score
- If incidents are MORE relevant than the similarity score suggests, increase score
- Provide a specific reason for each adjustment
"""


# ---------------------------------------------------------------------------
# Agent
# ---------------------------------------------------------------------------


class HistoricalPatternAgent:
    """Matches proposed actions against past incident history.

    **Live mode**: delegates similarity search to Azure AI Search (BM25 full-text
    search on the ``incident-history`` index).  Results ranked by Azure Search
    are normalised to a [0, 0.8] similarity band so the SRI scoring formula
    stays consistent with mock mode.  A Microsoft Agent Framework agent then
    calls the evaluation tool and synthesises reasoning via GPT-4.1.

    **Mock mode**: loads incidents from a local JSON file and uses a four-
    dimensional keyword similarity algorithm (identical to original behaviour).

    Usage::

        agent = HistoricalPatternAgent()
        result: HistoricalResult = agent.evaluate(action)
        print(result.sri_historical, result.most_relevant_incident)
    """

    def __init__(
        self,
        incidents_path: str | Path | None = None,
        cfg=None,
    ) -> None:
        # Always load the local JSON for mock-mode fallback.
        path = Path(incidents_path) if incidents_path else _DEFAULT_INCIDENTS_PATH
        with open(path, encoding="utf-8") as fh:
            self._incidents: list[dict] = json.load(fh)

        # Azure AI Search client — live mode queries the cloud index.
        self._search = AzureSearchClient()

        self._cfg = cfg or _default_settings

        self._use_framework: bool = (
            not self._cfg.use_local_mocks
            and bool(self._cfg.azure_openai_endpoint)
        )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def evaluate(self, action: ProposedAction, force_deterministic: bool = False) -> HistoricalResult:
        """Match the proposed action against the incident history.

        Routes to the Microsoft Agent Framework agent in live mode, or to the
        deterministic rule-based engine in mock mode.

        Args:
            action: The proposed infrastructure action to evaluate.

        Returns:
            :class:`~src.core.models.HistoricalResult` containing:

            * ``sri_historical`` — 0–100 risk score
            * ``similar_incidents`` — incidents with similarity ≥ threshold
            * ``most_relevant_incident`` — top-similarity incident (or None)
            * ``recommended_procedure`` — lesson from the best match
            * ``reasoning`` — explanation (enriched by GPT-4.1 in live mode)
        """
        if not self._use_framework or force_deterministic:
            return self._evaluate_rules(action)

        try:
            return await self._evaluate_with_framework(action)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "HistoricalPatternAgent: framework call failed (%s) — falling back to rules.",
                exc,
            )
            return self._evaluate_rules(action)

    # ------------------------------------------------------------------
    # Microsoft Agent Framework path (live mode)
    # ------------------------------------------------------------------

    async def _evaluate_with_framework(self, action: ProposedAction) -> HistoricalResult:
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

        result_holder: list[HistoricalResult] = []
        llm_decision_holder: list[dict] = []

        @af.tool(
            name="evaluate_historical_rules",
            description=(
                "Search the incident history for similar past events and compute "
                "the SRI:Historical score. Returns a JSON object with sri_historical, "
                "similar_incidents, most_relevant_incident, recommended_procedure, "
                "and reasoning."
            ),
        )
        async def evaluate_historical_rules(action_json: str) -> str:
            """Match the action against historical incident records."""
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
            name="historical-pattern-analyst",
            instructions=_AGENT_INSTRUCTIONS,
            tools=[evaluate_historical_rules, submit_governance_decision],
        )

        from src.infrastructure.llm_throttle import run_with_throttle
        from src.governance_agents._llm_governance import parse_llm_decision

        prompt = (
            f"## Proposed Action\n{action.model_dump_json()}\n\n"
            f"## Ops Agent's Reasoning\n{action.reason}\n\n"
            "INSTRUCTIONS: First call evaluate_historical_rules to get the baseline score "
            "and similar incidents. Reason about whether each matched incident truly reflects "
            "the risk of this specific action given the ops agent's intent. "
            "Then call submit_governance_decision with your adjusted score and justification."
        )
        await run_with_throttle(agent.run, prompt)

        if result_holder:
            base = result_holder[-1]
            adjusted_score, adjustment_text, _ = parse_llm_decision(
                llm_decision_holder, base.sri_historical
            )
            return HistoricalResult(
                sri_historical=adjusted_score,
                similar_incidents=base.similar_incidents,
                most_relevant_incident=base.most_relevant_incident,
                recommended_procedure=base.recommended_procedure,
                reasoning=base.reasoning + adjustment_text,
            )

        # Tool was never called — return plain rule-based result (async to avoid blocking)
        return await self._evaluate_rules_async(action)

    # ------------------------------------------------------------------
    # Async rule-based evaluation (Phase 20 fix — avoids blocking event loop)
    # ------------------------------------------------------------------

    async def _evaluate_rules_async(self, action: ProposedAction) -> HistoricalResult:
        """Async variant of :meth:`_evaluate_rules` — offloads Azure AI Search I/O.

        In live mode, ``search_incidents()`` is a blocking network call. Running it
        directly on the event loop would block all concurrent ``asyncio.gather()``
        governance agents. We offload it to a thread pool via ``asyncio.to_thread()``.

        In mock mode, the evaluation is pure in-memory computation so we call
        ``_evaluate_rules()`` directly (no thread overhead needed).
        """
        import asyncio

        if not self._search.is_mock:
            # Live mode: Azure AI Search call — offload to thread pool
            return await asyncio.to_thread(self._evaluate_rules, action)
        # Mock mode: pure computation, no I/O — safe to call synchronously
        return self._evaluate_rules(action)

    # ------------------------------------------------------------------
    # Deterministic rule-based evaluation
    # ------------------------------------------------------------------

    def _evaluate_rules(self, action: ProposedAction) -> HistoricalResult:
        """Run the full deterministic historical pattern analysis."""
        if not self._search.is_mock:
            # ── Live mode: delegate to Azure AI Search ──────────────────────
            resource_name = action.target.resource_id.split("/")[-1]
            query = (
                f"{action.action_type.value} {action.target.resource_type} "
                f"{resource_name} {action.reason[:120]}"
            )
            raw_hits = self._search.search_incidents(
                query=query,
                action_type=action.action_type.value,
                resource_type=action.target.resource_type,
                top=5,
            )
            similar_incidents = self._hits_to_similar_incidents(raw_hits)
            logger.info(
                "HistoricalPatternAgent (Azure Search): action=%s hits=%d",
                action.action_type.value,
                len(similar_incidents),
            )
        else:
            # ── Mock mode: local JSON keyword similarity ──────────────────────
            scored: list[tuple[float, dict]] = []
            for incident in self._incidents:
                sim = self._compute_similarity(incident, action)
                if sim >= _SIMILARITY_THRESHOLD:
                    scored.append((sim, incident))
            scored.sort(key=lambda t: t[0], reverse=True)
            similar_incidents = [
                self._to_similar_incident(inc, sim) for sim, inc in scored
            ]
            logger.info(
                "HistoricalPatternAgent (mock): action=%s similar=%d",
                action.action_type.value,
                len(similar_incidents),
            )

        most_relevant = similar_incidents[0] if similar_incidents else None
        recommended_procedure = most_relevant.lesson if most_relevant else None
        sri = self._calculate_sri(similar_incidents)

        # Supplement with the system's own governance decision history.
        # This keeps the historical score stable across runs when Azure AI
        # Search query variance would otherwise drop it to 0.
        gov_boost, gov_reason = self._governance_history_boost(action)
        sri = min(sri + gov_boost, 100.0)

        logger.info(
            "HistoricalPatternAgent: resource_type=%s similar=%d "
            "score=%.1f (gov_boost=%d)",
            action.target.resource_type,
            len(similar_incidents),
            sri,
            gov_boost,
        )

        reasoning = self._build_reasoning(action, similar_incidents, sri)
        if gov_reason:
            reasoning = reasoning.rstrip() + f"\n\n{gov_reason}"

        return HistoricalResult(
            sri_historical=sri,
            similar_incidents=similar_incidents,
            most_relevant_incident=most_relevant,
            recommended_procedure=recommended_procedure,
            reasoning=reasoning,
        )

    # ------------------------------------------------------------------
    # Azure Search result conversion
    # ------------------------------------------------------------------

    def _hits_to_similar_incidents(self, hits: list[dict]) -> list[SimilarIncident]:
        """Convert Azure AI Search result dicts to typed ``SimilarIncident`` models.

        Azure Search returns a ``@search.score`` (BM25 float, unbounded above).
        We normalise it so the top result maps to 0.8 and others scale down
        proportionally.  A floor of ``_SIMILARITY_THRESHOLD`` (0.30) is applied
        so low-quality Azure Search matches are still filtered out.
        """
        if not hits:
            return []

        raw_scores = [h.get("@search.score", 1.0) for h in hits]
        max_score = max(raw_scores) if max(raw_scores) > 0 else 1.0

        incidents: list[SimilarIncident] = []
        for hit, raw_score in zip(hits, raw_scores):
            similarity = round(min((raw_score / max_score) * 0.8, 0.8), 2)
            if similarity >= _SIMILARITY_THRESHOLD:
                incidents.append(self._to_similar_incident(hit, similarity))

        return incidents

    # ------------------------------------------------------------------
    # Mock-mode similarity computation
    # ------------------------------------------------------------------

    def _compute_similarity(self, incident: dict, action: ProposedAction) -> float:
        """Score how similar a past incident is to the proposed action.

        Returns a float in [0.0, 1.0] as the weighted sum of four
        dimension scores.  Used only in mock mode.
        """
        score = 0.0

        # 1. Action type
        incident_action = incident.get("action_taken", "").split(":")[0]
        if incident_action == action.action_type.value:
            score += _W_ACTION

        # 2. Resource type
        if incident.get("resource_type") == action.target.resource_type:
            score += _W_RESOURCE_TYPE

        # 3. Resource name substring match
        target_name = action.target.resource_id.split("/")[-1].lower()
        if target_name and target_name in incident.get("action_taken", "").lower():
            score += _W_RESOURCE_NAME

        # 4. Tag relevance
        action_keywords = _ACTION_TYPE_TAGS.get(action.action_type, set())
        incident_tags = {t.lower() for t in incident.get("tags", [])}
        if action_keywords & incident_tags:
            score += _W_TAGS

        return round(score, 2)

    # ------------------------------------------------------------------
    # SRI scoring
    # ------------------------------------------------------------------

    def _calculate_sri(self, similar_incidents: list[SimilarIncident]) -> float:
        """Compute SRI:Historical (0–100) from the list of similar incidents.

        Primary signal is the best-matching incident.  Additional incidents
        contribute a 20 % diminishing-return bonus.
        """
        if not similar_incidents:
            return 0.0

        best = similar_incidents[0]
        score = (best.similarity_score or 0.0) * _SEVERITY_WEIGHT.get(
            best.severity, 0.0
        )

        for inc in similar_incidents[1:]:
            bonus = (
                (inc.similarity_score or 0.0)
                * _SEVERITY_WEIGHT.get(inc.severity, 0.0)
                * _SECONDARY_WEIGHT
            )
            score += bonus

        return round(min(score, 100.0), 2)

    # ------------------------------------------------------------------
    # Model conversion
    # ------------------------------------------------------------------

    @staticmethod
    def _to_similar_incident(incident: dict, similarity: float) -> SimilarIncident:
        """Convert a raw incident dict to a typed ``SimilarIncident``."""
        return SimilarIncident(
            incident_id=incident.get("incident_id", "unknown"),
            description=incident.get("description", ""),
            action_taken=incident.get("action_taken", ""),
            outcome=incident.get("outcome", ""),
            lesson=incident.get("lesson", ""),
            service=incident.get("service", ""),
            severity=incident.get("severity", "low"),
            date=incident.get("date", ""),
            similarity_score=similarity,
        )

    # ------------------------------------------------------------------
    # Governance history supplement (deterministic)
    # ------------------------------------------------------------------

    def _governance_history_boost(self, action: ProposedAction) -> tuple[int, str]:
        """Check the system's own governance decision history for this action_type.

        Azure AI Search results are non-deterministic (query text varies slightly
        per run, BM25 scores shift).  This supplement adds a *deterministic* boost
        based on how often the same action_type has been ESCALATED in recent
        governance decisions, so repeated violations keep a high historical score
        regardless of search query variance.

        Returns:
            (boost_points, reason_string)  — boost is already capped at _GOV_BOOST_CAP.
        """
        try:
            from src.core.decision_tracker import DecisionTracker  # noqa: PLC0415
            tracker = DecisionTracker()
            recent = tracker.get_recent(_GOV_HISTORY_LOOKBACK)
        except Exception as exc:  # noqa: BLE001
            logger.debug("HistoricalAgent: governance history unavailable — %s", exc)
            return 0, ""

        action_key = action.action_type.value
        escalated = 0
        approved = 0
        for rec in recent:
            if rec.get("action_type") != action_key:
                continue
            decision = rec.get("decision", "").upper()
            if decision == "ESCALATED":
                escalated += 1
            elif decision == "APPROVED":
                approved += 1

        boost = min(
            escalated * _GOV_BOOST_PER_ESCALATED + approved * _GOV_BOOST_PER_APPROVED,
            _GOV_BOOST_CAP,
        )

        if boost == 0:
            return 0, ""

        parts = []
        if escalated:
            parts.append(f"{escalated} escalation(s)")
        if approved:
            parts.append(f"{approved} prior approval(s)")
        reason = (
            f"Governance history: {', '.join(parts)} for '{action_key}' "
            f"in last {_GOV_HISTORY_LOOKBACK} decisions (+{boost} pts)"
        )
        logger.info("HistoricalAgent: governance boost +%d — %s", boost, reason)
        return boost, reason

    # ------------------------------------------------------------------
    # Reasoning
    # ------------------------------------------------------------------

    def _build_reasoning(
        self,
        action: ProposedAction,
        similar_incidents: list[SimilarIncident],
        score: float,
    ) -> str:
        """Build a human-readable explanation of the historical risk assessment."""
        backend = "Azure AI Search" if not self._search.is_mock else "local mock"

        if not similar_incidents:
            return (
                f"No historical incidents found matching "
                f"'{action.action_type.value}' on '{action.target.resource_type}' "
                f"(via {backend}). "
                "No historical risk signal — score is 0."
            )

        best = similar_incidents[0]
        desc_preview = best.description[:80] + (
            "..." if len(best.description) > 80 else ""
        )
        lines = [
            f"Found {len(similar_incidents)} similar historical incident(s) for "
            f"'{action.action_type.value}' on '{action.target.resource_type}' "
            f"(via {backend}).",
            f"Most relevant: {best.incident_id} "
            f"(similarity {best.similarity_score:.0%}, severity: {best.severity})"
            f' — "{desc_preview}"',
        ]

        if len(similar_incidents) > 1:
            other_ids = ", ".join(i.incident_id for i in similar_incidents[1:])
            lines.append(f"Additional precedents: {other_ids}.")

        lines.append(f"SRI:Historical score: {score:.1f}/100.")
        return "\n".join(lines)
