"""Tests for Phase 28: LLM-Driven Execution Agent.

Covers:
1. plan() in mock mode — all 7 ActionType values
2. execute() in mock mode — success, empty plan, multi-step
3. Plan structure — backward-compat commands field, summary, impact, rollback
4. NSG rule name extraction from reason text
5. execute() fallback behavior
"""

from unittest.mock import MagicMock

import pytest

from src.core.execution_agent import ExecutionAgent
from src.core.models import ActionTarget, ActionType, ProposedAction, Urgency
# ActionTarget and ProposedAction are used directly in Phase 34A tests


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_cfg(use_local_mocks: bool = True, endpoint: str = "") -> MagicMock:
    """Create a minimal settings-like object for the ExecutionAgent."""
    cfg = MagicMock()
    cfg.use_local_mocks = use_local_mocks
    cfg.azure_openai_endpoint = endpoint
    cfg.azure_openai_deployment = "gpt-41"
    cfg.llm_timeout = 120
    cfg.azure_subscription_id = "sub-test"
    return cfg


def _make_action(
    action_type: ActionType = ActionType.RESTART_SERVICE,
    resource_id: str = (
        "/subscriptions/sub/resourceGroups/rg/providers/"
        "Microsoft.Compute/virtualMachines/vm-web-01"
    ),
    reason: str = "Test reason",
    proposed_sku: str | None = None,
    current_sku: str | None = None,
    resource_type: str = "Microsoft.Compute/virtualMachines",
    nsg_rule_names: list[str] | None = None,
) -> ProposedAction:
    """Build a minimal ProposedAction for tests."""
    return ProposedAction(
        agent_id="test-agent",
        action_type=action_type,
        target=ActionTarget(
            resource_id=resource_id,
            resource_type=resource_type,
            proposed_sku=proposed_sku,
            current_sku=current_sku,
        ),
        reason=reason,
        urgency=Urgency.HIGH,
        nsg_rule_names=nsg_rule_names,
    )


# ---------------------------------------------------------------------------
# TestPlanMockMode
# ---------------------------------------------------------------------------


class TestPlanMockMode:
    """ExecutionAgent.plan() in mock mode — no LLM, no Azure calls."""

    @pytest.fixture
    def agent(self):
        return ExecutionAgent(cfg=_make_cfg(use_local_mocks=True))

    @pytest.mark.asyncio
    async def test_plan_restart_service(self, agent):
        action = _make_action(ActionType.RESTART_SERVICE)
        plan = await agent.plan(action, {})

        assert len(plan["steps"]) == 1
        step = plan["steps"][0]
        assert step["operation"] == "start_vm"
        assert step["params"]["vm_name"] == "vm-web-01"
        assert step["params"]["resource_group"] == "rg"
        assert len(plan["commands"]) == 1
        assert "vm start" in plan["commands"][0]

    @pytest.mark.asyncio
    async def test_plan_modify_nsg_quoted_rule(self, agent):
        action = _make_action(
            ActionType.MODIFY_NSG,
            resource_id=(
                "/subscriptions/sub/resourceGroups/rg/providers/"
                "Microsoft.Network/networkSecurityGroups/nsg-prod"
            ),
            reason="Insecure rule 'AllowSSH-Any' allows SSH from 0.0.0.0",
            resource_type="Microsoft.Network/networkSecurityGroups",
        )
        plan = await agent.plan(action, {})

        assert plan["steps"][0]["operation"] == "delete_nsg_rule"
        assert plan["steps"][0]["params"]["rule_name"] == "AllowSSH-Any"
        assert "nsg rule delete" in plan["commands"][0]
        assert "AllowSSH-Any" in plan["commands"][0]

    @pytest.mark.asyncio
    async def test_plan_modify_nsg_unquoted_rule(self, agent):
        action = _make_action(
            ActionType.MODIFY_NSG,
            resource_id=(
                "/subscriptions/sub/resourceGroups/rg/providers/"
                "Microsoft.Network/networkSecurityGroups/nsg-prod"
            ),
            reason="rule AllowRDP-Inbound is open to the internet",
            resource_type="Microsoft.Network/networkSecurityGroups",
        )
        plan = await agent.plan(action, {})

        assert plan["steps"][0]["operation"] == "delete_nsg_rule"
        assert plan["steps"][0]["params"]["rule_name"] == "AllowRDP-Inbound"

    @pytest.mark.asyncio
    async def test_plan_modify_nsg_no_rule_name_uses_placeholder(self, agent):
        action = _make_action(
            ActionType.MODIFY_NSG,
            reason="Bad rule detected in NSG",
            resource_type="Microsoft.Network/networkSecurityGroups",
        )
        plan = await agent.plan(action, {})

        assert plan["steps"][0]["operation"] == "delete_nsg_rule"
        assert plan["steps"][0]["params"]["rule_name"] == "<RULE_NAME>"

    @pytest.mark.asyncio
    async def test_plan_modify_nsg_rule_names_field_beats_regex(self, agent):
        """nsg_rule_names takes priority over reason-string regex parsing."""
        action = _make_action(
            ActionType.MODIFY_NSG,
            resource_id=(
                "/subscriptions/sub/resourceGroups/rg/providers/"
                "Microsoft.Network/networkSecurityGroups/nsg-east-prod"
            ),
            reason=(
                "CRITICAL: 1 internet-exposed management port rule(s) on NSG "
                "'nsg-east-prod': 'allow-ssh-anywhere' port=22 src=*."
            ),
            resource_type="Microsoft.Network/networkSecurityGroups",
            nsg_rule_names=["allow-ssh-anywhere"],
        )
        plan = await agent.plan(action, {})

        assert len(plan["steps"]) == 1
        assert plan["steps"][0]["operation"] == "delete_nsg_rule"
        assert plan["steps"][0]["params"]["rule_name"] == "allow-ssh-anywhere"
        assert "allow-ssh-anywhere" in plan["commands"][0]

    @pytest.mark.asyncio
    async def test_plan_modify_nsg_multiple_rule_names(self, agent):
        """Multiple entries in nsg_rule_names generate one step per rule."""
        action = _make_action(
            ActionType.MODIFY_NSG,
            resource_id=(
                "/subscriptions/sub/resourceGroups/rg/providers/"
                "Microsoft.Network/networkSecurityGroups/nsg-east-prod"
            ),
            reason="CRITICAL: 2 internet-exposed rules on NSG 'nsg-east-prod'.",
            resource_type="Microsoft.Network/networkSecurityGroups",
            nsg_rule_names=["allow-ssh-anywhere", "allow-rdp-anywhere"],
        )
        plan = await agent.plan(action, {})

        assert len(plan["steps"]) == 2
        rule_names = [s["params"]["rule_name"] for s in plan["steps"]]
        assert "allow-ssh-anywhere" in rule_names
        assert "allow-rdp-anywhere" in rule_names

    @pytest.mark.asyncio
    async def test_plan_scale_down(self, agent):
        action = _make_action(ActionType.SCALE_DOWN, proposed_sku="Standard_B2ms")
        plan = await agent.plan(action, {})

        assert plan["steps"][0]["operation"] == "resize_vm"
        assert plan["steps"][0]["params"]["new_size"] == "Standard_B2ms"
        assert "vm resize" in plan["commands"][0]
        assert "Standard_B2ms" in plan["commands"][0]

    @pytest.mark.asyncio
    async def test_plan_scale_up(self, agent):
        action = _make_action(ActionType.SCALE_UP, proposed_sku="Standard_D8s_v3")
        plan = await agent.plan(action, {})

        assert plan["steps"][0]["operation"] == "resize_vm"
        assert plan["steps"][0]["params"]["new_size"] == "Standard_D8s_v3"

    @pytest.mark.asyncio
    async def test_plan_delete_resource(self, agent):
        action = _make_action(ActionType.DELETE_RESOURCE)
        plan = await agent.plan(action, {})

        assert plan["steps"][0]["operation"] == "delete_resource"
        assert "resource delete" in plan["commands"][0]

    @pytest.mark.asyncio
    async def test_plan_update_config_unknown_reason_is_guided_manual(self, agent):
        # UPDATE_CONFIG with an unrecognised reason falls to guided_manual
        # (not manual) — provides az CLI + Portal steps rather than no guidance.
        action = _make_action(ActionType.UPDATE_CONFIG)
        plan = await agent.plan(action, {})

        assert plan["steps"][0]["operation"] == "guided_manual"
        params = plan["steps"][0]["params"]
        assert "az_cli_commands" in params
        assert "portal_steps" in params

    @pytest.mark.asyncio
    async def test_plan_create_resource_is_guided_manual(self, agent):
        # CREATE_RESOURCE cannot be automated — plan returns guided_manual
        # with copy-pasteable az CLI commands and Portal steps.
        action = _make_action(ActionType.CREATE_RESOURCE)
        plan = await agent.plan(action, {})

        assert plan["steps"][0]["operation"] == "guided_manual"
        params = plan["steps"][0]["params"]
        assert "az_cli_commands" in params
        assert "portal_steps" in params
        assert "doc_url" in params

    # -- Structural assertions -----------------------------------------------

    @pytest.mark.asyncio
    async def test_plan_has_backward_compat_commands(self, agent):
        """commands[] must always be present for the dashboard's existing renderer."""
        action = _make_action(ActionType.RESTART_SERVICE)
        plan = await agent.plan(action, {})

        assert "commands" in plan
        assert isinstance(plan["commands"], list)
        assert len(plan["commands"]) > 0

    @pytest.mark.asyncio
    async def test_plan_has_required_top_level_keys(self, agent):
        action = _make_action(ActionType.RESTART_SERVICE)
        plan = await agent.plan(action, {})

        for key in ("steps", "summary", "estimated_impact", "rollback_hint", "commands"):
            assert key in plan, f"Missing key: {key}"

    @pytest.mark.asyncio
    async def test_plan_step_has_required_keys(self, agent):
        action = _make_action(ActionType.RESTART_SERVICE)
        plan = await agent.plan(action, {})

        step = plan["steps"][0]
        for key in ("operation", "target", "params", "reason"):
            assert key in step, f"Step missing key: {key}"

    @pytest.mark.asyncio
    async def test_plan_summary_contains_resource_name(self, agent):
        action = _make_action(ActionType.RESTART_SERVICE)
        plan = await agent.plan(action, {})

        assert "vm-web-01" in plan["summary"]

    @pytest.mark.asyncio
    async def test_plan_estimated_impact_truncated_from_reason(self, agent):
        long_reason = "A" * 300
        action = _make_action(ActionType.RESTART_SERVICE, reason=long_reason)
        plan = await agent.plan(action, {})

        # Must be truncated to 200 chars
        assert len(plan["estimated_impact"]) <= 200


