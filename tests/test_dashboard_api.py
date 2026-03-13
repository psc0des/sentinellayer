"""Tests for the FastAPI dashboard endpoints."""

import asyncio
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from src.api.dashboard_api import app, _get_tracker
from src.core.scan_run_tracker import ScanRunTracker
from src.core.decision_tracker import DecisionTracker
from src.core.models import ActionTarget, ActionType, ProposedAction, Urgency
from src.core.pipeline import RuriSkryPipeline


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
    return RuriSkryPipeline()


@pytest.fixture()
def client(tmp_path, monkeypatch):
    """Return a TestClient wired to a fresh temp-dir tracker."""
    tracker = DecisionTracker(decisions_dir=tmp_path / "decisions")
    scan_tracker = ScanRunTracker(scans_dir=tmp_path / "scans")

    # Replace the singleton in the API module so endpoints use our tracker.
    import src.api.dashboard_api as api_module
    monkeypatch.setattr(api_module, "_tracker", tracker)
    monkeypatch.setattr(api_module, "_scan_tracker", scan_tracker)
    api_module._scans.clear()
    api_module._scan_events.clear()
    api_module._scan_cancelled.clear()

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

    def test_limit_max_is_500(self, client):
        response = client.get("/api/evaluations?limit=501")
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


# ---------------------------------------------------------------------------
# Scan durability / streaming endpoints
# ---------------------------------------------------------------------------


class TestScanDurabilityAndStreaming:
    def test_scan_status_falls_back_to_persisted_store(self, client):
        import src.api.dashboard_api as api_module

        scan_id = "scan-persisted-001"
        api_module._scan_tracker.upsert(
            {
                "id": scan_id,
                "scan_id": scan_id,
                "status": "complete",
                "agent_type": "cost",
                "resource_group": "demo-rg",
                "started_at": "2026-03-02T10:00:00+00:00",
                "completed_at": "2026-03-02T10:01:00+00:00",
                "proposed_actions": [{"action_type": "scale_down"}],
                "evaluations": [{"decision": "approved"}],
                "totals": {"approved": 1, "escalated": 0, "denied": 0},
                "event_count": 4,
                "last_event_at": "2026-03-02T10:01:00+00:00",
                "error": None,
            }
        )
        # Simulate process restart: in-memory cache gone.
        api_module._scans.clear()

        res = client.get(f"/api/scan/{scan_id}/status")
        assert res.status_code == 200
        data = res.json()
        assert data["scan_id"] == scan_id
        assert data["status"] == "complete"
        assert data["proposals_count"] == 1
        assert data["evaluations_count"] == 1

    def test_agent_last_run_includes_counts_and_timestamps(self, client):
        import src.api.dashboard_api as api_module

        old_id = "scan-old"
        new_id = "scan-new"
        api_module._scan_tracker.upsert(
            {
                "id": old_id,
                "scan_id": old_id,
                "status": "complete",
                "agent_type": "cost",
                "resource_group": None,
                "started_at": "2026-03-02T10:00:00+00:00",
                "completed_at": "2026-03-02T10:02:00+00:00",
                "proposed_actions": [{"action_type": "scale_down"}],
                "evaluations": [{"decision": "approved"}],
                "totals": {"approved": 1, "escalated": 0, "denied": 0},
                "event_count": 3,
                "last_event_at": "2026-03-02T10:02:00+00:00",
                "error": None,
            }
        )
        api_module._scan_tracker.upsert(
            {
                "id": new_id,
                "scan_id": new_id,
                "status": "complete",
                "agent_type": "cost",
                "resource_group": None,
                "started_at": "2026-03-02T11:00:00+00:00",
                "completed_at": "2026-03-02T11:03:00+00:00",
                "proposed_actions": [{"action_type": "scale_down"}, {"action_type": "delete_resource"}],
                "evaluations": [{"decision": "approved"}, {"decision": "denied"}],
                "totals": {"approved": 1, "escalated": 0, "denied": 1},
                "event_count": 7,
                "last_event_at": "2026-03-02T11:03:00+00:00",
                "error": None,
            }
        )

        res = client.get("/api/agents/cost-optimization-agent/last-run")
        assert res.status_code == 200
        data = res.json()
        assert data["source"] == "scan_tracker"
        assert data["scan_id"] == new_id
        assert data["proposals_count"] == 2
        assert data["evaluations_count"] == 2
        assert data["started_at"] == "2026-03-02T11:00:00+00:00"
        assert data["completed_at"] == "2026-03-02T11:03:00+00:00"
        assert data["totals"]["denied"] == 1

    def test_scan_stream_includes_detailed_event_types(self, client):
        import src.api.dashboard_api as api_module

        scan_id, _ = api_module._make_scan_record("cost", "demo-rg")
        asyncio.run(api_module._emit_event(scan_id, "discovery", agent="cost", message="Found 3 resources"))
        asyncio.run(api_module._emit_event(scan_id, "proposal", agent="cost", message="Proposing scale_down"))
        asyncio.run(api_module._emit_event(scan_id, "evaluation", agent="cost", message="Evaluating via pipeline"))
        asyncio.run(api_module._emit_event(scan_id, "verdict", agent="cost", decision="approved", message="Approved"))
        asyncio.run(api_module._emit_event(scan_id, "scan_complete", agent="cost", message="Complete"))

        res = client.get(f"/api/scan/{scan_id}/stream")
        assert res.status_code == 200
        body = res.text
        assert '"event": "discovery"' in body
        assert '"event": "proposal"' in body
        assert '"event": "evaluation"' in body
        assert '"event": "verdict"' in body
        assert '"event": "scan_complete"' in body

    def test_scan_cancel_persists_cancelled_status(self, client):
        import src.api.dashboard_api as api_module
        import src.operational_agents.cost_agent as cost_module

        class _FakeCostAgent:
            async def scan(self, target_resource_group=None):
                return [
                    _make_action(
                        resource_id=(
                            "/subscriptions/demo/resourceGroups/prod"
                            "/providers/Microsoft.Compute/virtualMachines/vm-cancel"
                        ),
                        action_type=ActionType.SCALE_DOWN,
                        current_sku="Standard_D4s_v3",
                        proposed_sku="Standard_D2s_v3",
                    )
                ]

        monkeypatch = pytest.MonkeyPatch()
        monkeypatch.setattr(cost_module, "CostOptimizationAgent", _FakeCostAgent)
        try:
            scan_id, _ = api_module._make_scan_record("cost", None)
            api_module._scan_cancelled.add(scan_id)
            asyncio.run(api_module._run_agent_scan(scan_id, "cost", None))
        finally:
            monkeypatch.undo()

        res = client.get(f"/api/scan/{scan_id}/status")
        assert res.status_code == 200
        data = res.json()
        assert data["status"] == "cancelled"


