"""Tests for Slack notification integration.

Coverage:
- All three public functions: send_verdict_notification, send_alert_notification,
  send_alert_resolved_notification
- Smart retry behaviour: no retry on 4xx, retry on 5xx, 429 Retry-After respected
- Rate limiter and localhost warning are exercised without slow real sleeps
- Pipeline resilience: a broken Slack webhook must not prevent pipeline.evaluate()
  from returning a verdict

All tests mock _get_client() so no real HTTP calls are made and the shared client
singleton is never created during test runs.
"""

import asyncio
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

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
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def reset_slack_state(monkeypatch):
    """Reset all module-level state before each test.

    Prevents inter-test interference from:
    - The shared httpx.AsyncClient singleton
    - The rate-limiter timestamp (_last_send_at)
    - The localhost-URL warning flag (_localhost_warned)
    - asyncio.Lock objects that may be bound to a prior event loop
    """
    import src.notifications.slack_notifier as mod

    monkeypatch.setattr(mod, "_client", None)
    monkeypatch.setattr(mod, "_client_lock", asyncio.Lock())
    monkeypatch.setattr(mod, "_rate_lock", asyncio.Lock())
    monkeypatch.setattr(mod, "_last_send_at", 0.0)
    monkeypatch.setattr(mod, "_localhost_warned", False)


def _mock_http_client(*, side_effect=None):
    """Return a mock httpx.AsyncClient whose .post() succeeds by default."""
    mock_resp = MagicMock()
    mock_resp.raise_for_status = MagicMock()  # no-op = success

    mock_client = AsyncMock()
    if side_effect is not None:
        mock_client.post = AsyncMock(side_effect=side_effect)
    else:
        mock_client.post = AsyncMock(return_value=mock_resp)
    return mock_client


def _http_status_error(status: int, retry_after: str | None = None):
    """Build an httpx.HTTPStatusError for the given HTTP status code."""
    mock_resp = MagicMock()
    mock_resp.status_code = status
    mock_resp.headers = {"Retry-After": retry_after} if retry_after else {}
    return httpx.HTTPStatusError(
        f"HTTP {status}", request=MagicMock(), response=mock_resp
    )


# ---------------------------------------------------------------------------
# Helpers — build domain objects
# ---------------------------------------------------------------------------


def _make_action(
    resource_id: str = (
        "/subscriptions/demo/resourceGroups/prod"
        "/providers/Microsoft.Compute/virtualMachines/vm-dr-01"
    ),
) -> ProposedAction:
    return ProposedAction(
        agent_id="cost-optimization-agent",
        action_type=ActionType.DELETE_RESOURCE,
        target=ActionTarget(
            resource_id=resource_id,
            resource_type="Microsoft.Compute/virtualMachines",
            current_monthly_cost=847.0,
        ),
        reason="VM idle for 30 days — estimated savings $847/month",
        urgency=Urgency.HIGH,
    )


def _make_verdict(
    decision: SRIVerdict = SRIVerdict.DENIED,
    composite: float = 77.0,
) -> GovernanceVerdict:
    return GovernanceVerdict(
        action_id="test-notif-001",
        timestamp=datetime.now(timezone.utc),
        proposed_action=_make_action(),
        skry_risk_index=SRIBreakdown(
            sri_infrastructure=65.0,
            sri_policy=100.0,
            sri_historical=62.0,
            sri_cost=45.0,
            sri_composite=composite,
        ),
        decision=decision,
        reason="DENIED — POL-DR-001 violation. SRI 77.0 > 60.",
        agent_results={
            "policy_compliance": {
                "violations": [{
                    "policy_id": "POL-DR-001",
                    "name": "Disaster Recovery Protection",
                    "rule": "Cannot delete DR resources",
                    "severity": "critical",
                }]
            }
        },
    )


# ---------------------------------------------------------------------------
# TestSlackVerdictNotification — send_verdict_notification
# ---------------------------------------------------------------------------


