"""Tests for the FastAPI dashboard endpoints."""

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from src.api.dashboard_api import app, _get_tracker
from src.core.decision_tracker import DecisionTracker
from src.core.models import ActionTarget, ActionType, ProposedAction, Urgency
from src.core.pipeline import SentinelLayerPipeline


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_action(
    resource_id: str = (
        "/subscriptions/demo/resourceGroups/prod"
        "/providers/Microsoft.Compute/virtualMachines/web-tier-01"
    ),
    action_type: ActionType = ActionType.SCALE_UP,
    current_monthly_cost: float | None = 420.0,
    current_sku: str | None = "Standard_D4s_v3",
    proposed_sku: str | None = "Standard_D8s_v3",
) -> ProposedAction:
    return ProposedAction(
        agent_id="test-agent",
        action_type=action_type,
        target=ActionTarget(
            resource_id=resource_id,
            resource_type="Microsoft.Compute/virtualMachines",
            current_monthly_cost=current_monthly_cost,
            current_sku=current_sku,
            proposed_sku=proposed_sku,
        ),
        reason="Dashboard API test.",
        urgency=Urgency.MEDIUM,
    )


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def pipeline():
    return SentinelLayerPipeline()


@pytest.fixture()
def client(tmp_path, monkeypatch):
    """Return a TestClient wired to a fresh temp-dir tracker."""
    tracker = DecisionTracker(decisions_dir=tmp_path / "decisions")

    # Replace the singleton in the API module so endpoints use our tracker.
    import src.api.dashboard_api as api_module
    monkeypatch.setattr(api_module, "_tracker", tracker)

    return TestClient(app)


@pytest.fixture()
async def populated_client(client, pipeline):
    """A client whose tracker already has 3 approved + 1 denied decision."""
    import src.api.dashboard_api as api_module
    tracker = api_module._tracker

    # 3 scale-up (approved)
    for _ in range(3):
        tracker.record(await pipeline.evaluate(_make_action()))

    # 1 delete on vm-23 (denied — violates POL-DR-001)
    tracker.record(
        await pipeline.evaluate(
            _make_action(
                resource_id=(
                    "/subscriptions/demo/resourceGroups/prod"
                    "/providers/Microsoft.Compute/virtualMachines/vm-23"
                ),
                action_type=ActionType.DELETE_RESOURCE,
                current_monthly_cost=847.0,
                current_sku=None,
                proposed_sku=None,
            )
        )
    )
    return client


# ---------------------------------------------------------------------------
# GET /api/evaluations
# ---------------------------------------------------------------------------


class TestListEvaluations:
    def test_returns_200(self, client):
        response = client.get("/api/evaluations")
        assert response.status_code == 200

    def test_empty_returns_zero_count(self, client):
        data = client.get("/api/evaluations").json()
        assert data["count"] == 0
        assert data["evaluations"] == []

    def test_returns_correct_count(self, populated_client):
        data = populated_client.get("/api/evaluations").json()
        assert data["count"] == 4

    def test_respects_limit_param(self, populated_client):
        data = populated_client.get("/api/evaluations?limit=2").json()
        assert data["count"] == 2
        assert len(data["evaluations"]) == 2

    def test_limit_max_is_100(self, client):
        response = client.get("/api/evaluations?limit=200")
        assert response.status_code == 422  # FastAPI validation error

    def test_limit_min_is_1(self, client):
        response = client.get("/api/evaluations?limit=0")
        assert response.status_code == 422

    def test_resource_id_filter(self, populated_client):
        data = populated_client.get("/api/evaluations?resource_id=vm-23").json()
        assert data["count"] == 1
        assert "vm-23" in data["evaluations"][0]["resource_id"]

    def test_resource_id_filter_no_match(self, populated_client):
        data = populated_client.get(
            "/api/evaluations?resource_id=does-not-exist"
        ).json()
        assert data["count"] == 0

    def test_each_evaluation_has_action_id(self, populated_client):
        data = populated_client.get("/api/evaluations").json()
        assert all("action_id" in e for e in data["evaluations"])


# ---------------------------------------------------------------------------
# GET /api/evaluations/{id}
# ---------------------------------------------------------------------------