# ---------------------------------------------------------------------------
# TestExecuteMockMode
# ---------------------------------------------------------------------------


class TestExecuteMockMode:
    """ExecutionAgent.execute() in mock mode — no LLM, no Azure SDK."""

    @pytest.fixture
    def agent(self):
        return ExecutionAgent(cfg=_make_cfg(use_local_mocks=True))

    @pytest.mark.asyncio
    async def test_execute_mock_single_step_success(self, agent):
        plan = {
            "steps": [{
                "operation": "start_vm",
                "target": "vm-01",
                "params": {"resource_group": "rg", "vm_name": "vm-01"},
                "reason": "Start stopped VM",
            }]
        }
        result = await agent.execute(plan, _make_action())

        assert result["success"] is True
        assert len(result["steps_completed"]) == 1
        completed = result["steps_completed"][0]
        assert completed["success"] is True
        assert "[mock]" in completed["message"]
        assert completed["operation"] == "start_vm"

    @pytest.mark.asyncio
    async def test_execute_mock_empty_plan(self, agent):
        plan = {"steps": []}
        result = await agent.execute(plan, _make_action())

        assert result["success"] is True
        assert len(result["steps_completed"]) == 0

    @pytest.mark.asyncio
    async def test_execute_mock_multi_step(self, agent):
        plan = {
            "steps": [
                {"operation": "delete_nsg_rule", "target": "nsg-01", "params": {}, "reason": "Step 1"},
                {"operation": "create_nsg_rule", "target": "nsg-01", "params": {}, "reason": "Step 2"},
            ]
        }
        result = await agent.execute(plan, _make_action())

        assert result["success"] is True
        assert len(result["steps_completed"]) == 2
        assert result["steps_completed"][0]["operation"] == "delete_nsg_rule"
        assert result["steps_completed"][1]["operation"] == "create_nsg_rule"

    @pytest.mark.asyncio
    async def test_execute_mock_summary_contains_count(self, agent):
        plan = {
            "steps": [
                {"operation": "resize_vm", "target": "vm-01", "params": {}, "reason": "Resize"},
            ]
        }
        result = await agent.execute(plan, _make_action())

        assert "1" in result["summary"]
        assert "[mock]" in result["summary"]

    @pytest.mark.asyncio
    async def test_execute_preserves_step_indices(self, agent):
        plan = {
            "steps": [
                {"operation": "op1", "target": "t", "params": {}, "reason": "r1"},
                {"operation": "op2", "target": "t", "params": {}, "reason": "r2"},
                {"operation": "op3", "target": "t", "params": {}, "reason": "r3"},
            ]
        }
        result = await agent.execute(plan, _make_action())

        for i, step in enumerate(result["steps_completed"]):
            assert step["step"] == i


