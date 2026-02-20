"""Tests for DecisionTracker — local JSON audit trail."""

import json
from pathlib import Path

import pytest

from src.core.decision_tracker import DecisionTracker
from src.core.models import ActionTarget, ActionType, ProposedAction, Urgency
from src.core.pipeline import SentinelLayerPipeline


# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def pipeline():
    """One pipeline for the whole module — agents load data once."""
    return SentinelLayerPipeline()


def _make_action(
    resource_id: str = "/subscriptions/demo/resourceGroups/prod"
    "/providers/Microsoft.Compute/virtualMachines/web-tier-01",
    resource_type: str = "Microsoft.Compute/virtualMachines",
    action_type: ActionType = ActionType.SCALE_UP,
    agent_id: str = "test-agent",
    reason: str = "Testing decision tracker.",
    current_monthly_cost: float | None = 420.0,
    current_sku: str | None = "Standard_D4s_v3",
    proposed_sku: str | None = "Standard_D8s_v3",
) -> ProposedAction:
    return ProposedAction(
        agent_id=agent_id,
        action_type=action_type,
        target=ActionTarget(
            resource_id=resource_id,
            resource_type=resource_type,
            current_monthly_cost=current_monthly_cost,
            current_sku=current_sku,
            proposed_sku=proposed_sku,
        ),
        reason=reason,
        urgency=Urgency.MEDIUM,
    )


@pytest.fixture()
def tracker(tmp_path):
    """Isolated tracker writing to a temp directory."""
    return DecisionTracker(decisions_dir=tmp_path / "decisions")


@pytest.fixture()
def verdict(pipeline):
    """One APPROVED verdict (scale_up web-tier) for reuse."""
    return pipeline.evaluate(_make_action())


# ---------------------------------------------------------------------------
# record()
# ---------------------------------------------------------------------------


class TestRecord:
    def test_creates_json_file(self, tracker, verdict):
        tracker.record(verdict)
        files = list(tracker._dir.glob("*.json"))
        assert len(files) == 1

    def test_filename_is_action_id(self, tracker, verdict):
        tracker.record(verdict)
        files = list(tracker._dir.glob("*.json"))
        assert files[0].stem == verdict.action_id

    def test_json_is_valid(self, tracker, verdict):
        tracker.record(verdict)
        path = tracker._dir / f"{verdict.action_id}.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        assert isinstance(data, dict)

    def test_required_fields_present(self, tracker, verdict):
        tracker.record(verdict)
        path = tracker._dir / f"{verdict.action_id}.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        required = {
            "action_id", "timestamp", "decision", "sri_composite",
            "sri_breakdown", "resource_id", "resource_type",
            "action_type", "agent_id", "action_reason",
            "verdict_reason", "violations",
        }
        assert required.issubset(data.keys())

    def test_decision_value_is_string(self, tracker, verdict):
        tracker.record(verdict)
        path = tracker._dir / f"{verdict.action_id}.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        assert data["decision"] in ("approved", "escalated", "denied")

    def test_sri_composite_is_float(self, tracker, verdict):
        tracker.record(verdict)
        path = tracker._dir / f"{verdict.action_id}.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        assert isinstance(data["sri_composite"], float)

    def test_sri_breakdown_has_four_dimensions(self, tracker, verdict):
        tracker.record(verdict)
        path = tracker._dir / f"{verdict.action_id}.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        bd = data["sri_breakdown"]
        assert set(bd.keys()) == {"infrastructure", "policy", "historical", "cost"}

    def test_violations_is_list(self, tracker, verdict):
        tracker.record(verdict)
        path = tracker._dir / f"{verdict.action_id}.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        assert isinstance(data["violations"], list)

    def test_multiple_records_create_multiple_files(self, tracker, pipeline):
        for _ in range(3):
            v = pipeline.evaluate(_make_action())
            tracker.record(v)
        files = list(tracker._dir.glob("*.json"))
        assert len(files) == 3

    def test_denied_verdict_has_violations(self, tracker, pipeline):
        """A DELETE on vm-23 should be DENIED with POL-DR-001 listed."""
        action = _make_action(
            resource_id=(
                "/subscriptions/demo/resourceGroups/prod"
                "/providers/Microsoft.Compute/virtualMachines/vm-23"
            ),
            action_type=ActionType.DELETE_RESOURCE,
            current_monthly_cost=847.0,
            current_sku=None,
            proposed_sku=None,
            reason="Delete idle VM",
        )
        v = pipeline.evaluate(action)
        tracker.record(v)
        path = tracker._dir / f"{v.action_id}.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        assert data["decision"] == "denied"
        assert len(data["violations"]) >= 1


# ---------------------------------------------------------------------------
# get_recent()
# ---------------------------------------------------------------------------


