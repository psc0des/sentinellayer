"""Tests for the ActionInterceptor — SentinelLayer's governance entry point.

These tests check the ActionInterceptor in isolation by *mocking* the
SentinelLayerPipeline and DecisionTracker.  Mocking means we replace the real
objects with fake ones that return pre-defined answers.

Why mock instead of using real objects?
- The real pipeline loads data files and runs four agents — that is slow for a
  unit test.
- We want to test only the interception *logic* (does it call the pipeline?
  does it call the tracker? does it format the result correctly?) without
  worrying about what the agents actually score.
- Using mocks makes these tests fast and deterministic (same input always
  produces the same output, regardless of the data files).
"""

from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest

import src.core.interception as interception_module
from src.core.interception import ActionInterceptor, get_interceptor
from src.core.models import (
    ActionTarget,
    ActionType,
    GovernanceVerdict,
    ProposedAction,
    SRIBreakdown,
    SRIVerdict,
    Urgency,
)


# ---------------------------------------------------------------------------
# Test helpers — tiny factories to avoid repeating boilerplate
# ---------------------------------------------------------------------------


def _make_action(
    resource_id: str = "vm-test",
    action_type: ActionType = ActionType.SCALE_DOWN,
    agent_id: str = "test-agent",
) -> ProposedAction:
    """Return a minimal valid ProposedAction."""
    return ProposedAction(
        agent_id=agent_id,
        action_type=action_type,
        target=ActionTarget(
            resource_id=resource_id,
            resource_type="Microsoft.Compute/virtualMachines",
        ),
        reason="Unit-test action",
        urgency=Urgency.LOW,
    )


def _make_verdict(
    action: ProposedAction | None = None,
    decision: SRIVerdict = SRIVerdict.APPROVED,
    composite: float = 7.15,
) -> GovernanceVerdict:
    """Return a minimal GovernanceVerdict for use as a mock return value."""
    if action is None:
        action = _make_action()
    return GovernanceVerdict(
        action_id="test-uuid-1234",
        timestamp=datetime(2026, 2, 20, 12, 0, 0, tzinfo=timezone.utc),
        proposed_action=action,
        sentinel_risk_index=SRIBreakdown(
            sri_infrastructure=10.0,
            sri_policy=5.0,
            sri_historical=8.0,
            sri_cost=3.0,
            sri_composite=composite,
        ),
        decision=decision,
        reason=f"{decision.value.upper()} — SRI Composite {composite} is within threshold.",
        agent_results={},
    )


def _make_interceptor(
    action: ProposedAction | None = None,
    verdict: GovernanceVerdict | None = None,
) -> tuple[ActionInterceptor, MagicMock, MagicMock, ProposedAction, GovernanceVerdict]:
    """Build an ActionInterceptor backed by mocked pipeline and tracker.

    Returns a tuple of (interceptor, mock_pipeline, mock_tracker, action, verdict)
    so individual tests can inspect the mocks after calling methods.
    """
    if action is None:
        action = _make_action()
    if verdict is None:
        verdict = _make_verdict(action)

    mock_pipeline = MagicMock()
    mock_pipeline.evaluate.return_value = verdict

    mock_tracker = MagicMock()

    interceptor = ActionInterceptor(pipeline=mock_pipeline, tracker=mock_tracker)
    return interceptor, mock_pipeline, mock_tracker, action, verdict


# ---------------------------------------------------------------------------
# 1. Construction tests
# ---------------------------------------------------------------------------


class TestActionInterceptorConstruction:
    """The ActionInterceptor can be built in different ways."""

    def test_accepts_injected_pipeline_and_tracker(self):
        """When both objects are injected, no real data loading happens."""
        mock_pipeline = MagicMock()
        mock_tracker = MagicMock()
        interceptor = ActionInterceptor(pipeline=mock_pipeline, tracker=mock_tracker)
        assert interceptor is not None

    def test_returns_action_interceptor_instance(self):
        mock_pipeline = MagicMock()
        mock_tracker = MagicMock()
        interceptor = ActionInterceptor(pipeline=mock_pipeline, tracker=mock_tracker)
        assert isinstance(interceptor, ActionInterceptor)


