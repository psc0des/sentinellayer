"""Tests for Phase 10 — A2A Protocol.

Tests are mock-first and do not require a running A2A server or Azure services.

Test coverage:
1. Agent Card structure and fields
2. AgentRegistry CRUD operations (using a temp directory)
3. A2A task submission via SentinelAgentExecutor (mock pipeline)
4. Dashboard API new /api/agents endpoints
"""

import json
import os
import uuid
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# Force mock mode — no Azure needed
os.environ.setdefault("USE_LOCAL_MOCKS", "true")


# ---------------------------------------------------------------------------
# 1. Agent Card
# ---------------------------------------------------------------------------


class TestAgentCard:
    """Verify the A2A Agent Card structure advertised by SentinelLayer."""

    def test_agent_card_has_required_fields(self) -> None:
        """Agent Card must have name, url, version, skills, and capabilities."""
        from src.a2a.sentinel_a2a_server import _build_agent_card

        card = _build_agent_card("http://localhost:8000")

        assert card.name == "SentinelLayer Governance Engine"
        assert card.version == "1.0.0"
        assert card.url == "http://localhost:8000"
        # Streaming must be enabled for SSE support
        assert card.capabilities is not None
        assert card.capabilities.streaming is True

    def test_agent_card_has_three_skills(self) -> None:
        """Agent Card must advertise exactly 3 skills."""
        from src.a2a.sentinel_a2a_server import _build_agent_card

        card = _build_agent_card("http://localhost:8000")
        skill_ids = {s.id for s in card.skills}

        assert "evaluate_action" in skill_ids
        assert "query_decision_history" in skill_ids
        assert "get_resource_risk_profile" in skill_ids

    def test_agent_card_url_from_env(self) -> None:
        """Agent Card URL should reflect the A2A_SERVER_URL environment variable."""
        from src.a2a.sentinel_a2a_server import _build_agent_card

        card = _build_agent_card("http://my-server:9000")
        assert card.url == "http://my-server:9000"

    def test_agent_card_description_mentions_sri(self) -> None:
        """Description must mention SRI™ so external agents understand the purpose."""
        from src.a2a.sentinel_a2a_server import _build_agent_card

        card = _build_agent_card("http://localhost:8000")
        assert "SRI" in card.description


# ---------------------------------------------------------------------------
# 2. Agent Registry CRUD
# ---------------------------------------------------------------------------


class TestAgentRegistry:
    """Test the AgentRegistry in mock mode (local JSON files)."""

    @pytest.fixture
    def registry(self, tmp_path: Path):
        """Create a registry wired to a temporary directory so tests are isolated."""
        from src.a2a.agent_registry import AgentRegistry

        # Patch the settings to force mock mode
        cfg = MagicMock()
        cfg.use_local_mocks = True
        cfg.cosmos_endpoint = ""
        return AgentRegistry(cfg=cfg, agents_dir=tmp_path)

    def test_register_new_agent(self, registry) -> None:
        """Registering a new agent creates an entry with zero counters."""
        entry = registry.register_agent("test-agent", "http://test-agent:9000")

        assert entry["name"] == "test-agent"
        assert entry["agent_card_url"] == "http://test-agent:9000"
        assert entry["total_actions_proposed"] == 0
        assert entry["approval_count"] == 0
        assert entry["denial_count"] == 0
        assert entry["escalation_count"] == 0

    def test_register_same_agent_twice_preserves_counters(self, registry) -> None:
        """Re-registering an existing agent should only update last_seen."""
        registry.register_agent("test-agent", "http://test-agent:9000")
        registry.update_agent_stats("test-agent", "approved")

        # Re-register
        entry = registry.register_agent("test-agent", "http://test-agent:9000")

        # Counter should still be 1 — not reset
        assert entry["approval_count"] == 1

    def test_update_stats_approved(self, registry) -> None:
        """update_agent_stats('approved') increments approval_count."""
        registry.register_agent("cost-agent")
        registry.update_agent_stats("cost-agent", "approved")

        stats = registry.get_agent_stats("cost-agent")
        assert stats is not None
        assert stats["approval_count"] == 1
        assert stats["denial_count"] == 0
        assert stats["total_actions_proposed"] == 1

    def test_update_stats_denied(self, registry) -> None:
        """update_agent_stats('denied') increments denial_count."""
        registry.register_agent("cost-agent")
        registry.update_agent_stats("cost-agent", "denied")

        stats = registry.get_agent_stats("cost-agent")
        assert stats["denial_count"] == 1

    def test_update_stats_escalated(self, registry) -> None:
        """update_agent_stats('escalated') increments escalation_count."""
        registry.register_agent("deploy-agent")
        registry.update_agent_stats("deploy-agent", "escalated")

        stats = registry.get_agent_stats("deploy-agent")
        assert stats["escalation_count"] == 1

    def test_auto_register_on_update(self, registry) -> None:
        """Calling update_agent_stats for an unknown agent auto-registers it."""
        # No explicit register_agent call
        registry.update_agent_stats("surprise-agent", "approved")

        stats = registry.get_agent_stats("surprise-agent")
        assert stats is not None
        assert stats["approval_count"] == 1

    def test_get_connected_agents_returns_all(self, registry) -> None:
        """get_connected_agents() returns all registered agents."""
        registry.register_agent("agent-1")
        registry.register_agent("agent-2")
        registry.register_agent("agent-3")

        agents = registry.get_connected_agents()
        names = {a["name"] for a in agents}

        assert "agent-1" in names
        assert "agent-2" in names
        assert "agent-3" in names

    def test_get_agent_stats_unknown_returns_none(self, registry) -> None:
        """get_agent_stats for an unregistered agent returns None."""
        result = registry.get_agent_stats("does-not-exist")
        assert result is None

    def test_multiple_updates_accumulate(self, registry) -> None:
        """Repeated stat updates should accumulate correctly."""
        registry.register_agent("multi-agent")
        for decision in ["approved", "approved", "denied", "escalated"]:
            registry.update_agent_stats("multi-agent", decision)

        stats = registry.get_agent_stats("multi-agent")
        assert stats["total_actions_proposed"] == 4
        assert stats["approval_count"] == 2
        assert stats["denial_count"] == 1
        assert stats["escalation_count"] == 1