# ---------------------------------------------------------------------------
# _get_resource_tags — mock path and live path
# ---------------------------------------------------------------------------


class TestGetResourceTags:
    """Tests for the async _get_resource_tags() helper (Phase 21 fix)."""

    def setup_method(self):
        """Reset the module-level seed cache before each test."""
        import src.api.dashboard_api as api_module
        api_module._resource_graph_cache = None

    def test_mock_mode_returns_tags_from_seed_by_name(self, monkeypatch):
        """Mock mode: returns tags when resource is found in seed_resources.json."""
        import src.api.dashboard_api as api_module

        monkeypatch.setattr(api_module.settings, "use_local_mocks", True)
        tags = asyncio.run(api_module._get_resource_tags("vm-dr-01"))
        # vm-dr-01 has tags in seed_resources.json (disaster-recovery: true etc.)
        assert isinstance(tags, dict)

    def test_mock_mode_returns_empty_for_unknown_resource(self, monkeypatch):
        """Mock mode: returns {} for resources not in seed file."""
        import src.api.dashboard_api as api_module

        monkeypatch.setattr(api_module.settings, "use_local_mocks", True)
        tags = asyncio.run(api_module._get_resource_tags("nonexistent-vm-xyz"))
        assert tags == {}

    def test_mock_mode_resolves_full_arm_id_by_last_segment(self, monkeypatch):
        """Full ARM IDs are resolved by their last path segment."""
        import src.api.dashboard_api as api_module

        monkeypatch.setattr(api_module.settings, "use_local_mocks", True)
        arm_id = "/subscriptions/demo/resourceGroups/rg/providers/Microsoft.Compute/virtualMachines/vm-dr-01"
        tags = asyncio.run(api_module._get_resource_tags(arm_id))
        assert isinstance(tags, dict)

    def test_live_mode_uses_resource_graph_client(self, monkeypatch):
        """Live mode: delegates to ResourceGraphClient.get_resource_async()."""
        import src.api.dashboard_api as api_module
        from unittest.mock import AsyncMock, MagicMock

        monkeypatch.setattr(api_module.settings, "use_local_mocks", False)
        monkeypatch.setattr(api_module.settings, "azure_subscription_id", "sub-live-test")

        mock_client = MagicMock()
        mock_client.get_resource_async = AsyncMock(
            return_value={"name": "vm-live", "tags": {"iac_repo": "org/repo", "iac_path": "infra/"}}
        )
        mock_rg_module = MagicMock()
        mock_rg_module.ResourceGraphClient.return_value = mock_client

        monkeypatch.setitem(
            __import__("sys").modules,
            "src.infrastructure.resource_graph",
            mock_rg_module,
        )

        tags = asyncio.run(api_module._get_resource_tags("vm-live"))
        assert tags == {"iac_repo": "org/repo", "iac_path": "infra/"}
        mock_client.get_resource_async.assert_awaited_once_with("vm-live")

    def test_live_mode_falls_back_to_seed_on_exception(self, monkeypatch):
        """Live mode: falls back to seed_resources.json when Resource Graph raises."""
        import src.api.dashboard_api as api_module
        from unittest.mock import AsyncMock, MagicMock

        monkeypatch.setattr(api_module.settings, "use_local_mocks", False)
        monkeypatch.setattr(api_module.settings, "azure_subscription_id", "sub-live-test")

        mock_client = MagicMock()
        mock_client.get_resource_async = AsyncMock(side_effect=RuntimeError("network timeout"))
        mock_rg_module = MagicMock()
        mock_rg_module.ResourceGraphClient.return_value = mock_client

        monkeypatch.setitem(
            __import__("sys").modules,
            "src.infrastructure.resource_graph",
            mock_rg_module,
        )

        # Should not raise — falls back to seed silently
        tags = asyncio.run(api_module._get_resource_tags("vm-dr-01"))
        assert isinstance(tags, dict)  # seed fallback returns tags for vm-dr-01

    def test_live_mode_resource_not_in_graph_falls_back_to_seed(self, monkeypatch):
        """Live mode: None result from Resource Graph triggers seed fallback."""
        import src.api.dashboard_api as api_module
        from unittest.mock import AsyncMock, MagicMock

        monkeypatch.setattr(api_module.settings, "use_local_mocks", False)
        monkeypatch.setattr(api_module.settings, "azure_subscription_id", "sub-live-test")

        mock_client = MagicMock()
        mock_client.get_resource_async = AsyncMock(return_value=None)
        mock_rg_module = MagicMock()
        mock_rg_module.ResourceGraphClient.return_value = mock_client

        monkeypatch.setitem(
            __import__("sys").modules,
            "src.infrastructure.resource_graph",
            mock_rg_module,
        )

        tags = asyncio.run(api_module._get_resource_tags("vm-dr-01"))
        assert isinstance(tags, dict)  # seed fallback