# ---------------------------------------------------------------------------
# 2. intercept() — the direct Python entry point
# ---------------------------------------------------------------------------


class TestIntercept:
    """Tests for ActionInterceptor.intercept(action) -> GovernanceVerdict."""

    def test_returns_governance_verdict(self):
        """intercept() must always return a GovernanceVerdict object."""
        interceptor, _, _, action, _ = _make_interceptor()
        result = interceptor.intercept(action)
        assert isinstance(result, GovernanceVerdict)

    def test_calls_pipeline_evaluate_exactly_once(self):
        """intercept() must hand the action to the pipeline exactly once."""
        interceptor, mock_pipeline, _, action, _ = _make_interceptor()
        interceptor.intercept(action)
        mock_pipeline.evaluate.assert_called_once_with(action)

    def test_calls_tracker_record_exactly_once(self):
        """intercept() must record the verdict in the audit trail."""
        interceptor, _, mock_tracker, action, verdict = _make_interceptor()
        interceptor.intercept(action)
        mock_tracker.record.assert_called_once_with(verdict)

    def test_returns_the_verdict_from_the_pipeline(self):
        """The verdict returned by intercept() is the one the pipeline produced."""
        interceptor, _, _, action, expected_verdict = _make_interceptor()
        result = interceptor.intercept(action)
        assert result.action_id == expected_verdict.action_id

    def test_decision_matches_mock_verdict(self):
        """The decision in the returned verdict matches what the mock returned."""
        interceptor, _, _, action, _ = _make_interceptor()
        result = interceptor.intercept(action)
        assert result.decision == SRIVerdict.APPROVED

    def test_denied_verdict_is_passed_through(self):
        """intercept() does not modify the verdict — DENIED stays DENIED."""
        action = _make_action()
        denied_verdict = _make_verdict(action, decision=SRIVerdict.DENIED, composite=85.0)
        interceptor, _, _, _, _ = _make_interceptor(action=action, verdict=denied_verdict)
        result = interceptor.intercept(action)
        assert result.decision == SRIVerdict.DENIED

    def test_escalated_verdict_is_passed_through(self):
        """intercept() does not modify the verdict — ESCALATED stays ESCALATED."""
        action = _make_action()
        escalated_verdict = _make_verdict(action, decision=SRIVerdict.ESCALATED, composite=40.0)
        interceptor, _, _, _, _ = _make_interceptor(action=action, verdict=escalated_verdict)
        result = interceptor.intercept(action)
        assert result.decision == SRIVerdict.ESCALATED

    def test_tracker_record_called_before_return(self):
        """Tracking must happen during intercept(), not after the caller gets the verdict."""
        interceptor, _, mock_tracker, action, _ = _make_interceptor()
        interceptor.intercept(action)
        # If record was called, the mock remembers it
        assert mock_tracker.record.called

    def test_pipeline_receives_same_action_object(self):
        """The exact action passed to intercept() is forwarded to the pipeline."""
        interceptor, mock_pipeline, _, action, _ = _make_interceptor()
        interceptor.intercept(action)
        call_args = mock_pipeline.evaluate.call_args
        # call_args[0][0] is the first positional argument
        assert call_args[0][0] is action


# ---------------------------------------------------------------------------
# 3. intercept_from_dict() — the MCP / dict entry point
# ---------------------------------------------------------------------------


