"""Historical Pattern Agent — SRI:Historical dimension.

Matches a proposed action against a local incident history loaded from
``data/seed_incidents.json`` using rule-based similarity scoring.

This is the local-mock implementation.  In production, similarity search
would be delegated to Azure AI Search with vector embeddings.  The scoring
contract and output model are identical — only the retrieval backend changes.

Similarity dimensions (weights sum to 1.0)
-------------------------------------------
1. **Action type match** (0.40) — the incident's recorded action prefix equals
   the proposed action type.  *What you do* carries the most weight because the
   same class of action (delete, restart, modify-NSG) tends to produce the same
   class of failure.
2. **Resource type match** (0.30) — the Azure resource category matches.
   A past delete of *any* storage account is partially relevant to a new delete
   of a different storage account.
3. **Resource name match** (0.20) — the specific resource name appears in the
   incident's ``action_taken`` string.  Exact resource matches are higher signal.
4. **Tag relevance** (0.10) — a keyword associated with the proposed action type
   appears in the incident's tag list, catching partial matches missed above.

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
# Used for the tag-relevance dimension of similarity scoring.
_ACTION_TYPE_TAGS: dict[ActionType, set[str]] = {
    ActionType.RESTART_SERVICE: {"restart"},
    ActionType.DELETE_RESOURCE: {"deletion", "delete"},
    ActionType.MODIFY_NSG: {"nsg-change"},
    ActionType.SCALE_DOWN: {"scale-down"},
    ActionType.SCALE_UP: {"scale-up"},
    ActionType.UPDATE_CONFIG: {"config-change"},
    ActionType.CREATE_RESOURCE: set(),
}

# Dimension weights (must sum to 1.0)
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
    """Matches proposed actions against a local incident history.

    Loads incidents from a JSON file (mock for Azure AI Search vector index),
    scores each incident's similarity to the proposed action across four
    dimensions, and computes an SRI:Historical score (0–100).

    Usage::

        agent = HistoricalPatternAgent()
        result: HistoricalResult = agent.evaluate(action)
        print(result.sri_historical, result.most_relevant_incident)
    """

    def __init__(self, incidents_path: str | Path | None = None) -> None:
        path = Path(incidents_path) if incidents_path else _DEFAULT_INCIDENTS_PATH
        with open(path, encoding="utf-8") as fh:
            self._incidents: list[dict] = json.load(fh)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def evaluate(self, action: ProposedAction) -> HistoricalResult:
        """Match the proposed action against the incident history.

        Args:
            action: The proposed infrastructure action to evaluate.

        Returns:
            :class:`~src.core.models.HistoricalResult` containing:

            * ``sri_historical`` — 0–100 risk score
            * ``similar_incidents`` — all incidents with similarity ≥ threshold,
              sorted by similarity descending
            * ``most_relevant_incident`` — the top-similarity incident (or None)
            * ``recommended_procedure`` — the lesson from the best-matching
              incident, surfaced for human reviewers
            * ``reasoning`` — human-readable explanation of the score
        """
        # Score every incident; keep only those above the threshold
        scored: list[tuple[float, dict]] = []
        for incident in self._incidents:
            sim = self._compute_similarity(incident, action)
            if sim >= _SIMILARITY_THRESHOLD:
                scored.append((sim, incident))

        # Sort highest similarity first
        scored.sort(key=lambda t: t[0], reverse=True)

        similar_incidents: list[SimilarIncident] = [
            self._to_similar_incident(incident, sim)
            for sim, incident in scored
        ]

        most_relevant = similar_incidents[0] if similar_incidents else None
        recommended_procedure = most_relevant.lesson if most_relevant else None
        sri = self._calculate_sri(similar_incidents)

        logger.info(
            "HistoricalPatternAgent: action=%s resource_type=%s similar=%d score=%.1f",
            action.action_type.value,
            action.target.resource_type,
            len(similar_incidents),
            sri,
        )

        return HistoricalResult(
            sri_historical=sri,
            similar_incidents=similar_incidents,
            most_relevant_incident=most_relevant,
            recommended_procedure=recommended_procedure,
            reasoning=self._build_reasoning(action, similar_incidents, sri),
        )

    # ------------------------------------------------------------------
    # Similarity computation
    # ------------------------------------------------------------------

    def _compute_similarity(self, incident: dict, action: ProposedAction) -> float:
        """Score how similar a past incident is to the proposed action.

        Returns a float in [0.0, 1.0] as the weighted sum of four
        dimension scores.  See module docstring for full description.
        """
        score = 0.0

        # 1. Action type — incidents store "action_type:resource:..." so we
        #    parse just the first segment before the first colon.
        incident_action = incident.get("action_taken", "").split(":")[0]
        if incident_action == action.action_type.value:
            score += _W_ACTION

        # 2. Resource type — exact Azure resource category match
        if incident.get("resource_type") == action.target.resource_type:
            score += _W_RESOURCE_TYPE

        # 3. Resource name — the last path segment of the Azure resource ID,
        #    checked as a substring of the incident's action string.
        target_name = action.target.resource_id.split("/")[-1].lower()
        if target_name and target_name in incident.get("action_taken", "").lower():
            score += _W_RESOURCE_NAME

        # 4. Tag relevance — action-type keywords appear in incident tags
        action_keywords = _ACTION_TYPE_TAGS.get(action.action_type, set())
        incident_tags = {t.lower() for t in incident.get("tags", [])}
        if action_keywords & incident_tags:  # non-empty set intersection
            score += _W_TAGS

        return round(score, 2)

    # ------------------------------------------------------------------
    # SRI scoring
    # ------------------------------------------------------------------

    def _calculate_sri(self, similar_incidents: list[SimilarIncident]) -> float:
        """Compute SRI:Historical (0–100) from the list of similar incidents.

        The best-matching incident is the primary risk signal.  Each additional
        similar incident contributes a 20% bonus (diminishing returns prevent
        score inflation from many low-quality matches).

        The list is expected to already be sorted by similarity descending.
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
        """Convert a raw incident dict to a typed :class:`SimilarIncident`."""
        return SimilarIncident(
            incident_id=incident["incident_id"],
            description=incident["description"],
            action_taken=incident["action_taken"],
            outcome=incident["outcome"],
            lesson=incident["lesson"],
            service=incident["service"],
            severity=incident["severity"],
            date=incident["date"],
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
        if not similar_incidents:
            return (
                f"No historical incidents found matching "
                f"'{action.action_type.value}' on '{action.target.resource_type}'. "
                "No historical risk signal — score is 0."
            )

        best = similar_incidents[0]
        desc_preview = best.description[:80] + (
            "..." if len(best.description) > 80 else ""
        )
        lines = [
            f"Found {len(similar_incidents)} similar historical incident(s) for "
            f"'{action.action_type.value}' on '{action.target.resource_type}'.",
            f"Most relevant: {best.incident_id} "
            f"(similarity {best.similarity_score:.0%}, severity: {best.severity})"
            f' — "{desc_preview}"',
        ]

        if len(similar_incidents) > 1:
            other_ids = ", ".join(i.incident_id for i in similar_incidents[1:])
            lines.append(f"Additional precedents: {other_ids}.")

        lines.append(f"SRI:Historical score: {score:.1f}/100.")
        return "\n".join(lines)