class TestSlackVerdictNotification:
    """Tests for governance verdict notifications."""

    @pytest.mark.asyncio
    async def test_denied_sends_slack_notification(self, monkeypatch):
        """DENIED verdicts trigger a Slack HTTP POST."""
        from src.notifications.slack_notifier import send_verdict_notification

        monkeypatch.setattr("src.notifications.slack_notifier.settings.slack_webhook_url",
                            "https://hooks.slack.com/services/T/B/XXX")
        monkeypatch.setattr("src.notifications.slack_notifier.settings.slack_notifications_enabled", True)

        mock_client = _mock_http_client()
        with patch("src.notifications.slack_notifier._get_client", new_callable=AsyncMock,
                   return_value=mock_client):
            result = await send_verdict_notification(_make_verdict(SRIVerdict.DENIED), _make_action())

        assert result is True
        mock_client.post.assert_called_once()

    @pytest.mark.asyncio
    async def test_escalated_sends_slack_notification(self, monkeypatch):
        """ESCALATED verdicts trigger a Slack HTTP POST."""
        from src.notifications.slack_notifier import send_verdict_notification

        monkeypatch.setattr("src.notifications.slack_notifier.settings.slack_webhook_url",
                            "https://hooks.slack.com/services/T/B/XXX")
        monkeypatch.setattr("src.notifications.slack_notifier.settings.slack_notifications_enabled", True)

        mock_client = _mock_http_client()
        with patch("src.notifications.slack_notifier._get_client", new_callable=AsyncMock,
                   return_value=mock_client):
            result = await send_verdict_notification(
                _make_verdict(SRIVerdict.ESCALATED, composite=45.0), _make_action()
            )

        assert result is True
        mock_client.post.assert_called_once()

    @pytest.mark.asyncio
    async def test_approved_skips_slack(self, monkeypatch):
        """APPROVED verdicts do NOT trigger a Slack POST."""
        from src.notifications.slack_notifier import send_verdict_notification

        monkeypatch.setattr("src.notifications.slack_notifier.settings.slack_webhook_url",
                            "https://hooks.slack.com/services/T/B/XXX")

        result = await send_verdict_notification(
            _make_verdict(SRIVerdict.APPROVED, composite=12.0), _make_action()
        )
        assert result is True  # silently skipped — no POST needed

    @pytest.mark.asyncio
    async def test_missing_webhook_skips(self, monkeypatch):
        """Empty SLACK_WEBHOOK_URL skips without error."""
        from src.notifications.slack_notifier import send_verdict_notification

        monkeypatch.setattr("src.notifications.slack_notifier.settings.slack_webhook_url", "")

        result = await send_verdict_notification(_make_verdict(SRIVerdict.DENIED), _make_action())
        assert result is True  # silently skipped

    @pytest.mark.asyncio
    async def test_slack_notification_does_not_block_pipeline(self, monkeypatch):
        """Even if the Slack POST fails, pipeline.evaluate() must return a verdict."""
        from src.core.pipeline import RuriSkryPipeline

        pipeline = RuriSkryPipeline()

        monkeypatch.setattr("src.notifications.slack_notifier.settings.slack_webhook_url",
                            "https://broken.hooks.slack.com/fail")
        monkeypatch.setattr("src.notifications.slack_notifier.settings.slack_notifications_enabled", True)

        # ConnectionError is unexpected → caught by `except Exception` → abort, no retry
        mock_client = _mock_http_client(side_effect=ConnectionError("Network down"))

        action = ProposedAction(
            agent_id="test-agent",
            action_type=ActionType.DELETE_RESOURCE,
            target=ActionTarget(
                resource_id="/subscriptions/demo/resourceGroups/prod"
                            "/providers/Microsoft.Compute/virtualMachines/vm-23",
                resource_type="Microsoft.Compute/virtualMachines",
                current_monthly_cost=847.0,
            ),
            reason="Pipeline resilience test",
            urgency=Urgency.HIGH,
        )

        with patch("src.notifications.slack_notifier._get_client", new_callable=AsyncMock,
                   return_value=mock_client):
            verdict = await pipeline.evaluate(action)

        # Give any background tasks a chance to complete
        await asyncio.sleep(0)

        assert verdict is not None
        assert verdict.decision in (SRIVerdict.APPROVED, SRIVerdict.ESCALATED, SRIVerdict.DENIED)
        assert verdict.skry_risk_index.sri_composite >= 0