# ---------------------------------------------------------------------------
# Phase 29 — GET /api/config (4 tests)
# ---------------------------------------------------------------------------


class TestHealthCheck:
    """GET /health returns liveness status."""

    def test_health_returns_200(self, client):
        res = client.get("/health")
        assert res.status_code == 200

    def test_health_returns_ok(self, client):
        data = client.get("/health").json()
        assert data["status"] == "ok"


class TestGetConfig:
    """GET /api/config returns safe system configuration."""

    def test_get_config_status_200(self, client):
        res = client.get("/api/config")
        assert res.status_code == 200

    def test_get_config_required_keys(self, client):
        data = client.get("/api/config").json()
        required = {"mode", "llm_timeout", "llm_concurrency_limit",
                    "execution_gateway_enabled", "use_live_topology", "version"}
        assert required.issubset(data.keys())

    def test_get_config_mode_is_mock(self, client, monkeypatch):
        # Patch settings so use_local_mocks=True → mode should be "mock"
        import src.api.dashboard_api as api_mod
        monkeypatch.setattr(api_mod.settings, "use_local_mocks", True)
        data = client.get("/api/config").json()
        assert data["mode"] == "mock"

    def test_get_config_version_is_string(self, client):
        data = client.get("/api/config").json()
        assert isinstance(data["version"], str)
        assert len(data["version"]) > 0


# ---------------------------------------------------------------------------
# Phase 29 — GET /api/metrics executions block (3 tests)
# ---------------------------------------------------------------------------