class TestInterceptFromDict:
    """Tests for ActionInterceptor.intercept_from_dict(data) -> dict."""

    # --- Helper: minimal valid dict ---

    def _valid_data(self) -> dict:
        return {
            "resource_id": "vm-test",
            "resource_type": "Microsoft.Compute/virtualMachines",
            "action_type": "scale_down",
            "agent_id": "cost-agent",
            "reason": "VM idle for testing",
        }

    # --- Result shape ---

    def test_valid_dict_returns_a_dict(self):
        """Happy-path: valid input returns a plain dict."""
        interceptor, _, _, _, _ = _make_interceptor()
        result = interceptor.intercept_from_dict(self._valid_data())
        assert isinstance(result, dict)

    def test_result_contains_action_id(self):
        interceptor, _, _, _, _ = _make_interceptor()
        result = interceptor.intercept_from_dict(self._valid_data())
        assert "action_id" in result
        assert isinstance(result["action_id"], str)

    def test_result_contains_decision(self):
        interceptor, _, _, _, _ = _make_interceptor()
        result = interceptor.intercept_from_dict(self._valid_data())
        assert "decision" in result
        assert result["decision"] in ("approved", "escalated", "denied")

    def test_result_decision_is_string_not_enum(self):
        """MCP callers expect a plain string, not a Python Enum object."""
        interceptor, _, _, _, _ = _make_interceptor()
        result = interceptor.intercept_from_dict(self._valid_data())
        assert isinstance(result["decision"], str)

    def test_result_contains_reason(self):
        interceptor, _, _, _, _ = _make_interceptor()
        result = interceptor.intercept_from_dict(self._valid_data())
        assert "reason" in result
        assert len(result["reason"]) > 0

    def test_result_contains_sri_composite(self):
        interceptor, _, _, _, _ = _make_interceptor()
        result = interceptor.intercept_from_dict(self._valid_data())
        assert "sri_composite" in result
        assert isinstance(result["sri_composite"], float)

    def test_result_contains_sri_breakdown(self):
        interceptor, _, _, _, _ = _make_interceptor()
        result = interceptor.intercept_from_dict(self._valid_data())
        assert "sri_breakdown" in result
        assert isinstance(result["sri_breakdown"], dict)

    def test_sri_breakdown_has_four_dimensions(self):
        """The breakdown must have exactly the four SRI dimension keys."""
        interceptor, _, _, _, _ = _make_interceptor()
        result = interceptor.intercept_from_dict(self._valid_data())
        breakdown = result["sri_breakdown"]
        assert "infrastructure" in breakdown
        assert "policy" in breakdown
        assert "historical" in breakdown
        assert "cost" in breakdown

    def test_result_contains_thresholds(self):
        interceptor, _, _, _, _ = _make_interceptor()
        result = interceptor.intercept_from_dict(self._valid_data())
        assert "thresholds" in result
        assert "auto_approve" in result["thresholds"]
        assert "human_review" in result["thresholds"]

    def test_result_contains_timestamp_as_string(self):
        """Timestamps must be ISO strings, not datetime objects, for JSON safety."""
        interceptor, _, _, _, _ = _make_interceptor()
        result = interceptor.intercept_from_dict(self._valid_data())
        assert "timestamp" in result
        assert isinstance(result["timestamp"], str)

    def test_result_contains_resource_id(self):
        interceptor, _, _, _, _ = _make_interceptor()
        result = interceptor.intercept_from_dict(self._valid_data())
        assert "resource_id" in result

    def test_result_contains_agent_id(self):
        interceptor, _, _, _, _ = _make_interceptor()
        result = interceptor.intercept_from_dict(self._valid_data())
        assert "agent_id" in result

    # --- Pipeline is still called ---

    def test_still_calls_pipeline_evaluate(self):
        """intercept_from_dict must delegate to the pipeline, not bypass it."""
        interceptor, mock_pipeline, _, _, _ = _make_interceptor()
        interceptor.intercept_from_dict(self._valid_data())
        mock_pipeline.evaluate.assert_called_once()

    def test_still_calls_tracker_record(self):
        """intercept_from_dict must record the verdict in the audit trail."""
        interceptor, _, mock_tracker, _, _ = _make_interceptor()
        interceptor.intercept_from_dict(self._valid_data())
        mock_tracker.record.assert_called_once()

    # --- Optional fields ---

    def test_accepts_optional_urgency(self):
        """urgency is optional; when provided, it must be accepted."""
        interceptor, _, _, _, _ = _make_interceptor()
        data = {**self._valid_data(), "urgency": "high"}
        result = interceptor.intercept_from_dict(data)
        assert isinstance(result, dict)

    def test_defaults_urgency_to_medium(self):
        """When urgency is omitted, the method should not raise."""
        interceptor, mock_pipeline, _, _, _ = _make_interceptor()
        data = self._valid_data()  # no 'urgency' key
        interceptor.intercept_from_dict(data)
        call_action: ProposedAction = mock_pipeline.evaluate.call_args[0][0]
        assert call_action.urgency == Urgency.MEDIUM

    def test_accepts_optional_current_monthly_cost(self):
        interceptor, _, _, _, _ = _make_interceptor()
        data = {**self._valid_data(), "current_monthly_cost": 847.0}
        result = interceptor.intercept_from_dict(data)
        assert isinstance(result, dict)

    def test_accepts_optional_sku_fields(self):
        interceptor, _, _, _, _ = _make_interceptor()
        data = {
            **self._valid_data(),
            "current_sku": "Standard_D4s_v3",
            "proposed_sku": "Standard_D2s_v3",
        }
        result = interceptor.intercept_from_dict(data)
        assert isinstance(result, dict)

    # --- Error cases ---

    def test_missing_resource_id_raises_value_error(self):
        """Missing required field must raise ValueError, not KeyError."""
        interceptor, _, _, _, _ = _make_interceptor()
        data = self._valid_data()
        del data["resource_id"]
        with pytest.raises(ValueError):
            interceptor.intercept_from_dict(data)

    def test_missing_resource_type_raises_value_error(self):
        interceptor, _, _, _, _ = _make_interceptor()
        data = self._valid_data()
        del data["resource_type"]
        with pytest.raises(ValueError):
            interceptor.intercept_from_dict(data)

    def test_missing_agent_id_raises_value_error(self):
        interceptor, _, _, _, _ = _make_interceptor()
        data = self._valid_data()
        del data["agent_id"]
        with pytest.raises(ValueError):
            interceptor.intercept_from_dict(data)

    def test_missing_reason_raises_value_error(self):
        interceptor, _, _, _, _ = _make_interceptor()
        data = self._valid_data()
        del data["reason"]
        with pytest.raises(ValueError):
            interceptor.intercept_from_dict(data)

    def test_invalid_action_type_raises_value_error(self):
        """An action_type not in the ActionType enum must raise ValueError."""
        interceptor, _, _, _, _ = _make_interceptor()
        data = {**self._valid_data(), "action_type": "fly_to_moon"}
        with pytest.raises(ValueError):
            interceptor.intercept_from_dict(data)

    def test_invalid_urgency_raises_value_error(self):
        """An urgency value not in the Urgency enum must raise ValueError."""
        interceptor, _, _, _, _ = _make_interceptor()
        data = {**self._valid_data(), "urgency": "super-hyper-critical"}
        with pytest.raises(ValueError):
            interceptor.intercept_from_dict(data)

    def test_all_valid_action_types_accepted(self):
        """Every legal ActionType string must be accepted without error."""
        interceptor, _, _, _, _ = _make_interceptor()
        for action_type in ActionType:
            data = {**self._valid_data(), "action_type": action_type.value}
            result = interceptor.intercept_from_dict(data)
            assert isinstance(result, dict), f"Failed for action_type={action_type.value}"

    def test_all_valid_urgency_levels_accepted(self):
        """Every legal Urgency string must be accepted without error."""
        interceptor, _, _, _, _ = _make_interceptor()
        for urgency in Urgency:
            data = {**self._valid_data(), "urgency": urgency.value}
            result = interceptor.intercept_from_dict(data)
            assert isinstance(result, dict), f"Failed for urgency={urgency.value}"