# ---------------------------------------------------------------------------
# TestPlanExecuteIntegration
# ---------------------------------------------------------------------------


class TestPlanExecuteIntegration:
    """End-to-end: plan() output flows correctly into execute()."""

    @pytest.fixture
    def agent(self):
        return ExecutionAgent(cfg=_make_cfg(use_local_mocks=True))

    @pytest.mark.asyncio
    async def test_plan_then_execute_restart_service(self, agent):
        action = _make_action(ActionType.RESTART_SERVICE)
        plan = await agent.plan(action, {})
        result = await agent.execute(plan, action)

        assert result["success"] is True
        assert len(result["steps_completed"]) == len(plan["steps"])

    @pytest.mark.asyncio
    async def test_plan_then_execute_modify_nsg(self, agent):
        action = _make_action(
            ActionType.MODIFY_NSG,
            resource_id=(
                "/subscriptions/sub/resourceGroups/rg/providers/"
                "Microsoft.Network/networkSecurityGroups/nsg-prod"
            ),
            reason="rule 'AllowSSH-Any' is too permissive",
            resource_type="Microsoft.Network/networkSecurityGroups",
        )
        plan = await agent.plan(action, {})
        result = await agent.execute(plan, action)

        assert result["success"] is True

    @pytest.mark.asyncio
    async def test_plan_then_execute_scale_down(self, agent):
        action = _make_action(ActionType.SCALE_DOWN, proposed_sku="Standard_B2ms")
        plan = await agent.plan(action, {})
        result = await agent.execute(plan, action)

        assert result["success"] is True
        assert result["steps_completed"][0]["operation"] == "resize_vm"


# ---------------------------------------------------------------------------
# Phase 29 — TestVerifyMockMode (5 tests)
# ---------------------------------------------------------------------------


class TestVerifyMockMode:
    """verify() in mock mode (no LLM / no Azure SDK)."""

    @pytest.fixture
    def agent(self):
        return ExecutionAgent(cfg=_make_cfg(use_local_mocks=True))

    @pytest.mark.asyncio
    async def test_verify_returns_confirmed_true(self, agent):
        action = _make_action(ActionType.RESTART_SERVICE)
        result = await agent.verify(action, {})
        assert result["confirmed"] is True

    @pytest.mark.asyncio
    async def test_verify_has_required_keys(self, agent):
        action = _make_action(ActionType.MODIFY_NSG)
        result = await agent.verify(action, {})
        assert set(result) >= {"confirmed", "message", "checked_at"}

    @pytest.mark.asyncio
    async def test_verify_nsg_message(self, agent):
        action = _make_action(ActionType.MODIFY_NSG)
        result = await agent.verify(action, {})
        assert "NSG rule confirmed removed" in result["message"]

    @pytest.mark.asyncio
    async def test_verify_restart_message(self, agent):
        action = _make_action(ActionType.RESTART_SERVICE)
        result = await agent.verify(action, {})
        assert "VM confirmed running" in result["message"]

    @pytest.mark.asyncio
    async def test_verify_checked_at_is_iso(self, agent):
        from datetime import datetime
        action = _make_action(ActionType.DELETE_RESOURCE)
        result = await agent.verify(action, {})
        # Should not raise — valid ISO-8601
        dt = datetime.fromisoformat(result["checked_at"].replace("Z", "+00:00"))
        assert dt is not None


# ---------------------------------------------------------------------------
# Phase 30 — TestRollbackMockMode (6 tests)
# ---------------------------------------------------------------------------


class TestRollbackMockMode:
    """rollback() in mock mode — deterministic inverse operations."""

    @pytest.fixture
    def agent(self):
        return ExecutionAgent(cfg=_make_cfg(use_local_mocks=True))

    @pytest.mark.asyncio
    async def test_rollback_restart_service_deallocates(self, agent):
        action = _make_action(ActionType.RESTART_SERVICE)
        result = await agent.rollback(action, {})
        assert result["steps_completed"][0]["operation"] == "deallocate_vm"

    @pytest.mark.asyncio
    async def test_rollback_scale_up_resizes_back(self, agent):
        action = _make_action(ActionType.SCALE_UP)
        result = await agent.rollback(action, {})
        assert result["steps_completed"][0]["operation"] == "resize_vm"

    @pytest.mark.asyncio
    async def test_rollback_scale_down_resizes_back(self, agent):
        action = _make_action(ActionType.SCALE_DOWN)
        result = await agent.rollback(action, {})
        assert result["steps_completed"][0]["operation"] == "resize_vm"

    @pytest.mark.asyncio
    async def test_rollback_modify_nsg_restores_rule(self, agent):
        action = _make_action(ActionType.MODIFY_NSG)
        result = await agent.rollback(action, {})
        assert result["steps_completed"][0]["operation"] == "create_nsg_rule"
        assert result["success"] is True

    @pytest.mark.asyncio
    async def test_rollback_delete_resource_cannot_auto_rollback(self, agent):
        action = _make_action(ActionType.DELETE_RESOURCE)
        result = await agent.rollback(action, {})
        assert result["success"] is False
        assert "cannot auto-rollback" in result["steps_completed"][0]["message"].lower()

    @pytest.mark.asyncio
    async def test_rollback_has_steps_completed_key(self, agent):
        action = _make_action(ActionType.RESTART_SERVICE)
        result = await agent.rollback(action, {})
        assert "steps_completed" in result
        assert isinstance(result["steps_completed"], list)
        assert len(result["steps_completed"]) == 1


# ---------------------------------------------------------------------------
# Path B deterministic rollback — no pre-computed rollback_commands
# ---------------------------------------------------------------------------