# ---------------------------------------------------------------------------
# TestSlackAlertNotifications — send_alert_notification
# ---------------------------------------------------------------------------


class TestSlackAlertNotifications:
    """Tests for Azure Monitor alert-triggered notifications."""

    @pytest.mark.asyncio
    async def test_alert_notification_sends(self, monkeypatch):
        """send_alert_notification posts to Slack when webhook is configured."""
        from src.notifications.slack_notifier import send_alert_notification

        monkeypatch.setattr("src.notifications.slack_notifier.settings.slack_webhook_url",
                            "https://hooks.slack.com/services/T/B/XXX")
        monkeypatch.setattr("src.notifications.slack_notifier.settings.slack_notifications_enabled", True)

        mock_client = _mock_http_client()
        with patch("src.notifications.slack_notifier._get_client", new_callable=AsyncMock,
                   return_value=mock_client):
            result = await send_alert_notification(
                alert_id="alert-abc-123",
                resource_id="/subscriptions/demo/resourceGroups/prod"
                            "/providers/Microsoft.Compute/virtualMachines/vm-web-01",
                metric="Percentage CPU",
                severity="3",
                description="CPU exceeded 90% for 5 minutes",
            )

        assert result is True
        mock_client.post.assert_called_once()
        payload = mock_client.post.call_args[1]["json"]
        assert payload["attachments"][0]["color"] == "#D69E2E"  # amber for alert-fired

    @pytest.mark.asyncio
    async def test_alert_notification_disabled_skips(self, monkeypatch):
        """send_alert_notification respects slack_notifications_enabled=False."""
        from src.notifications.slack_notifier import send_alert_notification

        monkeypatch.setattr("src.notifications.slack_notifier.settings.slack_webhook_url",
                            "https://hooks.slack.com/services/T/B/XXX")
        monkeypatch.setattr("src.notifications.slack_notifier.settings.slack_notifications_enabled", False)

        result = await send_alert_notification(
            alert_id="alert-xyz", resource_id="vm-dr-01",
            metric="Heartbeat", severity="1",
        )
        assert result is True  # silently skipped


# ---------------------------------------------------------------------------
# TestSlackAlertResolvedNotifications — send_alert_resolved_notification
# ---------------------------------------------------------------------------