class TestGetEvaluation:

    def test_returns_404_for_unknown_id(self, client):
        response = client.get("/api/evaluations/nonexistent-id")
        assert response.status_code == 404


    def test_returns_200_for_known_id(self, populated_client):
        # Get a known ID from the list
        first = populated_client.get("/api/evaluations").json()["evaluations"][0]
        action_id = first["action_id"]
        response = populated_client.get(f"/api/evaluations/{action_id}")
        assert response.status_code == 200


    def test_returns_correct_record(self, populated_client):
        first = populated_client.get("/api/evaluations").json()["evaluations"][0]
        action_id = first["action_id"]
        detail = populated_client.get(f"/api/evaluations/{action_id}").json()
        assert detail["action_id"] == action_id


    def test_record_has_required_fields(self, populated_client):
        first = populated_client.get("/api/evaluations").json()["evaluations"][0]
        action_id = first["action_id"]
        detail = populated_client.get(f"/api/evaluations/{action_id}").json()
        required = {
            "action_id", "timestamp", "decision", "sri_composite",
            "sri_breakdown", "resource_id", "action_type",
        }
        assert required.issubset(detail.keys())


# ---------------------------------------------------------------------------
# GET /api/metrics
# ---------------------------------------------------------------------------


class TestGetMetrics:

    def test_returns_200(self, client):
        assert client.get("/api/metrics").status_code == 200


    def test_empty_metrics_structure(self, client):
        data = client.get("/api/metrics").json()
        assert data["total_evaluations"] == 0
        assert data["sri_composite"]["avg"] is None
        assert data["top_violations"] == []


    def test_total_evaluations_correct(self, populated_client):
        data = populated_client.get("/api/metrics").json()
        assert data["total_evaluations"] == 4


    def test_decisions_count_correct(self, populated_client):
        data = populated_client.get("/api/metrics").json()
        decisions = data["decisions"]
        assert decisions["denied"] == 1
        assert decisions["approved"] + decisions["escalated"] + decisions["denied"] == 4


    def test_decision_percentages_sum_to_100(self, populated_client):
        data = populated_client.get("/api/metrics").json()
        pcts = data["decision_percentages"]
        total = pcts["approved"] + pcts["escalated"] + pcts["denied"]
        assert abs(total - 100.0) < 0.2  # floating point tolerance


    def test_sri_composite_fields_present(self, populated_client):
        data = populated_client.get("/api/metrics").json()
        sri = data["sri_composite"]
        assert "avg" in sri and "min" in sri and "max" in sri


    def test_sri_composite_avg_is_float(self, populated_client):
        data = populated_client.get("/api/metrics").json()
        assert isinstance(data["sri_composite"]["avg"], float)


    def test_sri_composite_max_gte_min(self, populated_client):
        data = populated_client.get("/api/metrics").json()
        assert data["sri_composite"]["max"] >= data["sri_composite"]["min"]


    def test_sri_dimensions_has_four_keys(self, populated_client):
        data = populated_client.get("/api/metrics").json()
        dims = data["sri_dimensions"]
        assert set(dims.keys()) == {
            "avg_infrastructure", "avg_policy", "avg_historical", "avg_cost"
        }


    def test_top_violations_is_list(self, populated_client):
        data = populated_client.get("/api/metrics").json()
        assert isinstance(data["top_violations"], list)


    def test_denied_decision_populates_violations(self, populated_client):
        data = populated_client.get("/api/metrics").json()
        policy_ids = [v["policy_id"] for v in data["top_violations"]]
        assert "POL-DR-001" in policy_ids


    def test_most_evaluated_resources_is_list(self, populated_client):
        data = populated_client.get("/api/metrics").json()
        assert isinstance(data["most_evaluated_resources"], list)


    def test_most_evaluated_resources_have_count(self, populated_client):
        data = populated_client.get("/api/metrics").json()
        for entry in data["most_evaluated_resources"]:
            assert "resource_id" in entry and "count" in entry


# ---------------------------------------------------------------------------
# GET /api/resources/{id}/risk
# ---------------------------------------------------------------------------


class TestGetResourceRisk:
    def test_returns_404_for_unknown_resource(self, client):
        response = client.get("/api/resources/does-not-exist/risk")
        assert response.status_code == 404

    def test_returns_200_for_known_resource(self, populated_client):
        response = populated_client.get("/api/resources/web-tier-01/risk")
        assert response.status_code == 200

    def test_profile_has_required_fields(self, populated_client):
        data = populated_client.get("/api/resources/web-tier-01/risk").json()
        required = {
            "resource_id", "total_evaluations", "decisions",
            "avg_sri_composite", "max_sri_composite",
            "top_violations", "last_evaluated",
        }
        assert required.issubset(data.keys())

    def test_profile_total_evaluations_correct(self, populated_client):
        data = populated_client.get("/api/resources/web-tier-01/risk").json()
        assert data["total_evaluations"] == 3

    def test_vm23_profile_has_violations(self, populated_client):
        data = populated_client.get("/api/resources/vm-23/risk").json()
        assert "POL-DR-001" in data["top_violations"]

    def test_last_evaluated_is_string(self, populated_client):
        data = populated_client.get("/api/resources/web-tier-01/risk").json()
        assert isinstance(data["last_evaluated"], str)