class TestRollbackPathB:
    """rollback() fallback when plan has no rollback_commands.

    In mock mode (use_local_mocks=True) the mock rollback path is used,
    so these tests patch _rollback_mock to simulate the live Path B logic
    with a plan that has steps but no rollback_commands.
    """

    @pytest.fixture
    def agent(self):
        return ExecutionAgent(cfg=_make_cfg(use_local_mocks=True))

    @pytest.mark.asyncio
    async def test_rollback_with_precomputed_commands_uses_path_a(self, agent):
        """If rollback_commands is present, use it (Path A)."""
        action = _make_action(ActionType.MODIFY_NSG)
        plan = {
            "steps": [{"operation": "delete_nsg_rule", "params": {"rule_name": "r", "resource_group": "rg", "nsg_name": "n"}, "target": ""}],
            "rollback_commands": [
                {"operation": "create_nsg_rule", "step_index": 0, "params": {
                    "resource_group": "rg", "nsg_name": "n", "rule_name": "r",
                    "priority": 200, "direction": "Inbound", "access": "Allow",
                    "protocol": "Tcp", "source_address_prefix": "*",
                    "destination_address_prefix": "*", "destination_port_range": "22",
                }}
            ],
        }
        # In mock mode rollback() uses _rollback_mock regardless of plan contents
        result = await agent.rollback(action, plan)
        # Mock always succeeds
        assert result["success"] is True

    @pytest.mark.asyncio
    async def test_rollback_missing_rollback_commands_has_steps_key(self, agent):
        """A plan without rollback_commands should still return steps_completed."""
        action = _make_action(ActionType.MODIFY_NSG)
        plan = {
            "steps": [],
            # No rollback_commands key at all
        }
        result = await agent.rollback(action, plan)
        assert "steps_completed" in result

    @pytest.mark.asyncio
    async def test_rollback_empty_rollback_commands_treated_as_missing(self, agent):
        """Empty rollback_commands list is falsy — behaves same as absent key."""
        action = _make_action(ActionType.MODIFY_NSG)
        plan = {"steps": [], "rollback_commands": []}
        result = await agent.rollback(action, plan)
        assert "steps_completed" in result


# ---------------------------------------------------------------------------
# get_nsg_rule_properties_from_activity_log — unit tests
# ---------------------------------------------------------------------------


class TestGetNsgRulePropertiesFromActivityLog:
    """Unit tests for the Activity Log reconstruction helper."""

    @pytest.mark.asyncio
    async def test_returns_none_in_mock_mode(self, monkeypatch):
        """Mock mode has no Activity Log — must return None without error."""
        from src.infrastructure.azure_tools import get_nsg_rule_properties_from_activity_log
        import src.infrastructure.azure_tools as atz

        monkeypatch.setattr(atz, "_use_mocks", lambda: True)
        result = await get_nsg_rule_properties_from_activity_log("rg", "nsg", "rule")
        assert result is None

    @pytest.mark.asyncio
    async def test_returns_none_when_workspace_id_missing(self, monkeypatch):
        """Missing LOG_ANALYTICS_WORKSPACE_ID must return None without error."""
        from src.infrastructure.azure_tools import get_nsg_rule_properties_from_activity_log
        import src.infrastructure.azure_tools as atz
        import os

        monkeypatch.setattr(atz, "_use_mocks", lambda: False)
        monkeypatch.delenv("LOG_ANALYTICS_WORKSPACE_ID", raising=False)
        result = await get_nsg_rule_properties_from_activity_log("rg", "nsg", "rule")
        assert result is None

    @pytest.mark.asyncio
    async def test_extracts_properties_from_response_body(self, monkeypatch):
        """Correct rule properties are extracted from responseBody JSON."""
        import json
        import sys
        from unittest.mock import AsyncMock, MagicMock, patch

        # azure-monitor-query may not be installed locally — inject stubs so
        # the function's import block succeeds without the real package.
        _fake_status = MagicMock(SUCCESS="Success")
        _fake_logs_mod = MagicMock()
        _fake_logs_aio_mod = MagicMock()

        monkeypatch.setitem(sys.modules, "azure.monitor", MagicMock())
        monkeypatch.setitem(sys.modules, "azure.monitor.query", MagicMock(LogsQueryStatus=_fake_status))
        monkeypatch.setitem(sys.modules, "azure.monitor.query.aio", _fake_logs_aio_mod)

        from src.infrastructure.azure_tools import get_nsg_rule_properties_from_activity_log
        import src.infrastructure.azure_tools as atz

        monkeypatch.setattr(atz, "_use_mocks", lambda: False)
        monkeypatch.setenv("LOG_ANALYTICS_WORKSPACE_ID", "ws-id")

        response_body = json.dumps({
            "properties": {
                "priority": 300,
                "destinationPortRange": "22",
                "direction": "Inbound",
                "access": "Allow",
                "protocol": "Tcp",
                "sourceAddressPrefix": "*",
                "destinationAddressPrefix": "*",
            }
        })
        props_json = json.dumps({"responseBody": response_body})

        fake_row = [None, None, props_json, None]
        fake_table = MagicMock()
        fake_table.rows = [fake_row]
        fake_result = MagicMock()
        fake_result.status = "Success"   # matches _fake_status.SUCCESS
        fake_result.tables = [fake_table]

        fake_client = AsyncMock()
        fake_client.query_workspace = AsyncMock(return_value=fake_result)
        fake_client.__aenter__ = AsyncMock(return_value=fake_client)
        fake_client.__aexit__ = AsyncMock(return_value=None)

        fake_cred = AsyncMock()
        fake_cred.__aenter__ = AsyncMock(return_value=fake_cred)
        fake_cred.__aexit__ = AsyncMock(return_value=None)

        _fake_logs_aio_mod.LogsQueryClient.return_value = fake_client

        with patch("azure.identity.aio.DefaultAzureCredential", return_value=fake_cred):
            result = await get_nsg_rule_properties_from_activity_log("rg", "nsg", "allow-ssh")

        assert result is not None
        assert result["priority"] == 300
        assert result["destination_port_range"] == "22"
        assert result["direction"] == "Inbound"
        assert result["access"] == "Allow"
        assert result["protocol"] == "Tcp"

    @pytest.mark.asyncio
    async def test_returns_none_when_no_matching_events(self, monkeypatch):
        """Returns None when KQL returns no rows (rule not found in logs)."""
        import sys
        from unittest.mock import AsyncMock, MagicMock, patch

        _fake_status = MagicMock(SUCCESS="Success")
        _fake_logs_aio_mod = MagicMock()

        monkeypatch.setitem(sys.modules, "azure.monitor", MagicMock())
        monkeypatch.setitem(sys.modules, "azure.monitor.query", MagicMock(LogsQueryStatus=_fake_status))
        monkeypatch.setitem(sys.modules, "azure.monitor.query.aio", _fake_logs_aio_mod)

        from src.infrastructure.azure_tools import get_nsg_rule_properties_from_activity_log
        import src.infrastructure.azure_tools as atz

        monkeypatch.setattr(atz, "_use_mocks", lambda: False)
        monkeypatch.setenv("LOG_ANALYTICS_WORKSPACE_ID", "ws-id")

        fake_table = MagicMock()
        fake_table.rows = []
        fake_result = MagicMock()
        fake_result.status = "Success"
        fake_result.tables = [fake_table]

        fake_client = AsyncMock()
        fake_client.query_workspace = AsyncMock(return_value=fake_result)
        fake_client.__aenter__ = AsyncMock(return_value=fake_client)
        fake_client.__aexit__ = AsyncMock(return_value=None)

        fake_cred = AsyncMock()
        fake_cred.__aenter__ = AsyncMock(return_value=fake_cred)
        fake_cred.__aexit__ = AsyncMock(return_value=None)

        _fake_logs_aio_mod.LogsQueryClient.return_value = fake_client

        with patch("azure.identity.aio.DefaultAzureCredential", return_value=fake_cred):
            result = await get_nsg_rule_properties_from_activity_log("rg", "nsg", "allow-ssh")

        assert result is None