class TestSlackAlertResolvedNotifications:
    """Tests for alert-resolved (investigation complete) notifications."""

    @pytest.mark.asyncio
    async def test_alert_resolved_green_when_all_approved(self, monkeypatch):
        """All findings approved → green sidebar color."""
        from src.notifications.slack_notifier import send_alert_resolved_notification

        monkeypatch.setattr("src.notifications.slack_notifier.settings.slack_webhook_url",
                            "https://hooks.slack.com/services/T/B/XXX")
        monkeypatch.setattr("src.notifications.slack_notifier.settings.slack_notifications_enabled", True)

        mock_client = _mock_http_client()
        with patch("src.notifications.slack_notifier._get_client", new_callable=AsyncMock,
                   return_value=mock_client):
            result = await send_alert_resolved_notification(
                alert_id="alert-001", resource_id="vm-web-01",
                approved=3, escalated=0, denied=0,
            )

        assert result is True
        payload = mock_client.post.call_args[1]["json"]
        assert payload["attachments"][0]["color"] == "#38A169"  # green

    @pytest.mark.asyncio
    async def test_alert_resolved_red_when_any_denied(self, monkeypatch):
        """Any denied finding → red sidebar color."""
        from src.notifications.slack_notifier import send_alert_resolved_notification

        monkeypatch.setattr("src.notifications.slack_notifier.settings.slack_webhook_url",
                            "https://hooks.slack.com/services/T/B/XXX")
        monkeypatch.setattr("src.notifications.slack_notifier.settings.slack_notifications_enabled", True)

        mock_client = _mock_http_client()
        with patch("src.notifications.slack_notifier._get_client", new_callable=AsyncMock,
                   return_value=mock_client):
            result = await send_alert_resolved_notification(
                alert_id="alert-002", resource_id="vm-dr-01",
                approved=2, escalated=0, denied=1,
            )

        assert result is True
        payload = mock_client.post.call_args[1]["json"]
        assert payload["attachments"][0]["color"] == "#E53E3E"  # red

    @pytest.mark.asyncio
    async def test_alert_resolved_amber_when_only_escalated(self, monkeypatch):
        """No denials but some escalations → amber sidebar color."""
        from src.notifications.slack_notifier import send_alert_resolved_notification

        monkeypatch.setattr("src.notifications.slack_notifier.settings.slack_webhook_url",
                            "https://hooks.slack.com/services/T/B/XXX")
        monkeypatch.setattr("src.notifications.slack_notifier.settings.slack_notifications_enabled", True)

        mock_client = _mock_http_client()
        with patch("src.notifications.slack_notifier._get_client", new_callable=AsyncMock,
                   return_value=mock_client):
            result = await send_alert_resolved_notification(
                alert_id="alert-003", resource_id="vm-web-01",
                approved=1, escalated=2, denied=0,
            )

        assert result is True
        payload = mock_client.post.call_args[1]["json"]
        assert payload["attachments"][0]["color"] == "#D69E2E"  # amber

    @pytest.mark.asyncio
    async def test_alert_resolved_disabled_skips(self, monkeypatch):
        """send_alert_resolved_notification respects slack_notifications_enabled=False."""
        from src.notifications.slack_notifier import send_alert_resolved_notification

        monkeypatch.setattr("src.notifications.slack_notifier.settings.slack_webhook_url",
                            "https://hooks.slack.com/services/T/B/XXX")
        monkeypatch.setattr("src.notifications.slack_notifier.settings.slack_notifications_enabled", False)

        result = await send_alert_resolved_notification(
            alert_id="alert-004", resource_id="vm-dr-01",
            approved=0, escalated=0, denied=1,
        )
        assert result is True  # silently skipped


# ---------------------------------------------------------------------------
# TestSlackRetryBehaviour — _post() retry logic
# ---------------------------------------------------------------------------