# ---------------------------------------------------------------------------
# 3. A2A task submission via SentinelAgentExecutor
# ---------------------------------------------------------------------------


class TestSentinelAgentExecutor:
    """Test the A2A server executor with a mocked pipeline."""

    def _make_mock_verdict(self, decision: str = "denied") -> Any:
        """Build a minimal GovernanceVerdict mock."""
        from src.core.models import (
            ActionTarget,
            ActionType,
            GovernanceVerdict,
            ProposedAction,
            SRIBreakdown,
            SRIVerdict,
            Urgency,
        )
        from datetime import datetime, timezone

        action = ProposedAction(
            agent_id="test-agent",
            action_type=ActionType.DELETE_RESOURCE,
            target=ActionTarget(resource_id="vm-test", resource_type="Microsoft.Compute/virtualMachines"),
            reason="Test",
            urgency=Urgency.LOW,
        )
        sri = SRIBreakdown(
            sri_infrastructure=50.0,
            sri_policy=80.0,
            sri_historical=30.0,
            sri_cost=20.0,
            sri_composite=47.5,
        )
        return GovernanceVerdict(
            action_id=str(uuid.uuid4()),
            timestamp=datetime.now(timezone.utc),
            proposed_action=action,
            sentinel_risk_index=sri,
            decision=SRIVerdict(decision),
            reason="Test verdict",
        )

    @pytest.mark.asyncio
    async def test_execute_valid_action_completes(self) -> None:
        """executor.execute() should call complete() after a successful evaluation."""
        from src.core.models import ActionTarget, ActionType, ProposedAction, Urgency
        from src.a2a.sentinel_a2a_server import SentinelAgentExecutor

        action = ProposedAction(
            agent_id="test-agent",
            action_type=ActionType.DELETE_RESOURCE,
            target=ActionTarget(
                resource_id="vm-test",
                resource_type="Microsoft.Compute/virtualMachines",
            ),
            reason="Test",
            urgency=Urgency.LOW,
        )

        mock_verdict = self._make_mock_verdict("denied")

        with patch(
            "src.a2a.sentinel_a2a_server.get_pipeline"
        ) as mock_get_pipeline:
            pipeline = AsyncMock()
            pipeline.evaluate = AsyncMock(return_value=mock_verdict)
            mock_get_pipeline.return_value = pipeline

            executor = SentinelAgentExecutor()
            executor._pipeline = pipeline

            # Mock context
            context = MagicMock()
            context.task_id = str(uuid.uuid4())
            context.context_id = str(uuid.uuid4())
            context.get_user_input.return_value = action.model_dump_json()

            # Mock event queue (TaskUpdater writes to this)
            event_queue = MagicMock()

            with patch("src.a2a.sentinel_a2a_server.TaskUpdater") as MockUpdater:
                updater_instance = AsyncMock()
                MockUpdater.return_value = updater_instance

                await executor.execute(context, event_queue)

                # Pipeline must have been called once
                pipeline.evaluate.assert_called_once()

                # complete() must be called (task finalised)
                updater_instance.complete.assert_called_once()

    @pytest.mark.asyncio
    async def test_execute_invalid_json_calls_complete_with_error(self) -> None:
        """executor.execute() should call complete() even when JSON is invalid."""
        from src.a2a.sentinel_a2a_server import SentinelAgentExecutor

        with patch("src.a2a.sentinel_a2a_server.get_pipeline"):
            executor = SentinelAgentExecutor()
            executor._pipeline = AsyncMock()

            context = MagicMock()
            context.task_id = str(uuid.uuid4())
            context.context_id = str(uuid.uuid4())
            context.get_user_input.return_value = "this is not json"

            event_queue = MagicMock()

            with patch("src.a2a.sentinel_a2a_server.TaskUpdater") as MockUpdater:
                updater_instance = AsyncMock()
                MockUpdater.return_value = updater_instance

                await executor.execute(context, event_queue)

                # complete() still called (with error message) — task does not hang
                updater_instance.complete.assert_called_once()
                # Pipeline should NOT have been called
                executor._pipeline.evaluate.assert_not_called()

    @pytest.mark.asyncio
    async def test_execute_streams_progress_messages(self) -> None:
        """executor.execute() should call new_agent_message() at least once."""
        from src.core.models import ActionTarget, ActionType, ProposedAction, Urgency
        from src.a2a.sentinel_a2a_server import SentinelAgentExecutor

        action = ProposedAction(
            agent_id="test-agent",
            action_type=ActionType.SCALE_UP,
            target=ActionTarget(
                resource_id="vm-test",
                resource_type="Microsoft.Compute/virtualMachines",
            ),
            reason="High CPU",
            urgency=Urgency.HIGH,
        )

        mock_verdict = self._make_mock_verdict("approved")

        with patch("src.a2a.sentinel_a2a_server.get_pipeline"):
            executor = SentinelAgentExecutor()
            executor._pipeline = AsyncMock()
            executor._pipeline.evaluate = AsyncMock(return_value=mock_verdict)

            context = MagicMock()
            context.task_id = str(uuid.uuid4())
            context.context_id = str(uuid.uuid4())
            context.get_user_input.return_value = action.model_dump_json()

            event_queue = MagicMock()

            with patch("src.a2a.sentinel_a2a_server.TaskUpdater") as MockUpdater:
                updater_instance = AsyncMock()
                MockUpdater.return_value = updater_instance

                await executor.execute(context, event_queue)

                # Should stream at least 4 progress messages + 1 final summary
                assert updater_instance.new_agent_message.call_count >= 4