class TestGetRecent:
    def test_returns_list(self, tracker, verdict):
        tracker.record(verdict)
        result = tracker.get_recent()
        assert isinstance(result, list)

    def test_empty_tracker_returns_empty_list(self, tracker):
        assert tracker.get_recent() == []

    def test_respects_limit(self, tracker, pipeline):
        for _ in range(5):
            tracker.record(pipeline.evaluate(_make_action()))
        result = tracker.get_recent(limit=3)
        assert len(result) == 3

    def test_default_limit_is_10(self, tracker, pipeline):
        for _ in range(15):
            tracker.record(pipeline.evaluate(_make_action()))
        result = tracker.get_recent()
        assert len(result) == 10

    def test_newest_first(self, tracker, pipeline):
        """Timestamps should be in descending order."""
        for _ in range(3):
            tracker.record(pipeline.evaluate(_make_action()))
        results = tracker.get_recent()
        timestamps = [r["timestamp"] for r in results]
        assert timestamps == sorted(timestamps, reverse=True)

    def test_each_entry_has_action_id(self, tracker, verdict):
        tracker.record(verdict)
        results = tracker.get_recent()
        assert all("action_id" in r for r in results)


# ---------------------------------------------------------------------------
# get_by_resource()
# ---------------------------------------------------------------------------


class TestGetByResource:
    def test_filters_by_short_name(self, tracker, pipeline):
        v1 = pipeline.evaluate(_make_action(resource_id="/subscriptions/demo/.../vm-23"))
        v2 = pipeline.evaluate(_make_action(resource_id="/subscriptions/demo/.../web-tier-01"))
        tracker.record(v1)
        tracker.record(v2)
        results = tracker.get_by_resource("vm-23")
        assert len(results) == 1
        assert "vm-23" in results[0]["resource_id"]

    def test_no_match_returns_empty_list(self, tracker, verdict):
        tracker.record(verdict)
        results = tracker.get_by_resource("does-not-exist")
        assert results == []

    def test_respects_limit(self, tracker, pipeline):
        for _ in range(5):
            tracker.record(pipeline.evaluate(_make_action(resource_id="/sub/demo/vm/web-tier-01")))
        results = tracker.get_by_resource("web-tier-01", limit=2)
        assert len(results) == 2

    def test_newest_first(self, tracker, pipeline):
        for _ in range(3):
            tracker.record(pipeline.evaluate(_make_action()))
        results = tracker.get_by_resource("web-tier-01")
        timestamps = [r["timestamp"] for r in results]
        assert timestamps == sorted(timestamps, reverse=True)


# ---------------------------------------------------------------------------
# get_risk_profile()
# ---------------------------------------------------------------------------


class TestGetRiskProfile:
    def test_unknown_resource_returns_zero_profile(self, tracker):
        profile = tracker.get_risk_profile("unknown-resource")
        assert profile["total_evaluations"] == 0
        assert profile["avg_sri_composite"] is None
        assert profile["last_evaluated"] is None

    def test_profile_counts_evaluations(self, tracker, pipeline):
        for _ in range(3):
            tracker.record(pipeline.evaluate(_make_action()))
        profile = tracker.get_risk_profile("web-tier-01")
        assert profile["total_evaluations"] == 3

    def test_profile_has_correct_structure(self, tracker, pipeline):
        tracker.record(pipeline.evaluate(_make_action()))
        profile = tracker.get_risk_profile("web-tier-01")
        required = {
            "resource_id", "total_evaluations", "decisions",
            "avg_sri_composite", "max_sri_composite",
            "top_violations", "last_evaluated",
        }
        assert required.issubset(profile.keys())

    def test_decisions_dict_has_three_keys(self, tracker, pipeline):
        tracker.record(pipeline.evaluate(_make_action()))
        profile = tracker.get_risk_profile("web-tier-01")
        assert set(profile["decisions"].keys()) == {"approved", "escalated", "denied"}

    def test_avg_sri_is_float(self, tracker, pipeline):
        tracker.record(pipeline.evaluate(_make_action()))
        profile = tracker.get_risk_profile("web-tier-01")
        assert isinstance(profile["avg_sri_composite"], float)

    def test_last_evaluated_is_string(self, tracker, pipeline):
        tracker.record(pipeline.evaluate(_make_action()))
        profile = tracker.get_risk_profile("web-tier-01")
        assert isinstance(profile["last_evaluated"], str)

    def test_denied_resource_violations_tracked(self, tracker, pipeline):
        """Deleting vm-23 violates POL-DR-001; it should appear in top_violations."""
        action = _make_action(
            resource_id=(
                "/subscriptions/demo/resourceGroups/prod"
                "/providers/Microsoft.Compute/virtualMachines/vm-23"
            ),
            action_type=ActionType.DELETE_RESOURCE,
            current_monthly_cost=847.0,
            current_sku=None,
            proposed_sku=None,
            reason="Delete idle VM",
        )
        tracker.record(pipeline.evaluate(action))
        profile = tracker.get_risk_profile("vm-23")
        assert "POL-DR-001" in profile["top_violations"]