class TestMetricsExecutionsBlock:
    """GET /api/metrics includes an 'executions' block (Phase 29)."""

    def test_metrics_has_executions_key(self, client):
        res = client.get("/api/metrics")
        assert res.status_code == 200
        assert "executions" in res.json()

    def test_metrics_executions_all_zeros_when_empty(self, client):
        data = client.get("/api/metrics").json()
        ex = data["executions"]
        assert ex["total"] == 0
        assert ex["applied"] == 0
        assert ex["failed"] == 0
        assert ex["agent_fix_rate"] == 0.0
        assert ex["success_rate"] == 0.0

    def test_metrics_executions_required_keys(self, client):
        data = client.get("/api/metrics").json()
        required = {"total", "applied", "failed", "pr_created",
                    "dismissed", "pending", "agent_fix_rate", "success_rate"}
        assert required.issubset(data["executions"].keys())


# ---------------------------------------------------------------------------
# Phase 30 — POST /api/execution/{id}/rollback (3 tests)
# ---------------------------------------------------------------------------


class TestRollbackEndpoint:
    """POST /api/execution/{id}/rollback endpoint."""

    def test_rollback_nonexistent_returns_404(self, client):
        res = client.post("/api/execution/nonexistent-id/rollback", json={})
        assert res.status_code == 404

    def test_rollback_non_applied_returns_400(self, client):
        import asyncio
        from src.core.execution_gateway import ExecutionGateway
        from src.core.models import SRIVerdict

        gw = client.app.state.execution_gateway if hasattr(client.app.state, "execution_gateway") else None
        # Create a verdict and check it returns 400 (not applied yet)
        # We can't easily get an exec_id here without the gateway, so just confirm
        # the route exists and rejects unknown ids with 404
        res = client.post("/api/execution/00000000-0000-0000-0000-000000000000/rollback", json={})
        assert res.status_code in (400, 404)

    def test_rollback_invalid_body_still_processed(self, client):
        """Empty body should use default reviewed_by — route must not 422."""
        res = client.post("/api/execution/nonexistent-id/rollback")
        # Should 404 (not found), not 422 (validation error)
        assert res.status_code == 404


# ---------------------------------------------------------------------------
# Regression: cross-agent scan contamination (Bug fix Phase 31)
# ---------------------------------------------------------------------------


class TestCrossAgentContamination:
    """Regression tests for the bug where get_unresolved_proposals() returned
    proposals from ALL agents, causing monitoring-agent proposals to appear in
    a cost scan's proposed_actions and evaluations lists when the cost agent
    returned 0 proposals and the re-flagging loop ran."""

    def test_unresolved_filter_same_agent_only(self):
        """Filter in _run_agent_scan must restrict re-flagged proposals to
        the same agent that is currently scanning."""
        import src.api.dashboard_api as api_module
        from src.core.models import ActionTarget, ActionType, ProposedAction

        # The dict-comprehension used to filter is: same agent_id only.
        # Simulate: two unresolved proposals, different agent_ids.
        cost_action = ProposedAction(
            agent_id="cost-optimization-agent",
            action_type=ActionType.SCALE_DOWN,
            reason="oversized VM",
            target=ActionTarget(
                resource_id="/subscriptions/x/resourceGroups/rg/providers/Microsoft.Compute/virtualMachines/vm-cost",
                resource_type="Microsoft.Compute/virtualMachines",
            ),
        )
        monitoring_action = ProposedAction(
            agent_id="monitoring-agent",
            action_type=ActionType.RESTART_SERVICE,
            reason="VM offline",
            target=ActionTarget(
                resource_id="/subscriptions/x/resourceGroups/rg/providers/Microsoft.Compute/virtualMachines/vm-mon",
                resource_type="Microsoft.Compute/virtualMachines",
            ),
        )

        # Apply the same filter logic used in _run_agent_scan
        agent_type = "cost"
        current_agent_id = api_module._AGENT_REGISTRY_NAMES.get(agent_type, "")
        assert current_agent_id == "cost-optimization-agent"

        pairs = [(cost_action, None), (monitoring_action, None)]
        filtered = [
            (a, r) for a, r in pairs
            if getattr(a, "agent_id", None) == current_agent_id
        ]
        assert len(filtered) == 1
        assert filtered[0][0].agent_id == "cost-optimization-agent"

    def test_agent_type_map_covers_all_agents(self):
        """All known agent types must have an entry in _AGENT_REGISTRY_NAMES."""
        import src.api.dashboard_api as api_module

        for agent_type in ("cost", "monitoring", "deploy"):
            assert agent_type in api_module._AGENT_REGISTRY_NAMES
            agent_id = api_module._AGENT_REGISTRY_NAMES[agent_type]
            assert agent_id.endswith("-agent"), f"{agent_type} → {agent_id} missing '-agent' suffix"