# ---------------------------------------------------------------------------
# 4. Dashboard API — /api/agents endpoints
# ---------------------------------------------------------------------------


class TestDashboardAgentEndpoints:
    """Test the new A2A agent endpoints in the FastAPI dashboard."""

    @pytest.fixture
    def client(self, tmp_path: Path):
        """Create a FastAPI test client with registry wired to tmp_path."""
        from fastapi.testclient import TestClient
        from src.api.dashboard_api import app, _get_registry

        # Inject a registry backed by tmp_path
        from src.a2a.agent_registry import AgentRegistry
        cfg = MagicMock()
        cfg.use_local_mocks = True
        cfg.cosmos_endpoint = ""
        registry = AgentRegistry(cfg=cfg, agents_dir=tmp_path)

        # Override the singleton
        import src.api.dashboard_api as api_module
        original = api_module._registry
        api_module._registry = registry

        yield TestClient(app)

        # Restore original
        api_module._registry = original

    def test_get_agents_empty(self, client) -> None:
        """GET /api/agents returns an empty list when no agents are registered."""
        resp = client.get("/api/agents")
        assert resp.status_code == 200
        data = resp.json()
        assert data["count"] == 0
        assert data["agents"] == []

    def test_get_agents_after_registration(self, client, tmp_path: Path) -> None:
        """GET /api/agents reflects registered agents."""
        from src.a2a.agent_registry import AgentRegistry

        cfg = MagicMock()
        cfg.use_local_mocks = True
        cfg.cosmos_endpoint = ""
        registry = AgentRegistry(cfg=cfg, agents_dir=tmp_path)
        registry.register_agent("cost-agent")
        registry.register_agent("monitoring-agent")

        import src.api.dashboard_api as api_module
        api_module._registry = registry

        resp = client.get("/api/agents")
        assert resp.status_code == 200
        data = resp.json()
        assert data["count"] == 2
        names = {a["name"] for a in data["agents"]}
        assert "cost-agent" in names
        assert "monitoring-agent" in names

    def test_get_agent_history_404_for_unknown(self, client) -> None:
        """GET /api/agents/{name}/history returns 404 for unknown agents."""
        resp = client.get("/api/agents/unknown-agent/history")
        assert resp.status_code == 404

    def test_get_agent_history_known_agent(self, client, tmp_path: Path) -> None:
        """GET /api/agents/{name}/history returns 200 for registered agents."""
        from src.a2a.agent_registry import AgentRegistry

        cfg = MagicMock()
        cfg.use_local_mocks = True
        cfg.cosmos_endpoint = ""
        registry = AgentRegistry(cfg=cfg, agents_dir=tmp_path)
        registry.register_agent("deploy-agent")

        import src.api.dashboard_api as api_module
        api_module._registry = registry

        resp = client.get("/api/agents/deploy-agent/history")
        assert resp.status_code == 200
        data = resp.json()
        assert "agent" in data
        assert "history" in data
        assert data["agent"]["name"] == "deploy-agent"
