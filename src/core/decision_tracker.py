"""Decision Lineage Tracker — local JSON audit trail (mock for Cosmos DB).

Records every SRI(tm) governance verdict to a flat-file store under
``data/decisions/``.  Each decision is written as a single JSON file named
``{action_id}.json``.  This mimics the Cosmos DB audit trail described in the
project plan without requiring an Azure connection during development.

API
---
    tracker = DecisionTracker()
    tracker.record(verdict)               # write one verdict
    tracker.get_recent(limit=10)          # newest-first list of dicts
    tracker.get_by_resource("vm-23")      # decisions for one resource
    tracker.get_risk_profile("vm-23")     # aggregated stats for one resource
"""

import json
import logging
from datetime import datetime
from pathlib import Path

from src.core.models import GovernanceVerdict

logger = logging.getLogger(__name__)

_DEFAULT_DECISIONS_DIR = (
    Path(__file__).parent.parent.parent / "data" / "decisions"
)


class DecisionTracker:
    """Writes and queries governance verdicts as JSON files.

    Each call to ``record()`` creates one file:
    ``data/decisions/{action_id}.json``.

    The file contains a flat dict with all fields needed for audit,
    dashboards, and the MCP query tools.

    Args:
        decisions_dir: Override the storage directory (used in tests).
    """

    def __init__(self, decisions_dir: Path | None = None) -> None:
        self._dir = decisions_dir or _DEFAULT_DECISIONS_DIR
        self._dir.mkdir(parents=True, exist_ok=True)
        logger.info("DecisionTracker initialised — storage: %s", self._dir)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def record(self, verdict: GovernanceVerdict) -> None:
        """Persist a governance verdict to disk.

        Converts the Pydantic ``GovernanceVerdict`` into a flat dict and
        writes it to ``{decisions_dir}/{action_id}.json``.

        Args:
            verdict: The :class:`~src.core.models.GovernanceVerdict` returned
                by ``SentinelLayerPipeline.evaluate()``.
        """
        record = self._verdict_to_dict(verdict)
        path = self._dir / f"{verdict.action_id}.json"
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(record, fh, indent=2)
        logger.info(
            "DecisionTracker: recorded %s -> %s (SRI %.1f)",
            verdict.proposed_action.action_type.value,
            verdict.decision.value,
            verdict.sentinel_risk_index.sri_composite,
        )

    def get_recent(self, limit: int = 10) -> list[dict]:
        """Return the most recent ``limit`` decisions, newest first.

        Reads all JSON files in the decisions directory, sorts by the
        ``timestamp`` field (ISO 8601 string — lexicographic sort works
        correctly for ISO timestamps), and returns the last ``limit`` records.

        Args:
            limit: Maximum number of records to return.

        Returns:
            List of dicts, each representing one governance verdict.
        """
        records = self._load_all()
        records.sort(key=lambda r: r.get("timestamp", ""), reverse=True)
        return records[:limit]

    def get_by_resource(self, resource_id: str, limit: int = 10) -> list[dict]:
        """Return decisions for a specific resource, newest first.

        Matches any decision where the ``resource_id`` field contains
        ``resource_id`` as a substring (handles both short names like
        ``"vm-23"`` and full Azure IDs).

        Args:
            resource_id: Full or partial Azure resource ID / short name.
            limit: Maximum number of records to return.

        Returns:
            Filtered list of dicts, newest first, at most ``limit`` entries.
        """
        all_records = self._load_all()
        matched = [
            r for r in all_records
            if resource_id in r.get("resource_id", "")
        ]
        matched.sort(key=lambda r: r.get("timestamp", ""), reverse=True)
        return matched[:limit]

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
        """Flatten a GovernanceVerdict into a simple dict for JSON storage."""
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

    def _load_all(self) -> list[dict]:
        """Load every JSON file from the decisions directory."""
        records: list[dict] = []
        for path in self._dir.glob("*.json"):
            try:
                with open(path, encoding="utf-8") as fh:
                    records.append(json.load(fh))
            except (json.JSONDecodeError, OSError) as exc:
                logger.warning("DecisionTracker: skipping %s (%s)", path.name, exc)
        return records