# ---------------------------------------------------------------------------
# 4. get_interceptor() singleton
# ---------------------------------------------------------------------------


class TestGetInterceptorSingleton:
    """get_interceptor() should return a shared ActionInterceptor singleton."""

    def test_returns_action_interceptor_instance(self):
        """get_interceptor() must return an ActionInterceptor, not None."""
        interception_module._interceptor = None  # reset
        with patch("src.core.interception.SentinelLayerPipeline"), \
             patch("src.core.interception.DecisionTracker"):
            result = get_interceptor()
            assert isinstance(result, ActionInterceptor)
        interception_module._interceptor = None  # clean up

    def test_returns_same_instance_on_repeated_calls(self):
        """Second call must return exactly the same object (singleton)."""
        interception_module._interceptor = None  # reset
        with patch("src.core.interception.SentinelLayerPipeline"), \
             patch("src.core.interception.DecisionTracker"):
            i1 = get_interceptor()
            i2 = get_interceptor()
            assert i1 is i2  # same object in memory
        interception_module._interceptor = None  # clean up

    def test_pipeline_constructed_only_once(self):
        """SentinelLayerPipeline should be instantiated just once, not per call."""
        interception_module._interceptor = None
        with patch("src.core.interception.SentinelLayerPipeline") as MockPipeline, \
             patch("src.core.interception.DecisionTracker"):
            get_interceptor()
            get_interceptor()
            get_interceptor()
            # Only one pipeline was ever created
            assert MockPipeline.call_count == 1
        interception_module._interceptor = None