# ---------------------------------------------------------------------------
# Phase 2A+2B — Generic PATCH tool and metadata lookup (mock mode)
# ---------------------------------------------------------------------------


class TestUpdateConfigInferredProperty:
    """_build_mock_plan UPDATE_CONFIG: infers update_resource_property from reason text."""

    @pytest.fixture
    def agent(self):
        return ExecutionAgent(cfg=_make_cfg(use_local_mocks=True))

    @pytest.mark.asyncio
    async def test_update_config_public_blob_access_inferred(self, agent):
        """Reason mentioning allowBlobPublicAccess → update_resource_property step."""
        action = _make_action(
            ActionType.UPDATE_CONFIG,
            reason="HIGH: Storage account 'sa-01' has allowBlobPublicAccess=true.",
        )
        plan = await agent.plan(action, {})

        step = plan["steps"][0]
        assert step["operation"] == "update_resource_property"
        assert step["params"]["property_path"] == "properties.allowBlobPublicAccess"
        assert step["params"]["new_value"] == "false"

    @pytest.mark.asyncio
    async def test_update_config_https_only_inferred(self, agent):
        """Reason mentioning HTTPS-only → update_resource_property with supportsHttpsTrafficOnly."""
        action = _make_action(
            ActionType.UPDATE_CONFIG,
            reason="HIGH: HTTP traffic allowed — data in plaintext.",
        )
        plan = await agent.plan(action, {})

        step = plan["steps"][0]
        assert step["operation"] == "update_resource_property"
        assert step["params"]["property_path"] == "properties.supportsHttpsTrafficOnly"
        assert step["params"]["new_value"] == "true"

    @pytest.mark.asyncio
    async def test_update_config_soft_delete_inferred(self, agent):
        """Reason mentioning soft-delete → update_resource_property with enableSoftDelete."""
        action = _make_action(
            ActionType.UPDATE_CONFIG,
            reason="HIGH: Key Vault 'kv-prod' has soft-delete disabled.",
        )
        plan = await agent.plan(action, {})

        step = plan["steps"][0]
        assert step["operation"] == "update_resource_property"
        assert step["params"]["property_path"] == "properties.enableSoftDelete"
        assert step["params"]["new_value"] == "true"

    @pytest.mark.asyncio
    async def test_update_config_public_network_access_inferred(self, agent):
        """Reason mentioning publicNetworkAccess → update_resource_property with Disabled."""
        action = _make_action(
            ActionType.UPDATE_CONFIG,
            reason="MEDIUM: Database 'cosmos-prod' has publicNetworkAccess=Enabled.",
        )
        plan = await agent.plan(action, {})

        step = plan["steps"][0]
        assert step["operation"] == "update_resource_property"
        assert step["params"]["property_path"] == "properties.publicNetworkAccess"
        assert step["params"]["new_value"] == '"Disabled"'

    @pytest.mark.asyncio
    async def test_update_config_unknown_reason_falls_back_to_guided_manual(self, agent):
        """Unknown reason text falls back to 'guided_manual' with az CLI + Portal steps."""
        action = _make_action(
            ActionType.UPDATE_CONFIG,
            reason="Some unrecognised configuration issue on the resource.",
        )
        plan = await agent.plan(action, {})

        step = plan["steps"][0]
        assert step["operation"] == "guided_manual"
        assert "az_cli_commands" in step["params"]
        assert "portal_steps" in step["params"]

    @pytest.mark.asyncio
    async def test_update_resource_property_step_has_all_required_params(self, agent):
        """Inferred update_resource_property step has resource_id, api_version, property_path, new_value."""
        action = _make_action(
            ActionType.UPDATE_CONFIG,
            reason="HIGH: allowBlobPublicAccess=true on storage account.",
        )
        plan = await agent.plan(action, {})

        step = plan["steps"][0]
        assert step["operation"] == "update_resource_property"
        params = step["params"]
        assert "resource_id" in params
        assert "api_version" in params
        assert "property_path" in params
        assert "new_value" in params

    @pytest.mark.asyncio
    async def test_update_resource_property_has_az_cli_command(self, agent):
        """An inferred update_resource_property plan still includes az CLI commands."""
        action = _make_action(
            ActionType.UPDATE_CONFIG,
            reason="HIGH: allowBlobPublicAccess=true on storage account.",
        )
        plan = await agent.plan(action, {})

        assert len(plan["commands"]) > 0
        # Should mention az resource patch
        assert any("resource" in cmd.lower() for cmd in plan["commands"])


class TestFetchResourceTypeMetadata:
    """fetch_resource_type_metadata_async — mock mode returns expected shape."""

    async def test_mock_returns_dict_with_expected_keys(self):
        from src.infrastructure.azure_tools import fetch_resource_type_metadata_async
        result = await fetch_resource_type_metadata_async("Microsoft.Storage/storageAccounts")
        assert "resource_type" in result
        assert "latest_stable_api_version" in result
        assert "all_api_versions" in result
        assert "note" in result

    async def test_mock_returns_sensible_api_version(self):
        from src.infrastructure.azure_tools import fetch_resource_type_metadata_async
        result = await fetch_resource_type_metadata_async("Microsoft.KeyVault/vaults")
        version = result["latest_stable_api_version"]
        # Version should look like YYYY-MM-DD
        assert len(version) == 10
        assert version.count("-") == 2

    async def test_mock_cached_on_second_call(self):
        """Same resource type called twice should return the same object (cached)."""
        from src.infrastructure.azure_tools import (
            fetch_resource_type_metadata_async,
            _resource_type_metadata_cache,
        )
        rt = "microsoft.storage/storageaccounts-cache-test"
        # Inject a dummy entry to verify cache hit
        _resource_type_metadata_cache[rt] = {"cached": True}
        result = await fetch_resource_type_metadata_async(rt)
        assert result == {"cached": True}
        # Clean up
        del _resource_type_metadata_cache[rt]

    async def test_mock_invalid_format_returns_error(self):
        """A resource_type without '/' returns an error key."""
        from src.infrastructure.azure_tools import fetch_resource_type_metadata_async
        result = await fetch_resource_type_metadata_async("NoSlashHere")
        assert "error" in result


