"""Historical Pattern Agent — SRI:Historical dimension.

Matches a proposed action against past incident history.

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

from src.core.models import (
    ActionType,
    HistoricalResult,
    ProposedAction,
    SimilarIncident,
)
from src.infrastructure.openai_client import AzureOpenAIClient
from src.infrastructure.search_client import AzureSearchClient

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


# ---------------------------------------------------------------------------
# Agent
# ---------------------------------------------------------------------------


class HistoricalPatternAgent:
    """Matches proposed actions against past incident history.

    **Live mode**: delegates similarity search to Azure AI Search (BM25 full-text
    search on the ``incident-history`` index).  Results ranked by Azure Search
    are normalised to a [0, 0.8] similarity band so the SRI scoring formula
    stays consistent with mock mode.

    **Mock mode**: loads incidents from a local JSON file and uses a four-
    dimensional keyword similarity algorithm (identical to original behaviour).

    In both modes GPT-4.1 is optionally called (when Foundry credentials are
    available) to augment the rule-based ``reasoning`` string with an expert
    LLM analysis.

    Usage::

        agent = HistoricalPatternAgent()
        result: HistoricalResult = agent.evaluate(action)
        print(result.sri_historical, result.most_relevant_incident)
    """

    def __init__(self, incidents_path: str | Path | None = None) -> None:
        # Always load the local JSON for mock-mode fallback.
        # In live mode this data is not used — Azure Search takes over.
        path = Path(incidents_path) if incidents_path else _DEFAULT_INCIDENTS_PATH
        with open(path, encoding="utf-8") as fh:
            self._incidents: list[dict] = json.load(fh)

        # Azure AI Search client — live mode queries the cloud index.
        # Falls back to mock automatically if credentials are unavailable.
        self._search = AzureSearchClient()

        # LLM client — enriches reasoning with GPT-4.1 analysis in live mode.
        self._llm = AzureOpenAIClient()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def evaluate(self, action: ProposedAction) -> HistoricalResult:
        """Match the proposed action against the incident history.

        Routes to Azure AI Search in live mode or local keyword matching in
        mock mode, then applies the same SRI scoring formula in both paths.

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
        if not self._search.is_mock:
            # ── Live mode: delegate to Azure AI Search ──────────────────────
            # Build a rich query string combining action type, resource type,
            # resource name, and the agent's stated reason so BM25 can rank
            # on the most discriminating terms.
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

        logger.info(
            "HistoricalPatternAgent: resource_type=%s similar=%d score=%.1f",
            action.target.resource_type,
            len(similar_incidents),
            sri,
        )

        reasoning = self._build_reasoning(action, similar_incidents, sri)

        # Augment rule-based reasoning with GPT-4.1 analysis in live mode
        if not self._llm.is_mock:
            best = most_relevant
            llm_text = self._llm.analyze(
                action_context=(
                    f"Action: {action.action_type.value} on '{action.target.resource_type}'\n"
                    f"Similar historical incidents found: {len(similar_incidents)}\n"
                    f"Most relevant: {best.incident_id if best else 'none'} "
                    f"(severity: {best.severity if best else 'N/A'}, "
                    f"similarity: {best.similarity_score if best else 0:.0%})\n"
                    f"SRI:Historical score: {sri:.1f}/100\n"
                    f"Agent reason: {action.reason}"
                ),
                agent_role="historical incident pattern analyst",
            )
            reasoning = reasoning + "\n\nGPT-4.1 Analysis: " + llm_text

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

        Args:
            hits: Raw result dicts from ``AzureSearchClient.search_incidents()``.

        Returns:
            List of ``SimilarIncident`` objects, in relevance order.
        """
        if not hits:
            return []

        raw_scores = [h.get("@search.score", 1.0) for h in hits]
        max_score = max(raw_scores) if max(raw_scores) > 0 else 1.0

        incidents: list[SimilarIncident] = []
        for hit, raw_score in zip(hits, raw_scores):
            # Map BM25 score to [0, 0.8] — preserving relative ranking.
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