class TestSlackRetryBehaviour:
    """Tests for the smart retry logic in _post()."""

    @pytest.mark.asyncio
    async def test_no_retry_on_4xx_client_error(self, monkeypatch):
        """HTTP 400 is not retried — bad payload will not self-heal."""
        from src.notifications.slack_notifier import send_verdict_notification

        monkeypatch.setattr("src.notifications.slack_notifier.settings.slack_webhook_url",
                            "https://hooks.slack.com/services/T/B/XXX")
        monkeypatch.setattr("src.notifications.slack_notifier.settings.slack_notifications_enabled", True)

        mock_client = _mock_http_client(side_effect=_http_status_error(400))
        with patch("src.notifications.slack_notifier._get_client", new_callable=AsyncMock,
                   return_value=mock_client):
            with patch("src.notifications.slack_notifier.asyncio.sleep", new_callable=AsyncMock):
                result = await send_verdict_notification(_make_verdict(SRIVerdict.DENIED), _make_action())

        assert result is False
        assert mock_client.post.call_count == 1  # exactly one attempt, no retry

    @pytest.mark.asyncio
    async def test_retries_on_5xx_and_succeeds(self, monkeypatch):
        """HTTP 500 on first attempt retries and succeeds on second attempt."""
        from src.notifications.slack_notifier import send_verdict_notification

        monkeypatch.setattr("src.notifications.slack_notifier.settings.slack_webhook_url",
                            "https://hooks.slack.com/services/T/B/XXX")
        monkeypatch.setattr("src.notifications.slack_notifier.settings.slack_notifications_enabled", True)

        success_resp = MagicMock()
        success_resp.raise_for_status = MagicMock()

        mock_client = _mock_http_client(
            side_effect=[_http_status_error(500), success_resp]
        )
        with patch("src.notifications.slack_notifier._get_client", new_callable=AsyncMock,
                   return_value=mock_client):
            with patch("src.notifications.slack_notifier.asyncio.sleep", new_callable=AsyncMock):
                result = await send_verdict_notification(_make_verdict(SRIVerdict.DENIED), _make_action())

        assert result is True
        assert mock_client.post.call_count == 2  # failed once, succeeded on retry

    @pytest.mark.asyncio
    async def test_returns_false_after_all_retries_fail(self, monkeypatch):
        """All 3 attempts fail with 500 → returns False."""
        from src.notifications.slack_notifier import send_verdict_notification

        monkeypatch.setattr("src.notifications.slack_notifier.settings.slack_webhook_url",
                            "https://hooks.slack.com/services/T/B/XXX")
        monkeypatch.setattr("src.notifications.slack_notifier.settings.slack_notifications_enabled", True)

        mock_client = _mock_http_client(side_effect=_http_status_error(503))
        with patch("src.notifications.slack_notifier._get_client", new_callable=AsyncMock,
                   return_value=mock_client):
            with patch("src.notifications.slack_notifier.asyncio.sleep", new_callable=AsyncMock):
                result = await send_verdict_notification(_make_verdict(SRIVerdict.DENIED), _make_action())

        assert result is False
        assert mock_client.post.call_count == 3  # all 3 attempts exhausted

    @pytest.mark.asyncio
    async def test_429_respects_retry_after_header(self, monkeypatch):
        """HTTP 429 waits for Retry-After, then retries and succeeds."""
        from src.notifications.slack_notifier import send_verdict_notification

        monkeypatch.setattr("src.notifications.slack_notifier.settings.slack_webhook_url",
                            "https://hooks.slack.com/services/T/B/XXX")
        monkeypatch.setattr("src.notifications.slack_notifier.settings.slack_notifications_enabled", True)

        success_resp = MagicMock()
        success_resp.raise_for_status = MagicMock()

        mock_client = _mock_http_client(
            side_effect=[_http_status_error(429, retry_after="5"), success_resp]
        )
        sleep_mock = AsyncMock()
        with patch("src.notifications.slack_notifier._get_client", new_callable=AsyncMock,
                   return_value=mock_client):
            with patch("src.notifications.slack_notifier.asyncio.sleep", sleep_mock):
                result = await send_verdict_notification(_make_verdict(SRIVerdict.DENIED), _make_action())

        assert result is True
        assert mock_client.post.call_count == 2

        # Verify the Retry-After value (5.0) was used in at least one sleep call
        sleep_calls = [call.args[0] for call in sleep_mock.call_args_list]
        assert any(s == 5.0 for s in sleep_calls), (
            f"Expected a 5.0 s Retry-After sleep, got sleep calls: {sleep_calls}"
        )


# ---------------------------------------------------------------------------
# TestSlackLocalhostWarning — _warn_localhost_once()
# ---------------------------------------------------------------------------


class TestSlackLocalhostWarning:
    """Tests for the localhost dashboard_url misconfiguration warning."""

    @pytest.mark.asyncio
    async def test_warns_once_when_dashboard_url_is_localhost(self, monkeypatch, caplog):
        """A warning is logged when DASHBOARD_URL still points to localhost."""
        import logging
        from src.notifications.slack_notifier import send_verdict_notification

        monkeypatch.setattr("src.notifications.slack_notifier.settings.slack_webhook_url",
                            "https://hooks.slack.com/services/T/B/XXX")
        monkeypatch.setattr("src.notifications.slack_notifier.settings.slack_notifications_enabled", True)
        monkeypatch.setattr("src.notifications.slack_notifier.settings.dashboard_url",
                            "http://localhost:5173")

        mock_client = _mock_http_client()
        with patch("src.notifications.slack_notifier._get_client", new_callable=AsyncMock,
                   return_value=mock_client):
            with caplog.at_level(logging.WARNING, logger="src.notifications.slack_notifier"):
                await send_verdict_notification(_make_verdict(SRIVerdict.DENIED), _make_action())

        assert any("localhost" in r.message for r in caplog.records), (
            "Expected a localhost warning in logs"
        )