# ---------------------------------------------------------------------------
# 5. _build_action_from_dict() — private helper (tested via public API)
# ---------------------------------------------------------------------------


class TestBuildActionFromDict:
    """The action construction helper is tested indirectly through intercept_from_dict."""

    def test_action_type_is_set_correctly(self):
        """The constructed action must have the action_type from the dict."""
        interceptor, mock_pipeline, _, _, _ = _make_interceptor()
        data = {
            "resource_id": "vm-test",
            "resource_type": "Microsoft.Compute/virtualMachines",
            "action_type": "delete_resource",
            "agent_id": "cost-agent",
            "reason": "idle",
        }
        interceptor.intercept_from_dict(data)
        built_action: ProposedAction = mock_pipeline.evaluate.call_args[0][0]
        assert built_action.action_type == ActionType.DELETE_RESOURCE

    def test_agent_id_is_set_correctly(self):
        interceptor, mock_pipeline, _, _, _ = _make_interceptor()
        data = {
            "resource_id": "vm-test",
            "resource_type": "Microsoft.Compute/virtualMachines",
            "action_type": "scale_up",
            "agent_id": "my-special-agent",
            "reason": "scaling up",
        }
        interceptor.intercept_from_dict(data)
        built_action: ProposedAction = mock_pipeline.evaluate.call_args[0][0]
        assert built_action.agent_id == "my-special-agent"

    def test_resource_id_is_set_correctly(self):
        interceptor, mock_pipeline, _, _, _ = _make_interceptor()
        data = {
            "resource_id": "nsg-east",
            "resource_type": "Microsoft.Network/networkSecurityGroups",
            "action_type": "modify_nsg",
            "agent_id": "sec-agent",
            "reason": "tighten rules",
        }
        interceptor.intercept_from_dict(data)
        built_action: ProposedAction = mock_pipeline.evaluate.call_args[0][0]
        assert built_action.target.resource_id == "nsg-east"

    def test_optional_cost_is_passed_through(self):
        interceptor, mock_pipeline, _, _, _ = _make_interceptor()
        data = {
            "resource_id": "vm-test",
            "resource_type": "Microsoft.Compute/virtualMachines",
            "action_type": "scale_down",
            "agent_id": "cost-agent",
            "reason": "reduce spend",
            "current_monthly_cost": 512.50,
        }
        interceptor.intercept_from_dict(data)
        built_action: ProposedAction = mock_pipeline.evaluate.call_args[0][0]
        assert built_action.target.current_monthly_cost == 512.50
