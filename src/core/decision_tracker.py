"""Decision Lineage Tracker — audit trail backed by Cosmos DB or local JSON.

Routes all storage through ``CosmosDecisionClient`` which already handles
the mock/live split using the same env → Key Vault → mock-fallback pattern
as every other infrastructure client.

* **Live mode** (USE_LOCAL_MOCKS=false + Cosmos credentials available):
  Writes and queries the ``governance-decisions`` container in Azure Cosmos DB.

* **Mock mode** (USE_LOCAL_MOCKS=true or credentials missing):
  Falls back to flat JSON files under ``data/decisions/`` — identical to
  the original standalone behaviour.

API
---
    tracker = DecisionTracker()
    tracker.record(verdict)               # write one verdict
    tracker.get_recent(limit=10)          # newest-first list of dicts
    tracker.get_by_resource("vm-23")      # decisions for one resource
    tracker.get_risk_profile("vm-23")     # aggregated stats for one resource
"""

import logging
from pathlib import Path

from src.core.models import GovernanceVerdict
from src.infrastructure.cosmos_client import CosmosDecisionClient

logger = logging.getLogger(__name__)


class DecisionTracker:
    """Writes and queries governance verdicts via ``CosmosDecisionClient``.

    ``CosmosDecisionClient`` owns the storage logic — this class is responsible
    for converting ``GovernanceVerdict`` Pydantic objects to plain dicts and
    for the higher-level ``get_risk_profile()`` aggregation.

    Args:
        decisions_dir: Override the local JSON directory (used in tests to
            write to a temp directory instead of ``data/decisions/``).
            Passed through to ``CosmosDecisionClient`` unchanged.
    """

    def __init__(self, decisions_dir: Path | None = None) -> None:
        self._cosmos = CosmosDecisionClient(decisions_dir=decisions_dir)
        mode = "LIVE (Cosmos DB)" if not self._cosmos.is_mock else "MOCK (local JSON)"
        logger.info("DecisionTracker initialised — storage: %s", mode)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def record(self, verdict: GovernanceVerdict) -> None:
        """Persist a governance verdict to Cosmos DB (or JSON in mock mode).

        Converts the Pydantic ``GovernanceVerdict`` to a flat dict, adds the
        ``id`` field required by Cosmos DB (set to ``action_id``), then
        delegates to ``CosmosDecisionClient.upsert()``.

        Args:
            verdict: The :class:`~src.core.models.GovernanceVerdict` returned
                by ``SentinelLayerPipeline.evaluate()``.
        """
        record = self._verdict_to_dict(verdict)
        # Cosmos DB requires an "id" field as the document key.
        # We set it to action_id so Cosmos uses the same identifier as our
        # local JSON files (backwards-compatible).
        record["id"] = record["action_id"]
        self._cosmos.upsert(record)
        logger.info(
            "DecisionTracker: recorded %s -> %s (SRI %.1f)",
            verdict.proposed_action.action_type.value,
            verdict.decision.value,
            verdict.sentinel_risk_index.sri_composite,
        )

    def get_recent(self, limit: int = 10) -> list[dict]:
        """Return the most recent ``limit`` decisions, newest first.

        Args:
            limit: Maximum number of records to return.

        Returns:
            List of dicts, each representing one governance verdict.
        """
        return self._cosmos.get_recent(limit)

    def get_by_resource(self, resource_id: str, limit: int = 10) -> list[dict]:
        """Return decisions for a specific resource, newest first.

        Args:
            resource_id: Full or partial Azure resource ID / short name.
            limit: Maximum number of records to return.

        Returns:
            Filtered list of dicts, newest first, at most ``limit`` entries.
        """
        return self._cosmos.get_by_resource(resource_id, limit)

    def get_risk_profile(self, resource_id: str) -> dict:
        """Return an aggregated risk summary for a resource.

        Analyses all historical decisions for the given resource and returns
        summary statistics: decision counts, average SRI composite, most
        common violations, and the most recent decision.

        Args:
            resource_id: Full or partial Azure resource ID / short name.

        Returns:
            Dict with keys: ``resource_id``, ``total_evaluations``,
            ``decisions`` (counts per outcome), ``avg_sri_composite``,
            ``max_sri_composite``, ``top_violations`` (list of policy IDs,
            ordered by frequency), ``last_evaluated``.
            Returns an empty profile dict if no decisions found.
        """
        records = self.get_by_resource(resource_id, limit=1000)
        if not records:
            return {
                "resource_id": resource_id,
                "total_evaluations": 0,
                "decisions": {"approved": 0, "escalated": 0, "denied": 0},
                "avg_sri_composite": None,
                "max_sri_composite": None,
                "top_violations": [],
                "last_evaluated": None,
            }

        # Decision counts
        counts: dict[str, int] = {"approved": 0, "escalated": 0, "denied": 0}
        for r in records:
            decision = r.get("decision", "").lower()
            if decision in counts:
                counts[decision] += 1

        # SRI composite stats
        composites = [
            r["sri_composite"] for r in records if "sri_composite" in r
        ]
        avg_composite = round(sum(composites) / len(composites), 2) if composites else None
        max_composite = round(max(composites), 2) if composites else None

        # Violation frequency
        violation_freq: dict[str, int] = {}
        for r in records:
            for pol_id in r.get("violations", []):
                violation_freq[pol_id] = violation_freq.get(pol_id, 0) + 1
        top_violations = sorted(
            violation_freq, key=violation_freq.get, reverse=True  # type: ignore[arg-type]
        )[:5]

        return {
            "resource_id": resource_id,
            "total_evaluations": len(records),
            "decisions": counts,
            "avg_sri_composite": avg_composite,
            "max_sri_composite": max_composite,
            "top_violations": top_violations,
            "last_evaluated": records[0].get("timestamp") if records else None,
        }

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _verdict_to_dict(self, verdict: GovernanceVerdict) -> dict:
        """Flatten a GovernanceVerdict into a simple dict for storage."""
        action = verdict.proposed_action
        sri = verdict.sentinel_risk_index

        # Extract violation policy IDs from agent_results (list of dicts)
        policy_data = verdict.agent_results.get("policy", {})
        violations = [
            v["policy_id"]
            for v in policy_data.get("violations", [])
        ]

        return {
            "action_id": verdict.action_id,
            "timestamp": verdict.timestamp.isoformat(),
            "decision": verdict.decision.value,
            "sri_composite": sri.sri_composite,
            "sri_breakdown": {
                "infrastructure": sri.sri_infrastructure,
                "policy": sri.sri_policy,
                "historical": sri.sri_historical,
                "cost": sri.sri_cost,
            },
            "resource_id": action.target.resource_id,
            "resource_type": action.target.resource_type,
            "action_type": action.action_type.value,
            "agent_id": action.agent_id,
            "action_reason": action.reason,
            "verdict_reason": verdict.reason,
            "violations": violations,
        }
