"""Tests for Phase 34F: A2 Validator Agent.

Covers:
1. Mock mode returns a deterministic ValidatorBrief (no LLM call)
2. ValidatorBrief has all required fields with correct types
3. Timeout path: slow LLM returns validator_status="unavailable"
4. LLM error path: exception returns validator_status="unavailable"
5. Unavailable brief does NOT block execution — validate_proposed_action always returns
6. ValidatorBrief Pydantic model validates correctly
7. AzPlaybookExecution now carries validator_brief_id / summary / caveats fields
8. POST /validate endpoint returns 200 with ValidatorBrief structure
9. POST /execute endpoint passes validator_brief_id into audit record
"""

import asyncio
import uuid
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.core.models import (
    ActionTarget,
    ActionType,
    AzPlaybookExecution,
    ProposedAction,
    Urgency,
    ValidatorBrief,
)
from src.core.validator_agent import (
    _mock_brief,
    _unavailable_brief,
    validate_proposed_action,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _mock_cfg(use_local_mocks: bool = True, endpoint: str = "") -> MagicMock:
    cfg = MagicMock()
    cfg.use_local_mocks = use_local_mocks
    cfg.azure_openai_endpoint = endpoint
    cfg.azure_openai_deployment = "gpt-4.1-mini"
    return cfg


def _make_action(
    action_type: ActionType = ActionType.RESTART_SERVICE,
    resource_id: str = "/subscriptions/sub/resourceGroups/rg/providers/Microsoft.Web/sites/my-app",
    resource_type: str = "Microsoft.Web/sites",
) -> ProposedAction:
    return ProposedAction(
        agent_id="monitoring-agent",
        action_type=action_type,
        target=ActionTarget(
            resource_id=resource_id,
            resource_type=resource_type,
        ),
        reason="App Service reporting high error rate — restart to clear memory leak.",
        urgency=Urgency.HIGH,
    )


# ---------------------------------------------------------------------------
# 1. Mock mode returns deterministic brief — no LLM call
# ---------------------------------------------------------------------------

def test_mock_brief_returns_valid_brief():
    action = _make_action()
    resolved_call = {"argv": ["az", "webapp", "restart", "--name", "my-app", "--resource-group", "rg"]}
    brief = _mock_brief(action, resolved_call)

    assert isinstance(brief, ValidatorBrief)
    assert brief.validator_status == "ok"
    assert len(brief.summary) > 0
    assert len(brief.caveats) >= 1
    assert brief.risk_level in ("low", "medium", "high")
    assert "mock" in brief.raw_text.lower()


@pytest.mark.asyncio
async def test_validate_proposed_action_mock_mode():
    """With use_local_mocks=True, validate_proposed_action returns without LLM."""
    action = _make_action()
    resolved_call = {"argv": ["az", "webapp", "restart", "--name", "my-app", "--resource-group", "rg"]}
    cfg = _mock_cfg(use_local_mocks=True)

    brief = await validate_proposed_action(action, resolved_call, {}, cfg=cfg)

    assert isinstance(brief, ValidatorBrief)
    assert brief.validator_status == "ok"
    assert brief.summary
    assert len(brief.caveats) >= 1


# ---------------------------------------------------------------------------
# 2. ValidatorBrief has all required fields with correct types
# ---------------------------------------------------------------------------

def test_validator_brief_all_fields():
    brief = ValidatorBrief(
        summary="Restarts the App Service instance.",
        caveats=["Verify no active deployment is in progress."],
        risk_level="low",
        resource_state_at_validation={"running": True},
        validator_status="ok",
        raw_text="**Summary:** Restarts the App Service instance.",
    )
    assert brief.summary == "Restarts the App Service instance."
    assert brief.caveats == ["Verify no active deployment is in progress."]
    assert brief.risk_level == "low"
    assert brief.resource_state_at_validation == {"running": True}
    assert brief.validator_status == "ok"
    assert "Summary" in brief.raw_text


def test_validator_brief_invalid_status_rejected():
    """Pydantic should reject an invalid validator_status value."""
    import pydantic
    with pytest.raises((pydantic.ValidationError, ValueError)):
        ValidatorBrief(
            summary="x",
            caveats=[],
            risk_level="low",
            resource_state_at_validation={},
            validator_status="unknown",  # invalid
            raw_text="x",
        )


def test_validator_brief_invalid_risk_level_rejected():
    import pydantic
    with pytest.raises((pydantic.ValidationError, ValueError)):
        ValidatorBrief(
            summary="x",
            caveats=[],
            risk_level="critical",  # not in ("low", "medium", "high")
            resource_state_at_validation={},
            validator_status="ok",
            raw_text="x",
        )


# ---------------------------------------------------------------------------
# 3. Timeout path returns validator_status="unavailable"
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_validate_timeout_returns_unavailable():
    """When the LLM call takes longer than 5s, validator_status must be 'unavailable'."""
    action = _make_action()
    resolved_call = {"argv": ["az", "webapp", "restart", "--name", "my-app", "--resource-group", "rg"]}
    cfg = _mock_cfg(use_local_mocks=False, endpoint="https://fake.openai.azure.com")

    async def _slow_llm(*args, **kwargs):
        await asyncio.sleep(10)  # far longer than 5s limit
        return ValidatorBrief(summary="x", caveats=[], risk_level="low",
                              resource_state_at_validation={}, validator_status="ok", raw_text="x")

    with patch("src.core.validator_agent._call_llm", side_effect=_slow_llm):
        brief = await validate_proposed_action(action, resolved_call, {}, cfg=cfg)

    assert brief.validator_status in ("unavailable", "timeout")
    assert "unavailable" in brief.raw_text.lower() or "timeout" in brief.raw_text.lower()


# ---------------------------------------------------------------------------
# 4. LLM error path returns validator_status="unavailable"
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_validate_llm_error_returns_unavailable():
    """When the LLM raises any exception, validate_proposed_action catches it."""
    action = _make_action()
    resolved_call = {}
    cfg = _mock_cfg(use_local_mocks=False, endpoint="https://fake.openai.azure.com")

    async def _fail(*args, **kwargs):
        raise RuntimeError("Connection refused")

    with patch("src.core.validator_agent._call_llm", side_effect=_fail):
        brief = await validate_proposed_action(action, resolved_call, {}, cfg=cfg)

    assert brief.validator_status == "unavailable"
    assert brief.summary == ""


# ---------------------------------------------------------------------------
# 5. Unavailable brief never raises — validate_proposed_action always returns
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_validate_always_returns():
    """validate_proposed_action must never raise — it always returns a ValidatorBrief."""
    action = _make_action()
    cfg = _mock_cfg(use_local_mocks=False, endpoint="https://fake.openai.azure.com")

    with patch("src.core.validator_agent._call_llm", side_effect=Exception("chaos")):
        result = await validate_proposed_action(action, {}, {}, cfg=cfg)

    assert isinstance(result, ValidatorBrief)


# ---------------------------------------------------------------------------
# 6. _unavailable_brief helper
# ---------------------------------------------------------------------------

def test_unavailable_brief_structure():
    brief = _unavailable_brief("timeout")
    assert brief.validator_status == "unavailable"
    assert "timeout" in brief.raw_text
    assert brief.summary == ""
    assert brief.caveats == []


def test_unavailable_brief_no_reason():
    brief = _unavailable_brief()
    assert brief.validator_status == "unavailable"
    assert "unavailable" in brief.raw_text


# ---------------------------------------------------------------------------
# 7. AzPlaybookExecution now carries validator brief fields
# ---------------------------------------------------------------------------

def test_az_playbook_execution_has_validator_fields():
    """AzPlaybookExecution must serialise validator_brief_* fields without error."""
    record = AzPlaybookExecution(
        execution_id=str(uuid.uuid4()),
        decision_id=str(uuid.uuid4()),
        resource_id="/subscriptions/sub/resourceGroups/rg/providers/Microsoft.Web/sites/my-app",
        action_type="restart_service",
        az_command="az webapp restart --name my-app --resource-group rg",
        executable_args=["az", "webapp", "restart", "--name", "my-app", "--resource-group", "rg"],
        mode="live",
        approved_by="admin@example.com",
        allowlist_matched=True,
        created_at=datetime.now(timezone.utc),
        validator_brief_id="brief-123",
        validator_brief_summary="Restarts the App Service.",
        validator_brief_caveats=["Check active deployments first."],
    )
    data = record.model_dump(mode="json")
    assert data["validator_brief_id"] == "brief-123"
    assert data["validator_brief_summary"] == "Restarts the App Service."
    assert data["validator_brief_caveats"] == ["Check active deployments first."]


def test_az_playbook_execution_validator_fields_optional():
    """validator_brief_* fields default to None when not provided."""
    record = AzPlaybookExecution(
        execution_id=str(uuid.uuid4()),
        decision_id=str(uuid.uuid4()),
        resource_id="/subscriptions/sub/resourceGroups/rg/providers/Microsoft.Web/sites/my-app",
        action_type="restart_service",
        az_command="az webapp restart --name my-app --resource-group rg",
        executable_args=["az", "webapp", "restart", "--name", "my-app", "--resource-group", "rg"],
        mode="dry_run",
        approved_by="admin@example.com",
        allowlist_matched=True,
        created_at=datetime.now(timezone.utc),
    )
    assert record.validator_brief_id is None
    assert record.validator_brief_summary is None
    assert record.validator_brief_caveats is None


# ---------------------------------------------------------------------------
# 8. POST /validate API endpoint
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_api_validate_endpoint_returns_brief(tmp_path):
    """POST /api/decisions/{id}/validate returns a ValidatorBrief-shaped dict."""
    from fastapi.testclient import TestClient
    from src.api.dashboard_api import app

    client = TestClient(app)

    # Seed a tracker record so the endpoint can find the decision
    import json
    from src.api.dashboard_api import _get_tracker  # noqa: PLC0415
    tracker = _get_tracker()
    action_id = str(uuid.uuid4())
    tracker_file = Path(tracker._path) if hasattr(tracker, "_path") else tmp_path / "tracker"

    # Bypass the real tracker by patching get_recent
    fake_records = [{
        "action_id": action_id,
        "action_type": "restart_service",
        "resource_id": "/subscriptions/sub/resourceGroups/rg/providers/Microsoft.Web/sites/my-app",
        "resource_type": "Microsoft.Web/sites",
        "action_reason": "High error rate",
        "agent_id": "monitoring-agent",
    }]

    async def mock_validate(action, resolved_call, decision, cfg=None):
        return ValidatorBrief(
            summary="Restarts the App Service.",
            caveats=["Verify no active deployment."],
            risk_level="low",
            resource_state_at_validation={},
            validator_status="ok",
            raw_text="**Summary:** Restarts the App Service.",
        )

    with (
        patch("src.api.dashboard_api._get_tracker") as mock_tracker_fn,
        patch("src.core.validator_agent.validate_proposed_action", side_effect=mock_validate),
    ):
        mock_tracker = MagicMock()
        mock_tracker.get_recent.return_value = fake_records
        mock_tracker_fn.return_value = mock_tracker

        with patch("src.api.dashboard_api._get_execution_gateway") as mock_gw_fn:
            mock_gw = MagicMock()
            mock_gw.get_records_for_verdict.return_value = []
            mock_gw_fn.return_value = mock_gw

            resp = client.post(
                f"/api/decisions/{action_id}/validate",
                json={"resolved_call": {"argv": ["az", "webapp", "restart", "--name", "my-app", "--resource-group", "rg"]}},
            )

    assert resp.status_code == 200
    data = resp.json()
    assert "validator_status" in data
    assert "summary" in data
    assert "caveats" in data
    assert "brief_id" in data  # tagged by endpoint


# ---------------------------------------------------------------------------
# 9. execute_playbook passes validator brief into audit record
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_execute_playbook_stores_validator_brief(tmp_path):
    """execute_playbook writes validator_brief_* fields into the AzPlaybookExecution."""
    from src.core.az_executor import execute_playbook
    from src.core.models import Playbook
    from src.infrastructure.cosmos_client import CosmosAzExecutionClient

    playbook = Playbook(
        action_type="restart_service",
        resource_id="/subscriptions/sub/resourceGroups/rg/providers/Microsoft.Web/sites/my-app",
        az_command="az webapp restart --name my-app --resource-group rg",
        executable_args=["az", "webapp", "restart", "--name", "my-app", "--resource-group", "rg"],
        rollback_command=None,
        expected_outcome="App Service restarted.",
        risk_level="low",
        estimated_duration_seconds=30,
        requires_downtime=False,
        supports_native_what_if=False,
    )

    cfg = _mock_cfg(use_local_mocks=True)

    cosmos = CosmosAzExecutionClient.__new__(CosmosAzExecutionClient)
    cosmos._cfg = cfg
    cosmos._mock_dir = tmp_path / "az_executions"
    cosmos._mock_dir.mkdir()

    stored: list[dict] = []
    cosmos.upsert = lambda doc: stored.append(doc)

    brief_id = str(uuid.uuid4())
    result = await execute_playbook(
        playbook=playbook,
        mode="dry_run",
        approved_by="analyst@example.com",
        decision_id=str(uuid.uuid4()),
        cfg=cfg,
        _cosmos=cosmos,
        validator_brief_id=brief_id,
        validator_brief_summary="Restarts the App Service.",
        validator_brief_caveats=["Verify no active deployment."],
    )

    assert result.validator_brief_id == brief_id
    assert result.validator_brief_summary == "Restarts the App Service."
    assert result.validator_brief_caveats == ["Verify no active deployment."]
    # Cosmos record must carry the brief fields too
    assert len(stored) == 1
    assert stored[0]["validator_brief_id"] == brief_id
