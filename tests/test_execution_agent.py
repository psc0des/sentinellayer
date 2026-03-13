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
    async def test_plan_update_config(self, agent):
        action = _make_action(ActionType.UPDATE_CONFIG)
        plan = await agent.plan(action, {})

        assert plan["steps"][0]["operation"] == "update_resource_tags"

    @pytest.mark.asyncio
    async def test_plan_create_resource_is_manual(self, agent):
        action = _make_action(ActionType.CREATE_RESOURCE)
        plan = await agent.plan(action, {})

        assert plan["steps"][0]["operation"] == "manual"

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