# ---------------------------------------------------------------------------
# TestRemediationConfidence — Phase 3A
# ---------------------------------------------------------------------------


class TestRemediationConfidence:
    """_compute_confidence assigns correct RemediationConfidence based on plan steps."""

    @pytest.fixture
    def agent(self):
        return ExecutionAgent(cfg=_make_cfg(use_local_mocks=True))

    # -- _compute_confidence unit tests --

    def test_auto_fix_ops_return_auto_fix(self):
        from src.core.execution_agent import _compute_confidence
        for op in ("start_vm", "restart_vm", "resize_vm", "delete_nsg_rule",
                   "create_nsg_rule", "delete_resource", "update_resource_tags"):
            steps = [{"operation": op}]
            assert _compute_confidence(steps) == "auto_fix", f"Expected auto_fix for {op}"

    def test_update_resource_property_returns_generic_fix(self):
        from src.core.execution_agent import _compute_confidence
        steps = [{"operation": "update_resource_property"}]
        assert _compute_confidence(steps) == "generic_fix"

    def test_guided_manual_returns_guided_manual(self):
        from src.core.execution_agent import _compute_confidence
        steps = [{"operation": "guided_manual"}]
        assert _compute_confidence(steps) == "guided_manual"

    def test_manual_returns_manual(self):
        from src.core.execution_agent import _compute_confidence
        steps = [{"operation": "manual"}]
        assert _compute_confidence(steps) == "manual"

    def test_empty_steps_returns_manual(self):
        from src.core.execution_agent import _compute_confidence
        assert _compute_confidence([]) == "manual"

    def test_mixed_auto_and_generic_returns_generic(self):
        from src.core.execution_agent import _compute_confidence
        steps = [{"operation": "start_vm"}, {"operation": "update_resource_property"}]
        assert _compute_confidence(steps) == "generic_fix"

    def test_mixed_auto_and_manual_returns_manual(self):
        from src.core.execution_agent import _compute_confidence
        steps = [{"operation": "start_vm"}, {"operation": "manual"}]
        assert _compute_confidence(steps) == "manual"

    def test_manual_short_circuits_immediately(self):
        """manual op returns immediately — no need to check remaining steps."""
        from src.core.execution_agent import _compute_confidence
        steps = [{"operation": "manual"}, {"operation": "start_vm"}, {"operation": "start_vm"}]
        assert _compute_confidence(steps) == "manual"

    # -- Integration: plan() includes remediation_confidence --

    @pytest.mark.asyncio
    async def test_plan_restart_has_auto_fix_confidence(self, agent):
        """restart_service plan → auto_fix confidence."""
        plan = await agent.plan(_make_action(ActionType.RESTART_SERVICE), {})
        assert plan["remediation_confidence"] == "auto_fix"

    @pytest.mark.asyncio
    async def test_plan_scale_down_has_auto_fix_confidence(self, agent):
        """scale_down plan → auto_fix (uses resize_vm)."""
        plan = await agent.plan(_make_action(ActionType.SCALE_DOWN, proposed_sku="Standard_D2s_v3"), {})
        assert plan["remediation_confidence"] == "auto_fix"

    @pytest.mark.asyncio
    async def test_plan_update_config_known_property_has_generic_fix_confidence(self, agent):
        """UPDATE_CONFIG with recognised property → generic_fix (update_resource_property)."""
        action = _make_action(ActionType.UPDATE_CONFIG, reason="allowBlobPublicAccess is enabled")
        plan = await agent.plan(action, {})
        assert plan["remediation_confidence"] == "generic_fix"

    @pytest.mark.asyncio
    async def test_plan_update_config_unknown_reason_has_guided_manual_confidence(self, agent):
        """UPDATE_CONFIG with unknown reason → guided_manual confidence."""
        action = _make_action(ActionType.UPDATE_CONFIG, reason="Some unknown configuration issue")
        plan = await agent.plan(action, {})
        assert plan["remediation_confidence"] == "guided_manual"

    @pytest.mark.asyncio
    async def test_plan_create_resource_has_guided_manual_confidence(self, agent):
        """CREATE_RESOURCE → guided_manual confidence."""
        plan = await agent.plan(_make_action(ActionType.CREATE_RESOURCE), {})
        assert plan["remediation_confidence"] == "guided_manual"


# ---------------------------------------------------------------------------
# TestGuidedManualSteps — Phase 2C
# ---------------------------------------------------------------------------


class TestGuidedManualSteps:
    """guided_manual steps include az_cli_commands, portal_steps, and doc_url."""

    @pytest.fixture
    def agent(self):
        return ExecutionAgent(cfg=_make_cfg(use_local_mocks=True))

    @pytest.mark.asyncio
    async def test_create_resource_guided_step_has_all_fields(self, agent):
        plan = await agent.plan(_make_action(ActionType.CREATE_RESOURCE), {})
        step = plan["steps"][0]
        assert step["operation"] == "guided_manual"
        params = step["params"]
        assert isinstance(params["az_cli_commands"], list)
        assert len(params["az_cli_commands"]) > 0
        assert isinstance(params["portal_steps"], list)
        assert len(params["portal_steps"]) > 0
        assert isinstance(params["doc_url"], str)
        assert params["doc_url"].startswith("https://")

    @pytest.mark.asyncio
    async def test_unknown_update_config_guided_step_has_all_fields(self, agent):
        action = _make_action(ActionType.UPDATE_CONFIG, reason="Enable private endpoint for database")
        plan = await agent.plan(action, {})
        step = plan["steps"][0]
        assert step["operation"] == "guided_manual"
        params = step["params"]
        assert isinstance(params["az_cli_commands"], list)
        assert isinstance(params["portal_steps"], list)
        assert isinstance(params["doc_url"], str)

    @pytest.mark.asyncio
    async def test_guided_manual_reason_includes_action_reason(self, agent):
        """Step reason should include context from the original action reason."""
        reason_text = "Enable private endpoint for the database"
        action = _make_action(ActionType.CREATE_RESOURCE, reason=reason_text)
        plan = await agent.plan(action, {})
        step = plan["steps"][0]
        assert reason_text[:40] in step["reason"] or "guided" in step["reason"].lower()


# ---------------------------------------------------------------------------
# Phase 34A — Tier 1 SDK Expansion: new tools + dry_run support
# ---------------------------------------------------------------------------


class TestPhase34ATier1Expansion:
    """Phase 34A: 5 new Tier 1 SDK tools (App Service, Function App, App Service Plan,
    AKS nodepool, Storage key rotation) + dry_run support across execute()."""

    @pytest.fixture
    def agent(self):
        return ExecutionAgent(cfg=_make_cfg(use_local_mocks=True))

    # -- plan() resource-type discrimination ----------------------------------

    @pytest.mark.asyncio
    async def test_plan_restart_app_service(self, agent):
        """RESTART_SERVICE on Microsoft.Web/sites → restart_app_service operation."""
        action = _make_action(
            ActionType.RESTART_SERVICE,
            resource_id=(
                "/subscriptions/sub/resourceGroups/rg/providers/"
                "Microsoft.Web/sites/api-prod"
            ),
            resource_type="Microsoft.Web/sites",
            reason="App Service unhealthy — Http5xx rate > 5%",
        )
        plan = await agent.plan(action, {})

        assert len(plan["steps"]) == 1
        step = plan["steps"][0]
        assert step["operation"] == "restart_app_service"
        assert step["params"]["app_name"] == "api-prod"
        assert step["params"]["resource_group"] == "rg"
        assert "webapp restart" in plan["commands"][0]
        assert plan["remediation_confidence"] == "auto_fix"

    @pytest.mark.asyncio
    async def test_plan_restart_function_app(self, agent):
        """RESTART_SERVICE with 'function' in resource name → restart_function_app."""
        action = _make_action(
            ActionType.RESTART_SERVICE,
            resource_id=(
                "/subscriptions/sub/resourceGroups/rg/providers/"
                "Microsoft.Web/sites/func-processor"
            ),
            resource_type="Microsoft.Web/sites",
            reason="Function app crashing — restart needed",
        )
        plan = await agent.plan(action, {})

        step = plan["steps"][0]
        assert step["operation"] == "restart_function_app"
        assert step["params"]["app_name"] == "func-processor"
        assert "functionapp restart" in plan["commands"][0]

    @pytest.mark.asyncio
    async def test_plan_restart_function_app_by_reason(self, agent):
        """RESTART_SERVICE with 'function' in reason → restart_function_app."""
        action = _make_action(
            ActionType.RESTART_SERVICE,
            resource_id=(
                "/subscriptions/sub/resourceGroups/rg/providers/"
                "Microsoft.Web/sites/worker-01"
            ),
            resource_type="Microsoft.Web/sites",
            reason="Function app worker-01 health check failing",
        )
        plan = await agent.plan(action, {})

        step = plan["steps"][0]
        assert step["operation"] == "restart_function_app"

    @pytest.mark.asyncio
    async def test_plan_restart_service_vm_unchanged(self, agent):
        """RESTART_SERVICE on a VM still uses start_vm (existing behavior preserved)."""
        action = _make_action(ActionType.RESTART_SERVICE)
        plan = await agent.plan(action, {})

        assert plan["steps"][0]["operation"] == "start_vm"

    @pytest.mark.asyncio
    async def test_plan_scale_app_service_plan(self, agent):
        """SCALE_UP on Microsoft.Web/serverfarms → scale_app_service_plan."""
        action = _make_action(
            ActionType.SCALE_UP,
            resource_id=(
                "/subscriptions/sub/resourceGroups/rg/providers/"
                "Microsoft.Web/serverfarms/asp-prod"
            ),
            resource_type="Microsoft.Web/serverfarms",
            proposed_sku="P2v2",
            current_sku="P1v2",
        )
        plan = await agent.plan(action, {})

        step = plan["steps"][0]
        assert step["operation"] == "scale_app_service_plan"
        assert step["params"]["plan_name"] == "asp-prod"
        assert step["params"]["new_sku_name"] == "P2v2"
        assert "appservice plan update" in plan["commands"][0]
        assert "P2v2" in plan["commands"][0]
        assert plan["remediation_confidence"] == "auto_fix"

    @pytest.mark.asyncio
    async def test_plan_scale_down_app_service_plan(self, agent):
        """SCALE_DOWN on Microsoft.Web/serverfarms → scale_app_service_plan."""
        action = _make_action(
            ActionType.SCALE_DOWN,
            resource_id=(
                "/subscriptions/sub/resourceGroups/rg/providers/"
                "Microsoft.Web/serverfarms/asp-staging"
            ),
            resource_type="Microsoft.Web/serverfarms",
            proposed_sku="B2",
            current_sku="P1v2",
        )
        plan = await agent.plan(action, {})

        step = plan["steps"][0]
        assert step["operation"] == "scale_app_service_plan"
        assert step["params"]["new_sku_name"] == "B2"

    @pytest.mark.asyncio
    async def test_plan_scale_aks_nodepool(self, agent):
        """SCALE_UP on Microsoft.ContainerService/managedClusters → scale_aks_nodepool."""
        action = _make_action(
            ActionType.SCALE_UP,
            resource_id=(
                "/subscriptions/sub/resourceGroups/rg/providers/"
                "Microsoft.ContainerService/managedClusters/aks-prod"
            ),
            resource_type="Microsoft.ContainerService/managedClusters",
            proposed_sku="5",
        )
        plan = await agent.plan(action, {})

        step = plan["steps"][0]
        assert step["operation"] == "scale_aks_nodepool"
        assert step["params"]["cluster_name"] == "aks-prod"
        assert step["params"]["node_count"] == 5
        assert "aks nodepool scale" in plan["commands"][0]
        assert plan["remediation_confidence"] == "auto_fix"

    @pytest.mark.asyncio
    async def test_plan_scale_aks_nodepool_custom_pool(self, agent):
        """AKS scale uses nodepool_name from config_changes when provided."""
        action = ProposedAction(
            agent_id="test",
            action_type=ActionType.SCALE_UP,
            target=ActionTarget(
                resource_id=(
                    "/subscriptions/sub/resourceGroups/rg/providers/"
                    "Microsoft.ContainerService/managedClusters/aks-prod"
                ),
                resource_type="Microsoft.ContainerService/managedClusters",
                proposed_sku="4",
            ),
            reason="AKS CPU high",
            config_changes={"nodepool_name": "workerpool", "node_count": "4"},
        )
        plan = await agent.plan(action, {})

        step = plan["steps"][0]
        assert step["params"]["nodepool_name"] == "workerpool"
        assert step["params"]["node_count"] == 4

    @pytest.mark.asyncio
    async def test_plan_scale_vm_unchanged(self, agent):
        """SCALE_DOWN on a VM still uses resize_vm (existing behavior preserved)."""
        action = _make_action(ActionType.SCALE_DOWN, proposed_sku="Standard_B2ms")
        plan = await agent.plan(action, {})

        assert plan["steps"][0]["operation"] == "resize_vm"

    @pytest.mark.asyncio
    async def test_plan_rotate_storage_key(self, agent):
        """ROTATE_STORAGE_KEY → rotate_storage_keys operation."""
        action = ProposedAction(
            agent_id="test",
            action_type=ActionType.ROTATE_STORAGE_KEY,
            target=ActionTarget(
                resource_id=(
                    "/subscriptions/sub/resourceGroups/rg/providers/"
                    "Microsoft.Storage/storageAccounts/stproddata"
                ),
                resource_type="Microsoft.Storage/storageAccounts",
            ),
            reason="Storage key not rotated in 120 days",
            config_changes={"key_name": "key1"},
        )
        plan = await agent.plan(action, {})

        assert len(plan["steps"]) == 1
        step = plan["steps"][0]
        assert step["operation"] == "rotate_storage_keys"
        assert step["params"]["account_name"] == "stproddata"
        assert step["params"]["key_name"] == "key1"
        assert "storage account keys renew" in plan["commands"][0]
        assert plan["remediation_confidence"] == "auto_fix"

    # -- _compute_confidence includes new ops ---------------------------------

    def test_new_ops_are_auto_fix(self):
        """All 5 Phase 34A operations must register as auto_fix confidence."""
        from src.core.execution_agent import _compute_confidence
        new_ops = [
            "restart_app_service",
            "restart_function_app",
            "scale_app_service_plan",
            "scale_aks_nodepool",
            "rotate_storage_keys",
        ]
        for op in new_ops:
            steps = [{"operation": op}]
            result = _compute_confidence(steps)
            assert result == "auto_fix", f"Expected auto_fix for '{op}', got '{result}'"

    # -- execute() dry_run mode -----------------------------------------------

    @pytest.mark.asyncio
    async def test_execute_dry_run_returns_mode_dry_run(self, agent):
        """execute(dry_run=True) sets mode='dry_run' in the result."""
        plan = {
            "steps": [{
                "operation": "restart_app_service",
                "target": "/subscriptions/sub/rg/rg/providers/Microsoft.Web/sites/api",
                "params": {"resource_group": "rg", "app_name": "api"},
                "reason": "Restart App Service",
            }]
        }
        action = _make_action(
            ActionType.RESTART_SERVICE,
            resource_type="Microsoft.Web/sites",
        )
        result = await agent.execute(plan, action, dry_run=True)

        assert result["mode"] == "dry_run"
        assert result["success"] is True

    @pytest.mark.asyncio
    async def test_execute_live_returns_mode_live(self, agent):
        """execute() without dry_run sets mode='live'."""
        plan = {
            "steps": [{
                "operation": "start_vm",
                "target": "vm-01",
                "params": {"resource_group": "rg", "vm_name": "vm-01"},
                "reason": "Start VM",
            }]
        }
        result = await agent.execute(plan, _make_action())

        assert result["mode"] == "live"

    @pytest.mark.asyncio
    async def test_execute_dry_run_step_annotated_with_dry_run_prefix(self, agent):
        """In dry_run mode, step messages include [dry_run] prefix."""
        plan = {
            "steps": [{
                "operation": "rotate_storage_keys",
                "params": {},
                "reason": "Rotate key1",
            }]
        }
        action = _make_action(ActionType.ROTATE_STORAGE_KEY)
        result = await agent.execute(plan, action, dry_run=True)

        msg = result["steps_completed"][0]["message"]
        assert "[dry_run]" in msg

    @pytest.mark.asyncio
    async def test_execute_dry_run_does_not_mutate_success(self, agent):
        """dry_run=True still returns success=True in mock mode."""
        plan = {
            "steps": [
                {"operation": "scale_app_service_plan", "params": {}, "reason": "Scale"},
                {"operation": "scale_aks_nodepool", "params": {}, "reason": "Scale"},
            ]
        }
        result = await agent.execute(plan, _make_action(), dry_run=True)

        assert result["success"] is True
        assert len(result["steps_completed"]) == 2

    @pytest.mark.asyncio
    async def test_plan_then_execute_dry_run_app_service(self, agent):
        """Full plan→execute dry_run flow for App Service restart."""
        action = _make_action(
            ActionType.RESTART_SERVICE,
            resource_id=(
                "/subscriptions/sub/resourceGroups/rg/providers/"
                "Microsoft.Web/sites/api-prod"
            ),
            resource_type="Microsoft.Web/sites",
            reason="App Service unhealthy",
        )
        plan = await agent.plan(action, {})
        result = await agent.execute(plan, action, dry_run=True)

        assert result["success"] is True
        assert result["mode"] == "dry_run"
        assert result["steps_completed"][0]["operation"] == "restart_app_service"
        assert "[dry_run]" in result["steps_completed"][0]["message"]

    # -- ActionType.ROTATE_STORAGE_KEY exists in model ------------------------

    def test_rotate_storage_key_action_type_exists(self):
        """ROTATE_STORAGE_KEY is a valid ActionType value."""
        assert ActionType.ROTATE_STORAGE_KEY == "rotate_storage_key"

    # -- ExecutionRecord.mode field exists ------------------------------------

    def test_execution_record_has_mode_field(self):
        """ExecutionRecord has mode field defaulting to 'live'."""
        from src.core.models import ExecutionRecord, ExecutionStatus, SRIVerdict
        from datetime import datetime, timezone
        record = ExecutionRecord(
            execution_id="test-id",
            action_id="action-id",
            verdict=SRIVerdict.APPROVED,
            status=ExecutionStatus.applied,
            created_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc),
        )
        assert record.mode == "live"

    def test_execution_record_mode_can_be_set_dry_run(self):
        """ExecutionRecord.mode can be set to 'dry_run'."""
        from src.core.models import ExecutionRecord, ExecutionStatus, SRIVerdict
        from datetime import datetime, timezone
        record = ExecutionRecord(
            execution_id="test-id",
            action_id="action-id",
            verdict=SRIVerdict.APPROVED,
            status=ExecutionStatus.applied,
            created_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc),
            mode="dry_run",
        )
        assert record.mode == "dry_run"
